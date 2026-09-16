"""Project, document and revision endpoints.

The three original routes keep their paths, their payloads and their status
codes so the existing panel and any script written against them keep working:

    GET  /api/projects
    GET  /api/projects/{slug}/files/{name}
    PUT  /api/projects/{slug}/files/{name}
    GET  /api/projects/{slug}/status

Everything else is additive.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from skyground.api import serializers
from skyground.api.deps import (
    ProjectContext,
    get_session,
    get_workspace,
    project_context,
    require_user,
)
from skyground.core.workspace import Workspace
from skyground.db.models import ROLES, User
from skyground.errors import NotFound, ValidationError
from skyground.services import accounts, audit
from skyground.services import documents as document_service
from skyground.services import projects as project_service

router = APIRouter()


@router.get("/api/projects")
def list_projects(
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    workspace: Workspace = Depends(get_workspace),
) -> list[dict]:
    service = document_service.DocumentService(session, workspace)
    return [
        serializers.project_payload(project, workspace, service)
        for project in project_service.list_for_user(session, user)
    ]


@router.put("/api/projects/{slug}")
async def create_project(
    slug: str,
    request: Request,
    name: str | None = Header(default=None, alias="X-Skyground-Name"),
    template: str | None = Query(default=None),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    workspace: Workspace = Depends(get_workspace),
) -> dict:
    """A new project from a raw video, in one request.

    The body is the footage, streamed to disk; the project is laid out around
    it and registered, and the caller becomes its owner. Follow with
    `POST /api/projects/{slug}/cut/full` and the film comes back edited.
    """
    import pathlib
    import tempfile

    from skyground.api.uploads import receive_object
    from skyground.storage.local import LocalObjectStorage

    if workspace.exists(slug):
        raise ValidationError(f"il progetto esiste già: {slug}")
    spool_root = pathlib.Path(tempfile.gettempdir()) / "skyground-intake"
    spool = LocalObjectStorage(spool_root)
    suffix = pathlib.Path(request.headers.get("X-Skyground-Filename") or "raw.mov").suffix or ".mov"
    key = f"{slug}/raw{suffix.lower()}"
    stored = await receive_object(request, spool, key, content_type=request.headers.get("Content-Type", ""))
    try:
        created = workspace.create_project(
            slug, name or slug, spool_root / key, template_project=template,
            language=(request.headers.get("X-Skyground-Language") or "it"),
        )
    finally:
        (spool_root / key).unlink(missing_ok=True)
    project = project_service.register_workspace_project(session, workspace, slug, owner=user)
    audit.record(session, "project.create", actor=user, project=project,
                 data={"bytes": stored.size, "sha256": stored.sha256, **created})
    service = document_service.DocumentService(session, workspace)
    payload = serializers.project_payload(project, workspace, service)
    payload["source"] = created
    return payload


@router.get("/api/projects/{slug}")
def read_project(context: ProjectContext = Depends(project_context)) -> dict:
    payload = serializers.project_payload(context.project, context.workspace, context.documents)
    payload["role"] = context.role
    payload["problems"] = context.documents.problems(context.project)
    return payload


@router.get("/api/projects/{slug}/status")
def project_status(context: ProjectContext = Depends(project_context)) -> dict:
    """Asset availability and editorial problems, as the panel has always read them."""
    assets: list[dict] = []
    try:
        assets = context.workspace.asset_status(context.project.slug)
    except (NotFound, FileNotFoundError, KeyError):
        assets = []
    return {"assets": assets, "problems": context.documents.problems(context.project)}


@router.get("/api/projects/{slug}/files/{name}")
def read_document(
    name: str, response: Response, context: ProjectContext = Depends(project_context)
):
    """Return the document itself, exactly as the first version did.

    The revision it belongs to travels in the headers, so a client that wants
    safe writes can send it back as `If-Match` without the body changing shape.
    """
    context.require("document:read")
    state = context.documents.read(context.project, name)
    response.headers["ETag"] = f'"{state.etag}"'
    response.headers["X-Skyground-Revision"] = str(state.revision)
    return state.content


@router.put("/api/projects/{slug}/files/{name}")
def write_document(
    name: str,
    request: Request,
    response: Response,
    content=Body(...),
    context: ProjectContext = Depends(project_context),
):
    """Save a document.

    `If-Match` makes the write conditional: when the document changed in the
    meantime the request is refused with 409 and the current content, instead of
    silently discarding somebody else's edit. Without the header the write is
    unconditional, which is what the existing panel does today.
    """
    context.require("document:write")
    base_etag = (request.headers.get("If-Match") or "").strip('"') or None
    message = request.headers.get("X-Skyground-Message", "")
    state = context.documents.write(
        context.project,
        name,
        content,
        actor=context.user,
        base_etag=base_etag,
        message=message,
    )
    problems = context.documents.problems(context.project)
    response.headers["ETag"] = f'"{state.etag}"'
    response.headers["X-Skyground-Revision"] = str(state.revision)
    return {
        "saved": True,
        "problems": problems,
        "etag": state.etag,
        "revision": state.revision,
    }


@router.get("/api/projects/{slug}/files/{name}/revisions")
def list_revisions(
    name: str, limit: int = 50, context: ProjectContext = Depends(project_context)
) -> dict:
    context.require("revision:read")
    revisions = context.documents.history(context.project, name, limit=min(limit, 200))
    return {"document": name, "revisions": [serializers.revision_payload(r) for r in revisions]}


@router.get("/api/projects/{slug}/files/{name}/revisions/{number}")
def read_revision(
    name: str, number: int, context: ProjectContext = Depends(project_context)
) -> dict:
    context.require("revision:read")
    revision = context.documents.revision(context.project, name, number)
    return serializers.revision_payload(revision, include_content=True)


@router.post("/api/projects/{slug}/files/{name}/revisions/{number}/restore")
def restore_revision(
    name: str, number: int, context: ProjectContext = Depends(project_context)
) -> dict:
    context.require("revision:restore")
    state = context.documents.restore(context.project, name, number, actor=context.user)
    return {
        "restored": number,
        "revision": state.revision,
        "etag": state.etag,
        "problems": context.documents.problems(context.project),
    }


# ------------------------------------------------------------------- members


@router.get("/api/projects/{slug}/members")
def list_members(context: ProjectContext = Depends(project_context)) -> list[dict]:
    context.require("project:read")
    return [
        serializers.member_payload(membership, user)
        for membership, user in accounts.members(context.session, context.project)
    ]


@router.post("/api/projects/{slug}/members")
def add_member(
    payload: dict = Body(...), context: ProjectContext = Depends(project_context)
) -> dict:
    context.require("member:manage")
    role = (payload.get("role") or "viewer").strip()
    if role not in ROLES:
        raise ValidationError(f"ruolo non valido: {role}")
    user = accounts.get_user(context.session, payload.get("email", ""))
    if user is None:
        raise NotFound(f"utente non trovato: {payload.get('email')}")
    membership = accounts.add_member(
        context.session, context.project, user, role, actor=context.user
    )
    return serializers.member_payload(membership, user)


@router.delete("/api/projects/{slug}/members/{email}")
def remove_member(email: str, context: ProjectContext = Depends(project_context)) -> dict:
    context.require("member:manage")
    user = accounts.get_user(context.session, email)
    if user is None:
        raise NotFound(f"utente non trovato: {email}")
    accounts.remove_member(context.session, context.project, user, actor=context.user)
    return {"removed": user.email}


@router.get("/api/projects/{slug}/audit")
def read_audit(limit: int = 100, context: ProjectContext = Depends(project_context)) -> list[dict]:
    context.require("member:manage")
    return [
        serializers.audit_payload(event)
        for event in audit.history(context.session, context.project, limit=min(limit, 500))
    ]
