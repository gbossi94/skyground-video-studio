"""S3/R2 backend, exercised against a fake endpoint running in this process.

No cloud resource and no real credential is involved: the fake bucket is an
in-memory dictionary behind a local HTTP server. What is being verified is that
the backend really speaks S3 — signed requests, the right verbs, presigned URLs
— so the production path is not just a wrapper nobody ever ran.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from skyground.config import load_settings
from skyground.errors import ConfigurationError, NotFound
from skyground.storage import build_storage
from skyground.storage.s3 import S3ObjectStorage

BUCKET = "skyground-test"


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[tuple[str, str, dict]] = []


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


@pytest.fixture
def endpoint(bucket: FakeBucket):
    """A minimal S3 endpoint: PUT, GET, HEAD, DELETE and list-objects-v2."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _key(self) -> str:
            path = urllib.parse.urlparse(self.path).path.lstrip("/")
            prefix = f"{BUCKET}/"
            return path[len(prefix) :] if path.startswith(prefix) else path

        def _record(self) -> None:
            bucket.requests.append((self.command, self.path, dict(self.headers)))

        def _send(self, status: int, body: bytes = b"", content_type="application/xml") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_PUT(self):
            self._record()
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            bucket.objects[self._key()] = (
                body,
                self.headers.get("Content-Type", "application/octet-stream"),
            )
            self.send_response(200)
            self.send_header("ETag", '"fake"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._record()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("list-type") == ["2"]:
                prefix = query.get("prefix", [""])[0]
                items = "".join(
                    f"<Contents><Key>{key}</Key><Size>{len(data)}</Size></Contents>"
                    for key, (data, _type) in sorted(bucket.objects.items())
                    if key.startswith(prefix)
                )
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<Name>{BUCKET}</Name><KeyCount>{len(bucket.objects)}</KeyCount>"
                    f"<IsTruncated>false</IsTruncated>{items}</ListBucketResult>"
                ).encode()
                return self._send(200, body)
            stored = bucket.objects.get(self._key())
            if stored is None:
                return self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
            body, content_type = stored
            return self._send(200, body, content_type)

        def do_HEAD(self):
            self._record()
            stored = bucket.objects.get(self._key())
            if stored is None:
                return self._send(404)
            body, content_type = stored
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

        def do_DELETE(self):
            self._record()
            bucket.objects.pop(self._key(), None)
            self._send(204)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def storage(endpoint: str) -> S3ObjectStorage:
    return S3ObjectStorage(
        bucket=BUCKET,
        endpoint_url=endpoint,
        region="auto",
        access_key_id="chiave-di-test",
        secret_access_key="segreto-di-test",
        default_ttl=300,
    )


def test_round_trip(storage: S3ObjectStorage, bucket: FakeBucket):
    stored = storage.put("projects/demo/raw.mov", b"contenuto", "video/quicktime")
    assert stored.size == 9
    assert bucket.objects["projects/demo/raw.mov"][0] == b"contenuto"
    assert storage.get("projects/demo/raw.mov") == b"contenuto"
    assert storage.stat("projects/demo/raw.mov").content_type == "video/quicktime"


def test_requests_are_signed(storage: S3ObjectStorage, bucket: FakeBucket):
    """R2 rejects anything that is not a v4 signature."""
    storage.put("projects/demo/a.txt", b"x")
    _method, _path, headers = bucket.requests[-1]
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=chiave-di-test/")
    assert "aws4_request" in headers["Authorization"]
    assert "x-amz-date" in {key.lower() for key in headers}


def test_upload_a_file_from_disk(storage: S3ObjectStorage, bucket: FakeBucket, tmp_path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video")
    stored = storage.put_file("projects/demo/renders/render.mp4", source, "video/mp4")
    assert stored.size == 5
    assert bucket.objects["projects/demo/renders/render.mp4"][0] == b"video"


def test_download_to_disk(storage: S3ObjectStorage, tmp_path):
    storage.put("projects/demo/raw.mov", b"contenuto")
    target = storage.download("projects/demo/raw.mov", tmp_path / "giu" / "raw.mov")
    assert target.read_bytes() == b"contenuto"


def test_missing_object_is_not_found(storage: S3ObjectStorage):
    with pytest.raises(NotFound):
        storage.get("projects/demo/assente.mp4")
    assert storage.exists("projects/demo/assente.mp4") is False


def test_delete(storage: S3ObjectStorage, bucket: FakeBucket):
    storage.put("projects/demo/a.txt", b"x")
    storage.delete("projects/demo/a.txt")
    assert "projects/demo/a.txt" not in bucket.objects


def test_list_by_prefix(storage: S3ObjectStorage):
    storage.put("projects/a/one.txt", b"1")
    storage.put("projects/b/two.txt", b"2")
    assert [item.key for item in storage.list("projects/a/")] == ["projects/a/one.txt"]


def test_presigned_get_url(storage: S3ObjectStorage):
    url = storage.signed_url("projects/demo/raw.mov", expires_in=120)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["120"]
    assert query["X-Amz-Signature"]
    assert "/skyground-test/projects/demo/raw.mov" in url


def test_presigned_put_url_is_a_different_signature(storage: S3ObjectStorage):
    get_url = storage.signed_url("projects/demo/raw.mov", method="GET")
    put_url = storage.signed_url("projects/demo/raw.mov", method="PUT")
    get_signature = urllib.parse.parse_qs(urllib.parse.urlparse(get_url).query)["X-Amz-Signature"]
    put_signature = urllib.parse.parse_qs(urllib.parse.urlparse(put_url).query)["X-Amz-Signature"]
    assert get_signature != put_signature


def test_a_public_base_url_replaces_signing(endpoint: str):
    storage = S3ObjectStorage(
        bucket=BUCKET,
        endpoint_url=endpoint,
        access_key_id="k",
        secret_access_key="s",
        public_base_url="https://media.skyground.online/",
    )
    assert (
        storage.signed_url("projects/demo/raw.mov")
        == "https://media.skyground.online/projects/demo/raw.mov"
    )


def test_incomplete_configuration_is_refused():
    settings = load_settings(
        {
            "SKYGROUND_STORAGE_BACKEND": "s3",
            "SKYGROUND_S3_BUCKET": BUCKET,
            "SKYGROUND_SECRET_KEY": "x" * 40,
        }
    )
    with pytest.raises(ConfigurationError) as error:
        build_storage(settings)
    assert "SKYGROUND_S3_ENDPOINT" in str(error.value)

