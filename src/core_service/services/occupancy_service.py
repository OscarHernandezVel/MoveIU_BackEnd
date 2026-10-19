"""Occupancy pipeline: one reading in, one live state out.

Steps for a new reading (all under a per-bus lock, coordinated by a :class:`Saga`):

1. push it on the bus's reading stack           (undo: pop it)
2. fuse cameras and door sensors, compute the % (pure)
3. detect direction from the stack's progress
4. save the live state                          (undo: restore the previous one)
5. store the telemetry row (idempotent)
6. publish ``OccupancyUpdated`` (alerts, findings, ... react to it)

A failure in 1-5 rolls back in reverse order, so memory, document store and SQL never disagree.
Late or duplicate readings (offline bursts, retries) are only stored: they never rewrite the
live state, which always reflects the newest reading.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from core_service.clock import Clock
from core_service.config import OccupancySettings
from core_service.data_structures import Array
from core_service.errors import DomainError, ValidationError
from core_service.metrics import MetricsRegistry
from core_service.models import Direction, LiveBusState, Reading
from core_service.occupancy import CountFusion, FusionReason, OccupancyCalculator, ReadingHistory, ReadingHistoryStore
from core_service.persistence.relational import TelemetryRepository
from core_service.routing import DirectionDetector, RouteCatalog
from core_service.services.event_bus import EventBus
from core_service.services.events import OccupancyUpdated
from core_service.services.keyed_locks import KeyedLocks, LockManager
from core_service.services.live_state_store import LiveStateStore
from core_service.services.saga import Saga

log = logging.getLogger(__name__)


class ProcessStatus(str, Enum):
    APPLIED = "applied"
    LATE_STORED = "late_stored"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class ProcessOutcome:
    status: ProcessStatus
    state: LiveBusState | None = None
    fusion_reason: FusionReason | None = None


@dataclass(frozen=True)
class BatchItemResult:
    index: int
    outcome: ProcessOutcome | None = None
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not None


class OccupancyService:
    def __init__(
        self,
        catalog: RouteCatalog,
        histories: ReadingHistoryStore,
        live_states: LiveStateStore,
        telemetry: TelemetryRepository,
        events: EventBus,
        calculator: OccupancyCalculator,
        fusion: CountFusion,
        direction_detector: DirectionDetector,
        clock: Clock,
        settings: OccupancySettings,
        metrics: MetricsRegistry,
        locks: LockManager | None = None,
    ) -> None:
        self._catalog = catalog
        self._histories = histories
        self._live_states = live_states
        self._telemetry = telemetry
        self._events = events
        self._calculator = calculator
        self._fusion = fusion
        self._detector = direction_detector
        self._clock = clock
        self._settings = settings
        self._metrics = metrics
        self._locks = locks or KeyedLocks()

    # ------------------------------------------------------------------ public
    def process(self, reading: Reading) -> ProcessOutcome:
        started = time.perf_counter()
        bus = self._catalog.require_bus(reading.bus_id)
        self._check_timestamp(reading)
        with self._locks.acquire(bus.bus_id):
            previous_state = self._live_states.get(bus.bus_id)
            history = self._histories.sync(bus.bus_id, previous_state.reading_ts if previous_state else None)
            newest_ts = self._newest_known_ts(history, previous_state)
            if newest_ts is not None and reading.ts == newest_ts:
                self._metrics.increment("readings", status=ProcessStatus.DUPLICATE.value)
                return ProcessOutcome(ProcessStatus.DUPLICATE)
            if newest_ts is not None and reading.ts < newest_ts:
                result = self._calculator.calculate(bus.capacity, reading)
                self._telemetry.save(reading, result.percentage)
                self._metrics.increment("readings", status=ProcessStatus.LATE_STORED.value)
                return ProcessOutcome(ProcessStatus.LATE_STORED)

            track = self._catalog.find_track(bus.route_id)
            progress: float | None = None
            off_route = False
            at_terminal = False
            if track is not None:
                hint = self._continuity_hint(previous_state, reading)
                projection = (
                    track.path.project(reading.location)
                    if hint is None or previous_state is None
                    else track.path.project(
                        reading.location, hint, self._max_jump_m(reading.ts - previous_state.reading_ts)
                    )
                )
                off_route = projection.distance_m > self._settings.off_route_tolerance_m
                if not off_route:
                    progress = projection.progress
                    at_terminal = (
                        track.distance_from_terminal_m(progress) <= self._settings.terminal_radius_m
                        and (reading.speed_kmh or 0.0) <= self._settings.terminal_max_speed_kmh
                    )

            with Saga() as saga:
                saga.step(lambda: history.push(reading, progress), history.discard_latest)
                decision = self._fusion.decide(history)
                result = self._calculator.calculate(bus.capacity, reading, decision.use_door_count)
                direction = self._direction(history, previous_state, progress, at_terminal)
                state = LiveBusState(
                    bus_id=bus.bus_id,
                    route_id=bus.route_id,
                    direction=direction,
                    lat=reading.location.lat,
                    lng=reading.location.lng,
                    speed_kmh=reading.speed_kmh,
                    seated=result.seated,
                    standing=result.standing,
                    standing_out_of_position=result.standing_out_of_position,
                    people_on_board=result.people_on_board,
                    occupancy_pct=result.percentage,
                    state=result.state,
                    free_seats=result.free_seats,
                    free_standing=result.free_standing,
                    wheelchair_free=result.wheelchair_free,
                    confidence=result.confidence,
                    approximate_count=result.approximate_count,
                    outbound_progress=progress,
                    off_route=off_route,
                    model_version=reading.model_version,
                    reading_ts=reading.ts,
                    updated_at=self._clock.now(),
                )
                saga.step(
                    lambda: self._live_states.save(state),
                    (lambda: self._live_states.save(previous_state))
                    if previous_state
                    else (lambda: self._live_states.delete(bus.bus_id)),
                )
                self._telemetry.save(reading, result.percentage)
            if at_terminal:
                dropped = history.reset()  # people get off at the terminal: door counters start over
                log.debug("Bus %s at terminal: %s history entries dropped", bus.bus_id, dropped)
            self._histories.persist(bus.bus_id)
        self._metrics.increment("readings", status=ProcessStatus.APPLIED.value)
        self._metrics.observe("occupancy_process_ms", (time.perf_counter() - started) * 1000.0)
        self._events.publish(OccupancyUpdated(state, previous_state, decision.reason, reading))
        return ProcessOutcome(ProcessStatus.APPLIED, state, decision.reason)

    def process_batch(self, readings: Sequence[Reading]) -> list[BatchItemResult]:
        """Process an offline burst in timestamp order; each reading succeeds or fails on its own."""
        indexed: Array[tuple[int, Reading]] = Array.from_iterable(enumerate(readings))
        ordered = indexed.merge_sorted(key=lambda item: (item[1].bus_id, item[1].ts))
        results: list[BatchItemResult] = []
        for index, reading in ordered:
            try:
                results.append(BatchItemResult(index, self.process(reading)))
            except DomainError as error:
                results.append(BatchItemResult(index, error_code=error.code))
        return sorted(results, key=lambda item: item.index)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _newest_known_ts(history: ReadingHistory, live_state: LiveBusState | None) -> float | None:
        """Timestamp of the newest reading already accepted for the bus.

        Both sources are consulted on purpose: the in-memory history is emptied by a terminal reset and
        lost on restart, while the live state (shared, with a TTL) survives both. Trusting only the
        history would let a late or duplicated reading overwrite a newer live state."""
        candidates = [entry.reading.ts for entry in (history.latest(),) if entry is not None]
        if live_state is not None:
            candidates.append(live_state.reading_ts)
        return max(candidates) if candidates else None

    def _check_timestamp(self, reading: Reading) -> None:
        now = self._clock.now()
        if reading.ts > now + self._settings.max_future_skew_seconds:
            raise ValidationError("invalid_telemetry", details=["timestamp is in the future"])
        if reading.ts < now - self._settings.max_reading_age_seconds:
            raise ValidationError("invalid_telemetry", details=["timestamp is too old"])

    def _direction(
        self,
        history: ReadingHistory,
        previous: LiveBusState | None,
        progress: float | None,
        at_terminal: bool,
    ) -> Direction | None:
        previous_direction = previous.direction if previous else None
        if progress is None:
            return previous_direction  # off route or no route: keep what we knew
        if at_terminal:
            return Direction.OUTBOUND if progress < 0.5 else Direction.RETURN  # heading out again
        samples = [
            (e.reading.ts, e.outbound_progress)
            for e in history.recent(self._detector.window)
            if e.outbound_progress is not None
        ]
        return self._detector.detect_series(samples, previous_direction)

    @staticmethod
    def _max_jump_m(elapsed_seconds: float) -> float:
        """How far along the route a bus can have moved: 25 m/s (90 km/h) plus 60 m of GPS error."""
        return 60.0 + 25.0 * max(0.0, elapsed_seconds)

    def _continuity_hint(self, previous: LiveBusState | None, reading: Reading) -> float | None:
        """Previous progress of the bus, used to keep it on the right leg of streets the route uses twice.
        Only a recent position is trusted: after a long silence the bus may be anywhere."""
        if (
            previous is None
            or previous.outbound_progress is None
            or previous.off_route
            or reading.ts - previous.reading_ts > self._settings.continuity_max_gap_seconds
        ):
            return None
        return previous.outbound_progress
