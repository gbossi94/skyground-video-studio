"""Invitations: a new person gets an account without anyone else handling
their password. The administrator names them, the link works once, and the
person who opens it chooses the password."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tests.conftest import PASSWORD

NEW_PASSWORD = "una-password-scelta-da-lui"


def token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["invito"][0]


def test_an_admin_invites_and_the_person_chooses_the_password(client, make_user, sign_in):
    make_user("admin@skyground.online", is_admin=True)
    sign_in("admin@skyground.online")

    made = client.post(
        "/api/invitations", json={"email": "Gianluca@Skyground.online", "name": "Gianluca", "admin": True}
    )
    assert made.status_code == 200, made.text
    invitation = made.json()
    assert invitation["email"] == "gianluca@skyground.online"
    assert "/app/?invito=" in invitation["url"]
    assert [item["email"] for item in client.get("/api/invitations").json()] == ["gianluca@skyground.online"]

    client.post("/api/auth/logout")
    token = token_of(invitation["url"])
    seen = client.get(f"/api/invitations/open/{token}")
    assert seen.json() == {"email": "gianluca@skyground.online", "name": "Gianluca", "admin": True}

    accepted = client.post(f"/api/invitations/open/{token}", json={"password": NEW_PASSWORD})
    assert accepted.status_code == 200, accepted.text
    me = client.get("/api/auth/me").json()["user"]
    assert me["email"] == "gianluca@skyground.online" and me["isAdmin"] is True

    # Once: the same link does not open a second account, or reset this one.
    client.post("/api/auth/logout")
    assert client.get(f"/api/invitations/open/{token}").status_code == 404
    assert client.post(f"/api/invitations/open/{token}", json={"password": "un-altra-password-lunga"}).status_code == 404
    assert client.post(
        "/api/auth/login", json={"email": "gianluca@skyground.online", "password": NEW_PASSWORD}
    ).status_code == 200


def test_only_an_admin_invites(client, make_user, sign_in):
    make_user("editor@skyground.online")
    sign_in("editor@skyground.online")
    assert client.post("/api/invitations", json={"email": "x@skyground.online"}).status_code == 403
    assert client.get("/api/invitations").status_code == 403


def test_a_weak_password_is_refused_and_the_link_still_works(client, make_user, sign_in):
    make_user("admin@skyground.online", is_admin=True)
    sign_in("admin@skyground.online")
    token = token_of(client.post("/api/invitations", json={"email": "nuovo@skyground.online"}).json()["url"])
    client.post("/api/auth/logout")

    assert client.post(f"/api/invitations/open/{token}", json={"password": "corta"}).status_code == 422
    assert client.post(f"/api/invitations/open/{token}", json={"password": NEW_PASSWORD}).status_code == 200


def test_a_new_invitation_replaces_the_old_and_an_existing_account_is_refused(client, make_user, sign_in):
    make_user("admin@skyground.online", is_admin=True)
    sign_in("admin@skyground.online")
    first = token_of(client.post("/api/invitations", json={"email": "doppio@skyground.online"}).json()["url"])
    second = token_of(client.post("/api/invitations", json={"email": "doppio@skyground.online"}).json()["url"])
    assert client.get(f"/api/invitations/open/{first}").status_code == 404
    assert client.get(f"/api/invitations/open/{second}").status_code == 200

    assert client.post("/api/invitations", json={"email": "admin@skyground.online"}).status_code == 409


def test_a_revoked_invitation_opens_nothing(client, make_user, sign_in):
    make_user("admin@skyground.online", is_admin=True)
    sign_in("admin@skyground.online")
    made = client.post("/api/invitations", json={"email": "revocato@skyground.online"}).json()
    assert client.delete(f"/api/invitations/{made['id']}").status_code == 200
    assert client.get(f"/api/invitations/open/{token_of(made['url'])}").status_code == 404


def test_the_password_change_route_works_for_an_invited_person(client, make_user, sign_in):
    make_user("persona@skyground.online")
    sign_in("persona@skyground.online")
    changed = client.post(
        "/api/auth/password", json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD}
    )
    assert changed.status_code == 200
