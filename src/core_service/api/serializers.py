"""Turns domain objects into the JSON the clients receive (localised, never colour-only)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core_service.alerts import InboxItem
from core_service.audit import MetricsReport
from core_service.comparator import NextBus
from core_service.i18n import MessageCatalog
from core_service.models import AuditSample, Finding, LiveBusState, ProximityAlert
from core_service.routing import RouteTrack


class Serializer:
    def __init__(self, messages: MessageCatalog) -> None:
        self._messages = messages

    def live_state(self, state: LiveBusState, locale: str) -> dict[str, Any]:
        return {
            "bus_id": state.bus_id,
            "route_id": state.route_id,
            "direction": state.direction.value if state.direction else None,
            "location": {"lat": state.lat, "lng": state.lng},
            "occupancy": {
                "percentage": state.occupancy_pct,
                "state": state.state.value,
                "label": self._messages.render(f"occupancy.state.{state.state.value}", locale),
                "icon": state.state.icon,
                "seated": state.seated,
                "standing": state.standing,
                "free_seats": state.free_seats,
                "free_standing": state.free_standing,
                "wheelchair_free": state.wheelchair_free,
                "people_on_board": state.people_on_board,
                "approximate": state.approximate_count,
                "confidence": round(state.confidence, 2),
            },
            "off_route": state.off_route,
            "updated_at": state.updated_at,
        }

    def next_bus(self, item: NextBus, locale: str) -> dict[str, Any]:
        return {
            "bus": self.live_state(item.state, locale),
            "stops_remaining": item.stops_remaining,
            "distance_m": item.distance_m,
            "recommended": item.recommended,
        }

    @staticmethod
    def route(track: RouteTrack, include_path: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "route_id": track.route_id,
            "name": track.name,
            "stops": [
                {
                    "stop_id": s.stop.stop_id,
                    "name": s.stop.name,
                    "order": s.order,
                    "location": {"lat": s.stop.location.lat, "lng": s.stop.location.lng},
                }
                for s in track.stops()
            ],
        }
        if include_path:
            data["path"] = [{"lat": p.lat, "lng": p.lng} for p in track.path.points]
        return data

    @staticmethod
    def alert(alert: ProximityAlert) -> dict[str, Any]:
        return alert.to_dict()

    @staticmethod
    def inbox_item(item: InboxItem) -> dict[str, Any]:
        return {**item.alert.to_dict(), "read": item.read}

    @staticmethod
    def finding(finding: Finding) -> dict[str, Any]:
        return finding.to_dict()

    @staticmethod
    def audit_sample(sample: AuditSample) -> dict[str, Any]:
        return {
            "sample_id": sample.sample_id,
            "bus_id": sample.bus_id,
            "stop_id": sample.stop_id,
            "manual": {
                "seated": sample.manual_seated,
                "standing": sample.manual_standing,
                "total": sample.manual_total,
            },
            "model": {"seated": sample.model_seated, "standing": sample.model_standing, "total": sample.model_total},
            "absolute_error": abs(sample.model_total - sample.manual_total),
            "model_version": sample.model_version,
            "sampled_at": sample.sampled_at,
        }

    @staticmethod
    def metrics(report: MetricsReport) -> dict[str, Any]:
        return asdict(report)
