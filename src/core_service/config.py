"""Application settings, loaded from environment variables and validated on start-up.

Secrets never have a usable default in production: the service refuses to start.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core_service.errors import ValidationError

MIN_SECRET_LENGTH = 32


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _secret(source: Mapping[str, str], name: str, default: str = "") -> str:
    """Read a secret from ``NAME`` or, when ``NAME_FILE`` is set, from that file (Docker/Kubernetes secrets).

    Keeping secrets in mounted files instead of environment variables stops them from leaking through
    process listings, crash dumps or ``docker inspect``."""
    file_path = source.get(f"{name}_FILE", "").strip()
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    return source.get(name, default)


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class SecuritySettings:
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "capacidad-bus"
    access_token_ttl: float = 15 * 60
    refresh_token_ttl: float = 30 * 24 * 3600
    pbkdf2_iterations: int = 600_000
    max_login_attempts: int = 3
    login_lock_seconds: float = 5 * 60
    ingest_key: str = ""
    #: Shared secret of the scheduler that calls ``/api/v1/internal/jobs/run`` (Vercel Cron or a worker container).
    cron_secret: str = ""
    cors_origins: tuple[str, ...] = ()
    trust_proxy: bool = False
    rate_limit_per_minute: int = 120
    max_body_bytes: int = 1_048_576


@dataclass(frozen=True)
class OccupancySettings:
    door_gap_threshold_people: int = 2
    door_gap_window_seconds: float = 30.0
    door_gap_max_reading_gap_seconds: float = 20.0
    min_confidence: float = 0.6
    no_data_after_seconds: float = 60.0
    off_route_tolerance_m: float = 80.0
    terminal_radius_m: float = 40.0
    terminal_max_speed_kmh: float = 5.0
    direction_window: int = 4
    direction_min_progress_delta: float = 0.002
    #: A previous position older than this is not used to disambiguate the projection (map matching).
    continuity_max_gap_seconds: float = 120.0
    live_state_ttl_seconds: float = 600.0
    history_size: int = 120
    max_future_skew_seconds: float = 60.0
    max_reading_age_seconds: float = 48 * 3600.0


@dataclass(frozen=True)
class AlertSettings:
    distance_threshold_m: float = 600.0
    consecutive_hits: int = 2
    hit_window_seconds: float = 20.0
    expiration_seconds: float = 60 * 60.0
    max_stops_ahead: int = 5
    max_active_per_device: int = 5
    inbox_size: int = 50
    throttle_max_alerts: int = 3
    throttle_window_seconds: float = 60.0
    warning_dedupe_seconds: float = 10 * 60.0


@dataclass(frozen=True)
class AISettings:
    """``provider`` = ``openai_compat`` (open-weights model on a vLLM/Ollama server: the default of the open stack)
    or ``anthropic`` (optional hosted provider)."""

    enabled: bool = False
    provider: str = "anthropic"
    api_key: str = ""
    model: str = "claude-opus-5-5"
    base_url: str = "https://api.anthropic.com"
    timeout_seconds: float = 30.0
    max_tokens: int = 1500
    max_tool_rounds: int = 4
    context_max_buses: int = 60
    breaker_failures: int = 5
    breaker_window: int = 10
    breaker_recovery_seconds: float = 60.0


@dataclass(frozen=True)
class RealtimeSettings:
    """WebSocket stream of the map (``WS /ws/v1/buses/stream``, PB-02 / PB-N03)."""

    #: Seconds between two frames of a route. v7 asks for an update every 3 to 5 seconds.
    tick_seconds: float = 3.0
    max_connections: int = 5_000
    max_routes_per_connection: int = 5
    #: Time a client has to authenticate when it did not send an ``Authorization`` header.
    auth_timeout_seconds: float = 5.0
    #: Frames waiting for a slow client before it is switched to a fresh snapshot (back-pressure).
    outbox_size: int = 16
    #: An application-level heartbeat is sent after this long without any frame (mobile NATs, proxies).
    heartbeat_seconds: float = 20.0
    max_message_bytes: int = 2_048
    client_messages_per_minute: int = 60


@dataclass(frozen=True)
class NotificationSettings:
    team_emails: tuple[str, ...] = ()
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sender: str = "no-responder@capacidadbus.co"
    firebase_credentials_path: str = ""


@dataclass(frozen=True)
class DatabaseSettings:
    """PostgreSQL (relational data) with the TimescaleDB extension (telemetry time series)."""

    url: str = ""
    #: Optional second URL with a role that may run DDL (owner of the tables). When set, migrations and the
    #: TimescaleDB policy use it and the application itself connects with ``url`` (DML only, least privilege).
    migration_url: str = ""
    schema: str = ""
    pool_min: int = 2
    pool_max: int = 10
    statement_timeout_ms: int = 10_000
    #: Apply the schema when the process starts. Serverless deployments turn it off and run ``python -m core_service
    #: migrate`` from CI instead: a cold start must not hold DDL locks or take seconds.
    migrate_on_start: bool = True
    #: Server-side prepared statements. Off behind a transaction pooler (PgBouncer, Neon pooled endpoint).
    prepare_statements: bool = True
    #: Send the timeouts as connection start-up options. Poolers reject them: set the timeouts on the role instead.
    startup_options: bool = True
    require_timescale: bool = False
    require_postgis: bool = False
    telemetry_retention_days: int = 30
    telemetry_compress_after_days: int = 7


@dataclass(frozen=True)
class RedisSettings:
    """Redis: live state, de-duplication, sessions, locks, rate limits and cross-replica messages."""

    url: str = ""
    sentinels: str = ""
    sentinel_master: str = "mymaster"
    username: str = ""
    password: str = ""
    sentinel_password: str = ""
    tls_ca_path: str = ""
    key_prefix: str = "core"
    max_connections: int = 50

    @property
    def enabled(self) -> bool:
        return bool(self.url or self.sentinels)


_WEAK_SSL_MODES = {"disable", "allow", "prefer"}
_SUPERUSER_NAMES = {"postgres", "root", "admin", "timescaledb"}


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    #: Development-only fallback when Redis is not configured: an append-only file (or memory).
    document_store_path: str | None = None
    local_utc_offset_hours: int = -5
    security: SecuritySettings = field(default_factory=lambda: SecuritySettings(jwt_secret=secrets.token_urlsafe(48)))
    occupancy: OccupancySettings = field(default_factory=OccupancySettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    ai: AISettings = field(default_factory=AISettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    realtime: RealtimeSettings = field(default_factory=RealtimeSettings)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def for_testing(cls, **overrides: object) -> Settings:
        """Fast, deterministic settings for automated tests."""
        base = cls(
            environment="test",
            security=SecuritySettings(
                jwt_secret="test-secret-test-secret-test-secret-1234",  # noqa: S106 - fixed value, tests only
                pbkdf2_iterations=1_000,
                ingest_key="test-ingest-key-0123456789",
                rate_limit_per_minute=10_000,
            ),
        )
        return replace(base, **overrides)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        environment = source.get("CORE_ENV", "development").strip().lower()
        secret = _secret(source, "CORE_JWT_SECRET")
        if not secret and environment != "production":
            secret = secrets.token_urlsafe(48)
        security = SecuritySettings(
            jwt_secret=secret,
            jwt_algorithm=source.get("CORE_JWT_ALGORITHM", "HS256"),
            pbkdf2_iterations=int(source.get("CORE_PBKDF2_ITERATIONS", "600000")),
            ingest_key=_secret(source, "CORE_INGEST_KEY"),
            cron_secret=_secret(source, "CORE_CRON_SECRET") or source.get("CRON_SECRET", ""),
            cors_origins=_csv(source.get("CORE_CORS_ORIGINS")),
            trust_proxy=_bool(source.get("CORE_TRUST_PROXY"), False),
            rate_limit_per_minute=int(source.get("CORE_RATE_LIMIT_PER_MINUTE", "120")),
        )
        ai = AISettings(
            enabled=_bool(source.get("CORE_AI_ENABLED"), False),
            provider=source.get("CORE_AI_PROVIDER", AISettings.provider).strip().lower(),
            api_key=_secret(source, "CORE_AI_API_KEY"),
            model=source.get("CORE_AI_MODEL", AISettings.model),
            base_url=source.get("CORE_AI_BASE_URL", AISettings.base_url),
            timeout_seconds=float(source.get("CORE_AI_TIMEOUT_SECONDS", str(AISettings.timeout_seconds))),
        )
        notifications = NotificationSettings(
            team_emails=_csv(source.get("CORE_TEAM_EMAILS")),
            smtp_host=source.get("CORE_SMTP_HOST", ""),
            smtp_port=int(source.get("CORE_SMTP_PORT", "587")),
            smtp_user=source.get("CORE_SMTP_USER", ""),
            smtp_password=_secret(source, "CORE_SMTP_PASSWORD"),
            sender=source.get("CORE_SMTP_SENDER", NotificationSettings.sender),
            firebase_credentials_path=source.get("CORE_FIREBASE_CREDENTIALS", ""),
        )
        database = DatabaseSettings(
            url=_secret(source, "CORE_DATABASE_URL"),
            migration_url=_secret(source, "CORE_DATABASE_MIGRATION_URL"),
            schema=source.get("CORE_DATABASE_SCHEMA", ""),
            pool_min=int(source.get("CORE_DB_POOL_MIN", "2")),
            pool_max=int(source.get("CORE_DB_POOL_MAX", "10")),
            statement_timeout_ms=int(source.get("CORE_DB_STATEMENT_TIMEOUT_MS", "10000")),
            migrate_on_start=_bool(source.get("CORE_DB_MIGRATE_ON_START"), True),
            prepare_statements=_bool(source.get("CORE_DB_PREPARE"), True),
            startup_options=_bool(source.get("CORE_DB_STARTUP_OPTIONS"), True),
            require_timescale=_bool(source.get("CORE_TIMESCALE_REQUIRED"), environment == "production"),
            require_postgis=_bool(source.get("CORE_POSTGIS_REQUIRED"), environment == "production"),
            telemetry_retention_days=int(source.get("CORE_TELEMETRY_RETENTION_DAYS", "30")),
            telemetry_compress_after_days=int(source.get("CORE_TELEMETRY_COMPRESS_AFTER_DAYS", "7")),
        )
        redis_settings = RedisSettings(
            url=_secret(source, "CORE_REDIS_URL"),
            sentinels=source.get("CORE_REDIS_SENTINELS", ""),
            sentinel_master=source.get("CORE_REDIS_SENTINEL_MASTER", "mymaster"),
            username=source.get("CORE_REDIS_USERNAME", ""),
            password=_secret(source, "CORE_REDIS_PASSWORD"),
            sentinel_password=_secret(source, "CORE_REDIS_SENTINEL_PASSWORD"),
            tls_ca_path=source.get("CORE_REDIS_TLS_CA", ""),
            key_prefix=source.get("CORE_REDIS_PREFIX", "core"),
            max_connections=int(source.get("CORE_REDIS_MAX_CONNECTIONS", "50")),
        )
        realtime = RealtimeSettings(
            tick_seconds=float(source.get("CORE_WS_TICK_SECONDS", "3")),
            max_connections=int(source.get("CORE_WS_MAX_CONNECTIONS", "5000")),
            max_routes_per_connection=int(source.get("CORE_WS_MAX_ROUTES", "5")),
            outbox_size=int(source.get("CORE_WS_OUTBOX_SIZE", "16")),
        )
        settings = cls(
            environment=environment,
            database=database,
            redis=redis_settings,
            document_store_path=source.get("CORE_DOC_STORE_PATH") or None,
            security=security,
            ai=ai,
            notifications=notifications,
            realtime=realtime,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Fail fast on unsafe or inconsistent configuration."""
        problems: list[str] = []
        if len(self.security.jwt_secret) < MIN_SECRET_LENGTH:
            problems.append(f"CORE_JWT_SECRET must have at least {MIN_SECRET_LENGTH} characters")
        if self.security.pbkdf2_iterations < 1_000:
            problems.append("CORE_PBKDF2_ITERATIONS is too low")
        if self.is_production:
            if self.security.pbkdf2_iterations < 200_000:
                problems.append("CORE_PBKDF2_ITERATIONS must be at least 200000 in production")
            if len(self.security.ingest_key) < MIN_SECRET_LENGTH:
                problems.append(f"CORE_INGEST_KEY must have at least {MIN_SECRET_LENGTH} characters in production")
            problems.extend(self._production_data_problems())
            if "*" in self.security.cors_origins:
                problems.append("CORE_CORS_ORIGINS must not contain '*' in production")
        problems.extend(self._data_problems())
        problems.extend(self._realtime_problems())
        problems.extend(self._ai_problems())
        if problems:
            raise ValidationError("invalid_configuration", details=problems)

    def _ai_problems(self) -> list[str]:
        ai = self.ai
        if not ai.enabled:
            return []
        if ai.provider not in ("anthropic", "openai_compat"):
            return ["CORE_AI_PROVIDER must be 'openai_compat' (open model server) or 'anthropic'"]
        if ai.provider == "anthropic" and not ai.api_key:
            return ["CORE_AI_API_KEY is required when the AI module uses the Anthropic provider"]
        if ai.provider == "openai_compat":
            if ai.base_url == AISettings.base_url:
                return ["CORE_AI_BASE_URL must point to the model server (e.g. http://llm:8000/v1)"]
            if not ai.model or ai.model == AISettings.model:
                return ["CORE_AI_MODEL must name the model served by the open model server"]
        return []

    def _realtime_problems(self) -> list[str]:
        rt = self.realtime
        problems: list[str] = []
        if not 0 < rt.tick_seconds <= 5:
            problems.append("CORE_WS_TICK_SECONDS must be greater than 0 and at most 5 (v7: updates every 3-5 s)")
        if self.is_production and rt.tick_seconds < 1:
            problems.append("CORE_WS_TICK_SECONDS must be at least 1 in production (battery and data, PB-N02)")
        if rt.max_connections < 1 or not 1 <= rt.max_routes_per_connection <= 20 or rt.outbox_size < 2:
            problems.append("CORE_WS_MAX_CONNECTIONS, CORE_WS_MAX_ROUTES or CORE_WS_OUTBOX_SIZE are out of range")
        return problems

    def _data_problems(self) -> list[str]:
        """Rules that hold in every environment."""
        db = self.database
        problems: list[str] = []
        if not db.url:
            problems.append("CORE_DATABASE_URL is required (postgresql://user:password@host:5432/database)")
        elif urlparse(db.url).scheme not in {"postgresql", "postgres"}:
            problems.append("CORE_DATABASE_URL must be a PostgreSQL URL (SQLite is not supported)")
        if db.migration_url and urlparse(db.migration_url).scheme not in {"postgresql", "postgres"}:
            problems.append("CORE_DATABASE_MIGRATION_URL must be a PostgreSQL URL")
        if not 1 <= db.pool_min <= db.pool_max:
            problems.append("CORE_DB_POOL_MIN must be at least 1 and not greater than CORE_DB_POOL_MAX")
        if db.statement_timeout_ms < 100:
            problems.append("CORE_DB_STATEMENT_TIMEOUT_MS is too low")
        if not 1 <= db.telemetry_compress_after_days < db.telemetry_retention_days:
            problems.append("need 1 <= CORE_TELEMETRY_COMPRESS_AFTER_DAYS < CORE_TELEMETRY_RETENTION_DAYS")
        if self.redis.url and self.redis.sentinels:
            problems.append("set CORE_REDIS_URL or CORE_REDIS_SENTINELS, not both")
        return problems

    def _production_data_problems(self) -> list[str]:
        """Encryption in transit, least privilege and high availability requirements."""
        db, red = self.database, self.redis
        problems: list[str] = []
        for name, url in (("CORE_DATABASE_URL", db.url), ("CORE_DATABASE_MIGRATION_URL", db.migration_url)):
            if not url:
                continue
            parsed = urlparse(url)
            ssl_mode = parse_qs(parsed.query).get("sslmode", ["prefer"])[0]
            if ssl_mode in _WEAK_SSL_MODES:
                problems.append(f"{name} must use sslmode=require, verify-ca or verify-full in production")
            if (parsed.username or "").lower() in _SUPERUSER_NAMES:
                problems.append(f"{name} must use a least-privilege role, not a superuser")
            if not parsed.password:
                problems.append(f"{name} must include a password (or use certificate authentication)")
        if db.migration_url and db.migration_url == db.url:
            problems.append("CORE_DATABASE_MIGRATION_URL must differ from CORE_DATABASE_URL (separate roles)")
        if not db.require_timescale:
            problems.append("CORE_TIMESCALE_REQUIRED must be true in production (telemetry needs the hypertable)")
        if not db.require_postgis:
            problems.append("CORE_POSTGIS_REQUIRED must be true in production (routes and stops need geometry)")
        if not red.enabled:
            problems.append("CORE_REDIS_URL or CORE_REDIS_SENTINELS is required in production")
        else:
            if red.url and not red.url.startswith("rediss://") and not red.sentinels:
                problems.append("CORE_REDIS_URL must use rediss:// (TLS) in production")
            if not red.password and "@" not in red.url:
                problems.append(
                    "Redis needs authentication in production (CORE_REDIS_PASSWORD or credentials in the URL)"
                )
            if red.sentinels and not red.tls_ca_path:
                problems.append("CORE_REDIS_TLS_CA is required with Sentinel in production")
        return problems
