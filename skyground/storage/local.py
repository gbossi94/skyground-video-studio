"""Filesystem backend: the development and test storage.

Signed URLs point back at the application (`/media/blob/...`) and carry an HMAC
token, so the same "private asset, short lived URL" flow that R2 provides in
production is exercised locally too.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import pathlib
import shutil
import time
import urllib.parse
from collections.abc import Iterator
from typing import BinaryIO

from skyground.errors import NotFound, ValidationError
from skyground.storage import StoredObject, validate_key

SIGNED_PATH_PREFIX = "/media/blob/"


class LocalObjectStorage:
    backend = "local"

    def __init__(
        self,
        root: pathlib.Path | str,
        *,
        secret_key: str = "local",
        default_ttl: int = 900,
        base_url: str = "",
    ):
        self.root = pathlib.Path(root).resolve()
        self.secret_key = secret_key
        self.default_ttl = default_ttl
        self.base_url = base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def path_for(self, key: str) -> pathlib.Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValidationError(f"chiave oggetto fuori dallo storage: {key}")
        return path

    # ----------------------------------------------------------------- writes

    def put(self, key: str, data: bytes | BinaryIO, content_type: str = "") -> StoredObject:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temporary = path.with_name(path.name + ".part")
        with temporary.open("wb") as handle:
            if isinstance(data, bytes | bytearray):
                chunks: Iterator[bytes] = iter([bytes(data)])
            else:
                chunks = iter(lambda: data.read(1024 * 1024), b"")
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        temporary.replace(path)
        return StoredObject(
            key=key,
            size=size,
            content_type=content_type or guess_content_type(key),
            sha256=digest.hexdigest(),
        )

    def put_file(self, key: str, source: pathlib.Path, content_type: str = "") -> StoredObject:
        with pathlib.Path(source).open("rb") as handle:
            return self.put(key, handle, content_type)

    def delete(self, key: str) -> None:
        path = self.path_for(key)
        if path.is_file():
            path.unlink()

    # ------------------------------------------------------------------ reads

    def get(self, key: str) -> bytes:
        path = self.path_for(key)
        if not path.is_file():
            raise NotFound(f"oggetto non trovato: {key}")
        return path.read_bytes()

    def download(self, key: str, destination: pathlib.Path) -> pathlib.Path:
        path = self.path_for(key)
        if not path.is_file():
            raise NotFound(f"oggetto non trovato: {key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        return destination

    def stat(self, key: str) -> StoredObject:
        path = self.path_for(key)
        if not path.is_file():
            raise NotFound(f"oggetto non trovato: {key}")
        return StoredObject(key=key, size=path.stat().st_size, content_type=guess_content_type(key))

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def list(self, prefix: str = "") -> list[StoredObject]:
        objects = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.endswith(".part"):
                continue
            key = path.relative_to(self.root).as_posix()
            if prefix and not key.startswith(prefix):
                continue
            objects.append(
                StoredObject(
                    key=key,
                    size=path.stat().st_size,
                    content_type=guess_content_type(key),
                )
            )
        return objects

    # ------------------------------------------------------------ signed URLs

    def signed_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        method: str = "GET",
        now: float | None = None,
    ) -> str:
        validate_key(key)
        expires = int(now or time.time()) + int(expires_in or self.default_ttl)
        signature = self.sign(key, expires, method)
        query = urllib.parse.urlencode({"expires": expires, "signature": signature})
        return f"{self.base_url}{SIGNED_PATH_PREFIX}{urllib.parse.quote(key)}?{query}"

    def sign(self, key: str, expires: int, method: str = "GET") -> str:
        message = f"{method.upper()}\n{key}\n{expires}".encode()
        return hmac.new(self.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def verify(
        self,
        key: str,
        expires: int | str,
        signature: str,
        method: str = "GET",
        now: float | None = None,
    ) -> bool:
        try:
            expires_at = int(expires)
        except (TypeError, ValueError):
            return False
        if expires_at < int(now or time.time()):
            return False
        return hmac.compare_digest(self.sign(key, expires_at, method), signature or "")


def guess_content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"
