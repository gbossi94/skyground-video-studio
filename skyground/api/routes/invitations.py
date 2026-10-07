"""Invitations: an administrator names a person, the person chooses a password."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from skyground.api import serializers
from skyground.api.deps import SESSION_COOKIE, get_session, get_settings, require_user
from skyground.api.urls import absolute
from skyground.config import Settings
from skyground.db.models import Invitation, User
from skyground.errors import NotFound, PermissionDenied
from skyground.services import accounts
from skyground.services import invitations as service

router = APIRouter(prefix="/api/invitations")


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDenied("solo un amministratore può invitare")


def _payload(invitation: Invitation) -> dict:
    return {
        "id": invitation.id,
        "email": invitation.email,
        "name": invitation.name,
        "admin": invitation.is_admin,
        "createdAt": invitation.created_at.isoformat(),
        "expiresAt": invitation.expires_at.isoformat(),
    }


@router.post("")
def create_invitation(
    request: Request,
    payload: dict = Body(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    """The link is in the answer and nowhere else: it is shown once."""
    _require_admin(user)
    token, invitation = service.create(
        session,
        payload.get("email", ""),
        name=payload.get("name", ""),
        is_admin=bool(payload.get("admin")),
        actor=user,
    )
    return {**_payload(invitation), "url": absolute(request, f"/app/?invito={token}")}


@router.get("")
def list_invitations(
    user: User = Depends(require_user), session: Session = Depends(get_session)
) -> list[dict]:
    _require_admin(user)
    return [_payload(item) for item in service.pending(session)]


@router.delete("/{invitation_id}")
def revoke_invitation(
    invitation_id: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    _require_admin(user)
    service.revoke(session, invitation_id, actor=user)
    return {"revoked": invitation_id}


@router.get("/open/{token}")
def open_invitation(token: str, session: Session = Depends(get_session)) -> dict:
    """What the person opening the link needs to see: whose it is."""
    invitation = service.find(session, token)
    if invitation is None:
        raise NotFound("questo invito non è più valido: chiedine uno nuovo")
    return {"email": invitation.email, "name": invitation.name, "admin": invitation.is_admin}


@router.post("/open/{token}")
def accept_invitation(
    token: str,
    request: Request,
    response: Response,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Create the account with the password its owner chose, and sign them in."""
    user = service.accept(
        session, token, payload.get("password", ""), name=payload.get("name", "")
    )
    session_token, record = accounts.start_session(
        session, user, settings, user_agent=request.headers.get("User-Agent", "")
    )
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=int((record.expires_at - record.created_at).total_seconds()),
        path="/",
    )
    return {"user": serializers.user_payload(user)}
