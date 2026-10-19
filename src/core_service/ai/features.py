"""Online feature engineering for the AI module (PB-11): numeric, aggregated, updated per reading.

The orchestrator (RF-11.1) must receive *aggregated numbers*, never raw streams, and must run outside
the real-time path (RF-11.3). This module keeps, for every bus, a handful of **streaming statistics**
updated in O(1) per reading, and computes per-route **headway** indicators on demand, so the context
the model sees already says "this bus drifts", "these two buses travel together", instead of leaving
the model to infer it from rows.

Per bus (``BusFeatures``, stored in the shared document store, so every replica sees the same values):

* ``occupancy_ewma``     time-aware exponentially weighted mean of the occupancy % (tau = 5 min);
* ``gap_mean/gap_std``   exponentially weighted mean and deviation of *cameras minus door sensors*
                         (people); a persistent non-zero mean is sensor drift, not noise (RF-6.5);
* ``gap_z``              z-score of the newest gap against that baseline (a sudden disagreement);
* ``low_confidence_ratio`` / ``approximate_ratio``  share of recent readings below the confidence
                         threshold / with occlusion (RF-13.4);
* ``report_interval_s``  smoothed time between readings (a degrading 4G link shows up here first);
* ``speed_along_mps``    smoothed speed *along the route* (from the progress), independent of GPS speed.

Time-aware smoothing: with irregular readings a fixed alpha would weigh a burst of 1-second readings
like 30 seconds of history. ``alpha = 1 - exp(-dt / tau)`` makes the weight depend on elapsed time.
The exponentially weighted variance uses the incremental form (West, 1979): no history is stored.

Per route and direction (``headway``): buses ordered by progress along their direction (O(n log n)),
gaps between consecutive buses in metres, coefficient of variation of the gaps (0 = perfectly even
service), *bunching* pairs (gap below max(150 m, 25 % of the mean gap)) and *load imbalance* (the bus
ahead crowded while the one right behind has seats: the classic symptom of bunching).

Concurrency: statistics are written after each reading; two replicas processing readings of the same
bus at the same instant may overwrite each other's update (last writer wins). They are statistics for
analysis, never used for occupancy or alerts, so an occasional lost update is acceptable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from core_service.clock import Clock
from core_service.config import OccupancySettings
from core_service.models import Direction, LiveBusState, OccupancyState
from core_service.persistence.documents import DocumentStore
from core_service.routing import RouteCatalog, RouteTrack
from core_service.services.events import OccupancyUpdated
from core_service.services.live_state_store import LiveStateStore

COLLECTION = "bus_features"
OCCUPANCY_TAU_S = 300.0
GAP_TAU_S = 600.0
RATIO_TAU_S = 600.0
INTERVAL_TAU_S = 120.0
SPEED_TAU_S = 60.0
#: Readings needed before a z-score is reported (the baseline must exist first).
Z_WARMUP_SAMPLES = 10
MIN_STD_PEOPLE = 0.5
BUNCHING_MIN_GAP_M = 150.0
BUNCHING_SHARE_OF_MEAN = 0.25
IMBALANCE_POINTS = 40


def time_alpha(elapsed_seconds: float, tau_seconds: float) -> float:
    """Smoothing factor for a reading ``elapsed_seconds`` after the previous one."""
    if elapsed_seconds <= 0:
        return 0.0
    return 1.0 - math.exp(-elapsed_seconds / tau_seconds)


def ew_update(mean: float, variance: float, value: float, alpha: float) -> tuple[float, float]:
    """Exponentially weighted mean and variance after ``value`` (incremental, O(1))."""
    diff = value - mean
    increment = alpha * diff
    return mean + increment, (1.0 - alpha) * (variance + diff * increment)


@dataclass
class BusFeatures:
    bus_id: str
    route_id: str | None
    last_ts: float
    samples: int
    occupancy_ewma: float
    gap_mean: float
    gap_var: float
    gap_last: int
    gap_z: float | None
    low_confidence_ratio: float
    approximate_ratio: float
    report_interval_s: float | None
    speed_along_mps: float | None
    last_progress: float | None

    @property
    def gap_std(self) -> float:
        return math.sqrt(max(0.0, self.gap_var))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BusFeatures:
        return cls(**{name: data.get(name) for name in cls.__dataclass_fields__})  # type: ignore[arg-type]

    def view(self, now: float) -> dict[str, Any]:
        """Compact, rounded numbers for the AI context (no identifiers besides the bus)."""
        return {
            "samples": self.samples,
            "occupancy_ewma": round(self.occupancy_ewma, 1),
            "door_camera_gap_mean": round(self.gap_mean, 2),
            "door_camera_gap_std": round(self.gap_std, 2),
            "door_camera_gap_z": None if self.gap_z is None else round(self.gap_z, 2),
            "low_confidence_ratio": round(self.low_confidence_ratio, 3),
            "approximate_ratio": round(self.approximate_ratio, 3),
            "report_interval_s": None if self.report_interval_s is None else round(self.report_interval_s, 1),
            "speed_along_kmh": None if self.speed_along_mps is None else round(self.speed_along_mps * 3.6, 1),
            "seconds_since_update": round(max(0.0, now - self.last_ts), 1),
        }


class FleetFeatureService:
    def __init__(
        self,
        store: DocumentStore,
        catalog: RouteCatalog,
        live_states: LiveStateStore,
        clock: Clock,
        settings: OccupancySettings,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._live_states = live_states
        self._clock = clock
        self._settings = settings
        self._ttl = ttl_seconds

    # ---------------------------------------------------------- per reading
    def on_occupancy_updated(self, event: OccupancyUpdated) -> None:
        reading = event.reading
        if reading is None:
            return
        state = event.state
        document = self._store.get(COLLECTION, state.bus_id)
        previous = None if document is None else BusFeatures.from_dict(document)
        features = self.update(previous, state, reading.camera_total - reading.doors_total, reading.ts)
        self._store.put(COLLECTION, state.bus_id, features.to_dict(), ttl_seconds=self._ttl)

    def update(self, previous: BusFeatures | None, state: LiveBusState, gap: int, ts: float) -> BusFeatures:
        """Pure O(1) update of the statistics of one bus with its newest reading."""
        low_confidence = 1.0 if state.confidence < self._settings.min_confidence else 0.0
        approximate = 1.0 if state.approximate_count else 0.0
        if previous is None or ts <= previous.last_ts:
            if previous is not None:
                return previous  # late or repeated reading: statistics only move forward in time
            return BusFeatures(
                state.bus_id,
                state.route_id,
                ts,
                1,
                float(state.occupancy_pct),
                float(gap),
                0.0,
                gap,
                None,
                low_confidence,
                approximate,
                None,
                None,
                state.outbound_progress,
            )
        elapsed = ts - previous.last_ts
        z = None
        if previous.samples >= Z_WARMUP_SAMPLES:
            z = (gap - previous.gap_mean) / max(previous.gap_std, MIN_STD_PEOPLE)
        gap_mean, gap_var = ew_update(previous.gap_mean, previous.gap_var, float(gap), time_alpha(elapsed, GAP_TAU_S))
        occupancy, _ = ew_update(
            previous.occupancy_ewma, 0.0, float(state.occupancy_pct), time_alpha(elapsed, OCCUPANCY_TAU_S)
        )
        ratio_alpha = time_alpha(elapsed, RATIO_TAU_S)
        interval = (
            elapsed
            if previous.report_interval_s is None
            else ew_update(previous.report_interval_s, 0.0, elapsed, time_alpha(elapsed, INTERVAL_TAU_S))[0]
        )
        speed = previous.speed_along_mps
        track = self._catalog.find_track(state.route_id)
        if (
            track is not None
            and state.outbound_progress is not None
            and previous.last_progress is not None
            and state.route_id == previous.route_id
        ):
            metres = abs(state.outbound_progress - previous.last_progress) * track.path.length_m
            instant = metres / elapsed
            speed = instant if speed is None else ew_update(speed, 0.0, instant, time_alpha(elapsed, SPEED_TAU_S))[0]
        return BusFeatures(
            bus_id=state.bus_id,
            route_id=state.route_id,
            last_ts=ts,
            samples=previous.samples + 1,
            occupancy_ewma=occupancy,
            gap_mean=gap_mean,
            gap_var=gap_var,
            gap_last=gap,
            gap_z=z,
            low_confidence_ratio=ew_update(previous.low_confidence_ratio, 0.0, low_confidence, ratio_alpha)[0],
            approximate_ratio=ew_update(previous.approximate_ratio, 0.0, approximate, ratio_alpha)[0],
            report_interval_s=interval,
            speed_along_mps=speed,
            last_progress=state.outbound_progress,
        )

    # --------------------------------------------------------------- queries
    def bus_features(self, bus_ids: list[str]) -> dict[str, BusFeatures]:
        documents = self._store.get_many(COLLECTION, bus_ids)
        return {bus_id: BusFeatures.from_dict(document) for bus_id, document in documents.items()}

    def route_features(self, route_id: str, states: list[LiveBusState] | None = None) -> dict[str, Any]:
        """Headway per direction plus the per-bus statistics of a route (for the AI and the staff stream)."""
        track = self._catalog.require_track(route_id)
        now = self._clock.now()
        states = self._live_states.list_by_route(route_id) if states is None else states
        features = self.bus_features([s.bus_id for s in states])
        return {
            "route_id": route_id,
            "generated_at": now,
            "headway": {
                direction.value: headway_report(track, states, direction, now, self._settings.no_data_after_seconds)
                for direction in (Direction.OUTBOUND, Direction.RETURN)
            },
            "buses": {bus_id: item.view(now) for bus_id, item in sorted(features.items())},
        }


def headway_report(
    track: RouteTrack, states: list[LiveBusState], direction: Direction, now: float, stale_after_seconds: float
) -> dict[str, Any]:
    """Spacing of the buses of one route and direction, front to back. O(n log n)."""
    moving = [
        s
        for s in states
        if s.direction is direction
        and s.outbound_progress is not None
        and not s.off_route
        and s.state is not OccupancyState.NO_DATA
        and now - s.reading_ts <= stale_after_seconds
    ]
    ordered = sorted(moving, key=lambda s: -track.travel_progress(direction, s.outbound_progress or 0.0))
    gaps: list[dict[str, Any]] = []
    for ahead, behind in zip(ordered, ordered[1:], strict=False):
        metres = (
            track.travel_progress(direction, ahead.outbound_progress or 0.0)
            - track.travel_progress(direction, behind.outbound_progress or 0.0)
        ) * track.path.length_m
        gaps.append({"ahead": ahead.bus_id, "behind": behind.bus_id, "gap_m": round(metres), "_a": ahead, "_b": behind})
    report: dict[str, Any] = {"buses": len(ordered), "gaps_m": [g["gap_m"] for g in gaps]}
    if not gaps:
        report.update(mean_gap_m=None, gap_cv=None, bunching=[], load_imbalance=[])
        return report
    values = [float(g["gap_m"]) for g in gaps]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    threshold = max(BUNCHING_MIN_GAP_M, BUNCHING_SHARE_OF_MEAN * mean)
    report["mean_gap_m"] = round(mean)
    report["gap_cv"] = round(std / mean, 3) if mean > 0 else None
    report["bunching"] = [
        {"ahead": g["ahead"], "behind": g["behind"], "gap_m": g["gap_m"]} for g in gaps if g["gap_m"] < threshold
    ]
    report["load_imbalance"] = [
        {
            "ahead": g["ahead"],
            "behind": g["behind"],
            "ahead_pct": g["_a"].occupancy_pct,
            "behind_pct": g["_b"].occupancy_pct,
        }
        for g in gaps
        if g["_a"].state.is_crowded
        and g["_b"].state is OccupancyState.AVAILABLE_SEATS
        and g["_a"].occupancy_pct - g["_b"].occupancy_pct >= IMBALANCE_POINTS
    ]
    return report
