"""Occupancy calculator, seat map, reading history and door/camera fusion."""

from __future__ import annotations

import pytest

from core_service.errors import ValidationError
from core_service.models import BusCapacity, OccupancyState
from core_service.occupancy import (
    CountFusion,
    FusionReason,
    OccupancyCalculator,
    PositionKind,
    ReadingHistory,
    ReadingHistoryStore,
    SeatMap,
    round_half_up,
)
from factories import BUS_41, BUS_ACCESSIBLE, MINIBUS_25, make_reading

calculator = OccupancyCalculator()


class TestSeatMap:
    def test_layout_and_capacity(self) -> None:
        seat_map = SeatMap(BUS_ACCESSIBLE)
        assert len(seat_map) == 41 + 8 + 1 + 2  # doors are cells but add no capacity
        assert seat_map.position(0).kind is PositionKind.SEAT
        assert seat_map.position(len(seat_map) - 1).kind is PositionKind.DOOR
        assert BUS_ACCESSIBLE.total == 50

    def test_place_fills_positions_and_reports_overflow(self) -> None:
        seat_map = SeatMap(BUS_41)
        overflow = seat_map.place(seated=45, standing=10, wheelchair=1)
        assert (overflow.seated, overflow.standing, overflow.wheelchair) == (4, 2, 1)
        assert overflow.total == 7
        assert seat_map.free_count(PositionKind.SEAT) == 0
        assert seat_map.occupied_count(PositionKind.STANDING) == 8

    def test_partial_occupancy_counts(self) -> None:
        seat_map = SeatMap(BUS_41)
        seat_map.place(seated=38, standing=5, wheelchair=0)
        assert seat_map.occupied_count(PositionKind.SEAT) == 38
        assert seat_map.free_count(PositionKind.SEAT) == 3
        assert seat_map.free_count(PositionKind.STANDING) == 3


class TestOccupancyFormula:
    def test_full_bus_example_of_the_document(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=41, standing=4))
        assert result.percentage == 92
        assert result.state is OccupancyState.STANDING_ONLY
        assert (result.free_seats, result.free_standing) == (0, 4)

    def test_minibus_example_of_the_document(self) -> None:
        result = calculator.calculate(MINIBUS_25, make_reading(bus_id="1088", seated=25, standing=3))
        assert result.percentage == 97
        assert result.state is OccupancyState.STANDING_ONLY

    def test_seats_still_available(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=38, standing=5))
        assert result.percentage == 88
        assert result.state is OccupancyState.AVAILABLE_SEATS
        assert result.free_seats == 3

    def test_exactly_full_is_100_percent(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=41, standing=8))
        assert result.percentage == 100
        assert result.state is OccupancyState.FULL

    def test_out_of_position_person_means_overcrowding(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=41, standing=8, standing_out_of_position=2))
        assert result.percentage == 104
        assert result.state is OccupancyState.OVERCROWDED
        assert result.standing_out_of_position == 2

    def test_more_standing_than_positions_overflows(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=41, standing=10))
        assert result.standing == 8
        assert result.standing_out_of_position == 2
        assert result.state is OccupancyState.OVERCROWDED

    def test_children_in_arms_add_to_people_but_not_to_occupancy(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=41, standing=4, children_in_arms=2))
        assert result.people_on_board == 47
        assert result.percentage == 92

    def test_wheelchair_space_counts_as_one_position(self) -> None:
        free = calculator.calculate(BUS_ACCESSIBLE, make_reading(seated=41, standing=8))
        assert free.wheelchair_free is True
        assert free.percentage == 98
        assert free.state is OccupancyState.STANDING_ONLY
        taken = calculator.calculate(BUS_ACCESSIBLE, make_reading(seated=41, standing=8, wheelchair_occupied=1))
        assert taken.wheelchair_free is False
        assert taken.state is OccupancyState.FULL

    def test_door_count_replaces_the_camera_total(self) -> None:
        reading = make_reading(seated=30, standing=2, doors_total=52)
        result = calculator.calculate(BUS_41, reading, use_door_count=True)
        assert result.seated == 30
        assert result.standing == 8
        assert result.standing_out_of_position == 14
        assert result.state is OccupancyState.OVERCROWDED
        assert result.approximate_count is True
        assert result.confidence == 0.5

    def test_door_count_never_exceeds_the_door_total(self) -> None:
        reading = make_reading(seated=41, standing=8, doors_total=10)
        result = calculator.calculate(BUS_41, reading, use_door_count=True)
        assert result.occupants == 10

    def test_low_camera_confidence_is_kept_when_lower_than_the_door_ceiling(self) -> None:
        reading = make_reading(seated=10, confidence=0.3)
        assert calculator.calculate(BUS_41, reading, use_door_count=True).confidence == 0.3

    def test_empty_bus(self) -> None:
        result = calculator.calculate(BUS_41, make_reading(seated=0, standing=0))
        assert result.percentage == 0
        assert result.state is OccupancyState.AVAILABLE_SEATS

    def test_percentage_matches_the_formula_for_many_inputs(self) -> None:
        for seated in range(0, 42, 5):
            for standing in range(0, 9):
                result = calculator.calculate(BUS_41, make_reading(seated=seated, standing=standing))
                assert result.percentage == round_half_up((seated + standing) * 100 / 49)

    def test_rounds_half_up(self) -> None:
        assert round_half_up(0.5) == 1
        assert round_half_up(1.5) == 2
        assert round_half_up(2.4) == 2


class TestModelValidation:
    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValidationError):
            BusCapacity(seats=0, standing_positions=1)
        with pytest.raises(ValidationError):
            BusCapacity(seats=10, standing_positions=-1)

    def test_invalid_reading(self) -> None:
        with pytest.raises(ValidationError):
            make_reading(seated=-1)
        with pytest.raises(ValidationError):
            make_reading(confidence=1.5)
        with pytest.raises(ValidationError):
            make_reading(speed_kmh=-3)
        with pytest.raises(ValidationError):
            make_reading(model_version="")


class TestReadingHistory:
    def test_current_is_the_last_pushed(self) -> None:
        history = ReadingHistory("1045")
        history.push(make_reading(ts=1), 0.1)
        history.push(make_reading(ts=2), 0.2)
        current = history.current()
        assert current is not None and current.reading.ts == 2
        assert [entry.reading.ts for entry in history.recent(5)] == [2, 1]

    def test_discard_latest_undoes_a_push(self) -> None:
        history = ReadingHistory("1045")
        history.push(make_reading(ts=1))
        history.push(make_reading(ts=2))
        history.discard_latest()
        current = history.current()
        assert current is not None and current.reading.ts == 1
        history.discard_latest()
        assert history.discard_latest() is None
        assert history.current() is None

    def test_reset_empties_the_history(self) -> None:
        history = ReadingHistory("1045")
        for ts in range(5):
            history.push(make_reading(ts=ts))
        assert history.reset() == 5
        assert len(history) == 0

    def test_history_is_bounded(self) -> None:
        history = ReadingHistory("1045", max_size=3)
        for ts in range(10):
            history.push(make_reading(ts=ts))
        assert [entry.reading.ts for entry in history] == [9, 8, 7]

    def test_store_returns_the_same_history_per_bus(self) -> None:
        store = ReadingHistoryStore()
        assert store.get("1045") is store.get("1045")
        assert store.peek("9999") is None
        assert store.bus_ids() == ["1045"]


class TestCountFusion:
    fusion = CountFusion(threshold_people=2, window_seconds=30, min_confidence=0.6, max_reading_gap_seconds=20)

    @staticmethod
    def _feed(history: ReadingHistory, *, start: float, count: int, step: float, camera: int, doors: int) -> None:
        for i in range(count):
            history.push(make_reading(ts=start + i * step, seated=camera, doors_total=doors))

    def test_small_gap_uses_the_cameras(self) -> None:
        history = ReadingHistory("1045")
        self._feed(history, start=0, count=5, step=3, camera=40, doors=42)
        decision = self.fusion.decide(history)
        assert decision.use_door_count is False
        assert decision.reason is FusionReason.CAMERAS_OK

    def test_recent_disagreement_is_only_observed(self) -> None:
        history = ReadingHistory("1045")
        self._feed(history, start=0, count=4, step=3, camera=40, doors=46)
        decision = self.fusion.decide(history)
        assert decision.use_door_count is False
        assert decision.reason is FusionReason.DISAGREEMENT_UNDER_OBSERVATION
        assert decision.disagreement_seconds == 9

    def test_sustained_disagreement_switches_to_the_door_count(self) -> None:
        history = ReadingHistory("1045")
        self._feed(history, start=0, count=12, step=3, camera=40, doors=46)  # 33 s
        decision = self.fusion.decide(history)
        assert decision.use_door_count is True
        assert decision.reason is FusionReason.DISAGREEMENT_SUSTAINED

    def test_agreement_in_between_restarts_the_run(self) -> None:
        history = ReadingHistory("1045")
        self._feed(history, start=0, count=8, step=3, camera=40, doors=46)
        history.push(make_reading(ts=24, seated=40, doors_total=40))  # cameras agree again
        self._feed(history, start=27, count=3, step=3, camera=40, doors=46)
        decision = self.fusion.decide(history)
        assert decision.use_door_count is False
        assert decision.disagreement_seconds == 6

    def test_a_long_silence_breaks_the_run(self) -> None:
        history = ReadingHistory("1045")
        self._feed(history, start=0, count=6, step=3, camera=40, doors=46)
        self._feed(history, start=200, count=3, step=3, camera=40, doors=46)
        decision = self.fusion.decide(history)
        assert decision.reason is FusionReason.DISAGREEMENT_UNDER_OBSERVATION
        assert decision.disagreement_seconds == 6

    def test_occlusion_and_low_confidence_use_the_door_count_immediately(self) -> None:
        history = ReadingHistory("1045")
        history.push(make_reading(ts=1, approximate_count=True))
        assert self.fusion.decide(history).reason is FusionReason.OCCLUSION
        history = ReadingHistory("1045")
        history.push(make_reading(ts=1, confidence=0.4))
        assert self.fusion.decide(history).reason is FusionReason.LOW_CONFIDENCE

    def test_empty_history(self) -> None:
        assert self.fusion.decide(ReadingHistory("1045")).use_door_count is False
