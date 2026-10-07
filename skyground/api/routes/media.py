"""Media delivery.

Assets are private. Locally they are streamed from the checkout or from the
storage directory behind a short lived signed URL; in production the same call
returns a presigned R2 URL and the bytes never pass through the web service.
"""

from __future__ import annotations

import mimetypes
import pathlib
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from skyground.api.deps import ProjectContext, get_storage, project_context
from skyground.api.uploads import receive_object
from skyground.errors import NotFound, ValidationError
from skyground.storage import ObjectStorage, validate_key
from skyground.storage.local import LocalObjectStorage

router = APIRouter()

CHUNK = 1024 * 1024
RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")


def stream_file(path: pathlib.Path, request: Request) -> StreamingResponse:
    """Serve a file with byte range support, which video scrubbing needs."""
    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    requested = request.headers.get("Range")
    if requested:
        match = RANGE_PATTERN.fullmatch(requested.strip())
        if not match:
            raise ValidationError("intervallo non valido")
        if match.group(1):
            start = int(match.group(1))
        if match.group(2):
            end = min(int(match.group(2)), size - 1)
        if start > end or start >= size:
            raise ValidationError("intervallo non valido")
        status = 206

    def chunks():
        remaining = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                data = handle.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        chunks(),
        status_code=status,
        headers=headers,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


@router.get("/media/blob/{key:path}")
def read_blob(
    key: str,
    request: Request,
    expires: str = "",
    signature: str = "",
    storage: ObjectStorage = Depends(get_storage),
):
    """Serve an object from local storage against its signed URL.

    Only the local backend uses this route: with R2 the signed URL points at the
    bucket. The signature covers method, key and expiry, so a link cannot be
    edited into a different object or a later deadline.
    """
    if not isinstance(storage, LocalObjectStorage):
        raise NotFound("percorso non disponibile con questo storage")
    validate_key(key)
    if not storage.verify(key, expires, signature, "GET"):
        raise NotFound("link scaduto o non valido")
    path = storage.path_for(key)
    if not path.is_file():
        raise NotFound(f"oggetto non trovato: {key}")
    return stream_file(path, request)


@router.put("/media/blob/{key:path}")
async def write_blob(
    key: str,
    request: Request,
    expires: str = "",
    signature: str = "",
    storage: ObjectStorage = Depends(get_storage),
) -> dict:
    """Accept an object against the signed URL handed out by `upload-url`.

    The counterpart of `read_blob`, and the reason raw footage never travels
    through the authenticated API: the signature covers the *method* too, so a
    download link cannot be turned into a way to overwrite the file it points
    at. With R2 this route is unused, because the signed URL names the bucket.
    """
    if not isinstance(storage, LocalObjectStorage):
        raise NotFound("percorso non disponibile con questo storage")
    validate_key(key)
    if not storage.verify(key, expires, signature, "PUT"):
        raise NotFound("link scaduto o non valido")
    stored = await receive_object(
        request, storage, key, content_type=request.headers.get("Content-Type", "")
    )
    return {"key": stored.key, "size": stored.size, "sha256": stored.sha256}


@router.get("/media/{slug}/{relative:path}")
def read_media(
    relative: str,
    request: Request,
    context: ProjectContext = Depends(project_context),
    storage: ObjectStorage = Depends(get_storage),
):
    """Media of one project, addressed the way the panel already addresses it."""
    context.require("asset:read")
    if context.workspace.exists(context.project.slug):
        try:
            path = context.workspace.media_path(context.project.slug, relative)
            return stream_file(path, request)
        except NotFound:
            pass
    key = f"projects/{context.project.slug}/{relative.lstrip('/')}"
    if not storage.exists(validate_key(key)):
        raise NotFound("media non trovato")
    return RedirectResponse(storage.signed_url(key), status_code=307)
