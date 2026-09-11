"""Runtime configuration.

Every setting comes from the environment; nothing secret is ever read from a
versioned file. The defaults describe a local developer machine, so a checkout
runs with no configuration at all. `Settings.check_deployable()` refuses to
start a production process whose configuration would be unsafe.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from dataclasses import dataclass, field

from skyground.core.workspace import REPOSITORY_ROOT
from skyground.errors import ConfigurationError

LOCAL = "local"
PRODUCTION = "production"

AUTH_OPEN = "open"
AUTH_PASSWORD = "password"

STORAGE_LOCAL = "local"
STORAGE_S3 = "s3"

#: Identity used by the single-user local mode so that revisions and the audit
#: log always have an author, even before anybody signs in.
LOCAL_USER_EMAIL = "local@skyground.local"
LOCAL_USER_NAME = "Studio locale"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} deve essere un intero") from error


def normalize_database_url(url: str) -> str:
    """Accept the URL shapes Render and psql hand out and return a SQLAlchemy one."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class S3Settings:
    endpoint: str | None = None
    bucket: str | None = None
    region: str = "auto"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    #: Optional CDN or custom domain used to build public URLs instead of signing.
    public_base_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key_id and self.secret_access_key)


@dataclass(frozen=True)
class Settings:
    environment: str = LOCAL
    database_url: str = ""
    secret_key: str = ""
    auth_mode: str = AUTH_OPEN
    storage_backend: str = STORAGE_LOCAL
    storage_root: pathlib.Path = REPOSITORY_ROOT / ".skyground" / "storage"
    workspace_root: pathlib.Path = REPOSITORY_ROOT
    s3: S3Settings = field(default_factory=S3Settings)
    session_ttl_hours: int = 24 * 14
    signed_url_ttl_seconds: int = 900
    host: str = "127.0.0.1"
    port: int = 4173
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = ()
    worker_poll_seconds: float = 2.0
    worker_job_timeout_seconds: int = 3600
    #: False when `secret_key` is the derived development key rather than a
    #: value somebody set. Production requires an explicit one.
    secret_key_is_explicit: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment == PRODUCTION

    @property
    def uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def check_deployable(self) -> None:
        """Fail fast instead of serving a production deployment with local defaults."""
        problems: list[str] = []
        if not self.is_production:
            return
        if self.auth_mode != AUTH_PASSWORD:
            problems.append("SKYGROUND_AUTH_MODE deve essere 'password' in produzione")
        if not self.secret_key_is_explicit:
            problems.append("SKYGROUND_SECRET_KEY è obbligatoria in produzione")
        elif len(self.secret_key) < 32:
            problems.append("SKYGROUND_SECRET_KEY deve avere almeno 32 caratteri")
        if self.uses_sqlite:
            problems.append("SKYGROUND_DATABASE_URL deve puntare a PostgreSQL in produzione")
        if self.storage_backend == STORAGE_S3 and not self.s3.configured:
            problems.append("configurazione S3/R2 incompleta")
        if self.storage_backend == STORAGE_LOCAL:
            problems.append(
                "SKYGROUND_STORAGE_BACKEND deve essere 's3' in produzione: "
                "il disco di un'istanza web non è durevole"
            )
        if not self.cookie_secure:
            problems.append("SKYGROUND_COOKIE_SECURE deve restare attivo in produzione")
        if problems:
            raise ConfigurationError("configurazione non valida:\n- " + "\n- ".join(problems))


def load_settings(environ: dict | None = None) -> Settings:
    """Read settings from the process environment."""
    if environ is not None:  # used by tests to load an explicit environment
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update({key: str(value) for key, value in environ.items()})
        try:
            return load_settings()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    environment = (_env("SKYGROUND_ENV", LOCAL) or LOCAL).strip().lower()
    workspace_root = pathlib.Path(_env("SKYGROUND_WORKSPACE_ROOT", str(REPOSITORY_ROOT)))
    state_dir = workspace_root / ".skyground"
    database_url = normalize_database_url(
        _env("SKYGROUND_DATABASE_URL")
        or _env("DATABASE_URL")
        or f"sqlite+pysqlite:///{state_dir / 'studio.db'}"
    )

    secret = _env("SKYGROUND_SECRET_KEY")
    secret_is_explicit = bool(secret)
    if not secret:
        # A stable, machine-local development key. Never used in production:
        # `check_deployable()` requires an explicit secret there.
        secret = hashlib.sha256(f"skyground-local::{workspace_root}".encode()).hexdigest()

    default_auth = AUTH_PASSWORD if environment == PRODUCTION else AUTH_OPEN
    default_storage = STORAGE_S3 if environment == PRODUCTION else STORAGE_LOCAL
    origins = _env("SKYGROUND_ALLOWED_ORIGINS", "") or ""

    return Settings(
        environment=environment,
        database_url=database_url,
        secret_key=secret,
        auth_mode=(_env("SKYGROUND_AUTH_MODE", default_auth) or default_auth).strip().lower(),
        storage_backend=(
            _env("SKYGROUND_STORAGE_BACKEND", default_storage) or default_storage
        ).strip().lower(),
        storage_root=pathlib.Path(_env("SKYGROUND_STORAGE_ROOT", str(state_dir / "storage"))),
        workspace_root=workspace_root,
        s3=S3Settings(
            endpoint=_env("SKYGROUND_S3_ENDPOINT"),
            bucket=_env("SKYGROUND_S3_BUCKET"),
            region=_env("SKYGROUND_S3_REGION", "auto") or "auto",
            access_key_id=_env("SKYGROUND_S3_ACCESS_KEY_ID"),
            secret_access_key=_env("SKYGROUND_S3_SECRET_ACCESS_KEY"),
            public_base_url=_env("SKYGROUND_S3_PUBLIC_BASE_URL"),
        ),
        session_ttl_hours=_int("SKYGROUND_SESSION_TTL_HOURS", 24 * 14),
        signed_url_ttl_seconds=_int("SKYGROUND_SIGNED_URL_TTL_SECONDS", 900),
        host=_env("SKYGROUND_HOST", "0.0.0.0" if environment == PRODUCTION else "127.0.0.1"),
        port=_int("PORT", _int("SKYGROUND_PORT", 4173)),
        cookie_secure=_flag("SKYGROUND_COOKIE_SECURE", environment == PRODUCTION),
        allowed_origins=tuple(item.strip() for item in origins.split(",") if item.strip()),
        worker_poll_seconds=float(_env("SKYGROUND_WORKER_POLL_SECONDS", "2") or 2),
        worker_job_timeout_seconds=_int("SKYGROUND_WORKER_JOB_TIMEOUT_SECONDS", 3600),
        secret_key_is_explicit=secret_is_explicit,
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the process settings (used by tests and by the CLI)."""
    global _settings
    _settings = settings
