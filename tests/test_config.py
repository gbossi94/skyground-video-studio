"""Configuration: local defaults that just work, production defaults that refuse
to start when something unsafe is missing."""

from __future__ import annotations

import pytest

from skyground.config import AUTH_OPEN, AUTH_PASSWORD, load_settings, normalize_database_url
from skyground.errors import ConfigurationError

PRODUCTION = {
    "SKYGROUND_ENV": "production",
    "SKYGROUND_SECRET_KEY": "s" * 48,
    "SKYGROUND_DATABASE_URL": "postgresql://user:pass@db.internal:5432/skyground",
    "SKYGROUND_STORAGE_BACKEND": "s3",
    "SKYGROUND_S3_ENDPOINT": "https://account.r2.cloudflarestorage.com",
    "SKYGROUND_S3_BUCKET": "skyground",
    "SKYGROUND_S3_ACCESS_KEY_ID": "id",
    "SKYGROUND_S3_SECRET_ACCESS_KEY": "secret",
}


def test_a_bare_checkout_needs_no_configuration():
    settings = load_settings({})
    assert settings.auth_mode == AUTH_OPEN
    assert settings.storage_backend == "local"
    assert settings.uses_sqlite
    assert settings.cookie_secure is False
    settings.check_deployable()  # local is always deployable


def test_a_complete_production_configuration_is_accepted():
    load_settings(PRODUCTION).check_deployable()


def test_production_defaults_to_passwords_and_secure_cookies():
    settings = load_settings(PRODUCTION)
    assert settings.auth_mode == AUTH_PASSWORD
    assert settings.cookie_secure is True
    assert settings.host == "0.0.0.0"


@pytest.mark.parametrize(
    "removed,expected",
    [
        ("SKYGROUND_SECRET_KEY", "SKYGROUND_SECRET_KEY"),
        ("SKYGROUND_S3_ACCESS_KEY_ID", "S3/R2"),
    ],
)
def test_production_refuses_to_start_without_a_secret(removed, expected):
    environment = {key: value for key, value in PRODUCTION.items() if key != removed}
    with pytest.raises(ConfigurationError) as error:
        load_settings(environment).check_deployable()
    assert expected in str(error.value)


def test_production_refuses_sqlite():
    environment = dict(PRODUCTION, SKYGROUND_DATABASE_URL="sqlite:///studio.db")
    with pytest.raises(ConfigurationError) as error:
        load_settings(environment).check_deployable()
    assert "PostgreSQL" in str(error.value)


def test_production_refuses_the_open_authentication_mode():
    environment = dict(PRODUCTION, SKYGROUND_AUTH_MODE="open")
    with pytest.raises(ConfigurationError) as error:
        load_settings(environment).check_deployable()
    assert "password" in str(error.value)


def test_production_refuses_insecure_cookies():
    environment = dict(PRODUCTION, SKYGROUND_COOKIE_SECURE="0")
    with pytest.raises(ConfigurationError):
        load_settings(environment).check_deployable()


def test_a_short_secret_is_refused():
    environment = dict(PRODUCTION, SKYGROUND_SECRET_KEY="corta")
    with pytest.raises(ConfigurationError) as error:
        load_settings(environment).check_deployable()
    assert "32 caratteri" in str(error.value)


@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///studio.db", "sqlite:///studio.db"),
    ],
)
def test_database_urls_are_normalised(given, expected):
    """Render hands out `postgres://`, which SQLAlchemy 2 no longer accepts."""
    assert normalize_database_url(given) == expected


def test_the_local_development_key_is_stable_but_never_used_in_production(tmp_path):
    first = load_settings({"SKYGROUND_WORKSPACE_ROOT": str(tmp_path)})
    second = load_settings({"SKYGROUND_WORKSPACE_ROOT": str(tmp_path)})
    assert first.secret_key == second.secret_key
    elsewhere = load_settings({"SKYGROUND_WORKSPACE_ROOT": str(tmp_path / "altro")})
    assert elsewhere.secret_key != first.secret_key


def test_render_supplies_the_port():
    assert load_settings({"PORT": "10000"}).port == 10000


def test_production_still_refuses_ephemeral_local_storage():
    """A container filesystem without a volume loses every render on deploy."""
    environment = dict(PRODUCTION, SKYGROUND_STORAGE_BACKEND="local")
    with pytest.raises(ConfigurationError) as error:
        load_settings(environment).check_deployable()
    assert "durevole" in str(error.value)


def test_a_declared_persistent_disk_is_accepted():
    """One service with a mounted volume is a legitimate deployment; two
    services are not, because a Render disk attaches to exactly one."""
    environment = dict(
        PRODUCTION,
        SKYGROUND_STORAGE_BACKEND="local",
        SKYGROUND_STORAGE_ROOT="/var/skyground",
        SKYGROUND_STORAGE_DURABLE="true",
    )
    settings = load_settings(environment)
    settings.check_deployable()
    assert settings.storage_is_durable is True


def test_durability_is_off_unless_asked_for():
    assert load_settings({}).storage_is_durable is False
