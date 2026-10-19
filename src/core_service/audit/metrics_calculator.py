"""Counting-accuracy metrics from audit samples (PB-14, RF-14.3 and RF-14.4).

Error of a sample = |model count - manual count|; the percentage is relative to the manual
count. A bus is flagged for recalibration when two audits in a row exceed the threshold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from core_service.data_structures import Array


@dataclass(frozen=True)
class AuditPoint:
    """One audit sample prepared for the calculation."""

    bus_id: str
    route_id: str | None
    local_time: datetime
    model_version: str
    manual_total: int
    model_total: int

    @property
    def absolute_error(self) -> int:
        return abs(self.model_total - self.manual_total)

    @property
    def percentage_error(self) -> float:
        return self.absolute_error * 100.0 / max(1, self.manual_total)


@dataclass(frozen=True)
class GroupMetrics:
    key: str
    samples: int
    mean_absolute_error: float
    mean_percentage_error: float


@dataclass(frozen=True)
class MetricsReport:
    by_bus: list[GroupMetrics]
    by_route: list[GroupMetrics]
    by_daypart: list[GroupMetrics]
    by_model_version: list[GroupMetrics]
    buses_to_recalibrate: list[str]
    overall_percentage_error: float
    total_samples: int


def daypart(moment: datetime) -> str:
    """Peak hours: 06:00-08:59 and 17:00-19:59 (local time); everything else is valley."""
    hour = moment.hour
    return "peak" if (6 <= hour < 9) or (17 <= hour < 20) else "valley"


def _round(value: float) -> float:
    return round(value, 2)


class AuditMetricsCalculator:
    def __init__(self, threshold_percentage: float = 5.0) -> None:
        self._threshold = threshold_percentage

    def calculate(self, points: list[AuditPoint]) -> MetricsReport:
        total = len(points)
        overall = sum(p.percentage_error for p in points) / total if total else 0.0
        return MetricsReport(
            by_bus=self._group(points, lambda p: p.bus_id),
            by_route=self._group(points, lambda p: p.route_id or "-"),
            by_daypart=self._group(points, lambda p: daypart(p.local_time)),
            by_model_version=self._group(points, lambda p: p.model_version),
            buses_to_recalibrate=self._buses_to_recalibrate(points),
            overall_percentage_error=_round(overall),
            total_samples=total,
        )

    def _group(self, points: list[AuditPoint], key: Callable[[AuditPoint], str]) -> list[GroupMetrics]:
        buckets: dict[str, list[AuditPoint]] = {}
        for point in points:
            buckets.setdefault(key(point), []).append(point)
        groups = Array.from_iterable(
            GroupMetrics(
                key=name,
                samples=len(items),
                mean_absolute_error=_round(sum(p.absolute_error for p in items) / len(items)),
                mean_percentage_error=_round(sum(p.percentage_error for p in items) / len(items)),
            )
            for name, items in buckets.items()
        )
        # Worst first; the stable merge sort keeps ties in first-seen order.
        return groups.merge_sorted(key=lambda g: g.mean_percentage_error, reverse=True).to_list()

    def _buses_to_recalibrate(self, points: list[AuditPoint]) -> list[str]:
        per_bus: dict[str, dict[date, list[float]]] = {}
        for point in points:
            per_bus.setdefault(point.bus_id, {}).setdefault(point.local_time.date(), []).append(point.percentage_error)
        flagged: list[str] = []
        for bus_id in sorted(per_bus):
            days = sorted(per_bus[bus_id])
            means = [sum(per_bus[bus_id][d]) / len(per_bus[bus_id][d]) for d in days]
            if any(a > self._threshold and b > self._threshold for a, b in zip(means, means[1:], strict=False)):
                flagged.append(bus_id)
        return flagged
