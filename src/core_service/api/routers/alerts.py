"""Proximity alerts and the on-screen notification inbox."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from core_service.api.dependencies import get_context, require_device
from core_service.api.schemas import AlertIn
from core_service.api.serializers import Serializer
from core_service.container import ApplicationContext
from core_service.errors import NotFoundError
from core_service.models import Direction

router = APIRouter(tags=["alerts"])


@router.post("/alerts", status_code=201, summary="Ask to be warned when a bus is N stops away")
def create_alert(
    body: AlertIn, device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    alert = context.alerts.create(
        device_id, body.route_id, Direction(body.direction), body.stop_id, body.stops_before, body.bus_id
    )
    return Serializer.alert(alert)


@router.get("/alerts", summary="Active alerts of this phone")
def list_alerts(
    device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    return {"alerts": [Serializer.alert(a) for a in context.alerts.list_active(device_id)]}


@router.delete("/alerts/{alert_id}", status_code=204, summary="Cancel an alert")
def cancel_alert(
    alert_id: str, device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> Response:
    context.alerts.cancel(device_id, alert_id)
    return Response(status_code=204)


@router.get("/notifications", summary="On-screen notification inbox (newest first)")
def inbox(
    limit: int = Query(default=20, ge=1, le=50),
    device_id: str = Depends(require_device),
    context: ApplicationContext = Depends(get_context),
) -> dict[str, Any]:
    items = context.inbox.latest(device_id, limit)
    return {"unread": context.inbox.unread_count(device_id), "items": [Serializer.inbox_item(i) for i in items]}


@router.post("/notifications/read-all", summary="Mark every notification as read")
def read_all(
    device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    return {"marked": context.inbox.mark_all_read(device_id)}


@router.post("/notifications/{notification_id}/read", status_code=204, summary="Mark one notification as read")
def read_one(
    notification_id: str, device_id: str = Depends(require_device), context: ApplicationContext = Depends(get_context)
) -> Response:
    if not context.inbox.mark_read(device_id, notification_id):
        raise NotFoundError("alert_not_found")
    return Response(status_code=204)
