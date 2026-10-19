"""Technical findings and the silent-bus monitor."""

from __future__ import annotations

from core_service.clock import FakeClock
from core_service.config import OccupancySettings
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.models import FindingKind, FindingSeverity, OccupancyState
from core_service.occupancy import FusionReason
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.persistence.relational import FindingRepository, Migrator
from core_service.services.event_bus import EventBus
from core_service.services.events import BusWentSilent, OccupancyUpdated
from core_service.services.findings_service import FindingsService
from core_service.services.live_state_store import LiveStateStore
from core_service.services.stale_bus_monitor import StaleBusMonitor
from factories import make_state
from fakes import RecordingNotifier
from pg import make_database


class Rig:
    def __init__(self, notifier_ok: bool = True) -> None:
        self.clock = FakeClock()
        db = make_database()
        Migrator(db, self.clock).migrate()
        self.repo = FindingRepository(db)
        self.store = InMemoryDocumentStore(self.clock)
        self.notifier = RecordingNotifier(notifier_ok)
        self.metrics = MetricsRegistry()
        self.service = FindingsService(
            self.repo, self.store, self.notifier, MessageCatalog(), self.clock, OccupancySettings(), self.metrics
        )
        self.live = LiveStateStore(self.store, self.clock)
        self.events = EventBus(self.metrics)
        self.monitor = StaleBusMonitor(self.live, self.events, self.clock, OccupancySettings(), self.metrics)


def record(
    rig: Rig,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    bus: str = "1045",
    kind: FindingKind = FindingKind.COUNT_MISMATCH,
):  # noqa: ANN201
    return rig.service.record(bus_id=bus, kind=kind, severity=severity, description="d")


class TestRecording:
    def test_a_finding_is_stored_once_per_bus_and_kind_within_the_dedupe_window(self) -> None:
        rig = Rig()
        assert record(rig) is not None
        assert record(rig) is None
        assert record(rig, bus="1102") is not None
        assert record(rig, kind=FindingKind.LOW_CONFIDENCE) is not None
        rig.clock.advance(3601)
        assert record(rig) is not None
        assert len(rig.repo.list_recent(50)) == 4

    def test_only_high_severity_is_emailed_immediately(self) -> None:
        rig = Rig()
        record(rig, FindingSeverity.MEDIUM)
        assert rig.notifier.messages == []
        finding = record(rig, FindingSeverity.HIGH, bus="1102")
        assert len(rig.notifier.messages) == 1 and rig.notifier.messages[0][0].startswith("[Capacidad buses]")
        assert finding is not None and rig.repo.list_unnotified_since(0) != []  # medium one is still pending

    def test_failed_email_leaves_the_finding_pending_for_the_digest(self) -> None:
        rig = Rig(notifier_ok=False)
        finding = record(rig, FindingSeverity.HIGH)
        assert finding is not None
        assert [f.finding_id for f in rig.repo.list_unnotified_since(0)] == [finding.finding_id]


class TestEventHandlers:
    def test_sustained_disagreement_and_low_confidence_create_findings(self) -> None:
        rig = Rig()
        state = make_state()
        rig.service.on_occupancy_updated(OccupancyUpdated(state, None, FusionReason.DISAGREEMENT_SUSTAINED))
        rig.service.on_occupancy_updated(OccupancyUpdated(state, None, FusionReason.LOW_CONFIDENCE))
        rig.service.on_occupancy_updated(OccupancyUpdated(state, None, FusionReason.CAMERAS_OK))
        kinds = sorted(f.kind.value for f in rig.repo.list_recent(10))
        assert kinds == ["count_mismatch", "low_confidence"]
        assert (
            "2 personas"
            in next(f for f in rig.repo.list_recent(10) if f.kind is FindingKind.COUNT_MISMATCH).description
        )

    def test_silent_bus_is_high_severity_and_emailed(self) -> None:
        rig = Rig()
        rig.service.on_bus_silent(BusWentSilent("1045", "C5", 0.0, 120.0))
        stored = rig.repo.list_recent(1)[0]
        assert stored.kind is FindingKind.SILENT_BUS and stored.severity is FindingSeverity.HIGH
        assert len(rig.notifier.messages) == 1


class TestDailyDigest:
    def test_digest_sends_pending_findings_once(self) -> None:
        rig = Rig()
        record(rig)
        record(rig, bus="1102", kind=FindingKind.LOW_CONFIDENCE)
        assert rig.service.send_daily_digest() is True
        subject, body = rig.notifier.messages[0]
        assert "(2)" in subject and body.count("\n") == 1
        assert rig.service.send_daily_digest() is False  # nothing pending any more

    def test_findings_older_than_a_day_are_not_included_and_failure_keeps_them_pending(self) -> None:
        rig = Rig(notifier_ok=False)
        record(rig)
        assert rig.service.send_daily_digest() is False
        assert len(rig.repo.list_unnotified_since(0)) == 1
        rig.clock.advance(2 * 86_400)
        assert rig.service.send_daily_digest() is False and len(rig.notifier.messages) == 1


class TestStaleBusMonitor:
    def test_silent_buses_are_marked_and_announced_once(self) -> None:
        rig = Rig()
        seen: list[BusWentSilent] = []
        rig.events.subscribe(BusWentSilent, seen.append)
        rig.live.save(make_state(bus_id="1045", reading_ts=rig.clock.now()))
        rig.live.save(make_state(bus_id="1102", reading_ts=rig.clock.now() - 100))
        assert rig.monitor.check() == 1
        assert [e.bus_id for e in seen] == ["1102"] and seen[0].silent_for_seconds == 100
        live = rig.live.get("1102")
        assert live is not None and live.state is OccupancyState.NO_DATA
        assert rig.monitor.check() == 0  # already marked: no repeated events
        rig.clock.advance(61)
        assert rig.monitor.check() == 1  # now 1045 is silent too
        assert rig.metrics.counter("buses_went_silent") == 2

    def test_wiring_findings_to_the_bus_creates_a_finding(self) -> None:
        rig = Rig()
        rig.events.subscribe(BusWentSilent, rig.service.on_bus_silent)
        rig.live.save(make_state(bus_id="1045", reading_ts=rig.clock.now() - 200))
        rig.monitor.check()
        assert rig.repo.list_recent(5)[0].kind is FindingKind.SILENT_BUS
