"""Sign-in, sign-out and identity."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from skyground.api import serializers
from skyground.api.deps import (
    SESSION_COOKIE,
    current_user,
    get_session,
    get_settings,
    get_workspace,
)
from skyground.config import AUTH_OPEN, Settings
from skyground.core.workspace import Workspace
from skyground.db.models import User
from skyground.errors import Unauthorized, ValidationError
from skyground.services import accounts
from skyground.services import projects as project_service

router = APIRouter(prefix="/api/auth")


@router.post("/login")
def login(
    request: Request,
    response: Response,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    if settings.auth_mode == AUTH_OPEN:
        # Nothing to sign into: the local studio is already the local user.
        return {"user": serializers.user_payload(accounts.ensure_local_user(session))}
    email = payload.get("email", "")
    password = payload.get("password", "")
    if not email or not password:
        raise ValidationError("email e password sono obbligatorie")
    user = accounts.authenticate(session, email, password)
    token, record = accounts.start_session(
        session, user, settings, user_agent=request.headers.get("User-Agent", "")
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int((record.expires_at - record.created_at).total_seconds()),
        path="/",
    )
    return {"user": serializers.user_payload(user)}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    accounts.end_session(session, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(
    user: User | None = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    if user is None:
        raise Unauthorized("autenticazione richiesta")
    return {"user": serializers.user_payload(user), "authMode": settings.auth_mode}


@router.post("/password")
def change_password(
    payload: dict = Body(...),
    user: User | None = Depends(current_user),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    if user is None:
        raise Unauthorized("autenticazione richiesta")
    if settings.auth_mode == AUTH_OPEN:
        raise ValidationError("la modalità locale non usa password")
    accounts.authenticate(session, user.email, payload.get("currentPassword", ""))
    accounts.set_password(session, user, payload.get("newPassword", ""))
    # A password change invalidates every other browser that was signed in.
    accounts.revoke_all_sessions(session, user)
    return {"ok": True}


@router.get("/setup")
def setup_required(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Whether this instance still has to be claimed."""
    if settings.auth_mode == AUTH_OPEN:
        return {"required": False, "reason": "modalità locale"}
    return {"required": accounts.is_empty(session)}


@router.post("/setup")
def claim_instance(
    request: Request,
    response: Response,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    workspace: Workspace = Depends(get_workspace),
) -> dict:
    """Create the first account, and hand it the projects in the checkout.

    Available only while the instance has no account. A freshly deployed studio
    is therefore usable by whoever gets there first and by nobody afterwards.
    """
    if settings.auth_mode == AUTH_OPEN:
        raise ValidationError("la modalità locale non richiede un account")

    email = (payload or {}).get("email", "")
    password = (payload or {}).get("password", "")
    if not email or not password:
        raise ValidationError("email e password sono obbligatorie")

    user = accounts.create_first_admin(
        session, email, password, name=(payload or {}).get("name", "")
    )
    registered = [
        project.slug for project in project_service.sync_workspace(session, workspace, owner=user)
    ]

    token, record = accounts.start_session(
        session, user, settings, user_agent=request.headers.get("User-Agent", "")
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int((record.expires_at - record.created_at).total_seconds()),
        path="/",
    )
    return {"user": serializers.user_payload(user), "projects": registered}
