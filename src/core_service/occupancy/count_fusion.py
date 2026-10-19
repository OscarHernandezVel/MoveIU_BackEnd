"""Cross-check between the cameras and the door sensors (RF-6.5, RF-13.4).

Algorithm (stack walk): starting at the top of the reading stack, walk down while
consecutive readings keep disagreeing by more than ``threshold`` people. The
length of that run in seconds decides whether the disagreement is sustained.
Complexity: O(k) where k is the length of the disagreement run.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core_service.occupancy.reading_history import ReadingHistory


class FusionReason(str, Enum):
    CAMERAS_OK = "cameras_ok"
    DISAGREEMENT_UNDER_OBSERVATION = "disagreement_under_observation"
    DISAGREEMENT_SUSTAINED = "disagreement_sustained"
    OCCLUSION = "occlusion"
    LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class FusionDecision:
    use_door_count: bool
    reason: FusionReason
    disagreement_seconds: float = 0.0


class CountFusion:
    def __init__(
        self,
        threshold_people: int = 2,
        window_seconds: float = 30.0,
        min_confidence: float = 0.6,
        max_reading_gap_seconds: float = 20.0,
    ) -> None:
        self._threshold = threshold_people
        self._window = window_seconds
        self._min_confidence = min_confidence
        self._max_gap = max_reading_gap_seconds

    def decide(self, history: ReadingHistory) -> FusionDecision:
        current = history.current()
        if current is None:
            return FusionDecision(False, FusionReason.CAMERAS_OK)
        reading = current.reading
        if reading.approximate_count:
            return FusionDecision(True, FusionReason.OCCLUSION)
        if reading.confidence < self._min_confidence:
            return FusionDecision(True, FusionReason.LOW_CONFIDENCE)

        run_seconds = self._disagreement_run_seconds(history)
        if run_seconds is None:
            return FusionDecision(False, FusionReason.CAMERAS_OK)
        if run_seconds >= self._window:
            return FusionDecision(True, FusionReason.DISAGREEMENT_SUSTAINED, run_seconds)
        return FusionDecision(False, FusionReason.DISAGREEMENT_UNDER_OBSERVATION, run_seconds)

    def _disagreement_run_seconds(self, history: ReadingHistory) -> float | None:
        """Seconds covered by the run of disagreeing readings at the top of the stack."""
        newest_ts: float | None = None
        oldest_ts: float | None = None
        previous_ts: float | None = None
        for entry in history:  # most recent first
            reading = entry.reading
            disagrees = abs(reading.camera_total - reading.doors_total) > self._threshold
            if not disagrees:
                break
            if previous_ts is not None and previous_ts - reading.ts > self._max_gap:
                break  # the bus was silent for too long: the run is not continuous
            if newest_ts is None:
                newest_ts = reading.ts
            oldest_ts = reading.ts
            previous_ts = reading.ts
        if newest_ts is None or oldest_ts is None:
            return None
        return newest_ts - oldest_ts
