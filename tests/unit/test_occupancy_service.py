"""Occupancy pipeline: state calculation, ordering, direction, terminal handling and rollback."""

from __future__ import annotations

import pytest

from core_service.errors import NotFoundError, ValidationError
from core_service.models import Direction, OccupancyState
from core_service.occupancy import FusionReason
from core_service.services.occupancy_service import ProcessStatus
from factories import demo_track, make_reading
from harness import PipelineHarness, build_pipeline_harness

TRACK = demo_track()


def at(progress: float) -> dict:
    point = TRACK.path.point_at(progress)
    return {"lat": point.lat, "lng": point.lng}


@pytest.fixture
def h() -> PipelineHarness:
    return build_pipeline_harness()


def send(h: PipelineHarness, offset: float = 0.0, progress: float = 0.3, **kw: object):  # noqa: ANN201
    reading = make_reading(ts=h.clock.now() + offset, **{**at(progress), **kw})
    return h.service.process(reading)


class TestState:
    def test_first_reading_creates_the_live_state_and_publishes_it(self, h: PipelineHarness) -> None:
        outcome = send(h, seated=30, standing=5)
        state = h.live_states.get("1045")
        assert outcome.status is ProcessStatus.APPLIED and state is not None
        assert (state.occupancy_pct, state.state) == (71, OccupancyState.AVAILABLE_SEATS)  # 35 / 49
        assert (
            state.route_id == "C5" and not state.off_route and state.outbound_progress == pytest.approx(0.3, abs=0.01)
        )
        assert len(h.published) == 1 and h.published[0].previous is None
        assert h.telemetry.count() == 1

    def test_the_second_reading_replaces_the_state_and_carries_the_previous_one(self, h: PipelineHarness) -> None:
        send(h, seated=10)
        h.clock.advance(5)
        send(h, seated=12)
        assert h.live_states.get("1045").seated == 12  # type: ignore[union-attr]
        assert h.published[1].previous.seated == 10

    def test_full_and_overcrowded_states(self, h: PipelineHarness) -> None:
        send(h, seated=41, standing=8)
        assert h.live_states.get("1045").state is OccupancyState.FULL  # type: ignore[union-attr]
        h.clock.advance(5)
        send(h, seated=41, standing=8, standing_out_of_position=3)
        state = h.live_states.get("1045")
        assert state is not None and state.state is OccupancyState.OVERCROWDED and state.occupancy_pct == 106

    def test_disagreement_between_cameras_and_doors_switches_to_the_door_count(self, h: PipelineHarness) -> None:
        for step in range(8):  # 35 s of readings where doors say 6 more people than the cameras
            send(h, offset=step * 5, seated=20, standing=0, doors_total=26)
        h.clock.advance(40)
        state = h.live_states.get("1045")
        assert state is not None and state.approximate_count
        assert h.published[-1].fusion_reason is FusionReason.DISAGREEMENT_SUSTAINED
        assert state.seated + state.standing + state.standing_out_of_position == 26

    def test_low_confidence_uses_the_door_count_with_a_capped_confidence(self, h: PipelineHarness) -> None:
        send(h, seated=10, doors_total=14, confidence=0.3)
        state = h.live_states.get("1045")
        assert state is not None and state.confidence <= 0.5 and state.approximate_count
        assert h.published[-1].fusion_reason is FusionReason.LOW_CONFIDENCE


class TestOrdering:
    def test_duplicates_are_ignored(self, h: PipelineHarness) -> None:
        send(h, seated=10)
        assert send(h, seated=99).status is ProcessStatus.DUPLICATE
        assert h.live_states.get("1045").seated == 10  # type: ignore[union-attr]
        assert len(h.published) == 1

    def test_late_readings_are_stored_but_do_not_touch_the_live_state(self, h: PipelineHarness) -> None:
        send(h, offset=0, seated=10)
        outcome = send(h, offset=-30, seated=25)
        assert outcome.status is ProcessStatus.LATE_STORED
        assert h.live_states.get("1045").seated == 10  # type: ignore[union-attr]
        assert h.telemetry.count() == 2 and len(h.published) == 1

    def test_a_late_reading_after_a_terminal_reset_is_still_recognised_as_late(self, h: PipelineHarness) -> None:
        send(h, offset=0, progress=0.99, seated=10)
        send(h, offset=5, progress=1.0, speed_kmh=0.0, seated=12)  # terminal: the history stack is emptied
        assert len(h.histories.get("1045")) == 0
        outcome = send(h, offset=2, progress=0.5, seated=33)  # older than the terminal reading
        assert outcome.status is ProcessStatus.LATE_STORED
        state = h.live_states.get("1045")
        assert state is not None and state.seated == 12 and state.reading_ts == h.clock.now() + 5
        assert len(h.published) == 2

    def test_a_duplicate_of_the_terminal_reading_is_ignored(self, h: PipelineHarness) -> None:
        send(h, offset=0, progress=1.0, speed_kmh=0.0, seated=12)
        assert send(h, offset=0, progress=1.0, speed_kmh=0.0, seated=99).status is ProcessStatus.DUPLICATE
        assert h.live_states.get("1045").seated == 12  # type: ignore[union-attr]

    def test_after_a_restart_late_and_duplicate_readings_are_still_detected(self, h: PipelineHarness) -> None:
        from core_service.occupancy import ReadingHistoryStore

        send(h, offset=0, seated=10)
        h.service._histories = ReadingHistoryStore(120)  # noqa: SLF001 - the process restarted: memory is empty
        assert send(h, offset=0, seated=50).status is ProcessStatus.DUPLICATE
        assert send(h, offset=-20, seated=50).status is ProcessStatus.LATE_STORED
        assert h.live_states.get("1045").seated == 10  # type: ignore[union-attr]
        assert send(h, offset=5, seated=11).status is ProcessStatus.APPLIED

    def test_the_latest_reading_survives_a_terminal_reset_and_a_rollback_restores_it(self, h: PipelineHarness) -> None:
        send(h, offset=0, progress=0.9, seated=10)
        history = h.histories.get("1045")
        assert history.latest() is not None and history.latest().reading.seated == 10  # type: ignore[union-attr]
        send(h, offset=5, progress=1.0, speed_kmh=0.0, seated=20)
        assert history.current() is None and history.latest().reading.seated == 20  # type: ignore[union-attr]
        history.push(make_reading(ts=h.clock.now() + 9, seated=30))
        history.discard_latest()  # undo of a failed pipeline
        assert history.latest().reading.seated == 20  # type: ignore[union-attr]

    def test_a_batch_is_applied_in_time_order_and_reports_each_item(self, h: PipelineHarness) -> None:
        readings = [
            make_reading(ts=h.clock.now() - 20, seated=30, **at(0.3)),
            make_reading(ts=h.clock.now() - 60, seated=10, **at(0.2)),
            make_reading(bus_id="9999", ts=h.clock.now() - 10),
            make_reading(ts=h.clock.now() - 40, seated=20, **at(0.25)),
        ]
        results = h.service.process_batch(readings)
        assert [r.index for r in results] == [0, 1, 2, 3]
        assert [r.ok for r in results] == [True, True, False, True]
        assert results[2].error_code == "bus_not_found"
        assert all(r.outcome.status is ProcessStatus.APPLIED for r in results if r.ok)  # sorted: none was "late"
        assert h.live_states.get("1045").seated == 30  # type: ignore[union-attr]


class TestValidation:
    def test_unknown_bus(self, h: PipelineHarness) -> None:
        with pytest.raises(NotFoundError):
            h.service.process(make_reading(bus_id="0000", ts=h.clock.now()))

    @pytest.mark.parametrize("offset", [3_600, -3 * 86_400])
    def test_timestamps_far_from_now_are_rejected(self, h: PipelineHarness, offset: float) -> None:
        with pytest.raises(ValidationError):
            send(h, offset=offset)
        assert h.telemetry.count() == 0 and h.live_states.get("1045") is None


class TestDirectionAndPosition:
    def test_direction_follows_the_movement(self, h: PipelineHarness) -> None:
        for step, progress in enumerate((0.30, 0.34, 0.38, 0.42)):
            send(h, offset=step * 5, progress=progress)
        assert h.live_states.get("1045").direction is Direction.OUTBOUND  # type: ignore[union-attr]
        for step, progress in enumerate((0.60, 0.56, 0.52, 0.48, 0.44, 0.40), start=4):
            send(h, offset=step * 5, progress=progress)
        assert h.live_states.get("1045").direction is Direction.RETURN  # type: ignore[union-attr]

    def test_a_stopped_bus_keeps_its_direction(self, h: PipelineHarness) -> None:
        for step, progress in enumerate((0.30, 0.34, 0.38)):
            send(h, offset=step * 5, progress=progress)
        for step in range(3, 8):
            send(h, offset=step * 5, progress=0.38)
        assert h.live_states.get("1045").direction is Direction.OUTBOUND  # type: ignore[union-attr]

    def test_off_route_bus_has_no_progress_and_keeps_the_previous_direction(self, h: PipelineHarness) -> None:
        for step, progress in enumerate((0.30, 0.34, 0.38)):
            send(h, offset=step * 5, progress=progress)
        h.clock.advance(20)
        h.service.process(make_reading(ts=h.clock.now(), lat=1.30, lng=-77.40))
        state = h.live_states.get("1045")
        assert state is not None and state.off_route and state.outbound_progress is None
        assert state.direction is Direction.OUTBOUND

    def test_terminal_resets_the_history_and_points_the_bus_outwards_again(self, h: PipelineHarness) -> None:
        for step, progress in enumerate((0.90, 0.95, 0.99)):
            send(h, offset=step * 5, progress=progress)
        assert len(h.histories.get("1045")) == 3
        send(h, offset=20, progress=1.0, speed_kmh=0.0)
        state = h.live_states.get("1045")
        assert len(h.histories.get("1045")) == 0
        assert state is not None and state.direction is Direction.RETURN  # at the far end it leaves towards the start
        send(h, offset=21, progress=0.0, speed_kmh=0.0)
        assert h.live_states.get("1045").direction is Direction.OUTBOUND  # type: ignore[union-attr]

    def test_a_moving_bus_near_the_terminal_is_not_reset(self, h: PipelineHarness) -> None:
        send(h, progress=1.0, speed_kmh=30.0)
        assert len(h.histories.get("1045")) == 1


class TestRollback:
    def test_a_telemetry_failure_undoes_the_history_and_the_live_state(self, h: PipelineHarness) -> None:
        send(h, seated=10)
        h.clock.advance(5)

        def boom(*_: object, **__: object) -> bool:
            raise RuntimeError("disk full")

        h.service._telemetry = replace_save(h.telemetry, boom)  # noqa: SLF001
        with pytest.raises(RuntimeError):
            send(h, seated=20)
        assert len(h.histories.get("1045")) == 1  # the pushed entry was popped
        assert h.live_states.get("1045").seated == 10  # type: ignore[union-attr]  # previous state restored
        assert len(h.published) == 1  # nothing was announced

    def test_a_failure_on_the_first_reading_leaves_no_live_state(self, h: PipelineHarness) -> None:
        def boom(*_: object, **__: object) -> bool:
            raise RuntimeError("disk full")

        h.service._telemetry = replace_save(h.telemetry, boom)  # noqa: SLF001
        with pytest.raises(RuntimeError):
            send(h, seated=20)
        assert h.live_states.get("1045") is None and len(h.histories.get("1045")) == 0

    def test_a_failing_subscriber_does_not_break_the_pipeline(self, h: PipelineHarness) -> None:
        from core_service.services.events import OccupancyUpdated

        def bad_handler(_: object) -> None:
            raise ValueError("subscriber bug")

        h.events.subscribe(OccupancyUpdated, bad_handler)
        assert send(h).status is ProcessStatus.APPLIED
        assert h.metrics.counter("event_handler_errors", event="OccupancyUpdated") == 1


class _Failing:
    def __init__(self, inner: object, fail: object) -> None:
        self._inner, self._fail = inner, fail

    def save(self, *args: object, **kwargs: object) -> bool:
        return self._fail(*args, **kwargs)  # type: ignore[operator]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def replace_save(telemetry: object, fail: object) -> _Failing:
    return _Failing(telemetry, fail)
