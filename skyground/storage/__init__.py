"""Object storage abstraction.

Raw takes, proxies, angle inserts, audio stems and renders never live in Git.
They live behind this interface, which has two backends:

* `LocalObjectStorage` — a directory on disk, used for development and tests.
* `S3ObjectStorage` — any S3 compatible endpoint; Cloudflare R2 in production.

Both expose the same contract, including short lived signed URLs, so the rest of
the application never knows where a byte actually lives.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from skyground.errors import ValidationError

#: Object keys are POSIX-ish paths: no absolute paths, no traversal, no spaces.
KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*)*")
MAX_KEY_LENGTH = 512


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        raise ValidationError("chiave oggetto non valida")
    if ".." in key.split("/") or not KEY_PATTERN.fullmatch(key):
        raise ValidationError(f"chiave oggetto non valida: {key}")
    return key


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    content_type: str
    sha256: str | None = None


@runtime_checkable
class ObjectStorage(Protocol):
    """Contract implemented by every storage backend."""

    backend: str

    def put(self, key: str, data: bytes | BinaryIO, content_type: str = ...) -> StoredObject: ...

    def put_file(self, key: str, source: Path, content_type: str = ...) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def stat(self, key: str) -> StoredObject: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str = "") -> Iterable[StoredObject]: ...

    def signed_url(self, key: str, *, expires_in: int = ..., method: str = ...) -> str: ...


def build_storage(settings=None) -> ObjectStorage:
    """Create the storage backend described by the settings."""
    from skyground.config import STORAGE_S3, get_settings

    settings = settings or get_settings()
    if settings.storage_backend == STORAGE_S3:
        from skyground.storage.s3 import S3ObjectStorage

        return S3ObjectStorage.from_settings(settings)

    from skyground.storage.local import LocalObjectStorage

    return LocalObjectStorage(
        settings.storage_root,
        secret_key=settings.secret_key,
        default_ttl=settings.signed_url_ttl_seconds,
    )


__all__ = [
    "ObjectStorage",
    "StoredObject",
    "build_storage",
    "validate_key",
]
