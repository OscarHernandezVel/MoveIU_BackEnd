"""Configuration rules of the data layer: PostgreSQL, Redis, TLS and secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from core_service.config import Settings
from core_service.errors import ValidationError

DEV = {"CORE_ENV": "development", "CORE_DATABASE_URL": "postgresql://core:secret@localhost:5432/core"}
PRODUCTION = {
    "CORE_ENV": "production",
    "CORE_JWT_SECRET": "j" * 40,
    "CORE_INGEST_KEY": "i" * 40,
    "CORE_DATABASE_URL": "postgresql://core_app:s3cr3t@db.internal:5432/core?sslmode=verify-full",
    "CORE_TIMESCALE_REQUIRED": "true",
    "CORE_REDIS_URL": "rediss://core:s3cr3t@redis.internal:6379/0",
}


def problems(env: dict[str, str]) -> str:
    with pytest.raises(ValidationError) as caught:
        Settings.from_env(env)
    return " | ".join(caught.value.details)


class TestDatabaseUrl:
    def test_a_development_url_is_enough(self) -> None:
        settings = Settings.from_env(DEV)
        assert settings.database.url.startswith("postgresql://") and not settings.redis.enabled

    def test_the_url_is_mandatory(self) -> None:
        assert "CORE_DATABASE_URL is required" in problems({"CORE_ENV": "development"})

    def test_sqlite_is_not_accepted(self) -> None:
        assert "SQLite is not supported" in problems({**DEV, "CORE_DATABASE_URL": "sqlite:///core.db"})

    def test_pool_and_retention_are_validated(self) -> None:
        assert "POOL" in problems({**DEV, "CORE_DB_POOL_MIN": "5", "CORE_DB_POOL_MAX": "2"})
        assert "RETENTION" in problems({**DEV, "CORE_TELEMETRY_COMPRESS_AFTER_DAYS": "40"})

    def test_redis_url_and_sentinels_are_exclusive(self) -> None:
        text = problems({**DEV, "CORE_REDIS_URL": "redis://x", "CORE_REDIS_SENTINELS": "a:26379"})
        assert "not both" in text


class TestProductionRules:
    def test_a_complete_production_setup_is_accepted(self) -> None:
        settings = Settings.from_env(PRODUCTION)
        assert settings.is_production and settings.database.require_timescale and settings.redis.enabled

    @pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
    def test_postgres_must_use_tls(self, mode: str) -> None:
        url = f"postgresql://core_app:s3cr3t@db.internal:5432/core?sslmode={mode}"
        assert "sslmode" in problems({**PRODUCTION, "CORE_DATABASE_URL": url})

    def test_postgres_without_sslmode_is_rejected_because_the_default_is_not_enforcing(self) -> None:
        url = "postgresql://core_app:s3cr3t@db.internal:5432/core"
        assert "sslmode" in problems({**PRODUCTION, "CORE_DATABASE_URL": url})

    def test_the_application_must_not_connect_as_a_superuser(self) -> None:
        url = "postgresql://postgres:s3cr3t@db.internal:5432/core?sslmode=require"
        assert "least-privilege" in problems({**PRODUCTION, "CORE_DATABASE_URL": url})

    def test_a_password_is_required(self) -> None:
        url = "postgresql://core_app@db.internal:5432/core?sslmode=require"
        assert "password" in problems({**PRODUCTION, "CORE_DATABASE_URL": url})

    def test_postgis_is_required_in_production(self) -> None:
        assert "POSTGIS" in problems({**PRODUCTION, "CORE_POSTGIS_REQUIRED": "false"})
        assert Settings.from_env(PRODUCTION).database.require_postgis

    def test_postgis_is_optional_in_development(self) -> None:
        assert not Settings.from_env(DEV).database.require_postgis

    def test_timescale_is_required(self) -> None:
        assert "TIMESCALE" in problems({**PRODUCTION, "CORE_TIMESCALE_REQUIRED": "false"})

    def test_redis_is_required_and_must_use_tls_and_authentication(self) -> None:
        without = {k: v for k, v in PRODUCTION.items() if k != "CORE_REDIS_URL"}
        assert "REDIS" in problems(without)
        assert "rediss://" in problems({**PRODUCTION, "CORE_REDIS_URL": "redis://:pw@redis.internal:6379/0"})
        assert "authentication" in problems({**PRODUCTION, "CORE_REDIS_URL": "rediss://redis.internal:6379/0"})

    def test_sentinel_needs_a_ca_certificate(self) -> None:
        env = {k: v for k, v in PRODUCTION.items() if k != "CORE_REDIS_URL"}
        env.update({"CORE_REDIS_SENTINELS": "s1:26379,s2:26379", "CORE_REDIS_PASSWORD": "pw" * 8})
        assert "CORE_REDIS_TLS_CA" in problems(env)
        assert Settings.from_env({**env, "CORE_REDIS_TLS_CA": "/etc/ssl/ca.pem"}).redis.enabled


class TestSecretsFromFiles:
    def test_a_secret_can_come_from_a_mounted_file(self, tmp_path: Path) -> None:
        secret = tmp_path / "jwt"
        secret.write_text("f" * 48 + "\n", encoding="utf-8")
        settings = Settings.from_env({**DEV, "CORE_JWT_SECRET_FILE": str(secret)})
        assert settings.security.jwt_secret == "f" * 48  # trailing newline stripped

    def test_database_url_and_redis_password_can_come_from_files(self, tmp_path: Path) -> None:
        url, password = tmp_path / "url", tmp_path / "redis"
        url.write_text("postgresql://a:b@h/db\n", encoding="utf-8")
        password.write_text("pw\n", encoding="utf-8")
        settings = Settings.from_env(
            {"CORE_ENV": "development", "CORE_DATABASE_URL_FILE": str(url), "CORE_REDIS_PASSWORD_FILE": str(password)}
        )
        assert settings.database.url == "postgresql://a:b@h/db" and settings.redis.password == "pw"


class TestRealtimeSettings:
    def test_the_stream_interval_follows_v7(self) -> None:
        assert Settings.from_env(DEV).realtime.tick_seconds == 3.0
        assert "CORE_WS_TICK_SECONDS" in problems({**DEV, "CORE_WS_TICK_SECONDS": "6"})
        assert "CORE_WS_TICK_SECONDS" in problems({**DEV, "CORE_WS_TICK_SECONDS": "0"})
        assert "at least 1 in production" in problems({**PRODUCTION, "CORE_WS_TICK_SECONDS": "0.5"})

    def test_limits_are_validated(self) -> None:
        assert "CORE_WS_MAX_ROUTES" in problems({**DEV, "CORE_WS_MAX_ROUTES": "0"})
        assert Settings.from_env({**DEV, "CORE_WS_MAX_CONNECTIONS": "10"}).realtime.max_connections == 10
