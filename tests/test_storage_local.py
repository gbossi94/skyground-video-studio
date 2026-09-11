"""Filesystem storage backend and its signed URLs."""

from __future__ import annotations

import time

import pytest

from skyground.errors import NotFound, ValidationError
from skyground.storage import build_storage, validate_key
from skyground.storage.local import LocalObjectStorage


@pytest.fixture
def storage(tmp_path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path / "objects", secret_key="chiave-di-test", default_ttl=60)


def test_round_trip(storage: LocalObjectStorage):
    stored = storage.put("projects/demo/raw.mov", b"contenuto", "video/quicktime")
    assert stored.size == 9
    assert stored.content_type == "video/quicktime"
    assert storage.get("projects/demo/raw.mov") == b"contenuto"
    assert storage.exists("projects/demo/raw.mov")
    assert storage.stat("projects/demo/raw.mov").size == 9


def test_put_reports_the_digest(storage: LocalObjectStorage):
    import hashlib

    stored = storage.put("projects/demo/a.json", b"{}")
    assert stored.sha256 == hashlib.sha256(b"{}").hexdigest()


def test_missing_object(storage: LocalObjectStorage):
    with pytest.raises(NotFound):
        storage.get("projects/demo/assente.mp4")
    assert storage.exists("projects/demo/assente.mp4") is False


def test_delete_is_idempotent(storage: LocalObjectStorage):
    storage.put("projects/demo/a.txt", b"x")
    storage.delete("projects/demo/a.txt")
    storage.delete("projects/demo/a.txt")
    assert storage.exists("projects/demo/a.txt") is False


def test_list_filters_by_prefix(storage: LocalObjectStorage):
    storage.put("projects/a/one.txt", b"1")
    storage.put("projects/a/two.txt", b"2")
    storage.put("projects/b/three.txt", b"3")
    assert [item.key for item in storage.list("projects/a/")] == [
        "projects/a/one.txt",
        "projects/a/two.txt",
    ]


@pytest.mark.parametrize(
    "key",
    ["../escape.txt", "/absolute.txt", "projects/../../etc/passwd", "", "a//b", "spazio nel nome"],
)
def test_keys_that_must_be_refused(storage: LocalObjectStorage, key: str):
    with pytest.raises(ValidationError):
        storage.put(key, b"x")


def test_key_validation_accepts_normal_keys():
    assert validate_key("projects/demo-01/renders/demo-01-20260101.mp4")


def test_a_partial_upload_never_becomes_the_object(storage: LocalObjectStorage):
    class Exploding:
        def read(self, _size):
            raise OSError("connessione interrotta")

    with pytest.raises(OSError):
        storage.put("projects/demo/raw.mov", Exploding())
    assert storage.exists("projects/demo/raw.mov") is False


def test_signed_url_round_trip(storage: LocalObjectStorage):
    url = storage.signed_url("projects/demo/raw.mov", expires_in=120)
    assert url.startswith("/media/blob/projects/demo/raw.mov?")
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert storage.verify("projects/demo/raw.mov", expires, signature) is True


def test_signed_url_expires(storage: LocalObjectStorage):
    now = time.time()
    url = storage.signed_url("projects/demo/raw.mov", expires_in=10, now=now)
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert storage.verify("projects/demo/raw.mov", expires, signature, now=now + 11) is False


def test_a_signature_does_not_transfer_to_another_object(storage: LocalObjectStorage):
    url = storage.signed_url("projects/demo/raw.mov", expires_in=60)
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert storage.verify("projects/altro/raw.mov", expires, signature) is False


def test_a_signature_does_not_transfer_to_another_method(storage: LocalObjectStorage):
    url = storage.signed_url("projects/demo/raw.mov", expires_in=60, method="GET")
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert storage.verify("projects/demo/raw.mov", expires, signature, method="PUT") is False


def test_a_later_deadline_invalidates_the_signature(storage: LocalObjectStorage):
    url = storage.signed_url("projects/demo/raw.mov", expires_in=60)
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert storage.verify("projects/demo/raw.mov", expires + 3600, signature) is False


def test_another_secret_cannot_sign(tmp_path):
    mine = LocalObjectStorage(tmp_path / "objects", secret_key="segreto-vero")
    theirs = LocalObjectStorage(tmp_path / "objects", secret_key="segreto-falso")
    url = theirs.signed_url("projects/demo/raw.mov", expires_in=60)
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert mine.verify("projects/demo/raw.mov", expires, signature) is False


def test_build_storage_follows_the_settings(settings):
    storage = build_storage(settings)
    assert storage.backend == "local"
    assert storage.root == settings.storage_root.resolve()
