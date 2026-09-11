"""Request scoped dependencies: settings, database session, storage, identity."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from skyground.config import AUTH_OPEN, Settings
from skyground.core.workspace import Workspace
from skyground.db.models import Project, User
from skyground.errors import Unauthorized
from skyground.services import accounts, documents, permissions
from skyground.services import projects as project_service
from skyground.storage import ObjectStorage

SESSION_COOKIE = "skyground_session"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User | None:
    """Identify the caller, or None.

    In `open` mode — a developer running the studio on their own machine — every
    request is the local user, which keeps `studio.py serve` usable with no
    sign-in step. In `password` mode the session cookie is the only identity.
    """
    if settings.auth_mode == AUTH_OPEN:
        return accounts.ensure_local_user(session)
    return accounts.resolve_session(session, request.cookies.get(SESSION_COOKIE))


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise Unauthorized("autenticazione richiesta")
    return user


class ProjectContext:
    """A project the caller is allowed to see, with their role attached."""

    def __init__(
        self,
        project: Project,
        role: str,
        user: User,
        session: Session,
        workspace: Workspace,
    ):
        self.project = project
        self.role = role
        self.user = user
        self.session = session
        self.workspace = workspace

    @property
    def documents(self) -> documents.DocumentService:
        return documents.DocumentService(self.session, self.workspace)

    def require(self, permission: str) -> None:
        permissions.require(self.session, self.project, self.user, permission)


def project_context(
    slug: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    workspace: Workspace = Depends(get_workspace),
) -> ProjectContext:
    project = project_service.get_project(session, slug)
    role = permissions.require(session, project, user, "project:read")
    return ProjectContext(project, role, user, session, workspace)
