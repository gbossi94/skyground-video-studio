"""Assets over HTTP: upload, signed delivery and the metadata index."""

from __future__ import annotations

import urllib.parse

from skyground.db.models import Project
from skyground.services import assets as asset_service
from tests.conftest import PROJECT_ID


def test_an_editor_uploads_and_reads_back(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    uploaded = client.put(
        f"/api/projects/{PROJECT_ID}/assets/angles/angle-nuovo.mp4",
        content=b"contenuto video",
        headers={"Content-Type": "video/mp4", "X-Skyground-Kind": "angle"},
    )
    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["key"] == f"projects/{PROJECT_ID}/angles/angle-nuovo.mp4"
    assert payload["kind"] == "angle"
    assert payload["size"] == 15

    listed = client.get(f"/api/projects/{PROJECT_ID}/assets").json()
    assert [item["key"] for item in listed] == [payload["key"]]


def test_a_viewer_cannot_upload(client, registered_project, sign_in):
    sign_in("viewer@skyground.online")
    response = client.put(
        f"/api/projects/{PROJECT_ID}/assets/raw.mov", content=b"x"
    )
    assert response.status_code == 403


def test_only_an_owner_deletes(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    client.put(f"/api/projects/{PROJECT_ID}/assets/tmp.bin", content=b"x")
    assert client.delete(f"/api/projects/{PROJECT_ID}/assets/tmp.bin").status_code == 403

    client.post("/api/auth/logout")
    sign_in("owner@skyground.online")
    assert client.delete(f"/api/projects/{PROJECT_ID}/assets/tmp.bin").status_code == 200
    assert client.get(f"/api/projects/{PROJECT_ID}/assets").json() == []


def test_the_download_link_is_signed_and_works(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    client.put(f"/api/projects/{PROJECT_ID}/assets/proxy.mp4", content=b"proxy")
    url = client.get(f"/api/projects/{PROJECT_ID}/assets").json()[0]["url"]
    assert "signature=" in url and "expires=" in url
    assert client.get(url).content == b"proxy"


def test_an_unsigned_link_is_refused(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    client.put(f"/api/projects/{PROJECT_ID}/assets/proxy.mp4", content=b"proxy")
    path = f"/media/blob/projects/{PROJECT_ID}/proxy.mp4"
    assert client.get(path).status_code == 404
    assert client.get(f"{path}?expires=99999999999&signature=inventata").status_code == 404


def test_a_tampered_link_is_refused(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    client.put(f"/api/projects/{PROJECT_ID}/assets/proxy.mp4", content=b"proxy")
    url = client.get(f"/api/projects/{PROJECT_ID}/assets").json()[0]["url"]
    parts = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parts.query)
    later = int(query["expires"][0]) + 86400
    tampered = f"{parts.path}?expires={later}&signature={query['signature'][0]}"
    assert client.get(tampered).status_code == 404


def test_an_upload_url_is_handed_out_for_large_media(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    response = client.post(
        f"/api/projects/{PROJECT_ID}/assets/upload-url", json={"path": "raw/raw.mov"}
    )
    assert response.status_code == 200
    assert response.json()["method"] == "PUT"
    assert response.json()["key"] == f"projects/{PROJECT_ID}/raw/raw.mov"


def test_keys_are_namespaced_by_project(session, registered_project):
    project = session.get(Project, registered_project["project"].id)
    assert (
        asset_service.object_key(project, "renders/a.mp4")
        == f"projects/{PROJECT_ID}/renders/a.mp4"
    )


def test_an_upload_path_cannot_escape_the_project(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    response = client.post(
        f"/api/projects/{PROJECT_ID}/assets/upload-url", json={"path": "../../altro/raw.mov"}
    )
    assert response.status_code == 422


# ------------------------------------------------------- the signed round trip
#
# `test_an_upload_url_is_handed_out_for_large_media` above checks that a URL
# comes back, which is not the same as checking that anything can be uploaded
# to it — and for a while nothing could: `upload-url` signed a PUT while
# `/media/blob/...` answered only GET. These tests use the URL.


def _upload_url(client, sign_in, path: str) -> str:
    sign_in("editor@skyground.online")
    response = client.post(
        f"/api/projects/{PROJECT_ID}/assets/upload-url", json={"path": path}
    )
    assert response.status_code == 200, response.text
    return response.json()["url"]


def test_footage_goes_up_through_the_signed_url_and_comes_back_whole(
    client, registered_project, sign_in
):
    footage = b"\x00\x01girato\xff" * 5000
    url = _upload_url(client, sign_in, "raw/raw.mov")

    stored = client.put(url, content=footage, headers={"Content-Type": "video/quicktime"})
    assert stored.status_code == 200, stored.text
    assert stored.json()["size"] == len(footage)

    # Storing the bytes and recording the asset are two steps: the signed PUT
    # carries no identity, so the registration is a separate authenticated call.
    registered = client.post(
        f"/api/projects/{PROJECT_ID}/assets",
        json={"key": stored.json()["key"], "kind": "raw"},
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["size"] == len(footage)

    listed = client.get(f"/api/projects/{PROJECT_ID}/assets").json()
    entry = next(item for item in listed if item["key"].endswith("raw/raw.mov"))
    assert client.get(entry["url"]).content == footage


def test_a_download_link_cannot_be_turned_into_an_upload(client, registered_project, sign_in):
    """The signature covers the method: a GET link must not overwrite the file
    it was handed out to read."""
    sign_in("editor@skyground.online")
    client.put(
        f"/api/projects/{PROJECT_ID}/assets/raw/raw.mov",
        content=b"l'originale",
        headers={"Content-Type": "video/quicktime"},
    )
    listed = client.get(f"/api/projects/{PROJECT_ID}/assets").json()
    download = next(item for item in listed if item["key"].endswith("raw/raw.mov"))["url"]

    assert client.put(download, content=b"sostituito").status_code == 404
    assert client.get(download).content == b"l'originale"


def test_an_expired_or_edited_upload_link_is_refused(client, registered_project, sign_in):
    url = _upload_url(client, sign_in, "raw/raw.mov")
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)

    later = int(query["expires"][0]) + 86400
    assert client.put(
        f"{parts.path}?expires={later}&signature={query['signature'][0]}", content=b"x"
    ).status_code == 404
    assert client.put(
        f"{parts.path}?expires={query['expires'][0]}&signature=00", content=b"x"
    ).status_code == 404
    # Nothing was written by any of the refused attempts.
    assert client.get(f"/api/projects/{PROJECT_ID}/assets").json() == []


def test_an_upload_longer_than_the_limit_is_refused(client, registered_project, sign_in, monkeypatch):
    """The count that decides is the one taken while writing, not the header:
    a body arriving in chunks is stopped as soon as it goes over."""
    monkeypatch.setattr("skyground.api.uploads.MAX_UPLOAD_BYTES", 1024)
    sign_in("editor@skyground.online")
    response = client.put(
        f"/api/projects/{PROJECT_ID}/assets/raw/raw.mov", content=b"x" * 4096
    )
    assert response.status_code == 422
    assert "troppo grande" in response.json()["error"]
    assert client.get(f"/api/projects/{PROJECT_ID}/assets").json() == []


def test_an_empty_upload_is_refused(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    response = client.put(f"/api/projects/{PROJECT_ID}/assets/raw/raw.mov", content=b"")
    assert response.status_code == 422
    assert client.get(f"/api/projects/{PROJECT_ID}/assets").json() == []


def test_the_spool_file_does_not_survive_the_upload(client, registered_project, sign_in, settings):
    """A half gigabyte left behind on every upload would fill the disk."""
    import pathlib

    sign_in("editor@skyground.online")
    client.put(f"/api/projects/{PROJECT_ID}/assets/raw/raw.mov", content=b"girato")
    spool = pathlib.Path(settings.storage_root) / ".uploads"
    assert list(spool.glob("*")) == []


def test_the_urls_handed_out_are_usable_as_they_are(client, registered_project, sign_in):
    """The local backend signs a path, because it does not know what name the
    studio answers to; R2 signs a whole address. A client holding the response
    cannot tell, so the API resolves it before answering."""
    sign_in("editor@skyground.online")
    handed = client.post(
        f"/api/projects/{PROJECT_ID}/assets/upload-url", json={"path": "raw/raw.mov"}
    ).json()["url"]
    assert handed.startswith("http://") or handed.startswith("https://")

    stored = client.put(handed, content=b"girato", headers={"Content-Type": "video/quicktime"})
    assert stored.status_code == 200
    client.post(f"/api/projects/{PROJECT_ID}/assets", json={"key": stored.json()["key"]})
    download = client.get(f"/api/projects/{PROJECT_ID}/assets").json()[0]["url"]
    assert download.startswith("http://") or download.startswith("https://")
    assert client.get(download).content == b"girato"
