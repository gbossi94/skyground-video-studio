"""Receiving large media without holding it in memory.

Raw footage is hundreds of megabytes. Reading a request body into a `bytes` is
fine for a JSON document and reckless for a camera file: the web service and the
worker share one small machine, and a single upload would be enough to take both
of them down. So the body is written to disk as it arrives, a megabyte at a
time, and only then handed to the storage backend.
"""

from __future__ import annotations

import pathlib
import tempfile

from fastapi import Request

from skyground.errors import ValidationError
from skyground.storage import ObjectStorage, StoredObject
from skyground.storage.local import LocalObjectStorage

CHUNK = 1024 * 1024

#: Room for a long take at full quality, and a ceiling so a runaway upload
#: cannot fill the disk the renders live on.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024


def _spool_directory(storage: ObjectStorage) -> pathlib.Path | None:
    """Where to park the body while it arrives.

    Next to the objects themselves when storage is a local disk: the mounted
    volume has the room, and the finished file is then a rename away instead of
    a second copy. Elsewhere the system temporary directory has to do.
    """
    if isinstance(storage, LocalObjectStorage):
        spool = storage.root / ".uploads"
        spool.mkdir(parents=True, exist_ok=True)
        return spool
    return None


async def receive_object(
    request: Request,
    storage: ObjectStorage,
    key: str,
    *,
    content_type: str = "",
    limit: int | None = None,
) -> StoredObject:
    """Stream the request body into storage under `key`.

    Raises `ValidationError` if the body turns out to be longer than `limit`,
    including when the declared `Content-Length` lied: the count that matters is
    the one taken while writing.
    """
    limit = limit if limit is not None else MAX_UPLOAD_BYTES
    declared = int(request.headers.get("Content-Length") or 0)
    if declared > limit:
        raise ValidationError("file troppo grande")

    spool = _spool_directory(storage)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in the finally
        dir=spool, prefix="upload-", suffix=".part", delete=False
    )
    temporary = pathlib.Path(handle.name)
    received = 0
    try:
        with handle:
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit:
                    raise ValidationError("file troppo grande")
                handle.write(chunk)
        if received == 0:
            raise ValidationError("corpo della richiesta vuoto")
        return storage.put_file(key, temporary, content_type)
    finally:
        temporary.unlink(missing_ok=True)
