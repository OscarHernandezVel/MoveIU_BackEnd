"""Deterministic fallback analysis (no AI needed).

Used when the AI is disabled, unavailable (circuit open, provider error) or answered something
invalid. It applies simple, explainable rules to the same context the model would receive, and
returns the same :class:`AnalysisOutput` so callers cannot tell the difference except through
the ``mode`` field of the report.
"""

from __future__ import annotations

from typing import Any

from core_service.ai.schemas import AIFinding, AnalysisOutput, Anomaly, RecommendedAction
from core_service.models import FindingSeverity

SUSTAINED_SAMPLES = 3
#: Streaming statistics (``features`` in the context): a door/camera gap whose smoothed mean stays beyond
#: this many people over enough readings is sensor drift (RF-6.5), not a momentary disagreement.
DRIFT_PEOPLE = 2.5
DRIFT_MIN_SAMPLES = 20


class RuleBasedAnalyzer:
    def __init__(self, min_confidence: float = 0.6, stale_seconds: float = 60.0) -> None:
        self._min_confidence = min_confidence
        self._stale_seconds = stale_seconds

    def analyze(self, context: dict[str, Any]) -> AnalysisOutput:
        findings: list[AIFinding] = []
        buses_seen = 0
        for route in context.get("routes", []):
            route_id = route["route_id"] if route["route_id"] != "-" else None
            for bus in route.get("buses", []):
                buses_seen += 1
                findings.extend(self._analyze_bus(route_id, bus))
        summary = (
            f"Rule-based analysis of {buses_seen} buses: {len(findings)} findings."
            if findings
            else f"Rule-based analysis of {buses_seen} buses: no anomalies."
        )
        return AnalysisOutput(tuple(findings[:10]), summary)

    def _analyze_bus(self, route_id: str | None, bus: dict[str, Any]) -> list[AIFinding]:
        bus_id = bus["bus_id"]
        if bus["state"] == "no_data" or bus["seconds_since_reading"] > self._stale_seconds:
            return [
                AIFinding(
                    RecommendedAction.CHECK_BUS_OFFLINE,
                    route_id,
                    Anomaly.BUS_OFFLINE,
                    FindingSeverity.HIGH,
                    bus_id,
                    0.9,
                    f"Bus {bus_id} has not reported for {round(bus['seconds_since_reading'])} s.",
                )
            ]
        found: list[AIFinding] = []
        if bus["confidence"] < self._min_confidence:
            found.append(
                AIFinding(
                    RecommendedAction.REVIEW_CAMERA,
                    route_id,
                    Anomaly.LOW_MODEL_CONFIDENCE,
                    FindingSeverity.MEDIUM,
                    bus_id,
                    0.8,
                    f"Bus {bus_id} counts with confidence {bus['confidence']}, below {self._min_confidence}.",
                )
            )
        recent = bus.get("recent_occupancy_pct", [])
        if len(recent) >= SUSTAINED_SAMPLES and all(pct > 100 for pct in recent[:SUSTAINED_SAMPLES]):
            found.append(
                AIFinding(
                    RecommendedAction.MONITOR,
                    route_id,
                    Anomaly.SUSTAINED_OVERCROWDING,
                    FindingSeverity.MEDIUM,
                    bus_id,
                    0.7,
                    f"Bus {bus_id} stayed above 100% in its last {SUSTAINED_SAMPLES} readings.",
                )
            )
        stats = bus.get("features") or {}
        drift = stats.get("door_camera_gap_mean")
        if drift is not None and stats.get("samples", 0) >= DRIFT_MIN_SAMPLES and abs(drift) >= DRIFT_PEOPLE:
            side = "more" if drift > 0 else "fewer"
            found.append(
                AIFinding(
                    RecommendedAction.REVIEW_DOOR_SENSOR,
                    route_id,
                    Anomaly.DOOR_CAMERA_MISMATCH,
                    FindingSeverity.MEDIUM,
                    bus_id,
                    0.75,
                    f"Bus {bus_id}: cameras count on average {abs(drift)} {side} people than the door sensors "
                    f"over {stats['samples']} readings (persistent drift).",
                )
            )
        if bus["approximate_count"] and bus["occupancy_pct"] >= 90:
            found.append(
                AIFinding(
                    RecommendedAction.REVIEW_CAMERA,
                    route_id,
                    Anomaly.HIGH_DENSITY_OCCLUSION,
                    FindingSeverity.LOW,
                    bus_id,
                    0.5,
                    f"Bus {bus_id} is nearly full and only an approximate count is available (possible occlusion).",
                )
            )
        return found
