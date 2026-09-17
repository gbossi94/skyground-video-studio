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

import json
import pathlib
import shutil

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from skyground.api import serializers
from skyground.api.deps import (
    ProjectContext,
    get_session,
    get_settings,
    get_workspace,
    project_context,
    require_user,
)
from skyground.config import Settings
from skyground.core.workspace import Workspace
from skyground.db.models import ROLES, User
from skyground.errors import NotFound, PermissionDenied, ValidationError
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


@router.get("/api/projects/{slug}/export/{kind}")
def export_cut(
    kind: str,
    root: str = "",
    context: ProjectContext = Depends(project_context),
    settings: Settings = Depends(get_settings),
):
    """The cut for an editor a person already knows: `fcpxml` for DaVinci
    Resolve, Premiere Pro and Final Cut Pro; `srt` for the captions, which
    every editor — CapCut included — imports as subtitles; `capcut` for a
    zipped CapCut draft folder, raw footage inside, to unzip into CapCut's
    drafts folder (`root`, or the studio's `SKYGROUND_CAPCUT_DRAFTS`)."""
    import tempfile

    from fastapi.responses import FileResponse, PlainTextResponse
    from starlette.background import BackgroundTask

    context.require("document:read")
    if kind == "capcut":
        folder = pathlib.Path(tempfile.mkdtemp(prefix="skyground-capcut-"))
        target = context.workspace.export_capcut(
            context.project.slug, folder / f"{context.project.slug}-capcut.zip",
            drafts_root=root or settings.capcut_drafts,
        )
        return FileResponse(
            target, media_type="application/zip", filename=target.name,
            background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
        )
    if kind == "fcpxml":
        body, media = context.workspace.export_fcpxml(context.project.slug), "application/xml"
    elif kind == "srt":
        body, media = context.workspace.export_srt(context.project.slug), "text/plain; charset=utf-8"
    else:
        raise ValidationError(f"formato di export sconosciuto: {kind} (fcpxml o srt)")
    return PlainTextResponse(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{context.project.slug}.{kind}"'},
    )


@router.put("/api/capcut/sample/{name}")
async def upload_capcut_sample(
    name: str,
    request: Request,
    user: User = Depends(require_user),
    workspace: Workspace = Depends(get_workspace),
) -> dict:
    """One file of the sample draft CapCut saved, from which ours take their
    shape: `draft_info.json`, `draft_meta_info.json`, `template.tmp`, and the
    two attachment files. Administrators only; it is workspace data."""
    from skyground.core.capcut import SAMPLE_FILES

    if not user.is_admin:
        raise PermissionDenied("solo un amministratore carica la bozza campione di CapCut")
    allowed = SAMPLE_FILES + ("attachment_pc_common.json", "attachment_editing.json")
    if name not in allowed:
        raise ValidationError(f"file non previsto: {name} (uno di {', '.join(allowed)})")
    body = await request.body()
    if name.endswith(".json") or name == "template.tmp":
        try:
            json.loads(body)
        except ValueError as error:
            raise ValidationError(f"{name} non è JSON valido: {error}") from error
    folder = workspace.capcut_sample
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    return {"stored": name, "size": len(body), "present": sorted(p.name for p in folder.iterdir())}


@router.put("/api/capcut/sample")
async def upload_capcut_sample_folder(
    request: Request,
    user: User = Depends(require_user),
    workspace: Workspace = Depends(get_workspace),
) -> dict:
    """The whole sample draft folder, zipped as CapCut keeps it (a top-level
    folder with `draft_info.json` inside). Media and anything large stay out;
    the draft's own attachments and `draft.extra` come in, because CapCut
    would not open a draft without them."""
    import io
    import zipfile

    if not user.is_admin:
        raise PermissionDenied("solo un amministratore carica la bozza campione di CapCut")
    body = await request.body()
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as error:
        raise ValidationError(f"non è uno zip: {error}") from error
    media = {".mov", ".mp4", ".m4a", ".mp3", ".wav", ".png", ".jpeg"}
    roots = {n.split("/", 1)[0] for n in archive.namelist() if "/" in n and not n.startswith("__MACOSX")}
    root = next(iter(roots)) + "/" if len(roots) == 1 else ""
    folder = workspace.capcut_sample
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    stored = []
    for item in archive.infolist():
        name = item.filename
        if item.is_dir() or name.startswith("__MACOSX") or "/._" in f"/{name}":
            continue
        relative = name[len(root):] if root and name.startswith(root) else name
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            continue
        if pathlib.Path(relative).suffix.lower() in media or item.file_size > 4_000_000:
            continue
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
        stored.append(relative)
    ready = {"draft_info.json", "draft_meta_info.json"} <= set(stored)
    if not ready:
        raise ValidationError("lo zip non contiene draft_info.json e draft_meta_info.json")
    return {"stored": len(stored), "ready": ready}


@router.get("/api/capcut/sample")
def capcut_sample_state(
    user: User = Depends(require_user), workspace: Workspace = Depends(get_workspace)
) -> dict:
    folder = workspace.capcut_sample
    present = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    return {"present": present, "ready": all(f in present for f in ("draft_info.json", "draft_meta_info.json"))}


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
