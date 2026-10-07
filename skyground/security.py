"""Password hashing and session tokens (standard library only).

`hashlib.scrypt` is used rather than an external hashing library: it ships with
CPython, it is memory hard, and the parameters are stored inside the hash so
they can be raised later without invalidating existing passwords.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_LENGTH = 12

TOKEN_BYTES = 32


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return `scrypt$n$r$p$salt$key`, everything but the password itself."""
    if not isinstance(password, str) or not password:
        raise ValueError("password vuota")
    salt = salt or secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(key).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    if not password or not encoded:
        return False
    try:
        scheme, n, r, p, salt, key = encoded.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(key)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, base64.b64decode(key))


def password_problems(password: str) -> list[str]:
    """Minimum policy: length only, so that passphrases are not penalised."""
    problems = []
    if len(password or "") < MIN_PASSWORD_LENGTH:
        problems.append(f"la password deve avere almeno {MIN_PASSWORD_LENGTH} caratteri")
    if (password or "").strip() != (password or ""):
        problems.append("la password non può iniziare o finire con uno spazio")
    return problems


def new_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Session tokens are already high entropy, so a plain digest is enough and
    keeps lookups a single indexed comparison."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
