"""The existing video and the existing panel keep working.

The new application is compared directly against the original server: same
routes, same payloads, same status codes. The panel in `web/` is not modified by
this phase, so whatever it read before it must still read now.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from skyground.core.workspace import DOCUMENT_FILES
from skyground.legacy_server import make_handler
from tests.conftest import PROJECT_ID


@pytest.fixture
def legacy(workspace):
    """The original server, running on a random port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workspace))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def fetch(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}") as response:
        return response.status, json.loads(response.read())


def test_project_list_is_unchanged(legacy, local_client):
    _status, old = fetch(legacy, "/api/projects")
    new = local_client.get("/api/projects").json()
    assert len(old) == len(new) == 1
    # The new payload adds `storageMode`; every original key keeps its value.
    for key, value in old[0].items():
        assert new[0][key] == value, key


@pytest.mark.parametrize("name", DOCUMENT_FILES)
def test_every_document_is_served_unchanged(legacy, local_client, name):
    _status, old = fetch(legacy, f"/api/projects/{PROJECT_ID}/files/{name}")
    response = local_client.get(f"/api/projects/{PROJECT_ID}/files/{name}")
    assert response.status_code == 200
    assert response.json() == old


def test_status_is_unchanged(legacy, local_client):
    _status, old = fetch(legacy, f"/api/projects/{PROJECT_ID}/status")
    new = local_client.get(f"/api/projects/{PROJECT_ID}/status").json()
    assert new == old
    assert new["problems"] == []
    assert [item["destination"] for item in new["assets"]] == [
        item["destination"] for item in old["assets"]
    ]


def test_saving_returns_the_same_envelope(legacy, local_client, workspace):
    brand = workspace.read_document(PROJECT_ID, "brand.json")
    request = urllib.request.Request(
        f"{legacy}/api/projects/{PROJECT_ID}/files/brand.json",
        data=json.dumps(brand).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        old = json.loads(response.read())

    new = local_client.put(f"/api/projects/{PROJECT_ID}/files/brand.json", json=brand).json()
    assert old == {"saved": True, "problems": []}
    # `saved` and `problems` are what the panel reads; the rest is additive.
    assert new["saved"] is True
    assert new["problems"] == []
    assert set(old) <= set(new)


def test_an_unsupported_file_is_refused_by_both(legacy, local_client):
    try:
        fetch(legacy, f"/api/projects/{PROJECT_ID}/files/assets.lock.json")
        old_status = 200
    except urllib.error.HTTPError as error:  # noqa: F821 - urllib.error is imported lazily
        old_status = error.code
    new_status = local_client.get(
        f"/api/projects/{PROJECT_ID}/files/assets.lock.json"
    ).status_code
    assert old_status in (403, 422)
    assert new_status in (403, 422)


def test_media_is_served_with_byte_ranges(local_client, registered_project, sign_in):
    """Video scrubbing in the panel depends on range requests."""
    sign_in("viewer@skyground.online")
    path = f"/media/{PROJECT_ID}/composition/index.html"
    whole = local_client.get(path)
    assert whole.status_code == 200
    assert whole.headers["Accept-Ranges"] == "bytes"

    part = local_client.get(path, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert part.headers["Content-Range"].startswith("bytes 0-9/")
    assert part.content == whole.content[:10]


@pytest.mark.parametrize(
    "relative",
    [
        "../../../etc/passwd",
        "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "composition/../../../../etc/passwd",
        "../../../../../../etc/hostname",
    ],
)
def test_media_never_serves_a_file_outside_the_project(local_client, relative):
    """However the traversal is spelled, nothing outside `projects/<id>/` is read."""
    response = local_client.get(f"/media/{PROJECT_ID}/{relative}")
    assert response.status_code != 206
    assert b"root:" not in response.content
    assert response.headers.get("content-type", "").startswith(("text/html", "application/json"))


def test_both_servers_answer_the_identity_call_the_panel_makes(legacy, local_client):
    """`web/app.js` asks who it is talking to before anything else."""
    _status, old = fetch(legacy, "/api/auth/me")
    new = local_client.get("/api/auth/me").json()
    assert old["authMode"] == new["authMode"] == "open"
    assert old["user"]["email"] == new["user"]["email"]


def test_the_panel_is_served(local_client):
    page = local_client.get("/")
    assert page.status_code == 200
    assert "Skyground Video Studio" in page.text
    assert local_client.get("/app.js").status_code == 200
    assert local_client.get("/styles.css").status_code == 200


def test_the_panel_only_reads_fields_the_api_still_returns(local_client):
    """Guards against a payload change that would silently break `web/app.js`."""
    script = (
        __import__("pathlib")
        .Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("web/app.js")
        .read_text("utf-8")
    )
    project = local_client.get("/api/projects").json()[0]
    for field in re.findall(r"\bp(?:roject)?\.([a-zA-Z]+)", script):
        if field in {"id", "name", "status", "canvas", "files", "previewAvailable"}:
            assert field in project, field


def test_the_cli_and_the_api_report_the_same_problems(local_client, workspace):
    cards = workspace.read_document(PROJECT_ID, "cards.json")
    cards[0]["b"] = cards[0]["a"] - 1  # an impossible interval
    workspace.write_document(PROJECT_ID, "cards.json", cards)

    api = local_client.get(f"/api/projects/{PROJECT_ID}/status").json()["problems"]
    assert api == workspace.validate(PROJECT_ID)
    assert api != []
