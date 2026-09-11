"""Asset metadata: the index of what exists in object storage."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.db.models import Asset, Project, User
from skyground.errors import NotFound
from skyground.services import audit
from skyground.storage import ObjectStorage, validate_key

ASSET_KINDS = ("raw", "proxy", "angle", "audio", "render", "preview", "font", "other")


def object_key(project: Project, relative: str) -> str:
    """Namespace every object by project so one bucket holds the whole studio."""
    return validate_key(f"projects/{project.slug}/{relative.lstrip('/')}")


def register(
    session: Session,
    project: Project,
    *,
    key: str,
    kind: str = "other",
    size: int = 0,
    sha256: str | None = None,
    content_type: str = "",
    meta: dict | None = None,
    actor: User | None = None,
) -> Asset:
    validate_key(key)
    asset = session.scalar(
        select(Asset).where(Asset.project_id == project.id, Asset.key == key)
    )
    if asset is None:
        asset = Asset(project_id=project.id, key=key)
        session.add(asset)
    asset.kind = kind
    asset.size = size
    asset.sha256 = sha256
    asset.content_type = content_type
    asset.meta = meta or {}
    asset.uploaded_by = actor.id if actor else asset.uploaded_by
    session.flush()
    audit.record(
        session,
        "asset.register",
        actor=actor,
        project=project,
        target=key,
        data={"kind": kind, "size": size},
    )
    return asset


def get(session: Session, project: Project, key: str) -> Asset:
    asset = session.scalar(select(Asset).where(Asset.project_id == project.id, Asset.key == key))
    if asset is None:
        raise NotFound(f"asset non trovato: {key}")
    return asset


def list_for_project(session: Session, project: Project) -> list[Asset]:
    return list(
        session.scalars(
            select(Asset).where(Asset.project_id == project.id).order_by(Asset.key)
        )
    )


def delete(
    session: Session,
    project: Project,
    key: str,
    storage: ObjectStorage,
    *,
    actor: User | None = None,
) -> None:
    asset = get(session, project, key)
    storage.delete(key)
    session.delete(asset)
    session.flush()
    audit.record(session, "asset.delete", actor=actor, project=project, target=key)


def download_url(storage: ObjectStorage, key: str, *, expires_in: int | None = None) -> str:
    return storage.signed_url(key, expires_in=expires_in, method="GET")


def upload_url(storage: ObjectStorage, key: str, *, expires_in: int | None = None) -> str:
    return storage.signed_url(validate_key(key), expires_in=expires_in, method="PUT")
