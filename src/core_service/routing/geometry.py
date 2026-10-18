"""Route path geometry: projecting GPS points onto the trace (map matching).

The path is kept in a doubly linked list of vertices (the source of truth, walked in order),
and, at construction time, three **typed arrays** (``array('d')``: contiguous doubles) are
precomputed from it:

* ``x`` / ``y``   the vertices in a local equirectangular projection, in metres;
* ``cumulative``  distance along the path at each vertex (non-decreasing).

Algorithms
----------
* **Projection** (``project``): a uniform **grid spatial index** maps each cell of
  ``cell_size_m`` x ``cell_size_m`` to the segments that come near it. A query looks at the rings of
  cells around the point, nearest ring first, and stops as soon as no unseen segment can be closer
  than the best one found. Average cost: O(1) segments per query instead of O(v) (829-vertex route:
  roughly 700 µs -> 20 µs). The answer is *exact* (same as testing every segment); far-away points
  fall back to the full scan.
* **Continuity** (``hint_progress``): routes that use the same street twice (out-and-back legs,
  loops) have several *legs* at almost the same distance from a GPS fix. Picking "the nearest" then
  makes the bus jump between legs (wrong progress, wrong direction, wrong stops remaining). With a
  hint (the bus' previous progress), the nearest point of every leg within ``ambiguity_m`` of the best
  one is considered, legs the bus cannot have reached since the hint are discarded, and the nearest
  remaining leg wins. Stops are placed the same
  way with ``project_after`` (the first leg beyond the previous stop).
* **Inverse** (``point_at``): binary search over ``cumulative``: O(log v) instead of O(v).

Distances use a local equirectangular projection around the first vertex, accurate at city scale.
"""

from __future__ import annotations

import math
from array import array
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass

from core_service.data_structures import DoublyLinkedList
from core_service.errors import ValidationError
from core_service.models.geo import EARTH_RADIUS_M, GeoPoint

DEFAULT_CELL_SIZE_M = 100.0
DEFAULT_AMBIGUITY_M = 30.0
#: How far along the path a bus may move between two consecutive fixes when a hint is used.
DEFAULT_MAX_JUMP_M = 250.0
#: Two legs whose distances to a stop differ by less than this are a tie (the earlier leg is used).
STOP_TIE_M = 5.0
#: Beyond this search radius the grid gives up and the exact full scan answers (points far off route).
MAX_GRID_RADIUS_M = 1_500.0


@dataclass(frozen=True)
class Projection:
    """Result of projecting a point onto the path."""

    progress: float
    distance_m: float


class _SegmentGrid:
    """Uniform grid: cell -> indices of the segments passing within half a cell diagonal of its centre.

    The registration is a *superset* of the cells a segment crosses, which is what makes the ring search
    exact: the closest point ``Q`` of any segment lies in some cell, and that cell's centre is at most half
    a diagonal from ``Q`` (so the segment is registered there)."""

    def __init__(self, xs: array[float], ys: array[float], cell_size_m: float) -> None:
        self.cell = cell_size_m
        self.cells: dict[tuple[int, int], list[int]] = {}
        reach = cell_size_m * math.sqrt(2.0) / 2.0
        for index in range(len(xs) - 1):
            ax, ay, bx, by = xs[index], ys[index], xs[index + 1], ys[index + 1]
            for cx in range(self._cell(min(ax, bx) - reach), self._cell(max(ax, bx) + reach) + 1):
                for cy in range(self._cell(min(ay, by) - reach), self._cell(max(ay, by) + reach) + 1):
                    centre_x, centre_y = (cx + 0.5) * cell_size_m, (cy + 0.5) * cell_size_m
                    if _point_segment_distance(centre_x, centre_y, ax, ay, bx, by)[0] <= reach:
                        self.cells.setdefault((cx, cy), []).append(index)

    def _cell(self, value: float) -> int:
        return math.floor(value / self.cell)

    def ring(self, x: float, y: float, radius: int) -> Iterable[list[int]]:
        """Segment lists of the cells at Chebyshev distance ``radius`` from the cell of (x, y)."""
        cx, cy = self._cell(x), self._cell(y)
        if radius == 0:
            found = self.cells.get((cx, cy))
            if found:
                yield found
            return
        for dx in range(-radius, radius + 1):
            for dy in (-radius, radius):
                found = self.cells.get((cx + dx, cy + dy))
                if found:
                    yield found
        for dy in range(-radius + 1, radius):
            for dx in (-radius, radius):
                found = self.cells.get((cx + dx, cy + dy))
                if found:
                    yield found


def _point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float, float]:
    """Distance from P to segment AB, the clamped parameter t and the segment length."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq > 0.0:
        t = ((px - ax) * dx + (py - ay) * dy) / length_sq
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    else:
        t = 0.0
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t, math.sqrt(length_sq)


class PathGeometry:
    def __init__(
        self,
        points: Iterable[GeoPoint],
        *,
        cell_size_m: float = DEFAULT_CELL_SIZE_M,
        ambiguity_m: float = DEFAULT_AMBIGUITY_M,
    ) -> None:
        self._points: DoublyLinkedList[GeoPoint] = DoublyLinkedList(points)
        if len(self._points) < 2:
            raise ValidationError("invalid_path", details=["a path needs at least two points"])
        if cell_size_m <= 0 or ambiguity_m < 0:
            raise ValueError("cell_size_m must be positive and ambiguity_m non-negative")
        head = self._points.head
        assert head is not None
        self._origin = head.value
        self._cos_lat = math.cos(math.radians(self._origin.lat))
        self._ambiguity = ambiguity_m
        # Two points of one street within ``best + ambiguity`` of a fix that is up to ~100 m off the trace
        # are < 100 m apart along it; legs of a street used twice are much further apart along the path.
        self._leg_separation = max(150.0, 5.0 * ambiguity_m)
        self._xs: array[float] = array("d")
        self._ys: array[float] = array("d")
        self._cumulative: array[float] = array("d")
        travelled = 0.0
        for node in self._points.nodes():  # one pass over the linked list builds the arrays
            x, y = self._to_xy(node.value)
            if len(self._xs):
                travelled += math.hypot(x - self._xs[-1], y - self._ys[-1])
            self._xs.append(x)
            self._ys.append(y)
            self._cumulative.append(travelled)
        self._length_m = travelled
        if self._length_m <= 0:
            raise ValidationError("invalid_path", details=["the path has zero length"])
        self._grid = _SegmentGrid(self._xs, self._ys, cell_size_m)
        self._max_rings = max(1, math.ceil(MAX_GRID_RADIUS_M / cell_size_m))

    # ------------------------------------------------------------------ basics
    @property
    def length_m(self) -> float:
        return self._length_m

    @property
    def points(self) -> list[GeoPoint]:
        return self._points.to_list()

    @property
    def segment_count(self) -> int:
        return len(self._xs) - 1

    def _to_xy(self, point: GeoPoint) -> tuple[float, float]:
        x = math.radians(point.lng - self._origin.lng) * EARTH_RADIUS_M * self._cos_lat
        y = math.radians(point.lat - self._origin.lat) * EARTH_RADIUS_M
        return x, y

    # -------------------------------------------------------------- projection
    def project(
        self, point: GeoPoint, hint_progress: float | None = None, max_jump_m: float = DEFAULT_MAX_JUMP_M
    ) -> Projection:
        """Point of the path nearest to ``point`` (exact).

        With ``hint_progress`` (the previous progress of the same bus), when other *legs* of the path pass
        within ``ambiguity_m`` of the nearest one, only the legs the bus can have reached (at most
        ``max_jump_m`` along the path from the hint) are eligible, and the nearest of them wins. If none is
        reachable, the leg closest along the path to the hint is used."""
        if hint_progress is None:
            distance, along = self._legs(point, 0.0)[0]
            return Projection(along / self._length_m, distance)
        hint_along = min(1.0, max(0.0, hint_progress)) * self._length_m
        legs = self._legs(point, self._ambiguity)
        reachable = [leg for leg in legs if abs(leg[1] - hint_along) <= max_jump_m]
        distance, along = reachable[0] if reachable else min(legs, key=lambda leg: abs(leg[1] - hint_along))
        return Projection(along / self._length_m, distance)

    def project_after(self, point: GeoPoint, after_progress: float) -> Projection:
        """Projection for a *sequence* of points along the path (the stops of a route, in order).

        Among the legs within ``ambiguity_m`` of the nearest one, the nearest leg strictly beyond
        ``after_progress`` is chosen (the earliest one when several are within ``STOP_TIE_M`` of it).
        If every leg lies behind it, the nearest one is returned (the caller sees the point as out of order)."""
        legs = self._legs(point, self._ambiguity)
        after_along = after_progress * self._length_m
        ahead = [leg for leg in legs if leg[1] > after_along]  # nearest first
        if not ahead:
            distance, along = legs[0]
        else:
            # The nearest leg ahead, unless an earlier one is practically as near (a stop between two lanes).
            near = [leg for leg in ahead if leg[0] <= ahead[0][0] + STOP_TIE_M]
            distance, along = min(near, key=lambda leg: leg[1])
        return Projection(along / self._length_m, distance)

    def _legs(self, point: GeoPoint, slack: float) -> list[tuple[float, float]]:
        """Nearest point of each *leg* of the path within ``best + slack`` of ``point``, nearest first.

        Neighbouring segments of the same street all come close to the point; they are one leg. A candidate
        opens a new leg only when it is more than ``leg_separation_m`` along the path from every leg found."""
        px, py = self._to_xy(point)
        candidates = self._grid_candidates(px, py, slack)
        if candidates is None:
            candidates = self._scan(px, py, range(self.segment_count), slack)
        candidates.sort()
        legs = [candidates[0]]
        if slack == 0.0:
            return legs
        limit = candidates[0][0] + slack
        for distance, along in candidates[1:]:
            if distance > limit:
                break
            if all(abs(along - leg_along) > self._leg_separation for _, leg_along in legs):
                legs.append((distance, along))
        return legs

    def _grid_candidates(self, px: float, py: float, slack: float) -> list[tuple[float, float]] | None:
        """(distance, along) of the segments near P, or ``None`` when the point is too far for the grid."""
        seen: set[int] = set()
        found: list[tuple[float, float]] = []
        best = math.inf
        for radius in range(self._max_rings + 1):
            fresh: list[int] = []
            for segments in self._grid.ring(px, py, radius):
                for index in segments:
                    if index not in seen:
                        seen.add(index)
                        fresh.append(index)
            if fresh:
                batch = self._scan(px, py, fresh, 0.0)
                found.extend(batch)
                best = min(best, min(d for d, _ in batch))
            # Unseen segments are at least radius * cell away: stop once they cannot beat best + slack.
            if found and best + slack <= radius * self._grid.cell:
                return found
        return None

    def _scan(self, px: float, py: float, indices: Iterable[int], slack: float) -> list[tuple[float, float]]:
        xs, ys, cumulative = self._xs, self._ys, self._cumulative
        results: list[tuple[float, float]] = []
        for index in indices:
            distance, t, length = _point_segment_distance(px, py, xs[index], ys[index], xs[index + 1], ys[index + 1])
            results.append((distance, cumulative[index] + t * length))
        if slack == 0.0 or not results:
            return results
        best = min(d for d, _ in results)
        return [item for item in results if item[0] <= best + slack]

    # ----------------------------------------------------------------- inverse
    def point_at(self, progress: float) -> GeoPoint:
        """Coordinate at ``progress`` (0-1) along the path; the inverse of :meth:`project`. O(log v)."""
        target = max(0.0, min(1.0, progress)) * self._length_m
        index = min(max(bisect_right(self._cumulative, target) - 1, 0), self.segment_count - 1)
        start_along, end_along = self._cumulative[index], self._cumulative[index + 1]
        span = end_along - start_along
        t = 0.0 if span <= 0 else min(1.0, (target - start_along) / span)
        x = self._xs[index] + (self._xs[index + 1] - self._xs[index]) * t
        y = self._ys[index] + (self._ys[index + 1] - self._ys[index]) * t
        return self._to_geo(x, y)

    def _to_geo(self, x: float, y: float) -> GeoPoint:
        lat = self._origin.lat + math.degrees(y / EARTH_RADIUS_M)
        lng = self._origin.lng + math.degrees(x / (EARTH_RADIUS_M * self._cos_lat))
        return GeoPoint(lat, lng)
