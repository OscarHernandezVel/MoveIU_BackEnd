"""The read-only tool set offered to the AI."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from core_service.ai.features import FleetFeatureService
from core_service.ai.tools import Tool, ToolRegistry
from core_service.audit import AuditService
from core_service.persistence.relational import FindingRepository, TelemetryRepository
from core_service.services.live_state_store import LiveStateStore

_BUS_ID = {"type": "string", "maxLength": 20}
_ROUTE_ID = {"type": "string", "maxLength": 10}


def _state_view(state: Any) -> dict[str, Any]:
    return {
        "bus_id": state.bus_id,
        "route_id": state.route_id,
        "direction": state.direction.value if state.direction else None,
        "occupancy_pct": state.occupancy_pct,
        "state": state.state.value,
        "seated": state.seated,
        "standing": state.standing,
        "people_on_board": state.people_on_board,
        "confidence": state.confidence,
        "approximate_count": state.approximate_count,
        "off_route": state.off_route,
        "reading_ts": state.reading_ts,
    }


def build_default_tools(
    live_states: LiveStateStore,
    telemetry: TelemetryRepository,
    findings: FindingRepository,
    audits: AuditService,
    features: FleetFeatureService | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    def get_bus_state(args: dict[str, Any]) -> Any:
        state = live_states.get(args["bus_id"])
        return {"found": False} if state is None else {"found": True, **_state_view(state)}

    def list_route_buses(args: dict[str, Any]) -> Any:
        return [_state_view(s) for s in live_states.list_by_route(args["route_id"])]

    def get_recent_readings(args: dict[str, Any]) -> Any:
        rows = telemetry.recent(args["bus_id"], int(args.get("limit", 10)))
        keys = ("ts", "seated", "standing", "doors_total", "confidence", "occupancy_pct", "model_version")
        return [{k: row[k] for k in keys} for row in rows]

    def get_route_trend(args: dict[str, Any]) -> Any:
        minutes = int(args.get("minutes", 30))
        now = live_states.now()
        rows = telemetry.route_trend(args["route_id"], now - minutes * 60.0, max(1, minutes // 5))
        return [
            {
                "minutes_ago": round(max(0.0, now - float(row["bucket_ts"])) / 60.0),
                "avg_occupancy_pct": round(float(row["avg_occupancy_pct"] or 0.0), 1),
                "max_occupancy_pct": int(row["max_occupancy_pct"] or 0),
                "buses": int(row["buses"]),
            }
            for row in rows
        ]

    def get_bus_trend(args: dict[str, Any]) -> Any:
        minutes = int(args.get("minutes", 30))
        now = live_states.now()
        rows = telemetry.bus_trend(args["bus_id"], now - minutes * 60.0, max(1, minutes // 5))
        return [
            {
                "minutes_ago": round(max(0.0, now - float(row["bucket_ts"])) / 60.0),
                "avg_occupancy_pct": round(float(row["avg_occupancy_pct"] or 0.0), 1),
                "max_occupancy_pct": int(row["max_occupancy_pct"] or 0),
                "avg_confidence": round(float(row["avg_confidence"] or 0.0), 3),
                "approximate_samples": int(row["approximate_samples"]),
            }
            for row in rows
        ]

    def get_audit_metrics(args: dict[str, Any]) -> Any:
        report = audits.metrics(
            date.fromisoformat(args["start"]), date.fromisoformat(args["end"]), args.get("route_id")
        )
        return asdict(report)

    def get_open_findings(args: dict[str, Any]) -> Any:
        return [
            {
                "bus_id": f.bus_id,
                "kind": f.kind.value,
                "severity": f.severity.value,
                "description": f.description[:200],
                "created_at": f.created_at,
            }
            for f in findings.list_recent(int(args.get("limit", 10)), status="open")
        ]

    registry.register(
        Tool(
            "get_bus_state",
            "Current occupancy state of one bus.",
            {
                "type": "object",
                "required": ["bus_id"],
                "properties": {"bus_id": _BUS_ID},
                "additionalProperties": False,
            },
            get_bus_state,
        )
    )
    registry.register(
        Tool(
            "list_route_buses",
            "Current occupancy state of every bus serving a route.",
            {
                "type": "object",
                "required": ["route_id"],
                "properties": {"route_id": _ROUTE_ID},
                "additionalProperties": False,
            },
            list_route_buses,
        )
    )
    registry.register(
        Tool(
            "get_recent_readings",
            "Latest stored readings (newest first) of a bus: counts, confidence and occupancy.",
            {
                "type": "object",
                "required": ["bus_id"],
                "properties": {"bus_id": _BUS_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                "additionalProperties": False,
            },
            get_recent_readings,
        )
    )
    _minutes = {"type": "integer", "minimum": 5, "maximum": 180}
    registry.register(
        Tool(
            "get_route_trend",
            "Occupancy of a whole route in 5-minute buckets (newest first): how it is evolving right now.",
            {
                "type": "object",
                "required": ["route_id"],
                "properties": {"route_id": _ROUTE_ID, "minutes": _minutes},
                "additionalProperties": False,
            },
            get_route_trend,
        )
    )
    registry.register(
        Tool(
            "get_bus_trend",
            "Occupancy and counting confidence of one bus in 5-minute buckets (newest first).",
            {
                "type": "object",
                "required": ["bus_id"],
                "properties": {"bus_id": _BUS_ID, "minutes": _minutes},
                "additionalProperties": False,
            },
            get_bus_trend,
        )
    )
    registry.register(
        Tool(
            "get_audit_metrics",
            "Counting-accuracy metrics from manual audits between two ISO dates (YYYY-MM-DD).",
            {
                "type": "object",
                "required": ["start", "end"],
                "properties": {
                    "start": {"type": "string", "maxLength": 10},
                    "end": {"type": "string", "maxLength": 10},
                    "route_id": _ROUTE_ID,
                },
                "additionalProperties": False,
            },
            get_audit_metrics,
        )
    )
    registry.register(
        Tool(
            "get_open_findings",
            "Most recent technical findings that nobody has reviewed yet.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30}},
                "additionalProperties": False,
            },
            get_open_findings,
        )
    )
    if features is not None:

        def get_route_features(args: dict[str, Any]) -> Any:
            return features.route_features(args["route_id"])

        registry.register(
            Tool(
                "get_route_features",
                "Streaming statistics of a route: spacing between buses per direction (mean gap, variation, "
                "bunching, load imbalance) and, per bus, smoothed occupancy, door/camera gap baseline and z-score, "
                "share of low-confidence readings, reporting interval and speed along the route.",
                {
                    "type": "object",
                    "required": ["route_id"],
                    "properties": {"route_id": _ROUTE_ID},
                    "additionalProperties": False,
                },
                get_route_features,
            )
        )
    return registry
