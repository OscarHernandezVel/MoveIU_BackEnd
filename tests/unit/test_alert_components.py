"""Alert building blocks: rule, index, streaks, screen alerts, throttle, inbox and dispatcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from core_service.alerts import (
    AlertDispatcher,
    AlertIndex,
    AlertPriority,
    AlertSeverity,
    AlertThrottle,
    ChannelDeliveryError,
    ConsecutiveHitCounter,
    DeviceTarget,
    DispatchFailedError,
    InAppChannel,
    ProximityEngine,
    ProximityHit,
    PushChannel,
    RetryPolicy,
    ScreenAlert,
    ScreenAlertBuilder,
    ScreenAlertKind,
    ScreenInbox,
)
from core_service.clock import FakeClock
from core_service.comparator import NextBusHint
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.models import AlertStatus, Direction, OccupancyState, ProximityAlert
from core_service.persistence.documents import AppendOnlyDocumentStore, InMemoryDocumentStore
from factories import demo_track, make_state
from fakes import RecordingPushGateway, ScriptedChannel

NOW = 1_790_000_000.0
TRACK = demo_track()
TARGET = DeviceTarget("d1", "push-token-abcdef", "android")


def make_alert(**overrides: object) -> ProximityAlert:
    values: dict[str, object] = {
        "alert_id": "a1",
        "device_id": "d1",
        "route_id": "C5",
        "direction": Direction.OUTBOUND,
        "stop_id": "P-031",
        "stops_before": 2,
        "created_at": NOW,
        "expires_at": NOW + 3600,
    }
    values.update(overrides)
    return ProximityAlert(**values)  # type: ignore[arg-type]


class TestProximityEngine:
    engine = ProximityEngine(distance_threshold_m=600)

    def progress_after(self, stop_id: str, extra: float = 0.005) -> float:
        return TRACK.require_stop(stop_id).progress + extra

    def test_far_bus_does_not_trigger(self) -> None:
        state = make_state(progress=self.progress_after("P-001"))
        assert self.engine.evaluate(make_alert(), state, TRACK) is None

    def test_bus_two_stops_away_triggers(self) -> None:
        state = make_state(progress=self.progress_after("P-022"))
        hit = self.engine.evaluate(make_alert(), state, TRACK)
        assert hit is not None and hit.stops_remaining == 2

    def test_bus_within_600_metres_triggers_even_with_more_stops_pending(self) -> None:
        metres = 500 / TRACK.path.length_m
        state = make_state(progress=1.0 - metres)
        hit = self.engine.evaluate(make_alert(stops_before=1, stop_id="P-031"), state, TRACK)
        assert hit is not None and hit.distance_m <= 600

    def test_bus_that_already_passed_does_not_trigger(self) -> None:
        state = make_state(progress=1.0)
        assert self.engine.evaluate(make_alert(), state, TRACK) is None

    def test_other_direction_route_or_bus_do_not_trigger(self) -> None:
        near = self.progress_after("P-022")
        assert self.engine.evaluate(make_alert(), make_state(direction=Direction.RETURN, progress=near), TRACK) is None
        assert self.engine.evaluate(make_alert(), make_state(route_id="C1", progress=near), TRACK) is None
        assert self.engine.evaluate(make_alert(bus_id="1102"), make_state(progress=near), TRACK) is None
        assert self.engine.evaluate(make_alert(bus_id="1045"), make_state(progress=near), TRACK) is not None

    def test_unusable_states_do_not_trigger(self) -> None:
        near = self.progress_after("P-022")
        assert self.engine.evaluate(make_alert(), make_state(progress=near, off_route=True), TRACK) is None
        assert (
            self.engine.evaluate(make_alert(), make_state(progress=near, state=OccupancyState.NO_DATA), TRACK) is None
        )
        assert self.engine.evaluate(make_alert(), make_state(progress=None), TRACK) is None
        assert self.engine.evaluate(make_alert(stop_id="NOPE"), make_state(progress=near), TRACK) is None

    def test_return_direction(self) -> None:
        alert = make_alert(direction=Direction.RETURN, stop_id="P-001")
        state = make_state(direction=Direction.RETURN, progress=TRACK.require_stop("P-018").progress - 0.005)
        hit = self.engine.evaluate(alert, state, TRACK)
        assert hit is not None and hit.stops_remaining == 2  # P-009 and P-001


class TestAlertIndex:
    def test_add_query_and_remove_in_constant_time(self) -> None:
        index = AlertIndex()
        first, second, third = (
            make_alert(alert_id="a1"),
            make_alert(alert_id="a2"),
            make_alert(alert_id="a3", stop_id="P-018"),
        )
        for alert in (first, second, third):
            index.add(alert)
        assert len(index) == 3
        assert [a.alert_id for a in index.for_stop("P-031")] == ["a1", "a2"]
        assert index.remove("a1") == first
        assert index.remove("a1") is None
        assert [a.alert_id for a in index.for_stop("P-031")] == ["a2"]
        index.remove("a3")
        assert index.for_stop("P-018") == []
        assert index.get("a2") == second

    def test_adding_the_same_alert_twice_keeps_one_copy(self) -> None:
        index = AlertIndex()
        index.add(make_alert())
        index.add(make_alert())
        assert len(index) == 1 and len(index.for_stop("P-031")) == 1

    def test_clear(self) -> None:
        index = AlertIndex()
        index.add(make_alert())
        index.clear()
        assert len(index) == 0 and index.all() == []


class TestConsecutiveHitCounter:
    def test_streak_counts_and_resets(self) -> None:
        clock = FakeClock(NOW)
        counter = ConsecutiveHitCounter(clock, window_seconds=20)
        assert counter.hit("a1", "1045") == 1
        clock.advance(3)
        assert counter.hit("a1", "1045") == 2
        counter.reset("a1", "1045")
        assert counter.hit("a1", "1045") == 1

    def test_a_gap_longer_than_the_window_restarts_the_streak(self) -> None:
        clock = FakeClock(NOW)
        counter = ConsecutiveHitCounter(clock, window_seconds=20)
        counter.hit("a1", "1045")
        clock.advance(21)
        assert counter.hit("a1", "1045") == 1

    def test_streaks_are_independent_per_bus_and_forgotten_by_alert(self) -> None:
        clock = FakeClock(NOW)
        counter = ConsecutiveHitCounter(clock)
        counter.hit("a1", "1045")
        counter.hit("a1", "1045")
        assert counter.hit("a1", "1102") == 1
        counter.forget("a1")
        assert counter.hit("a1", "1045") == 1

    def test_sweep_removes_stale_streaks(self) -> None:
        clock = FakeClock(NOW)
        counter = ConsecutiveHitCounter(clock, window_seconds=10)
        counter.hit("a1", "1045")
        clock.advance(11)
        assert counter.sweep() == 1
        assert counter.sweep() == 0


class TestScreenAlertBuilder:
    builder = ScreenAlertBuilder(MessageCatalog(), FakeClock(NOW))
    hit = ProximityHit(stops_remaining=2, distance_m=812.4)

    def test_proximity_alert_in_spanish_with_all_accessibility_fields(self) -> None:
        state = make_state(state=OccupancyState.AVAILABLE_SEATS, pct=46)
        alert = self.builder.proximity(make_alert(), state, self.hit, "Unicentro")
        assert alert.kind is ScreenAlertKind.PROXIMITY
        assert alert.title == "Tu C5 está a 2 paradas"
        assert alert.body == "Bus 1045 hacia Unicentro: Asientos disponibles (46%)."
        assert "por ciento" in alert.accessibility_label
        assert alert.severity is AlertSeverity.INFO and alert.priority is AlertPriority.HIGH
        assert alert.haptic_pattern == (0, 700, 250, 700)
        assert alert.deep_link == "capacidadbus://bus/1045?route=C5&stop=P-031"
        assert alert.dedupe_key == "proximity:a1"
        assert alert.data["stops_remaining"] == "2" and alert.data["distance_m"] == "812"

    def test_crowded_bus_raises_the_severity_and_changes_the_vibration(self) -> None:
        state = make_state(state=OccupancyState.FULL, pct=100)
        alert = self.builder.proximity(make_alert(), state, self.hit, "Unicentro")
        assert alert.severity is AlertSeverity.WARNING
        assert alert.haptic_pattern == (0, 400, 150, 400, 150, 400)
        assert alert.haptic_pattern != (0, 700, 250, 700)  # distinguishable by touch alone

    def test_singular_distance_and_next_bus_hint(self) -> None:
        state = make_state(state=OccupancyState.STANDING_ONLY, pct=92)
        hint = NextBusHint("1102", OccupancyState.AVAILABLE_SEATS, 4)
        alert = self.builder.proximity(make_alert(), state, ProximityHit(1, 300), "Unicentro", hint)
        assert alert.title == "Tu C5 está a 1 parada"
        assert alert.body.endswith(" El siguiente: asientos disponibles, a 4 paradas.")
        assert alert.data["next_bus_id"] == "1102"

    def test_english_locale(self) -> None:
        alert = self.builder.proximity(make_alert(), make_state(), self.hit, "Unicentro", locale="en")
        assert alert.title == "Your C5 is 2 stops away"
        assert alert.locale == "en"

    def test_occupancy_warning_suggests_the_next_bus_when_it_has_seats(self) -> None:
        state = make_state(state=OccupancyState.FULL, pct=100)
        with_hint = self.builder.occupancy_warning(
            make_alert(), state, NextBusHint("1102", OccupancyState.AVAILABLE_SEATS, 5)
        )
        assert with_hint.title == "Tu bus C5 viene lleno"
        assert "1102" in with_hint.body and with_hint.severity is AlertSeverity.WARNING
        no_seats = self.builder.occupancy_warning(make_alert(), state, NextBusHint("1102", OccupancyState.FULL, 5))
        assert "1102" not in no_seats.body
        assert with_hint.dedupe_key == "occupancy_warning:a1:1045:full"

    def test_bus_lost_and_expired(self) -> None:
        lost = self.builder.bus_lost(make_alert(bus_id="1045"), "1045", 75)
        assert lost.kind is ScreenAlertKind.BUS_LOST and "75" in lost.body and lost.priority is AlertPriority.NORMAL
        expired = self.builder.alert_expired(make_alert(), "Unicentro")
        assert expired.kind is ScreenAlertKind.ALERT_EXPIRED and "60 minutos" in expired.body

    def test_alerts_have_unique_ids_and_survive_a_round_trip(self) -> None:
        one = self.builder.proximity(make_alert(), make_state(), self.hit, "Unicentro")
        two = self.builder.proximity(make_alert(), make_state(), self.hit, "Unicentro")
        assert one.notification_id != two.notification_id
        assert ScreenAlert.from_dict(one.to_dict()) == one

    def test_push_data_is_a_flat_string_map(self) -> None:
        data = self.builder.proximity(make_alert(), make_state(), self.hit, "Unicentro").push_data()
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())
        assert {"notification_id", "kind", "deep_link", "accessibility_label"} <= data.keys()


class TestAlertThrottle:
    def test_allows_up_to_the_limit_then_blocks_until_the_window_passes(self) -> None:
        clock = FakeClock(NOW)
        throttle = AlertThrottle(max_alerts=3, window_seconds=60, clock=clock)
        assert [throttle.allow("d1") for _ in range(3)] == [True, True, True]
        assert throttle.allow("d1") is False
        assert throttle.allow("d2") is True  # other devices are unaffected
        clock.advance(61)
        assert throttle.allow("d1") is True

    def test_sliding_window(self) -> None:
        clock = FakeClock(NOW)
        throttle = AlertThrottle(max_alerts=2, window_seconds=60, clock=clock)
        assert throttle.allow("d1")
        clock.advance(40)
        assert throttle.allow("d1")
        clock.advance(30)  # first one is 70 s old, second 30 s old
        assert throttle.allow("d1") is True
        assert throttle.allow("d1") is False

    def test_sweep_frees_idle_devices(self) -> None:
        clock = FakeClock(NOW)
        throttle = AlertThrottle(max_alerts=2, window_seconds=60, clock=clock, max_devices=2)
        throttle.allow("d1")
        throttle.allow("d2")
        clock.advance(100)
        assert throttle.allow("d3") is True  # triggers the sweep instead of growing forever
        throttle.sweep()


class TestScreenInbox:
    @staticmethod
    def alert(n: int) -> ScreenAlert:
        return ScreenAlertBuilder(MessageCatalog(), FakeClock(NOW + n)).alert_expired(
            make_alert(alert_id=f"a{n}"), "Unicentro"
        )

    def test_newest_first_and_bounded(self) -> None:
        inbox = ScreenInbox(InMemoryDocumentStore(FakeClock(NOW)), max_size=3)
        alerts = [self.alert(n) for n in range(5)]
        for alert in alerts:
            inbox.push("d1", alert)
        latest = inbox.latest("d1")
        assert [item.alert.dedupe_key for item in latest] == [
            "alert_expired:a4",
            "alert_expired:a3",
            "alert_expired:a2",
        ]
        assert [i.alert.dedupe_key for i in inbox.latest("d1", limit=1)] == ["alert_expired:a4"]

    def test_read_state(self) -> None:
        inbox = ScreenInbox(InMemoryDocumentStore(FakeClock(NOW)))
        first, second = self.alert(1), self.alert(2)
        inbox.push("d1", first)
        inbox.push("d1", second)
        assert inbox.unread_count("d1") == 2
        assert inbox.mark_read("d1", first.notification_id) is True
        assert inbox.mark_read("d1", "missing") is False
        assert inbox.unread_count("d1") == 1
        assert inbox.mark_all_read("d1") == 1
        assert inbox.unread_count("d1") == 0
        found = inbox.find("d1", first.notification_id)
        assert found is not None and found.read is True

    def test_inboxes_are_isolated_per_device(self) -> None:
        inbox = ScreenInbox(InMemoryDocumentStore(FakeClock(NOW)))
        inbox.push("d1", self.alert(1))
        assert inbox.latest("d2") == []

    def test_survives_a_restart(self, tmp_path: Path) -> None:
        clock = FakeClock(NOW)
        path = tmp_path / "docs.wal"
        store = AppendOnlyDocumentStore(path, clock)
        inbox = ScreenInbox(store)
        first, second = self.alert(1), self.alert(2)
        inbox.push("d1", first)
        inbox.push("d1", second)
        inbox.mark_read("d1", first.notification_id)
        store.close()

        reloaded = ScreenInbox(AppendOnlyDocumentStore(path, clock))
        items = reloaded.latest("d1")
        assert [i.alert.notification_id for i in items] == [second.notification_id, first.notification_id]
        assert [i.read for i in items] == [False, True]


class TestDispatcher:
    @staticmethod
    def dispatcher(
        channels, sleeps: list[float] | None = None, **kwargs
    ) -> tuple[AlertDispatcher, InMemoryDocumentStore]:
        store = InMemoryDocumentStore(FakeClock(NOW))
        recorded = sleeps if sleeps is not None else []
        return (
            AlertDispatcher(
                channels,
                store,
                RetryPolicy(max_attempts=3, base_delay=0.2, factor=2, max_delay=2),
                MetricsRegistry(),
                sleep=recorded.append,
                **kwargs,
            ),
            store,
        )

    @staticmethod
    def screen() -> ScreenAlert:
        return ScreenAlertBuilder(MessageCatalog(), FakeClock(NOW)).alert_expired(make_alert(), "Unicentro")

    def test_delivers_to_every_channel(self) -> None:
        push, inbox = ScriptedChannel("push"), ScriptedChannel("in_app")
        dispatcher, _ = self.dispatcher([push, inbox])
        report = dispatcher.dispatch(TARGET, self.screen())
        assert report.delivered and report.delivered_channels == ["push", "in_app"]
        assert push.calls == 1 and inbox.calls == 1

    def test_duplicates_are_not_sent_twice(self) -> None:
        channel = ScriptedChannel("push")
        dispatcher, _ = self.dispatcher([channel])
        alert = self.screen()
        assert dispatcher.dispatch(TARGET, alert).duplicate is False
        second = dispatcher.dispatch(TARGET, alert)
        assert second.duplicate is True and second.delivered is False
        assert channel.calls == 1

    def test_retries_with_exponential_backoff_then_succeeds(self) -> None:
        def fail_twice(call: int) -> None:
            if call <= 2:
                raise ChannelDeliveryError("timeout")

        channel = ScriptedChannel("push", fail_twice)
        sleeps: list[float] = []
        dispatcher, _ = self.dispatcher([channel], sleeps)
        report = dispatcher.dispatch(TARGET, self.screen())
        assert report.results[0].attempts == 3 and report.delivered
        assert sleeps == [0.2, 0.4]

    def test_non_retryable_failures_are_not_retried(self) -> None:
        def reject(_call: int) -> None:
            raise ChannelDeliveryError("token unregistered", retryable=False)

        channel = ScriptedChannel("push", reject)
        dispatcher, _ = self.dispatcher([channel, ScriptedChannel("in_app")])
        report = dispatcher.dispatch(TARGET, self.screen())
        assert channel.calls == 1
        assert report.results[0].delivered is False and report.results[0].error == "token unregistered"
        assert report.delivered_channels == ["in_app"]

    def test_falls_back_to_the_inbox_when_push_is_down(self) -> None:
        def always_down(_call: int) -> None:
            raise ChannelDeliveryError("provider down")

        push, inbox = ScriptedChannel("push", always_down), ScriptedChannel("in_app")
        dispatcher, _ = self.dispatcher([push, inbox])
        report = dispatcher.dispatch(TARGET, self.screen())
        assert report.delivered is True and report.delivered_channels == ["in_app"]
        assert push.calls == 3  # all attempts used before giving up

    def test_a_crashing_channel_does_not_stop_the_others(self) -> None:
        def crash(_call: int) -> None:
            raise RuntimeError("bug")

        dispatcher, _ = self.dispatcher([ScriptedChannel("push", crash), ScriptedChannel("in_app")])
        assert dispatcher.dispatch(TARGET, self.screen()).delivered_channels == ["in_app"]

    def test_total_failure_raises_and_releases_the_dedupe_key(self) -> None:
        def down(_call: int) -> None:
            raise ChannelDeliveryError("down")

        channel = ScriptedChannel("push", down)
        dispatcher, store = self.dispatcher([channel])
        alert = self.screen()
        with pytest.raises(DispatchFailedError) as caught:
            dispatcher.dispatch(TARGET, alert)
        assert caught.value.code == "alert_not_deliverable"
        assert not store.exists("dedupe", alert.dedupe_key)  # a later attempt can still succeed

    def test_admit_callback_can_suppress_and_releases_the_key(self) -> None:
        channel = ScriptedChannel("push")
        dispatcher, store = self.dispatcher([channel])
        alert = self.screen()
        report = dispatcher.dispatch(TARGET, alert, admit=lambda: False)
        assert report.suppressed is True and channel.calls == 0
        assert not store.exists("dedupe", alert.dedupe_key)
        assert dispatcher.dispatch(TARGET, alert).delivered is True

    def test_real_channels_build_a_complete_push_payload(self) -> None:
        gateway = RecordingPushGateway()
        store = InMemoryDocumentStore(FakeClock(NOW))
        inbox = ScreenInbox(store)
        dispatcher = AlertDispatcher([PushChannel(gateway), InAppChannel(inbox)], store, sleep=lambda _s: None)
        alert = ScreenAlertBuilder(MessageCatalog(), FakeClock(NOW)).proximity(
            make_alert(), make_state(state=OccupancyState.FULL), ProximityHit(2, 700), "Unicentro"
        )
        dispatcher.dispatch(TARGET, alert)
        payload = gateway.sent[0]
        assert payload.priority == "high" and payload.channel_id == "bus_alerts"
        assert payload.vibrate_pattern == alert.haptic_pattern
        assert payload.data["deep_link"].startswith("capacidadbus://bus/1045")
        assert inbox.latest("d1")[0].alert.notification_id == alert.notification_id

    def test_configuration_errors(self) -> None:
        with pytest.raises(ValueError):
            AlertDispatcher([], InMemoryDocumentStore())
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)
        assert RetryPolicy(base_delay=1, factor=10, max_delay=3).delay(2) == 3
        assert AlertStatus.ACTIVE.value == "active"
