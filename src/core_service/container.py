"""Composition root: builds every service once and wires them together.

Nothing else in the code base instantiates services. Each service receives its collaborators
through its constructor (dependency injection), so tests can replace any of them - the clock,
the push gateway, the language model, the team notifier - without patching. Communication
between services is asynchronous in spirit: they publish *events* on the :class:`EventBus`
(``OccupancyUpdated``, ``BusWentSilent``) and the interested services subscribe here.

.. code-block:: text

    OccupancyService --OccupancyUpdated--> AlertService, FindingsService
    StaleBusMonitor  --BusWentSilent-----> AlertService, FindingsService
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import redis

from core_service import __version__
from core_service.ai import (
    AIAnalysisService,
    AnthropicMessagesClient,
    CircuitBreaker,
    ContextBuilder,
    LLMClient,
    OpenAICompatibleClient,
    PrivacyGuard,
    RuleBasedAnalyzer,
    build_default_tools,
)
from core_service.ai.features import FleetFeatureService
from core_service.alerts import (
    AlertDispatcher,
    AlertIndex,
    AlertService,
    AlertThrottle,
    ConsecutiveHitCounter,
    InAppChannel,
    LimiterThrottle,
    LoggingPushGateway,
    ProximityEngine,
    PushChannel,
    PushGateway,
    RedisIndexReplicator,
    RetryPolicy,
    ScreenAlertBuilder,
    ScreenInbox,
    Throttle,
)
from core_service.audit import AuditMetricsCalculator, AuditService, TrainingDatasetExporter, WeeklyAuditReport
from core_service.auth import AuthService, LoginAttemptTracker, PasswordHasher, PasswordPolicy, TokenService
from core_service.clock import Clock, SystemClock
from core_service.comparator import NextBusService
from core_service.config import AISettings, Settings
from core_service.errors import ValidationError
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.notifications import TeamNotifier, build_team_notifier
from core_service.occupancy import CountFusion, OccupancyCalculator, ReadingHistoryStore
from core_service.persistence.documents import (
    AppendOnlyDocumentStore,
    DocumentStore,
    InMemoryDocumentStore,
    RedisDocumentStore,
    RedisOptions,
    build_redis_client,
    parse_sentinels,
)
from core_service.persistence.relational import (
    AlertRepository,
    AuditRepository,
    CatalogRepository,
    Database,
    DeviceRepository,
    FindingRepository,
    Migrator,
    SpatialManager,
    SpatialStatus,
    TelemetryRepository,
    TimescaleManager,
    TimescaleStatus,
    UserRepository,
)
from core_service.realtime.hub import RealtimeHub
from core_service.routing import DirectionDetector, RouteCatalog
from core_service.security import (
    RateLimiter,
    RedisSlidingWindowRateLimiter,
    ResilientRateLimiter,
    SlidingWindowRateLimiter,
)
from core_service.services.catalog_service import CatalogService
from core_service.services.demo_seed import load_demo_data
from core_service.services.event_bus import EventBus
from core_service.services.events import BusWentSilent, OccupancyUpdated
from core_service.services.findings_service import FindingsService
from core_service.services.health_service import HealthService
from core_service.services.jobs import JobRunner
from core_service.services.keyed_locks import KeyedLocks, LockManager, RedisLockManager
from core_service.services.live_state_store import LiveStateStore
from core_service.services.occupancy_service import OccupancyService
from core_service.services.stale_bus_monitor import StaleBusMonitor

log = logging.getLogger(__name__)


def build_llm_client(ai: AISettings) -> LLMClient:
    """The model provider chosen by ``CORE_AI_PROVIDER``: an open-weights model server, or Anthropic."""
    if ai.provider == "openai_compat":
        return OpenAICompatibleClient(ai.base_url, ai.model, ai.api_key, ai.timeout_seconds)
    return AnthropicMessagesClient(ai.api_key, ai.model, ai.base_url, ai.timeout_seconds)


def _default_push_gateway(settings: Settings) -> PushGateway:
    path = settings.notifications.firebase_credentials_path
    if path:
        from core_service.alerts.firebase_gateway import FirebasePushGateway

        return FirebasePushGateway(path)
    return LoggingPushGateway()


@dataclass
class ApplicationContext:
    """Every long-lived object of the process."""

    settings: Settings
    clock: Clock
    metrics: MetricsRegistry
    messages: MessageCatalog
    database: Database
    store: DocumentStore
    route_catalog: RouteCatalog
    catalog_repository: CatalogRepository
    catalog_service: CatalogService
    live_states: LiveStateStore
    histories: ReadingHistoryStore
    telemetry: TelemetryRepository
    events: EventBus
    occupancy: OccupancyService
    next_buses: NextBusService
    inbox: ScreenInbox
    alerts: AlertService
    findings: FindingsService
    findings_repository: FindingRepository
    auth: AuthService
    tokens: TokenService
    audits: AuditService
    dataset_exporter: TrainingDatasetExporter
    weekly_report: WeeklyAuditReport
    ai: AIAnalysisService
    stale_monitor: StaleBusMonitor
    jobs: JobRunner
    health: HealthService
    rate_limiter: RateLimiter
    devices: DeviceRepository
    users: UserRepository
    timescale: TimescaleStatus
    spatial: SpatialStatus
    index_replicator: RedisIndexReplicator | None = None
    redis_client: redis.Redis | None = None
    features: FleetFeatureService | None = None
    realtime: RealtimeHub | None = None

    # ------------------------------------------------------------------ factory
    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        push_gateway: PushGateway | None = None,
        llm_client: LLMClient | None = None,
        team_notifier: TeamNotifier | None = None,
        redis_client: redis.Redis | None = None,
        sleep: Callable[[float], None] = time.sleep,
        seed_demo: bool = False,
    ) -> ApplicationContext:
        settings.validate()
        clock = clock or SystemClock()
        metrics = MetricsRegistry()
        messages = MessageCatalog()

        # --- persistence: PostgreSQL (+ TimescaleDB) and Redis
        db_settings = settings.database
        database = Database(
            db_settings.url,
            schema=db_settings.schema or None,
            min_size=db_settings.pool_min,
            max_size=db_settings.pool_max,
            statement_timeout_ms=db_settings.statement_timeout_ms,
            server_side_prepare=db_settings.prepare_statements,
            startup_options=db_settings.startup_options,
        )
        # Schema changes need DDL rights. With a separate migration role the application never has them.
        owner = (
            Database(
                db_settings.migration_url,
                schema=db_settings.schema or None,
                min_size=1,
                max_size=1,
                statement_timeout_ms=max(db_settings.statement_timeout_ms, 120_000),
                lock_timeout_ms=60_000,
            )
            if db_settings.migration_url
            else database
        )
        try:
            if db_settings.migrate_on_start:
                Migrator(owner, clock).migrate()
                timescale = TimescaleManager(owner).configure(
                    db_settings.telemetry_retention_days, db_settings.telemetry_compress_after_days
                )
                spatial = SpatialManager(owner).configure()
            else:  # serverless: the schema was applied by the deploy step; only look at what is active
                timescale = TimescaleStatus(
                    TimescaleManager(database).is_hypertable(),
                    db_settings.telemetry_retention_days,
                    db_settings.telemetry_compress_after_days,
                )
                spatial = SpatialStatus(SpatialManager(database).extension_installed())
        finally:
            if owner is not database:
                owner.close()
        if db_settings.require_timescale and not timescale.enabled:
            database.close()
            raise ValidationError("invalid_configuration", details=["TimescaleDB is required but not active"])
        if db_settings.require_postgis and not spatial.enabled:
            database.close()
            raise ValidationError("invalid_configuration", details=["PostGIS is required but not active"])
        shared_client = redis_client or _build_redis(settings)
        store: DocumentStore
        if shared_client is not None:
            store = RedisDocumentStore(shared_client, settings.redis.key_prefix)
        elif settings.document_store_path:
            store = AppendOnlyDocumentStore(settings.document_store_path, clock)
        else:
            store = InMemoryDocumentStore(clock)
        shared = shared_client is not None  # several replicas may run: use cluster-wide state everywhere
        locks: LockManager = (
            RedisLockManager(shared_client, settings.redis.key_prefix) if shared_client is not None else KeyedLocks()
        )
        users, devices = UserRepository(database), DeviceRepository(database)
        catalog_repository = CatalogRepository(database)
        alert_repository, audit_repository = AlertRepository(database), AuditRepository(database)
        findings_repository, telemetry = FindingRepository(database), TelemetryRepository(database)
        if seed_demo:
            load_demo_data(catalog_repository)

        # --- catalog and live data
        route_catalog = RouteCatalog()
        catalog_service = CatalogService(catalog_repository, route_catalog, metrics)
        catalog_service.reload()
        occ = settings.occupancy
        live_states = LiveStateStore(store, clock, ttl_seconds=occ.live_state_ttl_seconds)
        histories = ReadingHistoryStore(occ.history_size, shared=store if shared else None)
        events = EventBus(metrics)
        occupancy = OccupancyService(
            route_catalog,
            histories,
            live_states,
            telemetry,
            events,
            OccupancyCalculator(),
            CountFusion(
                occ.door_gap_threshold_people,
                occ.door_gap_window_seconds,
                occ.min_confidence,
                occ.door_gap_max_reading_gap_seconds,
            ),
            DirectionDetector(occ.direction_window, occ.direction_min_progress_delta),
            clock,
            occ,
            metrics,
            locks,
        )
        next_buses = NextBusService(route_catalog, live_states)

        # --- notifications and alerts
        notifier = team_notifier or build_team_notifier(settings.notifications)
        inbox = ScreenInbox(store, max_size=settings.alerts.inbox_size, locks=locks)
        dispatcher = AlertDispatcher(
            [PushChannel(push_gateway or _default_push_gateway(settings)), InAppChannel(inbox)],
            store,
            RetryPolicy(max_attempts=3),
            metrics,
            sleep=sleep,
        )
        alert_settings = settings.alerts
        alert_index = AlertIndex()
        replicator: RedisIndexReplicator | None = (
            RedisIndexReplicator(shared_client, alert_index, settings.redis.key_prefix, metrics)
            if shared_client is not None
            else None
        )
        throttle: Throttle = AlertThrottle(
            alert_settings.throttle_max_alerts, alert_settings.throttle_window_seconds, clock
        )
        if shared_client is not None:
            throttle = LimiterThrottle(
                ResilientRateLimiter(
                    RedisSlidingWindowRateLimiter(
                        shared_client,
                        alert_settings.throttle_max_alerts,
                        alert_settings.throttle_window_seconds,
                        clock,
                        f"{settings.redis.key_prefix}:throttle",
                    ),
                    SlidingWindowRateLimiter(
                        alert_settings.throttle_max_alerts, alert_settings.throttle_window_seconds, clock
                    ),
                )
            )
        alerts = AlertService(
            alerts=alert_repository,
            devices=devices,
            catalog=route_catalog,
            index=alert_index,
            engine=ProximityEngine(alert_settings.distance_threshold_m),
            hits=ConsecutiveHitCounter(clock, alert_settings.hit_window_seconds, store if shared else None),
            builder=ScreenAlertBuilder(messages, clock),
            dispatcher=dispatcher,
            throttle=throttle,
            next_buses=next_buses,
            clock=clock,
            settings=alert_settings,
            metrics=metrics,
            index_sync=replicator,
        )
        alerts.rebuild_index()
        if replicator is not None:
            replicator.set_resync(alerts.rebuild_index)
        findings = FindingsService(findings_repository, store, notifier, messages, clock, occ, metrics)
        features = FleetFeatureService(store, route_catalog, live_states, clock, occ)
        stale_monitor = StaleBusMonitor(live_states, events, clock, occ, metrics)
        events.subscribe(OccupancyUpdated, alerts.on_occupancy_updated)
        events.subscribe(OccupancyUpdated, findings.on_occupancy_updated)
        events.subscribe(OccupancyUpdated, features.on_occupancy_updated)
        events.subscribe(BusWentSilent, alerts.on_bus_silent)
        events.subscribe(BusWentSilent, findings.on_bus_silent)

        # --- accounts
        sec = settings.security
        tokens = TokenService(sec, clock)
        auth = AuthService(
            users,
            devices,
            PasswordHasher(sec.pbkdf2_iterations),
            PasswordPolicy(),
            tokens,
            LoginAttemptTracker(
                clock, sec.max_login_attempts, sec.login_lock_seconds, store if shared else None, locks
            ),
            store,
            clock,
            metrics,
        )

        # --- audits
        audits = AuditService(
            audit_repository,
            telemetry,
            histories,
            route_catalog,
            store,
            AuditMetricsCalculator(5.0),
            clock,
            occ,
            metrics,
            utc_offset_hours=settings.local_utc_offset_hours,
        )
        weekly_report = WeeklyAuditReport(audits, notifier, store, messages, clock, 5.0)

        # --- AI (optional): the same interface serves a real provider or a scripted fake
        client = llm_client
        if client is None and settings.ai.enabled:
            client = build_llm_client(settings.ai)
        ai_settings = settings.ai
        ai = AIAnalysisService(
            client,
            build_default_tools(live_states, telemetry, findings_repository, audits, features),
            CircuitBreaker(
                clock, ai_settings.breaker_failures, ai_settings.breaker_window, ai_settings.breaker_recovery_seconds
            ),
            ContextBuilder(
                live_states,
                route_catalog,
                telemetry,
                clock,
                PrivacyGuard(),
                ai_settings.context_max_buses,
                features,
            ),
            RuleBasedAnalyzer(occ.min_confidence, occ.no_data_after_seconds),
            findings,
            store,
            clock,
            metrics,
            ai_settings,
        )

        # --- background jobs
        jobs = JobRunner(clock, metrics, coordinator=store if shared else None, node_id=uuid.uuid4().hex)
        jobs.add("stale_buses", 15, stale_monitor.check)
        jobs.add("alert_expiry", 60, alerts.expire_due)
        jobs.add("document_purge", 300, store.purge_expired)
        retention_seconds = db_settings.telemetry_retention_days * 86_400.0
        # With TimescaleDB the retention policy drops whole chunks; this job is the safety net for plain PostgreSQL.
        jobs.add("telemetry_retention", 3600, lambda: telemetry.prune_older_than(clock.now() - retention_seconds))
        if replicator is not None:
            jobs.add("alert_index_reconcile", 30, alerts.rebuild_index)
        jobs.add("findings_digest", 24 * 3600, findings.send_daily_digest)
        jobs.add("weekly_audit_report", 3600, weekly_report.run_if_due)

        def ai_status() -> dict[str, Any]:
            return {"enabled": ai.available, "circuit": ai.circuit_state}

        def data_details() -> dict[str, Any]:
            return {"telemetry_store": timescale.mode, "spatial": spatial.mode}

        health = HealthService(database, store, route_catalog, clock, ai_status, __version__, data_details)
        limiter: RateLimiter = SlidingWindowRateLimiter(sec.rate_limit_per_minute, 60.0, clock)
        if shared_client is not None:
            limiter = ResilientRateLimiter(
                RedisSlidingWindowRateLimiter(
                    shared_client, sec.rate_limit_per_minute, 60.0, clock, f"{settings.redis.key_prefix}:api"
                ),
                limiter,
            )
        return cls(
            settings,
            clock,
            metrics,
            messages,
            database,
            store,
            route_catalog,
            catalog_repository,
            catalog_service,
            live_states,
            histories,
            telemetry,
            events,
            occupancy,
            next_buses,
            inbox,
            alerts,
            findings,
            findings_repository,
            auth,
            tokens,
            audits,
            TrainingDatasetExporter(store),
            weekly_report,
            ai,
            stale_monitor,
            jobs,
            health,
            limiter,
            devices,
            users,
            timescale,
            spatial,
            replicator,
            shared_client,
            features,
            RealtimeHub(live_states, route_catalog, clock, settings.realtime, occ, metrics, features),
        )

    def start_background(self) -> None:
        """Start the periodic jobs and, with several replicas, the alert index subscription."""
        if self.index_replicator is not None:
            self.index_replicator.start()
        self.jobs.start()

    def stop_background(self) -> None:
        self.jobs.stop()
        if self.index_replicator is not None:
            self.index_replicator.stop()

    def close(self) -> None:
        """Stop background work and release connections (safe to call once at shutdown)."""
        self.stop_background()
        closer = getattr(self.store, "close", None)
        if callable(closer):
            closer()
        self.database.close()


def _build_redis(settings: Settings) -> redis.Redis | None:
    red = settings.redis
    if not red.enabled:
        return None
    return build_redis_client(
        RedisOptions(
            url=red.url,
            sentinels=parse_sentinels(red.sentinels),
            sentinel_master=red.sentinel_master,
            sentinel_password=red.sentinel_password,
            password=red.password,
            username=red.username,
            tls_ca_path=red.tls_ca_path,
            max_connections=red.max_connections,
        )
    )
