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
