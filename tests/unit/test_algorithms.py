"""Algorithmic improvements: grid-indexed map matching, continuity between legs, O(log n) stop counting,
bounded heap selection, robust direction estimation and batched live-state reads."""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from core_service.clock import FakeClock
from core_service.data_structures import BinaryHeap, EmptyStructureError, smallest_k
from core_service.models import Direction, GeoPoint, Stop
from core_service.persistence.documents import InMemoryDocumentStore, RedisDocumentStore
from core_service.routing import DirectionDetector, PathGeometry, RouteTrack
from core_service.routing.direction_detector import repeated_median_slope
from core_service.services.live_state_store import LiveStateStore
from factories import demo_track, make_state

ORIGIN = GeoPoint(1.2136, -77.2811)
M_PER_DEG = 111_195.0


def offset(dx_m: float, dy_m: float) -> GeoPoint:
    """Point ``dx`` metres east and ``dy`` metres north of ORIGIN."""
    return GeoPoint(ORIGIN.lat + dy_m / M_PER_DEG, ORIGIN.lng + dx_m / (M_PER_DEG * math.cos(math.radians(ORIGIN.lat))))


def brute_force(path: PathGeometry, point: GeoPoint) -> tuple[float, float]:
    """Reference: test every segment (the previous O(v) algorithm)."""
    pts = path.points
    to_xy = path._to_xy  # noqa: SLF001 - same projection on purpose
    px, py = to_xy(point)
    best, best_along, travelled = math.inf, 0.0, 0.0
    for a, b in zip(pts, pts[1:], strict=False):
        ax, ay = to_xy(a)
        bx, by = to_xy(b)
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (length * length)))
        distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if distance < best:
            best, best_along = distance, travelled + t * length
        travelled += length
    return best_along / path.length_m, best


def random_path(rng: random.Random, vertices: int) -> PathGeometry:
    x = y = 0.0
    points = [offset(0, 0)]
    heading = rng.uniform(0, 2 * math.pi)
    for _ in range(vertices - 1):
        heading += rng.uniform(-1.2, 1.2)
        step = rng.uniform(5, 400)
        x, y = x + step * math.cos(heading), y + step * math.sin(heading)
        points.append(offset(x, y))
    return PathGeometry(points)


# ------------------------------------------------------------------ geometry
class TestGridProjection:
    @pytest.mark.parametrize("seed", range(6))
    def test_the_grid_gives_exactly_the_brute_force_answer(self, seed: int) -> None:
        rng = random.Random(seed)
        path = random_path(rng, rng.randint(2, 300))
        for _ in range(300):
            spread = rng.choice([5.0, 50.0, 300.0, 3_000.0])  # on the trace, near, and far (full-scan fallback)
            anchor = path.point_at(rng.random())
            to_xy = path._to_xy(anchor)  # noqa: SLF001
            query = offset(to_xy[0] + rng.uniform(-spread, spread), to_xy[1] + rng.uniform(-spread, spread))
            got = path.project(query)
            progress, distance = brute_force(path, query)
            assert got.distance_m == pytest.approx(distance, abs=1e-6)
            if distance > 0:  # the same minimum can be reached at two places; the distance is what is unique
                assert brute_force_distance_at(path, query, got.progress) == pytest.approx(distance, abs=1e-6)

    def test_point_at_is_the_inverse_of_project_and_is_clamped(self) -> None:
        path = demo_track().path
        for progress in (0.0, 0.1, 0.25, 0.5, 0.77, 1.0):
            assert path.project(path.point_at(progress)).progress == pytest.approx(progress, abs=1e-6)
        assert path.point_at(-3) == path.point_at(0.0)
        assert path.point_at(9) == path.point_at(1.0)

    def test_invalid_parameters_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            PathGeometry([offset(0, 0), offset(10, 0)], cell_size_m=0)


def brute_force_distance_at(path: PathGeometry, point: GeoPoint, progress: float) -> float:
    target = path.point_at(progress)
    ax, ay = path._to_xy(point)  # noqa: SLF001
    bx, by = path._to_xy(target)  # noqa: SLF001
    return math.hypot(ax - bx, ay - by)


class TestContinuity:
    """An out-and-back route: the return leg runs 10 m from the outbound leg along the same street."""

    @staticmethod
    def out_and_back() -> PathGeometry:
        return PathGeometry([offset(0, 0), offset(2_000, 0), offset(2_000, 10), offset(0, 10)])

    def test_without_a_hint_the_nearest_leg_wins_even_if_the_bus_is_on_the_other_one(self) -> None:
        path = self.out_and_back()
        fix = offset(1_000, 6)  # GPS error: the bus drives on the return leg but the fix is closer to... y=10 leg
        assert path.project(fix).progress > 0.5  # nearest leg is the return one (y = 10)

    def test_a_hint_keeps_the_bus_on_its_leg(self) -> None:
        path = self.out_and_back()
        fix = offset(1_000, 6)
        outbound = path.project(fix, hint_progress=0.24)  # it was at 480 m on the way out 3 s ago
        assert outbound.progress == pytest.approx(1_000 / path.length_m, abs=1e-3)
        returning = path.project(fix, hint_progress=0.76)
        assert returning.progress > 0.5

    def test_an_unreachable_hint_falls_back_to_the_closest_leg_along_the_path(self) -> None:
        path = self.out_and_back()
        result = path.project(offset(1_000, 6), hint_progress=0.0, max_jump_m=10.0)
        assert result.progress < 0.5  # neither leg is reachable; the one nearer to the hint along the path wins

    def test_stops_are_placed_on_the_next_leg_in_route_order(self) -> None:
        path = self.out_and_back()
        stop = offset(1_000, 5)  # equidistant from both legs
        assert path.project_after(stop, 0.0).progress < 0.5
        assert path.project_after(stop, 0.4).progress > 0.5  # already past it on the way out: the return leg
        assert path.project_after(stop, 0.99).progress == pytest.approx(path.project(stop).progress)  # none ahead

    def test_a_route_using_a_street_twice_now_accepts_its_stops_in_order(self) -> None:
        path = self.out_and_back()
        stops = [
            Stop("A", "A", offset(500, 5)),
            Stop("B", "B", offset(1_500, 5)),
            Stop("C", "C", offset(1_500, 5.1)),  # same corner, on the way back
            Stop("D", "D", offset(500, 5.1)),
        ]
        track = RouteTrack("L1", "Loop", path, stops)
        progress = [s.progress for s in track.stops()]
        assert progress == sorted(progress) and progress[2] > 0.5


# ---------------------------------------------------------------- stop counting
def naive_remaining(track: RouteTrack, direction: Direction, progress: float, stop_id: str) -> int:
    """Reference implementation: walk the linked list stop by stop (the previous O(k) algorithm)."""
    stops = track.stops() if direction is Direction.OUTBOUND else list(reversed(track.stops()))
    ahead = [s for s in stops if (s.progress > progress if direction is Direction.OUTBOUND else s.progress < progress)]
    ids = [s.stop.stop_id for s in ahead]
    return ids.index(stop_id) + 1 if stop_id in ids else -1


class TestStopsRemaining:
    def test_index_arithmetic_matches_walking_the_list(self) -> None:
        track = demo_track()
        rng = random.Random(3)
        for _ in range(500):
            direction = rng.choice(list(Direction))
            progress = rng.choice([rng.random(), rng.choice(track.stops()).progress, 0.0, 1.0])
            stop = rng.choice(track.stops()).stop.stop_id
            assert track.stops_remaining(direction, progress, stop) == naive_remaining(track, direction, progress, stop)


# ----------------------------------------------------------------- binary heap
class TestBinaryHeap:
    def test_pops_in_order_and_is_stable_on_ties(self) -> None:
        heap: BinaryHeap[tuple[int, str]] = BinaryHeap(key=lambda item: item[0])
        items = [(3, "a"), (1, "b"), (3, "c"), (2, "d"), (1, "e")]
        for item in items:
            heap.push(item)
        assert heap.peek() == (1, "b")
        assert heap.drain() == [(1, "b"), (1, "e"), (2, "d"), (3, "a"), (3, "c")]

    def test_max_heap_and_replace_top(self) -> None:
        heap: BinaryHeap[int] = BinaryHeap(largest_first=True)
        for value in (4, 9, 1):
            heap.push(value)
        assert heap.replace_top(5) == 9
        assert heap.drain() == [5, 4, 1]

    def test_empty_heap_raises(self) -> None:
        heap: BinaryHeap[int] = BinaryHeap()
        for action in (heap.pop, heap.peek, lambda: heap.replace_top(1)):
            with pytest.raises(EmptyStructureError):
                action()

    @pytest.mark.parametrize("seed", range(5))
    def test_smallest_k_equals_a_stable_sort(self, seed: int) -> None:
        rng = random.Random(seed)
        items = [(rng.randint(0, 20), i) for i in range(rng.randint(0, 80))]
        for k in (0, 1, 2, 5, 200):
            assert smallest_k(items, k, key=lambda item: item[0]) == sorted(items, key=lambda item: item[0])[:k]


# ---------------------------------------------------------------- direction
class TestRobustDirection:
    @pytest.mark.parametrize("wild_index", [0, 1, 2, 3])
    def test_one_wild_fix_anywhere_in_the_window_is_ignored(self, wild_index: int) -> None:
        # the bus moves forward 0.01 every 3 s; one fix jumped to the other leg of the street (0.10)
        progress = [0.54, 0.53, 0.52, 0.51]
        progress[wild_index] = 0.10
        samples = [(12.0 - 3.0 * i, p) for i, p in enumerate(progress)]  # newest first
        assert DirectionDetector(window=4).detect_series(samples, None) is Direction.OUTBOUND

    def test_the_old_endpoint_difference_would_have_flipped(self) -> None:
        newest_wild = [0.10, 0.53, 0.52, 0.51]
        assert newest_wild[0] - newest_wild[-1] < 0  # "newest minus oldest" says RETURN: the bug this fixes

    def test_the_slope_of_a_line_is_exact(self) -> None:
        assert repeated_median_slope([(0.0, 0.0), (1.0, 2.0), (2.0, 4.0)]) == pytest.approx(2.0)
        assert repeated_median_slope([(1.0, 0.0), (1.0, 3.0)]) is None

    def test_a_stopped_bus_or_equal_timestamps_keep_the_previous_direction(self) -> None:
        detector = DirectionDetector(window=4, min_progress_delta=0.002)
        assert detector.detect_series([(9.0, 0.5), (6.0, 0.5), (3.0, 0.5)], Direction.RETURN) is Direction.RETURN
        assert detector.detect_series([(5.0, 0.6), (5.0, 0.4)], Direction.OUTBOUND) is Direction.OUTBOUND
        assert detector.detect_series([(5.0, 0.6)], None) is None

    def test_values_without_timestamps_still_work(self) -> None:
        assert DirectionDetector().detect([0.40, 0.41, 0.42, 0.43], None) is Direction.RETURN  # newest first


# ------------------------------------------------------------- batched reads
class CountingStore(InMemoryDocumentStore):
    def __init__(self) -> None:
        super().__init__(FakeClock(1_790_000_000.0))
        self.calls: dict[str, int] = {"get": 0, "get_many": 0}
        self._batch = False

    def get(self, collection: str, key: str) -> Any:
        if not self._batch:  # reads made by the batch itself are one round-trip in Redis (MGET)
            self.calls["get"] += 1
        return super().get(collection, key)

    def get_many(self, collection: str, keys: list[str]) -> Any:
        self.calls["get_many"] += 1
        self._batch = True
        try:
            return super().get_many(collection, keys)
        finally:
            self._batch = False


class TestBatchedLiveStates:
    def test_listing_routes_reads_the_documents_in_one_batch(self) -> None:
        store = CountingStore()
        live = LiveStateStore(store, FakeClock(1_790_000_000.0))
        for i in range(12):
            live.save(make_state(bus_id=f"B{i}", route_id="C1" if i % 2 else "C2"))
        store.calls.update(get=0, get_many=0)
        by_route = live.list_by_routes(["C1", "C2", "C9"])
        assert [len(by_route[r]) for r in ("C1", "C2", "C9")] == [6, 6, 0]
        assert store.calls == {"get": 0, "get_many": 1}

    def test_expired_members_are_removed_from_the_index(self) -> None:
        store = CountingStore()
        live = LiveStateStore(store, FakeClock(1_790_000_000.0))
        live.save(make_state(bus_id="X"))
        store.delete("live_state", "X")  # the document expired
        assert live.list_by_route("C5") == []
        assert store.set_members("live_route_index", "C5") == set()

    def test_redis_reads_many_documents_with_one_mget(self, redis_client: Any) -> None:
        store = RedisDocumentStore(redis_client, prefix="t")
        store.put("c", "a", {"n": 1})
        store.put("c", "b", {"n": 2})
        assert store.get_many("c", ["a", "missing", "b"]) == {"a": {"n": 1}, "b": {"n": 2}}
        assert store.get_many("c", []) == {}
