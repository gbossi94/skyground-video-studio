"""Shared fixtures.

Every test runs against a temporary copy of the real project, so the checkout is
never modified and the tests exercise the actual editorial data rather than a
simplified fake.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from skyground.config import Settings, load_settings, set_settings
from skyground.core.workspace import REPOSITORY_ROOT, Workspace
from skyground.db import build_engine, create_all
from skyground.db.models import Base
from skyground.services import accounts

PROJECT_ID = "beauty-centers-growth-01"

#: Media and fonts are not in Git; copying them is neither possible nor needed.
SKIPPED_SUFFIXES = (".ttf", ".mp4", ".mov", ".m4a", ".mp3", ".wav")


def _ignore_media(_directory, names):
    return [name for name in names if name.endswith(SKIPPED_SUFFIXES)]


@pytest.fixture
def workspace_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A checkout containing a copy of the real project."""
    root = tmp_path / "checkout"
    (root / "projects").mkdir(parents=True)
    shutil.copytree(
        REPOSITORY_ROOT / "projects" / PROJECT_ID,
        root / "projects" / PROJECT_ID,
        ignore=_ignore_media,
    )
    return root


@pytest.fixture
def workspace(workspace_root: pathlib.Path) -> Workspace:
    return Workspace(workspace_root)


def database_url(tmp_path: pathlib.Path) -> str:
    """SQLite by default; `SKYGROUND_TEST_DATABASE_URL` runs the same suite on
    PostgreSQL, which is what CI does before a deployment."""
    return os.environ.get("SKYGROUND_TEST_DATABASE_URL") or f"sqlite+pysqlite:///{tmp_path / 'studio.db'}"


@pytest.fixture
def settings(workspace_root: pathlib.Path, tmp_path: pathlib.Path) -> Settings:
    value = load_settings(
        {
            "SKYGROUND_ENV": "local",
            "SKYGROUND_WORKSPACE_ROOT": str(workspace_root),
            "SKYGROUND_DATABASE_URL": database_url(tmp_path),
            "SKYGROUND_STORAGE_ROOT": str(tmp_path / "storage"),
            "SKYGROUND_SECRET_KEY": "test-secret-key-that-is-long-enough-32",
            "SKYGROUND_AUTH_MODE": "password",
        }
    )
    set_settings(value)
    yield value
    set_settings(None)


@pytest.fixture
def local_settings(settings: Settings) -> Settings:
    """Same configuration with the single-user local mode enabled."""
    from dataclasses import replace

    value = replace(settings, auth_mode="open")
    set_settings(value)
    return value


@pytest.fixture
def engine(settings: Settings):
    engine = build_engine(settings)
    if not settings.uses_sqlite:
        # A shared PostgreSQL instance is reused between tests; start clean.
        Base.metadata.drop_all(engine)
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(session_factory):
    session = session_factory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def app(settings: Settings, engine):
    from skyground.api.app import create_app

    return create_app(settings, engine=engine)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def local_app(local_settings: Settings, engine):
    from skyground.api.app import create_app

    return create_app(local_settings, engine=engine)


@pytest.fixture
def local_client(local_app):
    with TestClient(local_app) as client:
        yield client


PASSWORD = "una-password-lunga-abbastanza"


@pytest.fixture
def make_user(session_factory):
    def factory(email: str, *, password: str = PASSWORD, is_admin: bool = False):
        with session_factory() as db:
            user = accounts.create_user(db, email, password, is_admin=is_admin)
            db.commit()
            return user

    return factory


@pytest.fixture
def registered_project(session_factory, workspace, make_user):
    """The real project, registered with an owner, an editor and a viewer."""
    from skyground.services import projects as project_service

    owner = make_user("owner@skyground.online")
    editor = make_user("editor@skyground.online")
    viewer = make_user("viewer@skyground.online")
    outsider = make_user("outsider@example.com")
    with session_factory() as db:
        project = project_service.register_workspace_project(
            db, workspace, PROJECT_ID, owner=db.get(type(owner), owner.id)
        )
        accounts.add_member(db, project, db.get(type(editor), editor.id), "editor")
        accounts.add_member(db, project, db.get(type(viewer), viewer.id), "viewer")
        db.commit()
        return {
            "project": project,
            "owner": owner,
            "editor": editor,
            "viewer": viewer,
            "outsider": outsider,
        }


@pytest.fixture
def sign_in(client):
    def login(email: str, password: str = PASSWORD):
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return response

    return login
