"""Response shapes.

`project_payload` reproduces exactly what the first version of the tool returned
from `GET /api/projects`, because `web/app.js` and any script written against it
read those keys.
"""

from __future__ import annotations

from skyground.core.workspace import Workspace
from skyground.db.models import (
    MODE_WORKSPACE,
    Asset,
    AuditEvent,
    Membership,
    Project,
    RenderJob,
    Revision,
    User,
)


def project_payload(project: Project, workspace: Workspace, documents=None) -> dict:
    if project.storage_mode == MODE_WORKSPACE and workspace.exists(project.slug):
        manifest = dict(workspace.read_document(project.slug, "project.json"))
        base = workspace.project_dir(project.slug)
        preview = manifest.get("files", {}).get("preview", "preview.mp4")
        manifest["path"] = str(base.relative_to(workspace.root))
        manifest["previewAvailable"] = (base / preview).exists()
    elif project.storage_mode != MODE_WORKSPACE and not workspace.exists(project.slug) and not project.canvas:
        # Created by name, waiting for its footage: nothing on disk to read yet.
        manifest = {"id": project.slug, "name": project.name, "canvas": {}, "path": None,
                    "previewAvailable": False}
    else:
        try:
            manifest = dict(documents.read(project, "project.json").content) if documents else {}
        except Exception:
            # A project whose files went missing must not take the whole list
            # down with it: it is shown, empty, and can be opened or removed.
            manifest = {}
        manifest.setdefault("id", project.slug)
        manifest.setdefault("name", project.name)
        manifest.setdefault("canvas", project.canvas)
        manifest["path"] = None
        manifest["previewAvailable"] = False
    manifest["storageMode"] = project.storage_mode
    manifest["hasSource"] = workspace.exists(project.slug)
    manifest["status"] = project.status or manifest.get("status", "draft")
    return manifest


def user_payload(user: User, role: str | None = None) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "isAdmin": user.is_admin,
        "role": role,
    }


def member_payload(membership: Membership, user: User) -> dict:
    return {
        "userId": user.id,
        "email": user.email,
        "name": user.name,
        "role": membership.role,
        "since": membership.created_at.isoformat() if membership.created_at else None,
    }


def revision_payload(revision: Revision, *, include_content: bool = False) -> dict:
    payload = {
        "number": revision.number,
        "etag": revision.etag,
        "parentEtag": revision.parent_etag,
        "message": revision.message,
        "authorId": revision.author_id,
        "restoredFrom": revision.restored_from,
        "createdAt": revision.created_at.isoformat() if revision.created_at else None,
    }
    if include_content:
        payload["content"] = revision.content
    return payload


def asset_payload(asset: Asset, url: str | None = None) -> dict:
    return {
        "key": asset.key,
        "kind": asset.kind,
        "size": asset.size,
        "sha256": asset.sha256,
        "contentType": asset.content_type,
        "meta": asset.meta,
        "updatedAt": asset.updated_at.isoformat() if asset.updated_at else None,
        "url": url,
    }


def job_payload(job: RenderJob) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "maxAttempts": job.max_attempts,
        "payload": job.payload,
        "result": job.result,
        "error": job.error,
        "availableAt": job.available_at.isoformat() if job.available_at else None,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
    }


def audit_payload(event: AuditEvent) -> dict:
    return {
        "action": event.action,
        "actorId": event.actor_id,
        "target": event.target,
        "data": event.data,
        "createdAt": event.created_at.isoformat() if event.created_at else None,
    }
