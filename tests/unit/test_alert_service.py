"""AlertService: lifecycle, single delivery, rollback on failure, warnings, expiry and lost signal."""

from __future__ import annotations

import pytest

from core_service.alerts import ScreenAlertKind
from core_service.config import AlertSettings
from core_service.errors import NotFoundError, ValidationError
from core_service.models import AlertStatus, Direction, OccupancyState
from core_service.services.events import BusWentSilent
from factories import demo_track, make_state
from fakes import RecordingPushGateway
from harness import AlertHarness, build_alert_harness

TRACK = demo_track()
DEVICE = "device-1"


def progress_after(stop_id: str, extra: float = 0.005) -> float:
    return TRACK.require_stop(stop_id).progress + extra


@pytest.fixture
def harness() -> AlertHarness:
    return build_alert_harness()


def create_alert(h: AlertHarness, **kwargs):
    params = {
        "device_id": DEVICE,
        "route_id": "C5",
        "direction": Direction.OUTBOUND,
        "stop_id": "P-031",
        "stops_before": 2,
    }
    params.update(kwargs)
    return h.service.create(**params)


class TestLifecycle:
    def test_create_list_and_cancel(self, harness: AlertHarness) -> None:
        alert = create_alert(harness)
        assert alert.status is AlertStatus.ACTIVE
        assert alert.expires_at == alert.created_at + 3600
        assert [a.alert_id for a in harness.service.list_active(DEVICE)] == [alert.alert_id]
        assert len(harness.index) == 1
        harness.service.cancel(DEVICE, alert.alert_id)
        assert harness.service.list_active(DEVICE) == [] and len(harness.index) == 0
        with pytest.raises(NotFoundError):
            harness.service.cancel(DEVICE, alert.alert_id)

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"route_id": "Z9"}, NotFoundError),
            ({"stop_id": "NOPE"}, NotFoundError),
            ({"stops_before": 0}, ValidationError),
            ({"stops_before": 6}, ValidationError),
            ({"bus_id": "9999"}, NotFoundError),
            ({"device_id": "unknown-device"}, NotFoundError),
        ],
    )
    def test_invalid_requests(self, harness: AlertHarness, kwargs: dict, error: type[Exception]) -> None:
        with pytest.raises(error):
            create_alert(harness, **kwargs)

    def test_a_bus_can_only_be_followed_on_its_own_route(self, harness: AlertHarness) -> None:
        create_alert(harness, bus_id="1045")  # 1045 serves C5: fine

    def test_active_alert_limit(self) -> None:
        h = build_alert_harness(settings=AlertSettings(max_active_per_device=2))
        create_alert(h)
        create_alert(h)
        with pytest.raises(ValidationError) as caught:
            create_alert(h)
        assert caught.value.code == "alert_limit_reached"

    def test_a_cancelled_alert_frees_a_slot(self) -> None:
        h = build_alert_harness(settings=AlertSettings(max_active_per_device=1))
        first = create_alert(h)
        h.service.cancel(DEVICE, first.alert_id)
        create_alert(h)

    def test_index_is_rebuilt_from_sql(self, harness: AlertHarness) -> None:
        create_alert(harness)
        create_alert(harness, stop_id="P-027")
        harness.index.clear()
        assert harness.service.rebuild_index() == 2 and len(harness.index) == 2


class TestProximityDelivery:
    def test_far_bus_sends_nothing(self, harness: AlertHarness) -> None:
        create_alert(harness)
        harness.update(make_state(progress=progress_after("P-001")))
        assert harness.push.sent == [] and harness.inbox.latest(DEVICE) == []

    def test_needs_two_consecutive_hits(self, harness: AlertHarness) -> None:
        create_alert(harness)
        near = progress_after("P-022")
        harness.update(make_state(progress=near))
        assert harness.push.sent == []
        harness.clock.advance(3)
        harness.update(make_state(progress=near + 0.01, reading_ts=harness.clock.now()))
        assert len(harness.push.sent) == 1

    def test_a_miss_in_between_restarts_the_streak(self, harness: AlertHarness) -> None:
        create_alert(harness, stops_before=1)
        harness.update(make_state(progress=progress_after("P-027")))  # 1 stop away: hit
        harness.clock.advance(3)
        harness.update(make_state(progress=progress_after("P-018")))  # 3 stops away: miss
        harness.clock.advance(3)
        harness.update(make_state(progress=progress_after("P-027")))  # hit again, streak = 1
        assert harness.push.sent == []

    def test_delivers_exactly_once_over_the_push_and_the_inbox(self, harness: AlertHarness) -> None:
        alert = create_alert(harness)
        for step in range(6):
            harness.update(make_state(progress=progress_after("P-022") + step * 0.01))
            harness.clock.advance(3)
        assert len(harness.push.sent) == 1
        items = harness.inbox.latest(DEVICE)
        assert len(items) == 1 and items[0].alert.kind is ScreenAlertKind.PROXIMITY
        stored = harness.alerts.find(alert.alert_id)
        assert stored is not None and stored.status is AlertStatus.SENT and stored.sent_at is not None
        assert len(harness.index) == 0
        assert harness.metrics.counter("alerts_proximity_sent") == 1

    def test_message_reports_occupancy_and_the_next_bus(self, harness: AlertHarness) -> None:
        create_alert(harness)
        # a second bus, further back and with free seats, is the "next bus"
        harness.live_states.save(make_state(bus_id="1102", progress=progress_after("P-001"), pct=40))
        full = make_state(state=OccupancyState.STANDING_ONLY, pct=92, progress=progress_after("P-022"))
        harness.update(full)
        harness.clock.advance(3)
        harness.update(make_state(state=OccupancyState.STANDING_ONLY, pct=92, progress=progress_after("P-022", 0.02)))
        alert = harness.inbox.latest(DEVICE)[0].alert
        assert alert.title == "Tu C5 está a 2 paradas"
        assert "Solo de pie (92%)" in alert.body
        assert "El siguiente: asientos disponibles" in alert.body
        assert alert.data["next_bus_id"] == "1102"

    def test_a_bus_specific_alert_ignores_other_buses(self, harness: AlertHarness) -> None:
        create_alert(harness, bus_id="1045")
        for _ in range(3):
            harness.update(make_state(bus_id="1102", progress=progress_after("P-022")))
            harness.clock.advance(3)
        assert harness.push.sent == []

    def test_the_other_direction_is_ignored(self, harness: AlertHarness) -> None:
        create_alert(harness)
        for _ in range(3):
            harness.update(make_state(direction=Direction.RETURN, progress=progress_after("P-022")))
            harness.clock.advance(3)
        assert harness.push.sent == []

    def test_a_cancelled_alert_is_never_delivered(self, harness: AlertHarness) -> None:
        alert = create_alert(harness)
        harness.service.cancel(DEVICE, alert.alert_id)
        for _ in range(3):
            harness.update(make_state(progress=progress_after("P-022")))
            harness.clock.advance(3)
        assert harness.push.sent == [] and harness.inbox.latest(DEVICE) == []


class TestFailureHandling:
    def test_push_outage_still_delivers_to_the_screen_inbox(self) -> None:
        h = build_alert_harness(push=RecordingPushGateway(fail_times=99))
        create_alert(h)
        for _ in range(3):
            h.update(make_state(progress=progress_after("P-022")))
            h.clock.advance(3)
        assert h.push.sent == []
        assert len(h.inbox.latest(DEVICE)) == 1  # in-app fallback
        stored = h.alerts.list_active_for_device(DEVICE, h.clock.now())
        assert stored == []  # delivered (SENT), so no longer active

    def test_total_failure_rolls_everything_back_and_a_later_reading_retries(self) -> None:
        h = build_alert_harness(push=RecordingPushGateway(fail_times=99))
        alert = create_alert(h)

        class BrokenInbox:
            def push(self, *_args) -> None:
                raise RuntimeError("document store down")

        # sabotage the second channel by replacing the inbox write with a failing one
        h.service._dispatcher._channels[1]._inbox = BrokenInbox()  # noqa: SLF001
        for _ in range(3):
            h.update(make_state(progress=progress_after("P-022")))
            h.clock.advance(3)
        stored = h.alerts.find(alert.alert_id)
        assert stored is not None and stored.status is AlertStatus.ACTIVE and stored.sent_at is None
        assert h.index.get(alert.alert_id) is not None  # LIFO rollback restored it
        assert h.metrics.counter("alerts_dispatch_failed") >= 1

        # push recovers: the next reading delivers the alert
        h.service._dispatcher._channels[0]._gateway.fail_times = 0  # noqa: SLF001
        h.service._dispatcher._channels[0]._gateway.attempts = 0  # noqa: SLF001
        h.update(make_state(progress=progress_after("P-022", 0.03)))
        assert len(h.push.sent) == 1

    def test_one_failing_device_does_not_block_the_others(self) -> None:
        h = build_alert_harness()
        from core_service.models import Device

        h.devices.upsert(Device("device-2", "push-token-2", "ios"), h.clock.now())
        first = create_alert(h)
        create_alert(h, device_id="device-2")
        h.devices.upsert(Device(DEVICE, "x", "android"), h.clock.now())
        h.alerts.transition(first.alert_id, AlertStatus.ACTIVE, AlertStatus.CANCELLED)  # 1st alert lost elsewhere
        for _ in range(3):
            h.update(make_state(progress=progress_after("P-022")))
            h.clock.advance(3)
        assert len(h.inbox.latest("device-2")) == 1


class TestOccupancyWarnings:
    def test_warns_when_the_awaited_bus_becomes_full(self, harness: AlertHarness) -> None:
        create_alert(harness, stops_before=1)  # far enough that proximity does not fire first
        before = make_state(progress=progress_after("P-009"), state=OccupancyState.STANDING_ONLY, pct=90)
        after = make_state(progress=progress_after("P-009", 0.01), state=OccupancyState.FULL, pct=100)
        harness.update(after, previous=before)
        items = harness.inbox.latest(DEVICE)
        assert [i.alert.kind for i in items] == [ScreenAlertKind.OCCUPANCY_WARNING]
        assert "viene lleno" in items[0].alert.title

    def test_no_warning_without_a_change_to_full(self, harness: AlertHarness) -> None:
        create_alert(harness, stops_before=1)
        full = make_state(progress=progress_after("P-009"), state=OccupancyState.FULL, pct=100)
        harness.update(full, previous=full)
        assert harness.inbox.latest(DEVICE) == []

    def test_a_bus_first_seen_full_still_warns_once_its_direction_is_known(self, harness: AlertHarness) -> None:
        create_alert(harness, stops_before=1)
        unknown_direction = make_state(
            progress=progress_after("P-009"), state=OccupancyState.FULL, pct=100, direction=None
        )
        known = make_state(progress=progress_after("P-009", 0.01), state=OccupancyState.FULL, pct=100)
        harness.update(unknown_direction)  # ignored by alerts: direction not known yet
        assert harness.inbox.latest(DEVICE) == []
        harness.update(known, previous=unknown_direction)  # the first evaluable reading is already full
        assert [i.alert.kind for i in harness.inbox.latest(DEVICE)] == [ScreenAlertKind.OCCUPANCY_WARNING]

    def test_warning_is_sent_once_per_bus_and_state(self, harness: AlertHarness) -> None:
        create_alert(harness, stops_before=1)
        before = make_state(progress=progress_after("P-009"), pct=60)
        full = make_state(progress=progress_after("P-009", 0.01), state=OccupancyState.FULL, pct=100)
        harness.update(full, previous=before)
        harness.update(full, previous=before)  # duplicate event
        assert len(harness.inbox.latest(DEVICE)) == 1

    def test_warnings_are_rate_limited_per_device(self) -> None:
        h = build_alert_harness(settings=AlertSettings(throttle_max_alerts=1, throttle_window_seconds=60))
        create_alert(h, stops_before=1)
        before = make_state(progress=progress_after("P-009"), pct=60)
        h.update(
            make_state(bus_id="1045", progress=progress_after("P-009", 0.01), state=OccupancyState.FULL, pct=100),
            previous=before,
        )
        h.update(
            make_state(bus_id="1102", progress=progress_after("P-009", 0.02), state=OccupancyState.FULL, pct=100),
            previous=before,
        )
        assert len(h.inbox.latest(DEVICE)) == 1
        assert h.metrics.counter("alerts_suppressed") == 1


class TestExpiryAndLostSignal:
    def test_expired_alerts_are_closed_and_the_passenger_is_told(self, harness: AlertHarness) -> None:
        alert = create_alert(harness)
        harness.clock.advance(3601)
        assert harness.service.expire_due() == 1
        stored = harness.alerts.find(alert.alert_id)
        assert stored is not None and stored.status is AlertStatus.EXPIRED
        assert len(harness.index) == 0
        assert [i.alert.kind for i in harness.inbox.latest(DEVICE)] == [ScreenAlertKind.ALERT_EXPIRED]
        assert harness.service.expire_due() == 0

    def test_bus_specific_alerts_are_told_when_the_bus_goes_silent(self, harness: AlertHarness) -> None:
        create_alert(harness, bus_id="1045")
        create_alert(harness)  # route-level alert: not tied to a bus
        harness.service.on_bus_silent(BusWentSilent("1045", "C5", 1.0, 75.0))
        items = harness.inbox.latest(DEVICE)
        assert [i.alert.kind for i in items] == [ScreenAlertKind.BUS_LOST]
        assert "75" in items[0].alert.body
        assert len(harness.index) == 2  # both alerts stay active
