"""Streaming statistics for the AI module, headway indicators, and the WebSocket feed and protocol."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from core_service.ai import RuleBasedAnalyzer
from core_service.ai.features import (
    Z_WARMUP_SAMPLES,
    BusFeatures,
    FleetFeatureService,
    ew_update,
    headway_report,
    time_alpha,
)
from core_service.clock import FakeClock
from core_service.config import OccupancySettings
from core_service.models import Direction, OccupancyState
from core_service.occupancy import FusionReason
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.realtime import Channel, InsightsFeed, ProtocolError, RouteFeed, compact_bus, parse_command
from core_service.realtime.protocol import Action
from core_service.routing import RouteCatalog
from core_service.services.events import OccupancyUpdated
from core_service.services.live_state_store import LiveStateStore
from factories import demo_buses, demo_track, make_reading, make_state

T0 = 1_790_000_000.0


# --------------------------------------------------------------- statistics
class TestStreamingStatistics:
    def test_alpha_depends_on_elapsed_time(self) -> None:
        assert time_alpha(0, 300) == 0.0
        assert time_alpha(300, 300) == pytest.approx(1 - math.exp(-1))
        assert time_alpha(3, 300) < time_alpha(30, 300) < 1.0

    def test_exponentially_weighted_moments_converge(self) -> None:
        mean, var = 0.0, 0.0
        for i in range(2_000):
            mean, var = ew_update(mean, var, 4.0 + (1.0 if i % 2 else -1.0), 0.05)
        assert mean == pytest.approx(4.0, abs=0.1)
        assert math.sqrt(var) == pytest.approx(1.0, abs=0.1)


def service() -> tuple[FleetFeatureService, FakeClock, LiveStateStore]:
    clock = FakeClock(T0)
    store = InMemoryDocumentStore(clock)
    catalog = RouteCatalog()
    catalog.replace_all([demo_track()], demo_buses())
    live = LiveStateStore(store, clock)
    return FleetFeatureService(store, catalog, live, clock, OccupancySettings()), clock, live


class TestBusFeatures:
    def test_a_steady_gap_builds_a_baseline_and_a_sudden_jump_gets_a_high_z(self) -> None:
        features, _, _ = service()
        current: BusFeatures | None = None
        for i in range(Z_WARMUP_SAMPLES + 5):
            current = features.update(current, make_state(pct=40, progress=0.1 + i * 0.001), 1, T0 + 3 * i)
        assert current is not None and current.gap_z is not None and abs(current.gap_z) < 1
        spike = features.update(current, make_state(pct=40, progress=0.2), 9, T0 + 100)
        assert spike.gap_z is not None and spike.gap_z > 5
        assert spike.samples == current.samples + 1

    def test_no_z_score_before_the_warm_up(self) -> None:
        features, _, _ = service()
        first = features.update(None, make_state(), 3, T0)
        second = features.update(first, make_state(), 30, T0 + 3)
        assert first.gap_z is None and second.gap_z is None and first.samples == 1

    def test_late_readings_do_not_move_the_statistics(self) -> None:
        features, _, _ = service()
        first = features.update(None, make_state(pct=10), 0, T0)
        assert features.update(first, make_state(pct=99), 0, T0 - 5) is first

    def test_speed_along_the_route_and_reporting_interval(self) -> None:
        features, _, _ = service()
        track = demo_track()
        first = features.update(None, make_state(progress=0.10), 0, T0)
        second = features.update(first, make_state(progress=0.11), 0, T0 + 10)
        assert second.speed_along_mps == pytest.approx(0.01 * track.path.length_m / 10)
        assert second.report_interval_s == pytest.approx(10.0)
        view = second.view(T0 + 12)
        assert view["seconds_since_update"] == 2.0 and view["speed_along_kmh"] is not None

    def test_the_event_handler_persists_and_round_trips(self) -> None:
        features, _, _ = service()
        event = OccupancyUpdated(make_state(), None, FusionReason.CAMERAS_OK, make_reading(seated=10, doors_total=12))
        features.on_occupancy_updated(event)
        stored = features.bus_features(["1045", "nope"])
        assert list(stored) == ["1045"] and stored["1045"].gap_last == -2
        features.on_occupancy_updated(OccupancyUpdated(make_state(), None, FusionReason.CAMERAS_OK))  # no reading
        assert features.bus_features(["1045"])["1045"].samples == 1


class TestHeadway:
    def test_gaps_bunching_and_load_imbalance(self) -> None:
        track = demo_track()
        states = [
            make_state(bus_id="A", progress=0.80, pct=95, state=OccupancyState.FULL),
            make_state(bus_id="B", progress=0.79, pct=30, state=OccupancyState.AVAILABLE_SEATS),  # right behind A
            make_state(bus_id="C", progress=0.30, pct=50),
            make_state(bus_id="D", progress=0.50, direction=Direction.RETURN),  # other direction
            make_state(bus_id="E", progress=0.60, state=OccupancyState.NO_DATA),  # silent
        ]
        report = headway_report(track, states, Direction.OUTBOUND, T0, 60.0)
        assert report["buses"] == 3
        assert report["gaps_m"][0] == round(0.01 * track.path.length_m)
        assert [b["ahead"] for b in report["bunching"]] == ["A"]
        assert report["load_imbalance"] == [{"ahead": "A", "behind": "B", "ahead_pct": 95, "behind_pct": 30}]
        assert report["gap_cv"] is not None and report["gap_cv"] > 0.5

    def test_one_bus_has_no_gaps_and_stale_buses_are_ignored(self) -> None:
        report = headway_report(demo_track(), [make_state(reading_ts=T0 - 600)], Direction.OUTBOUND, T0, 60.0)
        assert report["buses"] == 0 and report["mean_gap_m"] is None

    def test_route_features_combine_both_directions_and_the_bus_statistics(self) -> None:
        features, _, live = service()
        live.save(make_state(bus_id="1045"))
        features.on_occupancy_updated(OccupancyUpdated(make_state(), None, FusionReason.CAMERAS_OK, make_reading()))
        data = features.route_features("C5")
        assert set(data["headway"]) == {"outbound", "return"} and list(data["buses"]) == ["1045"]


class TestContextForTheModel:
    def test_the_context_carries_statistics_and_headway(self) -> None:
        from core_service.ai import ContextBuilder, PrivacyGuard
        from core_service.ai.schemas import AnalysisRequest

        features, clock, live = service()
        catalog = RouteCatalog()
        catalog.replace_all([demo_track()], demo_buses())
        for bus_id, progress in (("1045", 0.40), ("2031", 0.39)):
            state = make_state(bus_id=bus_id, progress=progress)
            live.save(state)
            features.on_occupancy_updated(OccupancyUpdated(state, None, FusionReason.CAMERAS_OK, make_reading()))
        builder = ContextBuilder(live, catalog, None, clock, PrivacyGuard(), 10, features)
        route = builder.build(AnalysisRequest("¿Hay buses juntos?", ("C5",)))["routes"][0]
        assert route["headway"]["outbound"]["bunching"][0]["ahead"] == "1045"
        assert all("features" in bus for bus in route["buses"])
        without = ContextBuilder(live, catalog, None, clock, PrivacyGuard(), 10)
        assert "headway" not in without.build(AnalysisRequest("x", ("C5",)))["routes"][0]


class TestRuleOnDrift:
    def test_a_persistent_door_camera_gap_becomes_a_door_sensor_finding(self) -> None:
        bus = {
            "bus_id": "1045",
            "state": "available_seats",
            "seconds_since_reading": 3,
            "confidence": 0.9,
            "approximate_count": False,
            "occupancy_pct": 40,
            "features": {"samples": 40, "door_camera_gap_mean": -3.2},
        }
        output = RuleBasedAnalyzer().analyze({"routes": [{"route_id": "C5", "buses": [bus]}]})
        assert [f.detected_anomaly.value for f in output.findings] == ["door_camera_mismatch"]
        assert "fewer" in output.findings[0].justification
        few = {**bus, "features": {"samples": 5, "door_camera_gap_mean": -9}}
        assert RuleBasedAnalyzer().analyze({"routes": [{"route_id": "C5", "buses": [few]}]}).findings == ()


# --------------------------------------------------------------------- feed
class TestRouteFeed:
    def test_first_tick_then_only_changes_are_sent(self) -> None:
        feed = RouteFeed("C5", 60.0)
        a, b = make_state(bus_id="A"), make_state(bus_id="B")
        first = feed.advance([a, b], T0)
        assert first.seq == 1 and first.delta is not None
        assert feed.advance([a, b], T0 + 3).delta is None  # nothing changed: nothing on the wire
        assert feed.seq == 1
        moved = replace(b, lat=b.lat + 0.001)
        update = feed.advance([a, moved], T0 + 6)
        body = json.loads(update.delta or "")
        assert body["seq"] == 2 and [x["bus"] for x in body["upsert"]] == ["B"] and body["remove"] == []
        gone = json.loads(feed.advance([a], T0 + 9).delta or "")
        assert gone["upsert"] == [] and gone["remove"] == ["B"]
        snapshot = json.loads(feed.snapshot())
        assert (
            snapshot["type"] == "snapshot" and snapshot["seq"] == 3 and [x["bus"] for x in snapshot["buses"]] == ["A"]
        )
        assert feed.snapshot() is feed.snapshot()  # serialised once, shared by every client

    def test_buses_of_other_routes_are_ignored(self) -> None:
        feed = RouteFeed("C5", 60.0)
        update = feed.advance([make_state(route_id="C1")], T0)
        assert update.delta is None and json.loads(feed.snapshot())["buses"] == []

    def test_a_silent_bus_is_published_as_no_data(self) -> None:
        state = make_state(reading_ts=T0)
        assert compact_bus(state, T0 + 10, 60.0)["state"] == "available_seats"
        assert compact_bus(state, T0 + 61, 60.0)["state"] == "no_data"
        assert compact_bus(state, T0, 60.0)["lat"] == round(state.lat, 5)

    def test_insights_ignore_the_timestamp_alone(self) -> None:
        feed = InsightsFeed("C5")
        assert feed.advance({"generated_at": 1, "x": 1}, T0) is not None
        assert feed.advance({"generated_at": 2, "x": 1}, T0 + 3) is None
        assert feed.advance({"generated_at": 3, "x": 2}, T0 + 6) is not None and feed.seq == 2


class TestProtocol:
    def parse(self, payload: object, max_routes: int = 5) -> object:
        return parse_command(json.dumps(payload), 2_048, max_routes)

    def test_v7_subscribe_message_is_accepted(self) -> None:
        command = self.parse({"action": "subscribe", "rutas": ["C1", "E5", "C1"]})
        assert command.action is Action.SUBSCRIBE and command.routes == ("C1", "E5")  # type: ignore[attr-defined]
        assert command.channel is Channel.FLEET  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            ([1, 2], "invalid_message"),
            ({"action": "dance"}, "unknown_action"),
            ({"action": "subscribe"}, "invalid_routes"),
            ({"action": "subscribe", "routes": ["C1; DROP"]}, "invalid_routes"),
            ({"action": "subscribe", "routes": ["A", "B", "C"]}, "too_many_routes"),
            ({"action": "subscribe", "routes": ["C1"], "channel": "secret"}, "unknown_channel"),
            ({"action": "auth", "token": ""}, "invalid_token"),
        ],
    )
    def test_invalid_messages_have_stable_codes(self, payload: object, code: str) -> None:
        with pytest.raises(ProtocolError) as caught:
            self.parse(payload, max_routes=2)
        assert caught.value.code == code

    def test_size_and_json_are_checked(self) -> None:
        with pytest.raises(ProtocolError, match="message_too_large"):
            parse_command("x" * 3_000, 2_048, 5)
        with pytest.raises(ProtocolError, match="invalid_json"):
            parse_command("{", 2_048, 5)
