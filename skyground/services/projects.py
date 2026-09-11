"""Project registry: the bridge between the Git checkout and the database."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.core.workspace import DOCUMENT_FILES, Workspace, content_digest
from skyground.db.models import (
    MODE_MANAGED,
    MODE_WORKSPACE,
    ROLE_OWNER,
    Document,
    Membership,
    Project,
    Revision,
    User,
)
from skyground.errors import Conflict, NotFound, ValidationError
from skyground.services import accounts, audit


def get_project(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise NotFound(f"progetto non trovato: {slug}")
    return project


def find_project(session: Session, slug: str) -> Project | None:
    return session.scalar(select(Project).where(Project.slug == slug))


def list_for_user(session: Session, user: User | None) -> list[Project]:
    if user is None:
        return []
    query = select(Project).where(Project.archived_at.is_(None)).order_by(Project.slug)
    if not user.is_admin:
        query = query.join(Membership, Membership.project_id == Project.id).where(
            Membership.user_id == user.id
        )
    return list(session.scalars(query))


def create_project(
    session: Session,
    *,
    slug: str,
    name: str,
    owner: User,
    storage_mode: str = MODE_MANAGED,
    canvas: dict | None = None,
    settings: dict | None = None,
    status: str = "draft",
) -> Project:
    if find_project(session, slug) is not None:
        raise Conflict(f"slug già in uso: {slug}")
    project = Project(
        slug=slug,
        name=name,
        status=status,
        storage_mode=storage_mode,
        canvas=canvas or {},
        settings=settings or {},
        created_by=owner.id,
    )
    session.add(project)
    session.flush()
    accounts.add_member(session, project, owner, ROLE_OWNER, actor=owner)
    audit.record(session, "project.create", actor=owner, project=project, target=slug)
    return project


def register_workspace_project(
    session: Session, workspace: Workspace, slug: str, *, owner: User
) -> Project:
    """Make a project that lives in the checkout visible to the application.

    Idempotent: running it again refreshes name, status and canvas and leaves
    memberships and history untouched. The JSON files stay authoritative.
    """
    if not workspace.exists(slug):
        raise NotFound(f"progetto non trovato nel workspace: {slug}")
    manifest = workspace.read_document(slug, "project.json")
    project = find_project(session, slug)
    if project is None:
        project = Project(
            slug=slug,
            name=manifest.get("name", slug),
            status=manifest.get("status", "draft"),
            storage_mode=MODE_WORKSPACE,
            canvas=manifest.get("canvas", {}),
            settings={"files": manifest.get("files", {}), "render": manifest.get("render", {})},
            created_by=owner.id,
        )
        session.add(project)
        session.flush()
        audit.record(session, "project.register", actor=owner, project=project, target=slug)
    else:
        project.name = manifest.get("name", project.name)
        project.status = manifest.get("status", project.status)
        project.canvas = manifest.get("canvas", project.canvas)

    if not session.scalars(
        select(Membership).where(Membership.project_id == project.id)
    ).first():
        accounts.add_member(session, project, owner, ROLE_OWNER, actor=owner)

    for name in DOCUMENT_FILES:
        ensure_document_row(session, project, name, workspace=workspace)
    session.flush()
    return project


def ensure_document_row(
    session: Session, project: Project, name: str, *, workspace: Workspace | None = None
) -> Document:
    document = session.scalar(
        select(Document).where(Document.project_id == project.id, Document.name == name)
    )
    if document is not None:
        return document
    content = None
    etag = ""
    if project.storage_mode == MODE_WORKSPACE and workspace is not None:
        try:
            content = workspace.read_document(project.slug, name)
            etag = content_digest(content)
        except NotFound:
            content, etag = None, ""
    document = Document(
        project_id=project.id,
        name=name,
        # A workspace document keeps its bytes on disk; the row is metadata only.
        content=None if project.storage_mode == MODE_WORKSPACE else content,
        etag=etag,
        revision_number=0,
    )
    session.add(document)
    session.flush()
    if content is not None:
        # Record what the document looked like when the application first saw it,
        # so the state that predates the cloud is itself restorable.
        session.add(
            Revision(
                project_id=project.id,
                document_name=name,
                number=1,
                content=content,
                etag=etag,
                message="stato iniziale acquisito dal workspace",
            )
        )
        document.revision_number = 1
        session.flush()
    return document


def convert_to_managed(
    session: Session, project: Project, workspace: Workspace, *, actor: User | None = None
) -> Project:
    """Move a project's documents from the checkout into the database.

    This is the one-way step of the cloud migration; run it once the deployment
    is ready to own the editorial content. `export_to_workspace` writes it back.
    """
    if project.storage_mode == MODE_MANAGED:
        return project
    for name in DOCUMENT_FILES:
        content = workspace.read_document(project.slug, name)
        document = ensure_document_row(session, project, name, workspace=workspace)
        document.content = content
        document.etag = content_digest(content)
    project.storage_mode = MODE_MANAGED
    session.flush()
    audit.record(session, "project.convert", actor=actor, project=project, target=MODE_MANAGED)
    return project


def export_to_workspace(
    session: Session, project: Project, workspace: Workspace, *, actor: User | None = None
) -> list[str]:
    """Write a managed project's documents back onto disk, so it can be reviewed
    as a Git diff and rendered locally."""
    written: list[str] = []
    for name in DOCUMENT_FILES:
        document = session.scalar(
            select(Document).where(Document.project_id == project.id, Document.name == name)
        )
        if document is None or document.content is None:
            continue
        workspace.write_document(project.slug, name, document.content)
        written.append(name)
    audit.record(
        session, "project.export", actor=actor, project=project, data={"documents": written}
    )
    return written


def sync_workspace(session: Session, workspace: Workspace, *, owner: User) -> list[Project]:
    """Register every project found in the checkout."""
    projects = []
    for manifest in workspace.list_projects():
        projects.append(register_workspace_project(session, workspace, manifest["id"], owner=owner))
    return projects


def update_manifest_fields(project: Project, manifest: dict) -> None:
    """Keep the project row aligned with `project.json` after an edit."""
    if not isinstance(manifest, dict):
        raise ValidationError("project.json deve essere un oggetto")
    project.name = manifest.get("name", project.name)
    project.status = manifest.get("status", project.status)
    project.canvas = manifest.get("canvas", project.canvas)
