"""Asset registry and upload/download URLs."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from skyground.api import serializers
from skyground.api.deps import ProjectContext, get_settings, get_storage, project_context
from skyground.api.uploads import receive_object
from skyground.api.urls import absolute
from skyground.config import Settings
from skyground.errors import ValidationError
from skyground.services import assets as asset_service
from skyground.storage import ObjectStorage, validate_key

router = APIRouter()


@router.get("/api/projects/{slug}/assets")
def list_assets(
    request: Request,
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
) -> list[dict]:
    context.require("asset:read")
    return [
        serializers.asset_payload(
            asset, absolute(request, asset_service.download_url(storage, asset.key))
        )
        for asset in asset_service.list_for_project(context.session, context.project)
    ]


@router.post("/api/projects/{slug}/assets/upload-url")
def create_upload_url(
    request: Request,
    payload: dict = Body(...),
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Hand the client a short lived URL so raw footage goes straight to storage
    instead of through the web service."""
    context.require("asset:write")
    relative = (payload.get("path") or "").strip()
    if not relative:
        raise ValidationError("percorso dell'asset mancante")
    key = asset_service.object_key(context.project, relative)
    return {
        "key": key,
        "url": absolute(
            request,
            asset_service.upload_url(storage, key, expires_in=settings.signed_url_ttl_seconds),
        ),
        "expiresIn": settings.signed_url_ttl_seconds,
        "method": "PUT",
    }


@router.post("/api/projects/{slug}/assets")
def register_asset(
    request: Request,
    payload: dict = Body(...),
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
) -> dict:
    """Record an object that already exists in storage."""
    context.require("asset:write")
    key = validate_key(payload.get("key") or "")
    stored = storage.stat(key)
    asset = asset_service.register(
        context.session,
        context.project,
        key=key,
        kind=payload.get("kind", "other"),
        size=stored.size,
        sha256=payload.get("sha256"),
        content_type=stored.content_type,
        meta=payload.get("meta") or {},
        actor=context.user,
    )
    return serializers.asset_payload(
        asset, absolute(request, asset_service.download_url(storage, key))
    )


@router.put("/api/projects/{slug}/assets/{relative:path}")
async def upload_asset(
    relative: str,
    request: Request,
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
) -> dict:
    """Direct upload: one call that stores the bytes and records the asset.

    The body is streamed to disk rather than read into memory, so this is a
    legitimate way in for a camera file and not only for small ones. It still
    passes through the web service, which `upload-url` avoids.
    """
    context.require("asset:write")
    key = asset_service.object_key(context.project, relative)
    stored = await receive_object(
        request, storage, key, content_type=request.headers.get("Content-Type", "")
    )
    asset = asset_service.register(
        context.session,
        context.project,
        key=key,
        kind=request.headers.get("X-Skyground-Kind", "other"),
        size=stored.size,
        sha256=stored.sha256,
        content_type=stored.content_type,
        actor=context.user,
    )
    return serializers.asset_payload(
        asset, absolute(request, asset_service.download_url(storage, key))
    )


@router.delete("/api/projects/{slug}/assets/{relative:path}")
def delete_asset(
    relative: str,
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
) -> dict:
    context.require("asset:delete")
    key = asset_service.object_key(context.project, relative)
    asset_service.delete(context.session, context.project, key, storage, actor=context.user)
    return {"deleted": key}
