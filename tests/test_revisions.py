"""Revision history, conflict detection, restore and the audit trail."""

from __future__ import annotations

import pytest

from skyground.core.workspace import content_digest
from skyground.db.models import Project, User
from skyground.errors import Conflict, ValidationError
from skyground.services import audit
from skyground.services.documents import DocumentService
from tests.conftest import PROJECT_ID


@pytest.fixture
def service(session, workspace, registered_project):
    return DocumentService(session, workspace)


@pytest.fixture
def project(session, registered_project) -> Project:
    return session.get(Project, registered_project["project"].id)


@pytest.fixture
def editor(session, registered_project) -> User:
    return session.get(User, registered_project["editor"].id)


def test_the_state_before_the_cloud_is_itself_a_revision(service, project):
    """Revision 1 is the content the application found in the repository."""
    state = service.read(project, "brand.json")
    assert state.revision == 1
    history = service.history(project, "brand.json")
    assert [item.number for item in history] == [1]
    assert history[0].message == "stato iniziale acquisito dal workspace"


def test_a_write_appends_a_revision(service, project, editor):
    before = service.read(project, "brand.json")
    content = dict(before.content)
    content["name"] = "Skyground Studio"
    state = service.write(project, "brand.json", content, actor=editor, message="rinomino il brand")

    assert state.revision == before.revision + 1
    assert state.etag == content_digest(content)
    history = service.history(project, "brand.json")
    assert history[0].number == state.revision
    assert history[0].author_id == editor.id
    assert history[0].message == "rinomino il brand"
    assert history[0].parent_etag == before.etag


def test_a_write_reaches_the_file_on_disk(service, project, editor, workspace):
    content = dict(service.read(project, "brand.json").content)
    content["name"] = "Skyground Studio"
    service.write(project, "brand.json", content, actor=editor)
    assert workspace.read_document(PROJECT_ID, "brand.json")["name"] == "Skyground Studio"


def test_writing_the_same_content_twice_creates_no_empty_revision(service, project, editor):
    state = service.read(project, "cards.json")
    again = service.write(project, "cards.json", state.content, actor=editor)
    assert again.revision == state.revision
    assert len(service.history(project, "cards.json")) == 1


def test_a_stale_write_is_refused(service, project, editor):
    original = service.read(project, "brand.json")
    first = dict(original.content)
    first["name"] = "Prima modifica"
    service.write(project, "brand.json", first, actor=editor, base_etag=original.etag)

    second = dict(original.content)
    second["name"] = "Seconda modifica"
    with pytest.raises(Conflict) as error:
        service.write(project, "brand.json", second, actor=editor, base_etag=original.etag)
    # The refusal carries what is actually stored, so the editor can merge.
    assert error.value.current["name"] == "Prima modifica"


def test_a_write_without_a_base_revision_still_goes_through(service, project, editor):
    """The existing panel does not send `If-Match`; it must keep working."""
    content = dict(service.read(project, "brand.json").content)
    content["name"] = "Senza If-Match"
    assert service.write(project, "brand.json", content, actor=editor).revision == 2


def test_an_edit_made_outside_the_application_is_recorded(service, project, workspace):
    """A `git pull` or an agent editing the file directly must not corrupt history."""
    content = workspace.read_document(PROJECT_ID, "brand.json")
    content["name"] = "Modificato da un agente"
    workspace.write_document(PROJECT_ID, "brand.json", content)

    state = service.read(project, "brand.json")
    assert state.content["name"] == "Modificato da un agente"
    assert state.revision == 2
    external = service.history(project, "brand.json")[0]
    assert external.author_id is None
    assert "fuori dall'applicazione" in external.message


def test_an_external_edit_does_not_get_silently_overwritten(service, project, editor, workspace):
    known = service.read(project, "brand.json")
    content = workspace.read_document(PROJECT_ID, "brand.json")
    content["name"] = "Modificato da un agente"
    workspace.write_document(PROJECT_ID, "brand.json", content)

    mine = dict(known.content)
    mine["name"] = "La mia versione"
    with pytest.raises(Conflict):
        service.write(project, "brand.json", mine, actor=editor, base_etag=known.etag)


def test_restore_writes_the_old_content_as_a_new_revision(service, project, editor, workspace):
    original = service.read(project, "brand.json")
    changed = dict(original.content)
    changed["name"] = "Nome sbagliato"
    service.write(project, "brand.json", changed, actor=editor)

    restored = service.restore(project, "brand.json", 1, actor=editor)
    assert restored.revision == 3  # history is appended to, never rewritten
    assert service.read(project, "brand.json").content == original.content
    assert workspace.read_document(PROJECT_ID, "brand.json") == original.content
    assert service.history(project, "brand.json")[0].restored_from is not None


def test_a_restore_is_audited(service, project, editor, session):
    changed = dict(service.read(project, "brand.json").content)
    changed["name"] = "Nome sbagliato"
    service.write(project, "brand.json", changed, actor=editor)
    service.restore(project, "brand.json", 1, actor=editor)

    actions = [event.action for event in audit.history(session, project)]
    assert "document.restore" in actions
    assert "document.write" in actions


def test_a_revision_keeps_its_exact_content(service, project, editor):
    cards = service.read(project, "cards.json").content
    trimmed = cards[:5]
    service.write(project, "cards.json", trimmed, actor=editor)
    assert service.revision(project, "cards.json", 1).content == cards
    assert service.revision(project, "cards.json", 2).content == trimmed


def test_documents_must_keep_their_shape(service, project, editor):
    with pytest.raises(ValidationError):
        service.write(project, "cards.json", {"non": "una lista"}, actor=editor)
    with pytest.raises(ValidationError):
        service.write(project, "brand.json", ["non", "un oggetto"], actor=editor)


def test_the_project_id_cannot_be_changed(service, project, editor):
    manifest = dict(service.read(project, "project.json").content)
    manifest["id"] = "un-altro-progetto"
    with pytest.raises(ValidationError):
        service.write(project, "project.json", manifest, actor=editor)


def test_an_unknown_document_is_refused(service, project, editor):
    with pytest.raises(ValidationError):
        service.write(project, "assets.lock.json", {}, actor=editor)


# ------------------------------------------------------------------ over HTTP


def test_revisions_over_http(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    read = client.get(f"/api/projects/{PROJECT_ID}/files/brand.json")
    etag = read.headers["ETag"].strip('"')
    brand = read.json()
    brand["name"] = "Skyground Studio"

    saved = client.put(
        f"/api/projects/{PROJECT_ID}/files/brand.json",
        json=brand,
        headers={"If-Match": etag, "X-Skyground-Message": "nome aggiornato"},
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2

    history = client.get(f"/api/projects/{PROJECT_ID}/files/brand.json/revisions").json()
    assert [item["number"] for item in history["revisions"]] == [2, 1]
    assert history["revisions"][0]["message"] == "nome aggiornato"


def test_a_conflicting_write_over_http_returns_409(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    read = client.get(f"/api/projects/{PROJECT_ID}/files/brand.json")
    stale_etag = read.headers["ETag"].strip('"')
    brand = read.json()

    first = dict(brand, name="Prima")
    client.put(
        f"/api/projects/{PROJECT_ID}/files/brand.json", json=first, headers={"If-Match": stale_etag}
    )
    second = dict(brand, name="Seconda")
    response = client.put(
        f"/api/projects/{PROJECT_ID}/files/brand.json", json=second, headers={"If-Match": stale_etag}
    )
    assert response.status_code == 409
    assert response.json()["current"]["name"] == "Prima"


def test_restore_over_http(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    original = client.get(f"/api/projects/{PROJECT_ID}/files/brand.json").json()
    client.put(f"/api/projects/{PROJECT_ID}/files/brand.json", json=dict(original, name="Errore"))

    response = client.post(f"/api/projects/{PROJECT_ID}/files/brand.json/revisions/1/restore")
    assert response.status_code == 200
    assert response.json()["restored"] == 1
    assert client.get(f"/api/projects/{PROJECT_ID}/files/brand.json").json() == original


def test_a_viewer_cannot_restore(client, registered_project, sign_in):
    sign_in("viewer@skyground.online")
    response = client.post(f"/api/projects/{PROJECT_ID}/files/brand.json/revisions/1/restore")
    assert response.status_code == 403


def test_the_audit_log_is_owner_only(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    assert client.get(f"/api/projects/{PROJECT_ID}/audit").status_code == 403
    client.post("/api/auth/logout")
    sign_in("owner@skyground.online")
    events = client.get(f"/api/projects/{PROJECT_ID}/audit")
    assert events.status_code == 200
    assert any(event["action"] == "auth.login" or event["action"] for event in events.json())
