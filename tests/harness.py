"""A small hand-wired assembly of the alert pipeline for service-level tests."""

from __future__ import annotations

from dataclasses import dataclass

from core_service.alerts import (
    AlertDispatcher,
    AlertIndex,
    AlertService,
    AlertThrottle,
    ConsecutiveHitCounter,
    InAppChannel,
    ProximityEngine,
    PushChannel,
    RetryPolicy,
    ScreenAlertBuilder,
    ScreenInbox,
)
from core_service.clock import FakeClock
from core_service.comparator.next_bus_service import NextBusService
from core_service.config import AlertSettings
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.models import Device, GeoPoint, LiveBusState
from core_service.occupancy import FusionReason
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.persistence.relational import (
    AlertRepository,
    CatalogRepository,
    Database,
    DeviceRepository,
    Migrator,
)
from core_service.routing import RouteCatalog
from core_service.services.events import OccupancyUpdated
from core_service.services.live_state_store import LiveStateStore
from factories import demo_buses, demo_data, demo_stops, demo_track
from fakes import RecordingPushGateway
from pg import make_database


@dataclass
class AlertHarness:
    clock: FakeClock
    db: Database
    store: InMemoryDocumentStore
    catalog: RouteCatalog
    live_states: LiveStateStore
    inbox: ScreenInbox
    push: RecordingPushGateway
    devices: DeviceRepository
    alerts: AlertRepository
    index: AlertIndex
    metrics: MetricsRegistry
    service: AlertService
    settings: AlertSettings

    def update(self, state: LiveBusState, previous: LiveBusState | None = None) -> None:
        """Save the state and publish it the way the occupancy service would."""
        self.live_states.save(state)
        self.service.on_occupancy_updated(OccupancyUpdated(state, previous, FusionReason.CAMERAS_OK))


def build_alert_harness(
    *,
    push: RecordingPushGateway | None = None,
    settings: AlertSettings | None = None,
    device_id: str = "device-1",
) -> AlertHarness:
    clock = FakeClock(1_790_000_000.0)
    settings = settings or AlertSettings()
    db = make_database()
    Migrator(db, clock).migrate()
    catalog_repo = CatalogRepository(db)
    for stop in demo_stops().values():
        catalog_repo.save_stop(stop)
    route = demo_data()["routes"][0]
    catalog_repo.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        catalog_repo.save_bus(bus)

    catalog = RouteCatalog()
    catalog.replace_all([demo_track()], demo_buses())
    store = InMemoryDocumentStore(clock)
    live_states = LiveStateStore(store, clock, ttl_seconds=600)
    inbox = ScreenInbox(store, max_size=settings.inbox_size)
    gateway = push or RecordingPushGateway()
    metrics = MetricsRegistry()
    devices = DeviceRepository(db)
    devices.upsert(Device(device_id, "push-token-abcdef", "android"), clock.now())
    alerts = AlertRepository(db)
    index = AlertIndex()
    dispatcher = AlertDispatcher(
        [PushChannel(gateway), InAppChannel(inbox)], store, RetryPolicy(max_attempts=3), metrics, sleep=lambda _s: None
    )
    service = AlertService(
        alerts=alerts,
        devices=devices,
        catalog=catalog,
        index=index,
        engine=ProximityEngine(settings.distance_threshold_m),
        hits=ConsecutiveHitCounter(clock, settings.hit_window_seconds),
        builder=ScreenAlertBuilder(MessageCatalog(), clock),
        dispatcher=dispatcher,
        throttle=AlertThrottle(settings.throttle_max_alerts, settings.throttle_window_seconds, clock),
        next_buses=NextBusService(catalog, live_states),
        clock=clock,
        settings=settings,
        metrics=metrics,
    )
    return AlertHarness(
        clock, db, store, catalog, live_states, inbox, gateway, devices, alerts, index, metrics, service, settings
    )


# ------------------------------------------------------------------ AI harness
from core_service.ai import (  # noqa: E402
    AIAnalysisService,
    CircuitBreaker,
    ContextBuilder,
    PrivacyGuard,
    RuleBasedAnalyzer,
    build_default_tools,
)
from core_service.audit import AuditMetricsCalculator, AuditService  # noqa: E402
from core_service.config import AISettings, OccupancySettings  # noqa: E402
from core_service.occupancy import ReadingHistoryStore  # noqa: E402
from core_service.persistence.relational import AuditRepository, FindingRepository, TelemetryRepository  # noqa: E402
from core_service.services.findings_service import FindingsService  # noqa: E402
from fakes import RecordingNotifier, ScriptedLLMClient  # noqa: E402


@dataclass
class AIHarness:
    clock: FakeClock
    store: InMemoryDocumentStore
    live_states: LiveStateStore
    telemetry: TelemetryRepository
    findings_repo: FindingRepository
    findings: FindingsService
    notifier: RecordingNotifier
    breaker: CircuitBreaker
    llm: ScriptedLLMClient | None
    service: AIAnalysisService
    metrics: MetricsRegistry


def build_ai_harness(
    script: list | None = None, *, enabled: bool = True, settings: AISettings | None = None
) -> AIHarness:
    clock = FakeClock(1_790_000_000.0)
    db = make_database()
    Migrator(db, clock).migrate()
    catalog_repo = CatalogRepository(db)
    for stop in demo_stops().values():
        catalog_repo.save_stop(stop)
    route = demo_data()["routes"][0]
    catalog_repo.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        catalog_repo.save_bus(bus)
    catalog = RouteCatalog()
    catalog.replace_all([demo_track()], demo_buses())
    store = InMemoryDocumentStore(clock)
    live_states = LiveStateStore(store, clock, ttl_seconds=600)
    telemetry = TelemetryRepository(db)
    findings_repo = FindingRepository(db)
    metrics = MetricsRegistry()
    notifier = RecordingNotifier()
    findings = FindingsService(findings_repo, store, notifier, MessageCatalog(), clock, OccupancySettings(), metrics)
    ai_settings = settings or AISettings(enabled=enabled, api_key="test-key")
    audits = AuditService(
        AuditRepository(db),
        telemetry,
        ReadingHistoryStore(),
        catalog,
        store,
        AuditMetricsCalculator(5.0),
        clock,
        OccupancySettings(),
        metrics,
    )
    tools = build_default_tools(live_states, telemetry, findings_repo, audits)
    breaker = CircuitBreaker(clock, failure_threshold=3, window=5, recovery_seconds=60)
    llm = ScriptedLLMClient(script) if script is not None else None
    service = AIAnalysisService(
        llm,
        tools,
        breaker,
        ContextBuilder(live_states, catalog, telemetry, clock, PrivacyGuard(), max_buses=ai_settings.context_max_buses),
        RuleBasedAnalyzer(),
        findings,
        store,
        clock,
        metrics,
        ai_settings,
    )
    return AIHarness(
        clock, store, live_states, telemetry, findings_repo, findings, notifier, breaker, llm, service, metrics
    )


# ------------------------------------------------------------------ occupancy pipeline harness
from core_service.occupancy import CountFusion, OccupancyCalculator  # noqa: E402
from core_service.routing import DirectionDetector  # noqa: E402
from core_service.services.event_bus import EventBus  # noqa: E402
from core_service.services.occupancy_service import OccupancyService  # noqa: E402


@dataclass
class PipelineHarness:
    clock: FakeClock
    db: Database
    store: InMemoryDocumentStore
    catalog: RouteCatalog
    histories: ReadingHistoryStore
    live_states: LiveStateStore
    telemetry: TelemetryRepository
    events: EventBus
    published: list
    metrics: MetricsRegistry
    service: OccupancyService


def build_pipeline_harness() -> PipelineHarness:
    clock = FakeClock(1_790_000_000.0)
    db = make_database()
    Migrator(db, clock).migrate()
    catalog = RouteCatalog()
    catalog.replace_all([demo_track()], demo_buses())
    store = InMemoryDocumentStore(clock)
    settings = OccupancySettings()
    histories = ReadingHistoryStore(settings.history_size)
    live_states = LiveStateStore(store, clock, ttl_seconds=settings.live_state_ttl_seconds)
    telemetry = TelemetryRepository(db)
    metrics = MetricsRegistry()
    events = EventBus(metrics)
    published: list = []
    events.subscribe(OccupancyUpdated, published.append)
    service = OccupancyService(
        catalog,
        histories,
        live_states,
        telemetry,
        events,
        OccupancyCalculator(),
        CountFusion(
            settings.door_gap_threshold_people,
            settings.door_gap_window_seconds,
            settings.min_confidence,
            settings.door_gap_max_reading_gap_seconds,
        ),
        DirectionDetector(settings.direction_window, settings.direction_min_progress_delta),
        clock,
        settings,
        metrics,
    )
    return PipelineHarness(
        clock, db, store, catalog, histories, live_states, telemetry, events, published, metrics, service
    )
