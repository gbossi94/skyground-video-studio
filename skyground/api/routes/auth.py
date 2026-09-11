"""Sign-in, sign-out and identity."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from skyground.api import serializers
from skyground.api.deps import SESSION_COOKIE, current_user, get_session, get_settings
from skyground.config import AUTH_OPEN, Settings
from skyground.db.models import User
from skyground.errors import Unauthorized, ValidationError
from skyground.services import accounts

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
