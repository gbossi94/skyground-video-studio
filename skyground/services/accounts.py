"""Users, sign-in sessions and project membership."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from skyground.config import LOCAL_USER_EMAIL, LOCAL_USER_NAME, Settings
from skyground.db.models import (
    ROLES,
    AuthSession,
    Membership,
    Project,
    User,
    utcnow,
)
from skyground.errors import Conflict, NotFound, Unauthorized, ValidationError
from skyground.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    password_problems,
    verify_password,
)
from skyground.services import audit


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_user(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == normalize_email(email)))


def create_user(
    session: Session,
    email: str,
    password: str,
    *,
    name: str = "",
    is_admin: bool = False,
    require_strong_password: bool = True,
) -> User:
    email = normalize_email(email)
    if "@" not in email or len(email) < 5:
        raise ValidationError("email non valida")
    if get_user(session, email) is not None:
        raise Conflict(f"utente già presente: {email}")
    if require_strong_password:
        problems = password_problems(password)
        if problems:
            raise ValidationError("; ".join(problems))
    user = User(
        email=email,
        name=name or email.split("@")[0],
        password_hash=hash_password(password) if password else "",
        is_admin=is_admin,
    )
    session.add(user)
    session.flush()
    audit.record(session, "user.create", actor=user, target=email, data={"admin": is_admin})
    return user


def set_password(session: Session, user: User, password: str) -> None:
    problems = password_problems(password)
    if problems:
        raise ValidationError("; ".join(problems))
    user.password_hash = hash_password(password)
    session.flush()
    audit.record(session, "user.password", actor=user, target=user.email)


def authenticate(session: Session, email: str, password: str) -> User:
    user = get_user(session, email)
    # The same message and the same work either way: a wrong address must not be
    # distinguishable from a wrong password, by text or by timing.
    if user is None or not user.password_hash:
        verify_password(password or "", hash_password("timing-equalizer"))
        raise Unauthorized("credenziali non valide")
    if not verify_password(password or "", user.password_hash):
        raise Unauthorized("credenziali non valide")
    if not user.is_active:
        raise Unauthorized("utente disattivato")
    user.last_login_at = utcnow()
    session.flush()
    return user


def start_session(
    session: Session, user: User, settings: Settings, *, user_agent: str = ""
) -> tuple[str, AuthSession]:
    """Create a session and return the clear token once; only its hash is stored."""
    token = new_session_token()
    record = AuthSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        user_agent=(user_agent or "")[:255],
    )
    session.add(record)
    session.flush()
    audit.record(session, "auth.login", actor=user, target=user.email)
    return token, record


def resolve_session(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    record = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
    )
    if record is None or record.revoked_at is not None or record.expires_at <= utcnow():
        return None
    user = session.get(User, record.user_id)
    return user if user and user.is_active else None


def end_session(session: Session, token: str | None) -> None:
    if not token:
        return
    record = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
    )
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()
        audit.record(session, "auth.logout", actor=record.user, target=record.user.email)
        session.flush()


def revoke_all_sessions(session: Session, user: User) -> int:
    records = session.scalars(
        select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
    )
    count = 0
    for record in records:
        record.revoked_at = utcnow()
        count += 1
    session.flush()
    return count


def is_empty(session: Session) -> bool:
    """Whether the instance has no real account yet.

    The local single-user identity does not count: it has no usable password and
    exists only so that revisions made on a laptop have an author.
    """
    return (
        session.scalar(
            select(User).where(User.email != LOCAL_USER_EMAIL).limit(1)
        )
        is None
    )


def create_first_admin(
    session: Session, email: str, password: str, *, name: str = ""
) -> User:
    """Claim a fresh instance.

    Only possible while no account exists — which is what makes it safe to leave
    the door open on a public URL: the first person through closes it behind
    them, and everybody after is invited.
    """
    if not is_empty(session):
        raise Conflict("questa istanza ha già un account: chiedi un invito")
    user = create_user(session, email, password, name=name, is_admin=True)
    audit.record(session, "instance.claim", actor=user, target=user.email)
    return user


def ensure_local_user(session: Session) -> User:
    """The identity used by single-user local mode.

    It has no usable password: `authenticate` refuses an empty hash, so this
    account cannot be signed into even if the database is later exposed.
    """
    user = get_user(session, LOCAL_USER_EMAIL)
    if user is None:
        user = User(email=LOCAL_USER_EMAIL, name=LOCAL_USER_NAME, password_hash="", is_admin=True)
        session.add(user)
        session.flush()
    return user


# --------------------------------------------------------------------- members


def add_member(
    session: Session, project: Project, user: User, role: str, *, actor: User | None = None
) -> Membership:
    if role not in ROLES:
        raise ValidationError(f"ruolo non valido: {role}")
    membership = session.scalar(
        select(Membership).where(
            Membership.project_id == project.id, Membership.user_id == user.id
        )
    )
    if membership is None:
        membership = Membership(project_id=project.id, user_id=user.id, role=role)
        session.add(membership)
        action = "member.add"
    else:
        membership.role = role
        action = "member.update"
    session.flush()
    audit.record(
        session, action, actor=actor, project=project, target=user.email, data={"role": role}
    )
    return membership


def remove_member(
    session: Session, project: Project, user: User, *, actor: User | None = None
) -> None:
    membership = session.scalar(
        select(Membership).where(
            Membership.project_id == project.id, Membership.user_id == user.id
        )
    )
    if membership is None:
        raise NotFound(f"{user.email} non è membro di {project.slug}")
    owners = session.scalars(
        select(Membership).where(Membership.project_id == project.id, Membership.role == "owner")
    ).all()
    if membership.role == "owner" and len(owners) <= 1:
        raise ValidationError("un progetto deve avere almeno un owner")
    session.delete(membership)
    session.flush()
    audit.record(session, "member.remove", actor=actor, project=project, target=user.email)


def members(session: Session, project: Project) -> list[tuple[Membership, User]]:
    rows = session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.project_id == project.id)
        .order_by(User.email)
    ).all()
    return [(membership, user) for membership, user in rows]
