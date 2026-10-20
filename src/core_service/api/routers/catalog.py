"""Routes, active buses and the 'this bus or the next one' comparison."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from core_service.api.dependencies import get_context, get_locale, get_principal
from core_service.api.serializers import Serializer
from core_service.container import ApplicationContext
from core_service.errors import NotFoundError, ValidationError
from core_service.models import Direction
from core_service.services.cabin_store import CabinStore

router = APIRouter(tags=["catalog"], dependencies=[Depends(get_principal)])
MAX_ROUTES_PER_QUERY = 5


@router.get("/routes", summary="Active routes with their stops")
def list_routes(context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return {"routes": [Serializer.route(track) for track in context.route_catalog.tracks()]}


@router.get("/routes/{route_id}", summary="One route with its path")
def get_route(route_id: str, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return Serializer.route(context.route_catalog.require_track(route_id), include_path=True)


@router.get("/buses/active", summary="Live occupancy of the buses of one to five routes")
def active_buses(
    route_id: list[str] = Query(default=[], max_length=20),
    context: ApplicationContext = Depends(get_context),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    unique = list(dict.fromkeys(route_id))
    if not 1 <= len(unique) <= MAX_ROUTES_PER_QUERY:
        raise ValidationError("too_many_routes", params={"max_routes": MAX_ROUTES_PER_QUERY})
    serializer = Serializer(context.messages)
    routes = []
    for identifier in unique:
        context.route_catalog.require_track(identifier)
        buses = [serializer.live_state(s, locale) for s in context.live_states.list_by_route(identifier)]
        routes.append({"route_id": identifier, "buses": buses})
    return {"routes": routes}


@router.get("/buses/{bus_id}/cabin", summary="Occupied positions of the cabin (data of the 3D twin, no images)")
def bus_cabin(bus_id: str, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    if not 1 <= len(bus_id) <= 20 or not bus_id.replace("-", "").isalnum():
        raise ValidationError("invalid_bus_id")
    state = context.live_states.get(bus_id)
    if state is None:
        raise NotFoundError("bus_not_found", params={"bus_id": bus_id})
    cabins = CabinStore(context.store, context.clock)
    snapshot = cabins.get(bus_id)
    fresh = snapshot is not None and cabins.is_fresh(snapshot)
    return {
        "bus_id": state.bus_id,
        "route_id": state.route_id,
        "updated_at": snapshot.received_at if snapshot else state.updated_at,
        "fresh": fresh,
        "occupied": list(snapshot.occupied) if snapshot and fresh else [],
        "counts": {
            "seated": state.seated,
            "standing": state.standing,
            "standing_out_of_position": state.standing_out_of_position,
            "people_on_board": state.people_on_board,
            "free_seats": state.free_seats,
            "free_standing": state.free_standing,
        },
        "confidence": round(state.confidence, 2),
        "approximate": state.approximate_count,
    }


@router.get("/stops/{stop_id}/next-buses", summary="This bus or the next one, compared")
def next_buses(
    stop_id: str,
    route_id: str = Query(pattern=r"^[A-Za-z0-9-]{1,10}$"),
    direction: Literal["outbound", "return"] = "outbound",
    limit: int = Query(default=2, ge=1, le=3),
    context: ApplicationContext = Depends(get_context),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    result = context.next_buses.get_next_buses(stop_id, route_id, Direction(direction), limit)
    serializer = Serializer(context.messages)
    return {
        "stop": {"stop_id": result.stop_id, "name": result.stop_name},
        "route_id": result.route_id,
        "direction": result.direction.value,
        "buses": [serializer.next_bus(item, locale) for item in result.buses],
    }
