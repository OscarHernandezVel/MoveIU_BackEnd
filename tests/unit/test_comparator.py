"""'This bus or the next one' comparison."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core_service.clock import FakeClock
from core_service.comparator import NextBusComparator, NextBusService
from core_service.errors import NotFoundError
from core_service.models import Direction, OccupancyState
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.routing import RouteCatalog
from core_service.services.live_state_store import LiveStateStore
from factories import demo_buses, demo_track, make_state

TRACK = demo_track()
STOP = TRACK.require_stop("P-022")
OUT = Direction.OUTBOUND
CROWDED = OccupancyState.FULL
FREE = OccupancyState.AVAILABLE_SEATS


def bus(bus_id: str, progress: float, state: OccupancyState = FREE, **kw: object):  # noqa: ANN201
    return replace(make_state(bus_id=bus_id, progress=progress, state=state), **kw)


class TestComparator:
    comparator = NextBusComparator()

    def test_nearest_bus_comes_first(self) -> None:
        result = self.comparator.compare([bus("A", 0.3), bus("B", 0.6)], TRACK, OUT, STOP)
        assert [r.state.bus_id for r in result] == ["B", "A"]
        assert result[0].stops_remaining == 1 and result[0].distance_m == pytest.approx(329, abs=5)

    def test_buses_that_already_passed_the_stop_are_ignored(self) -> None:
        result = self.comparator.compare([bus("A", 0.9), bus("B", 0.6)], TRACK, OUT, STOP)
        assert [r.state.bus_id for r in result] == ["B"]

    def test_unusable_buses_are_filtered(self) -> None:
        candidates = [
            bus("wrong-direction", 0.6, direction=Direction.RETURN),
            bus("off-route", 0.6, off_route=True),
            bus("silent", 0.6, state=OccupancyState.NO_DATA),
            bus("no-progress", 0.6, outbound_progress=None),
            bus("other-route", 0.6, route_id="C7"),
            bus("ok", 0.5),
        ]
        assert [r.state.bus_id for r in self.comparator.compare(candidates, TRACK, OUT, STOP)] == ["ok"]

    def test_second_bus_is_recommended_when_this_one_is_crowded_and_the_next_has_seats(self) -> None:
        result = self.comparator.compare([bus("A", 0.6, CROWDED), bus("B", 0.3, FREE)], TRACK, OUT, STOP)
        assert [r.recommended for r in result] == [False, True]

    @pytest.mark.parametrize(
        ("first", "second"),
        [(FREE, FREE), (CROWDED, CROWDED), (FREE, CROWDED), (CROWDED, OccupancyState.STANDING_ONLY)],
    )
    def test_no_recommendation_otherwise(self, first: OccupancyState, second: OccupancyState) -> None:
        result = self.comparator.compare([bus("A", 0.6, first), bus("B", 0.3, second)], TRACK, OUT, STOP)
        assert not any(r.recommended for r in result)

    def test_limit_is_respected_and_at_least_one_is_returned(self) -> None:
        buses = [bus(str(i), 0.1 + i * 0.1) for i in range(5)]
        assert len(self.comparator.compare(buses, TRACK, OUT, STOP, limit=3)) == 3
        assert len(self.comparator.compare(buses, TRACK, OUT, STOP, limit=0)) == 1
        assert self.comparator.compare([], TRACK, OUT, STOP) == []

    def test_return_direction_uses_the_reverse_order(self) -> None:
        stop = TRACK.require_stop("P-018")
        result = self.comparator.compare(
            [bus("far", 0.9, direction=Direction.RETURN), bus("near", 0.6, direction=Direction.RETURN)],
            TRACK,
            Direction.RETURN,
            stop,
        )
        assert [r.state.bus_id for r in result] == ["near", "far"]

    def test_hint_skips_the_bus_itself(self) -> None:
        result = self.comparator.compare([bus("A", 0.6), bus("B", 0.3, CROWDED)], TRACK, OUT, STOP)
        hint = self.comparator.hint_from(result, excluding_bus_id="A")
        assert hint is not None and (hint.bus_id, hint.state) == ("B", CROWDED)
        assert self.comparator.hint_from(result[:1], excluding_bus_id="A") is None


class TestNextBusService:
    def build(self) -> tuple[NextBusService, LiveStateStore]:
        clock = FakeClock()
        catalog = RouteCatalog()
        catalog.replace_all([TRACK], demo_buses())
        live = LiveStateStore(InMemoryDocumentStore(clock), clock)
        return NextBusService(catalog, live), live

    def test_reads_the_live_states_of_the_route(self) -> None:
        service, live = self.build()
        live.save(bus("1045", 0.6, CROWDED))
        live.save(bus("1102", 0.3, FREE))
        result = service.get_next_buses("P-022", "C5", OUT)
        assert result.stop_name == "Av. Panamericana" and [b.state.bus_id for b in result.buses] == ["1045", "1102"]
        assert result.buses[1].recommended

    def test_limit_is_clamped_to_three(self) -> None:
        service, live = self.build()
        for i, progress in enumerate((0.1, 0.2, 0.3, 0.4, 0.5)):
            live.save(bus(f"b{i}", progress))
        assert len(service.get_next_buses("P-022", "C5", OUT, limit=99).buses) == 3

    def test_unknown_route_or_stop(self) -> None:
        service, _ = self.build()
        with pytest.raises(NotFoundError):
            service.get_next_buses("P-022", "Z9", OUT)
        with pytest.raises(NotFoundError) as caught:
            service.get_next_buses("P-999", "C5", OUT)
        assert caught.value.code == "stop_not_in_route"

    def test_hint_after_a_bus(self) -> None:
        service, live = self.build()
        first, second = bus("1045", 0.6), bus("1102", 0.3, CROWDED)
        live.save(first)
        live.save(second)
        hint = service.hint_after(first, "P-022")
        assert hint is not None and hint.bus_id == "1102"
        assert service.hint_after(replace(first, route_id=None), "P-022") is None
        assert service.hint_after(replace(first, direction=None), "P-022") is None
        assert service.hint_after(first, "P-999") is None
