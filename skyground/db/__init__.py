"""Engine and session management."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from skyground.config import Settings, get_settings
from skyground.db.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None, **kwargs) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    options: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # The local database lives next to the checkout; create its directory.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        options["connect_args"] = {"check_same_thread": False}
        options.pop("pool_pre_ping")
    else:
        options["pool_size"] = 5
        options["max_overflow"] = 5
        options["pool_recycle"] = 300
    options.update(kwargs)
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        _enable_sqlite_integrity(engine)
    return engine


def _enable_sqlite_integrity(engine: Engine) -> None:
    """SQLite ignores foreign keys unless they are switched on per connection."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(connection, _record):  # pragma: no cover - trivial
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def configure(engine: Engine | None) -> None:
    """Point the process at a specific engine (tests, CLI, worker)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = (
        sessionmaker(bind=engine, expire_on_commit=False, future=True) if engine else None
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create the schema directly. Alembic owns the real deployments; this is for
    tests and for the throwaway SQLite database of a local checkout."""
    Base.metadata.create_all(engine or get_engine())


__all__ = [
    "Base",
    "build_engine",
    "configure",
    "create_all",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
