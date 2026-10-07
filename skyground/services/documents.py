"""Reading and writing editorial documents, with revision history.

Two storage modes share one behaviour:

* `workspace` — the JSON file in the Git checkout is authoritative. Edits made
  outside the app (a `git pull`, an agent editing the file directly) are noticed
  through the content digest and recorded as an external revision, so history is
  never silently wrong.
* `managed` — the database row is authoritative.

Every accepted write appends an immutable revision and an audit event, and a
write may declare the revision it is based on so two people cannot overwrite
each other without noticing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.core.workspace import DOCUMENT_FILES, Workspace, content_digest, validate_documents
from skyground.db.models import MODE_WORKSPACE, Document, Project, Revision, User
from skyground.errors import Conflict, NotFound, ValidationError
from skyground.services import audit
from skyground.services import projects as project_service

#: Top level JSON type each document must have.
DOCUMENT_SHAPES: dict[str, type] = {
    "project.json": dict,
    "timeline.json": dict,
    "captions.json": list,
    "cards.json": list,
    "angles.json": list,
    "brand.json": dict,
    "audio.json": dict,
}

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


@dataclass
class DocumentState:
    name: str
    content: dict | list
    etag: str
    revision: int
    updated_at: datetime | None = None
    updated_by: str | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "etag": self.etag,
            "revision": self.revision,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "updatedBy": self.updated_by,
            "content": self.content,
        }


class DocumentService:
    def __init__(self, session: Session, workspace: Workspace):
        self.session = session
        self.workspace = workspace

    # ------------------------------------------------------------------ reads

    def read(self, project: Project, name: str) -> DocumentState:
        self._check_name(name)
        document = project_service.ensure_document_row(
            self.session, project, name, workspace=self.workspace
        )
        content = self._current_content(project, document, name)
        self._absorb_external_change(project, document, content)
        return DocumentState(
            name=name,
            content=content,
            etag=document.etag,
            revision=document.revision_number,
            updated_at=document.updated_at,
            updated_by=self._author_email(document.updated_by),
        )

    def read_all(self, project: Project) -> dict:
        """All documents keyed without the `.json` suffix, ready for validation."""
        return {
            name.removesuffix(".json"): self.read(project, name).content
            for name in DOCUMENT_FILES
        }

    def problems(self, project: Project) -> list[str]:
        """Editorial problems, reported the same way in both storage modes.

        A workspace project also gets the filesystem checks (missing composition,
        missing media) that only make sense against a checkout.
        """
        if project.storage_mode == MODE_WORKSPACE:
            return self.workspace.validate(project.slug)
        return validate_documents(self.read_all(project))

    # ----------------------------------------------------------------- writes

    def write(
        self,
        project: Project,
        name: str,
        content,
        *,
        actor: User | None = None,
        base_etag: str | None = None,
        message: str = "",
    ) -> DocumentState:
        self._check_name(name)
        self._check_content(project, name, content)
        document = project_service.ensure_document_row(
            self.session, project, name, workspace=self.workspace
        )
        current = self._current_content(project, document, name)
        self._absorb_external_change(project, document, current)

        if base_etag and base_etag != document.etag:
            raise Conflict(
                "il documento è cambiato dopo il caricamento",
                current=current,
                etag=document.etag,
            )

        new_etag = content_digest(content)
        if new_etag == document.etag:
            # Nothing changed: do not create an empty revision.
            return DocumentState(
                name=name,
                content=content,
                etag=document.etag,
                revision=document.revision_number,
                updated_at=document.updated_at,
                updated_by=self._author_email(document.updated_by),
            )

        self._persist(project, document, name, content, new_etag)
        revision = self._append_revision(
            project,
            document,
            content,
            etag=new_etag,
            parent_etag=document.etag if document.revision_number else None,
            author=actor,
            message=message,
        )
        document.etag = new_etag
        document.revision_number = revision.number
        document.updated_by = actor.id if actor else None
        if name == "project.json":
            project_service.update_manifest_fields(project, content)
        self.session.flush()
        audit.record(
            self.session,
            "document.write",
            actor=actor,
            project=project,
            target=name,
            data={"revision": revision.number, "etag": new_etag},
        )
        return DocumentState(
            name=name,
            content=content,
            etag=new_etag,
            revision=revision.number,
            updated_at=document.updated_at,
            updated_by=self._author_email(document.updated_by),
        )

    # --------------------------------------------------------------- history

    def history(self, project: Project, name: str, limit: int = 50) -> list[Revision]:
        self._check_name(name)
        return list(
            self.session.scalars(
                select(Revision)
                .where(Revision.project_id == project.id, Revision.document_name == name)
                .order_by(Revision.number.desc())
                .limit(limit)
            )
        )

    def revision(self, project: Project, name: str, number: int) -> Revision:
        revision = self.session.scalar(
            select(Revision).where(
                Revision.project_id == project.id,
                Revision.document_name == name,
                Revision.number == number,
            )
        )
        if revision is None:
            raise NotFound(f"revisione {number} non trovata per {name}")
        return revision

    def restore(
        self, project: Project, name: str, number: int, *, actor: User | None = None
    ) -> DocumentState:
        """Restore an old revision by writing it again as a new one.

        History is never rewritten: restoring revision 3 creates revision 8 with
        the same content, so the fact that a restore happened stays visible.
        """
        target = self.revision(project, name, number)
        state = self.write(
            project,
            name,
            target.content,
            actor=actor,
            message=f"ripristino della revisione {number}",
        )
        restored = self.session.scalar(
            select(Revision).where(
                Revision.project_id == project.id,
                Revision.document_name == name,
                Revision.number == state.revision,
            )
        )
        if restored is not None:
            restored.restored_from = target.id
        audit.record(
            self.session,
            "document.restore",
            actor=actor,
            project=project,
            target=name,
            data={"from": number, "to": state.revision},
        )
        self.session.flush()
        return state

    # ---------------------------------------------------------------- helpers

    def _check_name(self, name: str) -> None:
        if name not in DOCUMENT_SHAPES:
            raise ValidationError(f"file non modificabile: {name}")

    def _check_content(self, project: Project, name: str, content) -> None:
        expected = DOCUMENT_SHAPES[name]
        if not isinstance(content, expected):
            kind = "un oggetto" if expected is dict else "una lista"
            raise ValidationError(f"{name} deve contenere {kind}")
        if name == "project.json":
            if content.get("id") not in (None, project.slug):
                raise ValidationError("project.json: l'id non può cambiare")
            if content.get("schemaVersion") != 1:
                raise ValidationError("project.json: schemaVersion deve essere 1")

    def _current_content(self, project: Project, document: Document, name: str):
        if project.storage_mode == MODE_WORKSPACE:
            return self.workspace.read_document(project.slug, name)
        if document.content is None:
            raise NotFound(f"documento non trovato: {name}")
        return document.content

    def _absorb_external_change(self, project: Project, document: Document, content) -> None:
        """Record a revision for a change that did not come through the app."""
        etag = content_digest(content)
        if etag == document.etag:
            return
        revision = self._append_revision(
            project,
            document,
            content,
            etag=etag,
            parent_etag=document.etag or None,
            author=None,
            message="modifica applicata fuori dall'applicazione",
        )
        document.etag = etag
        document.revision_number = revision.number
        self.session.flush()

    def _append_revision(
        self,
        project: Project,
        document: Document,
        content,
        *,
        etag: str,
        parent_etag: str | None,
        author: User | None,
        message: str,
    ) -> Revision:
        revision = Revision(
            project_id=project.id,
            document_name=document.name,
            number=document.revision_number + 1,
            content=content,
            etag=etag,
            parent_etag=parent_etag,
            author_id=author.id if author else None,
            message=message[:500],
        )
        self.session.add(revision)
        self.session.flush()
        return revision

    def _persist(self, project: Project, document: Document, name: str, content, etag: str) -> None:
        if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValidationError("documento troppo grande")
        if project.storage_mode == MODE_WORKSPACE:
            self.workspace.write_document(project.slug, name, content)
        else:
            document.content = content

    def _author_email(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        user = self.session.get(User, user_id)
        return user.email if user else None
