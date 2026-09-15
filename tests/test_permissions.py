"""Authentication, roles and what each role may actually do over HTTP."""

from __future__ import annotations

import pytest

from skyground.db.models import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER
from skyground.errors import Unauthorized, ValidationError
from skyground.security import hash_password, password_problems, verify_password
from skyground.services import accounts, permissions
from tests.conftest import PASSWORD, PROJECT_ID

# ------------------------------------------------------------------ passwords


def test_a_password_hash_is_salted_and_verifiable():
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second  # different salt every time
    assert first.startswith("scrypt$")
    assert PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert verify_password("un'altra password", first) is False


def test_a_corrupt_hash_never_verifies():
    for encoded in ["", "non-un-hash", "scrypt$1$2$3", "bcrypt$1$8$1$aaaa$bbbb"]:
        assert verify_password(PASSWORD, encoded) is False


def test_short_passwords_are_refused():
    assert password_problems("corta") != []
    assert password_problems(PASSWORD) == []


# ----------------------------------------------------------------- the matrix


@pytest.mark.parametrize(
    "role,permission,allowed",
    [
        (ROLE_VIEWER, "document:read", True),
        (ROLE_VIEWER, "document:write", False),
        (ROLE_VIEWER, "revision:restore", False),
        (ROLE_VIEWER, "member:manage", False),
        (ROLE_EDITOR, "document:write", True),
        (ROLE_EDITOR, "revision:restore", True),
        (ROLE_EDITOR, "job:create", True),
        (ROLE_EDITOR, "member:manage", False),
        (ROLE_EDITOR, "asset:delete", False),
        (ROLE_OWNER, "member:manage", True),
        (ROLE_OWNER, "asset:delete", True),
        (None, "document:read", False),
    ],
)
def test_permission_matrix(role, permission, allowed):
    assert permissions.has_permission(role, permission) is allowed


def test_an_admin_is_owner_everywhere(session, registered_project):
    from skyground.db.models import Project, User

    project = session.get(Project, registered_project["project"].id)
    admin = accounts.create_user(session, "admin@skyground.online", PASSWORD, is_admin=True)
    assert permissions.role_of(session, project, admin) == ROLE_OWNER
    outsider = session.get(User, registered_project["outsider"].id)
    assert permissions.role_of(session, project, outsider) is None


# ------------------------------------------------------------------- sessions


def test_login_and_identity(client, registered_project, sign_in):
    assert client.get("/api/auth/me").status_code == 401
    sign_in("owner@skyground.online")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "owner@skyground.online"


def test_the_session_cookie_is_not_readable_by_scripts(client, registered_project, sign_in):
    response = sign_in("owner@skyground.online")
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()


def test_a_wrong_password_is_refused(client, registered_project):
    response = client.post(
        "/api/auth/login", json={"email": "owner@skyground.online", "password": "sbagliata"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "credenziali non valide"


def test_an_unknown_address_looks_exactly_like_a_wrong_password(client, registered_project):
    unknown = client.post(
        "/api/auth/login", json={"email": "nessuno@example.com", "password": PASSWORD}
    )
    wrong = client.post(
        "/api/auth/login", json={"email": "owner@skyground.online", "password": "sbagliata"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_logout_invalidates_the_session(client, registered_project, sign_in):
    sign_in("owner@skyground.online")
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_the_local_account_cannot_be_signed_into(client, session_factory):
    """Local mode's identity must not become a door into a deployed instance."""
    with session_factory() as db:
        accounts.ensure_local_user(db)
        db.commit()
    response = client.post(
        "/api/auth/login", json={"email": "local@skyground.local", "password": ""}
    )
    assert response.status_code in (401, 422)


def test_a_deactivated_user_cannot_sign_in(session, make_user):
    user = make_user("sospeso@skyground.online")
    user = session.merge(user)
    user.is_active = False
    session.flush()
    with pytest.raises(Unauthorized):
        accounts.authenticate(session, "sospeso@skyground.online", PASSWORD)


# ----------------------------------------------------------- project access


def test_a_viewer_reads_but_cannot_write(client, registered_project, sign_in):
    sign_in("viewer@skyground.online")
    assert client.get(f"/api/projects/{PROJECT_ID}/files/cards.json").status_code == 200
    write = client.put(f"/api/projects/{PROJECT_ID}/files/cards.json", json=[])
    assert write.status_code == 403
    assert "viewer" in write.json()["error"]


def test_an_editor_writes(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    brand = client.get(f"/api/projects/{PROJECT_ID}/files/brand.json").json()
    brand["colors"]["primary"] = "#123456"
    response = client.put(f"/api/projects/{PROJECT_ID}/files/brand.json", json=brand)
    assert response.status_code == 200
    assert response.json()["saved"] is True


def test_an_editor_cannot_manage_members(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    response = client.post(
        f"/api/projects/{PROJECT_ID}/members",
        json={"email": "outsider@example.com", "role": "viewer"},
    )
    assert response.status_code == 403


def test_an_owner_manages_members(client, registered_project, sign_in):
    sign_in("owner@skyground.online")
    added = client.post(
        f"/api/projects/{PROJECT_ID}/members",
        json={"email": "outsider@example.com", "role": "editor"},
    )
    assert added.status_code == 200
    assert added.json()["role"] == "editor"
    emails = {member["email"] for member in client.get(f"/api/projects/{PROJECT_ID}/members").json()}
    assert "outsider@example.com" in emails


def test_a_project_always_keeps_an_owner(session, registered_project):
    from skyground.db.models import Project, User

    project = session.get(Project, registered_project["project"].id)
    owner = session.get(User, registered_project["owner"].id)
    with pytest.raises(ValidationError):
        accounts.remove_member(session, project, owner)


def test_a_stranger_is_told_the_project_does_not_exist(client, registered_project, sign_in):
    """404, not 403: an id in a link must not confirm somebody else's project."""
    sign_in("outsider@example.com")
    response = client.get(f"/api/projects/{PROJECT_ID}/files/cards.json")
    assert response.status_code == 404
    assert client.get("/api/projects").json() == []


def test_anonymous_requests_are_refused(client, registered_project):
    assert client.get("/api/projects").status_code == 401
    assert client.get(f"/api/projects/{PROJECT_ID}/status").status_code == 401
    assert client.put(f"/api/projects/{PROJECT_ID}/files/cards.json", json=[]).status_code == 401


def test_media_of_another_project_is_not_reachable(client, registered_project, sign_in):
    sign_in("outsider@example.com")
    assert client.get(f"/media/{PROJECT_ID}/composition/index.html").status_code == 404


def test_a_cross_site_write_is_refused(client, registered_project, sign_in):
    sign_in("editor@skyground.online")
    response = client.put(
        f"/api/projects/{PROJECT_ID}/files/brand.json",
        json={},
        headers={"Origin": "https://sito-malevolo.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "origine non consentita"


def test_local_mode_needs_no_sign_in(local_client):
    """`studio.py serve` on a laptop must keep working without an account."""
    assert local_client.get("/api/projects").status_code == 200
    assert local_client.get("/api/auth/me").json()["user"]["email"] == "local@skyground.local"


# ------------------------------------------------------- claiming an instance


def test_a_fresh_instance_asks_to_be_claimed(client, session_factory):
    """A studio deployed on a public URL has no account yet: somebody has to be
    first, and the API says so plainly."""
    assert client.get("/api/auth/setup").json()["required"] is True


def test_the_first_account_gets_the_projects_in_the_checkout(client, workspace):
    response = client.post(
        "/api/auth/setup",
        json={"email": "gabriele@skyground.online", "password": PASSWORD, "name": "Gabriele"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["isAdmin"] is True
    assert PROJECT_ID in response.json()["projects"]
    # Signed in straight away: no second trip through the login form.
    assert client.get("/api/auth/me").json()["user"]["email"] == "gabriele@skyground.online"
    assert [item["id"] for item in client.get("/api/projects").json()] == [PROJECT_ID]


def test_the_door_closes_after_the_first_account(client):
    client.post("/api/auth/setup", json={"email": "primo@skyground.online", "password": PASSWORD})
    client.post("/api/auth/logout")

    second = client.post(
        "/api/auth/setup", json={"email": "intruso@example.com", "password": PASSWORD}
    )
    assert second.status_code == 409
    assert "già un account" in second.json()["error"]
    assert client.get("/api/auth/setup").json()["required"] is False


def test_claiming_still_demands_a_real_password(client):
    response = client.post(
        "/api/auth/setup", json={"email": "gabriele@skyground.online", "password": "corta"}
    )
    assert response.status_code == 422
    assert client.get("/api/auth/setup").json()["required"] is True


def test_the_local_identity_does_not_count_as_an_account(client, session_factory):
    """Otherwise a studio that ran locally once could never be claimed."""
    with session_factory() as db:
        accounts.ensure_local_user(db)
        db.commit()
    assert client.get("/api/auth/setup").json()["required"] is True


def test_local_mode_has_nothing_to_claim(local_client):
    assert local_client.get("/api/auth/setup").json()["required"] is False
    assert local_client.post(
        "/api/auth/setup", json={"email": "x@y.it", "password": PASSWORD}
    ).status_code == 422
