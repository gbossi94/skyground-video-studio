"""Project roles and what each of them may do."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.db.models import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER, Membership, Project, User
from skyground.errors import NotFound, PermissionDenied

# Reading a project, its documents, its history and its assets.
VIEWER_PERMISSIONS = frozenset(
    {
        "project:read",
        "document:read",
        "revision:read",
        "asset:read",
        "job:read",
        "comment:write",
    }
)

# Everything an editor does to the video itself.
EDITOR_PERMISSIONS = VIEWER_PERMISSIONS | {
    "document:write",
    "revision:restore",
    "asset:write",
    "job:create",
}

# Everything about the project as an object: people, settings, deletion.
OWNER_PERMISSIONS = EDITOR_PERMISSIONS | {
    "project:update",
    "project:archive",
    "member:manage",
    "asset:delete",
    "job:cancel",
}

PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_VIEWER: VIEWER_PERMISSIONS,
    ROLE_EDITOR: frozenset(EDITOR_PERMISSIONS),
    ROLE_OWNER: frozenset(OWNER_PERMISSIONS),
}

RANK = {ROLE_VIEWER: 1, ROLE_EDITOR: 2, ROLE_OWNER: 3}


def role_of(session: Session, project: Project, user: User | None) -> str | None:
    """Effective role of `user` on `project`, or None when they are not a member.

    An instance administrator is treated as an owner everywhere: somebody has to
    be able to repair a project whose only owner left.
    """
    if user is None:
        return None
    if user.is_admin:
        return ROLE_OWNER
    membership = session.scalar(
        select(Membership).where(
            Membership.project_id == project.id, Membership.user_id == user.id
        )
    )
    return membership.role if membership else None


def has_permission(role: str | None, permission: str) -> bool:
    return permission in PERMISSIONS.get(role or "", frozenset())


def require(session: Session, project: Project, user: User | None, permission: str) -> str:
    """Return the caller's role or refuse the request.

    A caller who is not a member is told the project does not exist rather than
    that they lack a permission: project ids are shared in links and should not
    confirm the existence of somebody else's work.
    """
    role = role_of(session, project, user)
    if role is None:
        raise NotFound(f"progetto non trovato: {project.slug}")
    if not has_permission(role, permission):
        raise PermissionDenied(f"il ruolo {role} non può eseguire {permission}")
    return role


def at_least(role: str | None, minimum: str) -> bool:
    return RANK.get(role or "", 0) >= RANK[minimum]
