"""Travel-direction detection from the progress history of a bus.

Uses the top ``window`` entries of the reading stack (progress along the outbound trace and, when
known, the reading time).

Algorithm: **Siegel's repeated median** of the slope of progress over time. For every sample take the
median of its slopes to all the others; the slope is the median of those medians. One bad GPS fix
(a reflection between buildings, a jump to the other leg of the street) spoils the old "newest minus
oldest" difference whenever it is the newest or the oldest sample, and moves a least-squares fit, but
it cannot move the repeated median: its breakdown point is 50 %, so with the default window of four
readings any single outlier, wherever it is, is ignored. Cost O(w²) with w tiny.

The expected change over the window (slope x time span) must exceed ``min_progress_delta``; smaller
movements (a bus stopped at a stop) keep the previous direction, which avoids flapping.
"""

from __future__ import annotations

from collections.abc import Sequence

from core_service.models import Direction


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def repeated_median_slope(samples: Sequence[tuple[float, float]]) -> float | None:
    """Siegel's repeated-median slope of ``(x, y)`` samples; ``None`` when all x are equal."""
    medians: list[float] = []
    for i, (xi, yi) in enumerate(samples):
        slopes = [(yj - yi) / (xj - xi) for j, (xj, yj) in enumerate(samples) if j != i and xj != xi]
        if slopes:
            medians.append(_median(slopes))
    return _median(medians) if medians else None


class DirectionDetector:
    def __init__(self, window: int = 4, min_progress_delta: float = 0.002) -> None:
        if window < 2:
            raise ValueError("window must be at least 2")
        self._window = window
        self._min_delta = min_progress_delta

    @property
    def window(self) -> int:
        return self._window

    def detect(self, progress_newest_first: Sequence[float], previous: Direction | None) -> Direction | None:
        """Direction from progress values alone (samples assumed evenly spaced in time)."""
        samples = list(progress_newest_first[: self._window])
        count = len(samples)
        return self.detect_series([(float(count - i), p) for i, p in enumerate(samples)], previous)

    def detect_series(
        self, samples_newest_first: Sequence[tuple[float, float]], previous: Direction | None
    ) -> Direction | None:
        """Direction from ``(timestamp, progress)`` samples, newest first."""
        samples = list(samples_newest_first[: self._window])
        if len(samples) < 2:
            return previous
        slope = repeated_median_slope(samples)
        if slope is None:
            return previous
        span = samples[0][0] - samples[-1][0]
        expected_change = slope * span
        if abs(expected_change) < self._min_delta:
            return previous
        return Direction.OUTBOUND if expected_change > 0 else Direction.RETURN
