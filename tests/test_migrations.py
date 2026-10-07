"""Migrations describe exactly the model the application uses."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from skyground.core.workspace import REPOSITORY_ROOT
from skyground.db.models import Base

EXPECTED_TABLES = {
    "users",
    "auth_sessions",
    "projects",
    "memberships",
    "documents",
    "revisions",
    "assets",
    "render_jobs",
    "audit_events",
    "invitations",
}


@pytest.fixture
def alembic_config(tmp_path) -> tuple[Config, str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "skyground/db/migrations"))
    return config, url


def test_upgrade_creates_every_table(alembic_config):
    config, url = alembic_config
    engine = create_engine(url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert EXPECTED_TABLES <= set(inspect(connection).get_table_names())
    engine.dispose()


def test_the_migration_matches_the_models(alembic_config):
    """`alembic check` must find nothing: no model change without a migration."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    config, url = alembic_config
    engine = create_engine(url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()


def test_downgrade_removes_everything(alembic_config):
    config, url = alembic_config
    engine = create_engine(url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        assert EXPECTED_TABLES & set(inspect(connection).get_table_names()) == set()
    engine.dispose()


def test_there_is_a_single_head():
    """Two heads would mean two people migrated in parallel without merging."""
    script = ScriptDirectory(str(REPOSITORY_ROOT / "skyground/db/migrations"))
    assert len(script.get_heads()) == 1


def test_no_connection_string_is_versioned():
    text = (REPOSITORY_ROOT / "alembic.ini").read_text("utf-8")
    assert "sqlalchemy.url =" not in text
    assert "postgres" not in text.lower().replace("postgresql://", "")
