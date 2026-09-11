"""S3 compatible backend, used with Cloudflare R2 in production.

Nothing here is R2 specific beyond the defaults: the endpoint, the bucket and
the credentials come from the environment, so the same class also works against
AWS S3, MinIO or the fake endpoint used by the tests.
"""

from __future__ import annotations

import hashlib
import pathlib
from contextlib import contextmanager
from typing import BinaryIO

from skyground.errors import ConfigurationError, NotFound
from skyground.storage import StoredObject, validate_key
from skyground.storage.local import guess_content_type


class S3ObjectStorage:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str = "auto",
        access_key_id: str,
        secret_access_key: str,
        default_ttl: int = 900,
        public_base_url: str | None = None,
        client=None,
    ):
        self.bucket = bucket
        self.default_ttl = default_ttl
        self.public_base_url = (public_base_url or "").rstrip("/")
        self._client = client or self._build_client(
            endpoint_url=endpoint_url,
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )

    @staticmethod
    def _build_client(
        *, endpoint_url: str, region: str, access_key_id: str, secret_access_key: str
    ):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise ConfigurationError(
                "boto3 non installato: esegui `pip install -r requirements.txt`"
            ) from error
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # R2 only implements the v4 signature and rejects the checksum
            # trailers newer SDK defaults add to streaming uploads.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @classmethod
    def from_settings(cls, settings) -> S3ObjectStorage:
        if not settings.s3.configured:
            raise ConfigurationError(
                "storage S3 selezionato ma incompleto: servono SKYGROUND_S3_ENDPOINT, "
                "SKYGROUND_S3_BUCKET, SKYGROUND_S3_ACCESS_KEY_ID e SKYGROUND_S3_SECRET_ACCESS_KEY"
            )
        return cls(
            bucket=settings.s3.bucket,
            endpoint_url=settings.s3.endpoint,
            region=settings.s3.region,
            access_key_id=settings.s3.access_key_id,
            secret_access_key=settings.s3.secret_access_key,
            default_ttl=settings.signed_url_ttl_seconds,
            public_base_url=settings.s3.public_base_url,
        )

    # ----------------------------------------------------------------- writes

    def put(self, key: str, data: bytes | BinaryIO, content_type: str = "") -> StoredObject:
        validate_key(key)
        body = data if isinstance(data, bytes | bytearray) else data.read()
        body = bytes(body)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type or guess_content_type(key),
        )
        return StoredObject(
            key=key,
            size=len(body),
            content_type=content_type or guess_content_type(key),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def put_file(self, key: str, source: pathlib.Path, content_type: str = "") -> StoredObject:
        validate_key(key)
        source = pathlib.Path(source)
        self._client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type or guess_content_type(key)},
        )
        return StoredObject(
            key=key,
            size=source.stat().st_size,
            content_type=content_type or guess_content_type(key),
        )

    def delete(self, key: str) -> None:
        validate_key(key)
        self._client.delete_object(Bucket=self.bucket, Key=key)

    # ------------------------------------------------------------------ reads

    def get(self, key: str) -> bytes:
        validate_key(key)
        with translate_missing(key):
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def download(self, key: str, destination: pathlib.Path) -> pathlib.Path:
        validate_key(key)
        destination = pathlib.Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with translate_missing(key):
            self._client.download_file(self.bucket, key, str(destination))
        return destination

    def stat(self, key: str) -> StoredObject:
        validate_key(key)
        with translate_missing(key):
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        return StoredObject(
            key=key,
            size=int(head.get("ContentLength", 0)),
            content_type=head.get("ContentType") or guess_content_type(key),
        )

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
        except NotFound:
            return False
        return True

    def list(self, prefix: str = "") -> list[StoredObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        objects: list[StoredObject] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                objects.append(
                    StoredObject(
                        key=item["Key"],
                        size=int(item.get("Size", 0)),
                        content_type=guess_content_type(item["Key"]),
                    )
                )
        return objects

    # ------------------------------------------------------------ signed URLs

    def signed_url(self, key: str, *, expires_in: int | None = None, method: str = "GET") -> str:
        validate_key(key)
        if self.public_base_url and method.upper() == "GET":
            return f"{self.public_base_url}/{key}"
        operations = {"GET": "get_object", "PUT": "put_object", "HEAD": "head_object"}
        operation = operations[method.upper()]
        return self._client.generate_presigned_url(
            operation,
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(expires_in or self.default_ttl),
        )


@contextmanager
def translate_missing(key: str):
    """Turn "no such key" into `NotFound` and let every other error surface.

    An expired credential or a denied bucket policy must not be reported to the
    editor as a missing asset.
    """
    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover - dependency is declared
        yield
        return
    try:
        yield
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise NotFound(f"oggetto non trovato: {key}") from error
        raise
