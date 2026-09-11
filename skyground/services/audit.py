"""Append-only audit trail."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.db.models import AuditEvent, Project, User


def record(
    session: Session,
    action: str,
    *,
    actor: User | None = None,
    project: Project | None = None,
    target: str = "",
    data: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        action=action,
        actor_id=actor.id if actor else None,
        project_id=project.id if project else None,
        target=target[:200],
        data=data or {},
    )
    session.add(event)
    return event


def history(session: Session, project: Project, limit: int = 100) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.project_id == project.id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
    )
