"""Invitations: how a new person gets an account without anyone else
handling their password.

An administrator — or an agent acting for one — names the person; the studio
returns a link once. Only the hash of its token is stored, the link works a
single time and expires, and the person who opens it chooses the password.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.db.models import Invitation, User, utcnow
from skyground.errors import Conflict, NotFound, ValidationError
from skyground.security import hash_session_token, new_session_token
from skyground.services import accounts, audit

#: Long enough to reach somebody on holiday, short enough that a forwarded
#: link does not stay a way in for ever.
DEFAULT_TTL = timedelta(days=7)


def create(
    session: Session,
    email: str,
    *,
    name: str = "",
    is_admin: bool = False,
    actor: User | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> tuple[str, Invitation]:
    """A new invitation and its token, in the clear this once."""
    email = accounts.normalize_email(email)
    if "@" not in email or len(email) < 5:
        raise ValidationError("email non valida")
    existing = accounts.get_user(session, email)
    if existing is not None and existing.password_hash:
        raise Conflict(f"{email} ha già un account")
    # A second invitation to the same person replaces the first: one live link
    # per address, so an old one forwarded somewhere stops working.
    for older in pending(session, email=email):
        older.revoked_at = utcnow()
    token = new_session_token()
    invitation = Invitation(
        email=email,
        name=(name or "").strip(),
        is_admin=is_admin,
        token_hash=hash_session_token(token),
        created_by=actor.id if actor else None,
        expires_at=utcnow() + ttl,
    )
    session.add(invitation)
    session.flush()
    audit.record(session, "invitation.create", actor=actor, target=email, data={"admin": is_admin})
    return token, invitation


def pending(session: Session, *, email: str | None = None) -> list[Invitation]:
    query = select(Invitation).where(
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > utcnow(),
    )
    if email is not None:
        query = query.where(Invitation.email == accounts.normalize_email(email))
    return list(session.scalars(query.order_by(Invitation.created_at.desc())))


def find(session: Session, token: str) -> Invitation | None:
    """The live invitation this token opens, or None."""
    if not token:
        return None
    invitation = session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_session_token(token))
    )
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= utcnow()
    ):
        return None
    return invitation


def accept(session: Session, token: str, password: str, *, name: str = "") -> User:
    """The account the invitation was for, with the password its owner chose."""
    invitation = find(session, token)
    if invitation is None:
        raise NotFound("questo invito non è più valido: chiedine uno nuovo")
    user = accounts.get_user(session, invitation.email)
    display = (name or invitation.name or "").strip()
    if user is None:
        user = accounts.create_user(
            session, invitation.email, password, name=display, is_admin=invitation.is_admin
        )
    elif user.password_hash:
        raise Conflict(f"{invitation.email} ha già un account")
    else:
        accounts.set_password(session, user, password)
        user.is_admin = user.is_admin or invitation.is_admin
        if display:
            user.name = display
    invitation.accepted_at = utcnow()
    session.flush()
    audit.record(session, "invitation.accept", actor=user, target=user.email)
    return user


def revoke(session: Session, invitation_id: str, *, actor: User | None = None) -> None:
    invitation = session.get(Invitation, invitation_id)
    if invitation is None or invitation.accepted_at is not None:
        raise NotFound("invito non trovato")
    invitation.revoked_at = utcnow()
    session.flush()
    audit.record(session, "invitation.revoke", actor=actor, target=invitation.email)
