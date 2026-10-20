"""Accuracy audits (auditor and admin roles)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from core_service.api.dependencies import get_context, require_roles
from core_service.api.schemas import AuditIn
from core_service.api.serializers import Serializer
from core_service.container import ApplicationContext
from core_service.errors import ValidationError
from core_service.models import Principal, Role

router = APIRouter(prefix="/audits", tags=["audits"])
AUDITORS = require_roles(Role.AUDITOR, Role.ADMIN)
MAX_CSV_BYTES = 512 * 1024


@router.post("", status_code=201, summary="Register one manual count")
def register(
    body: AuditIn, principal: Principal = Depends(AUDITORS), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    sample = context.audits.register(
        principal.subject, body.bus_id, body.stop_id, body.manual_seated, body.manual_standing, at=body.timestamp
    )
    return Serializer.audit_sample(sample)


@router.post("/csv", summary="Upload offline counts as CSV (text/csv body)")
async def register_csv(
    request: Request, principal: Principal = Depends(AUDITORS), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_CSV_BYTES:
        raise ValidationError("payload_too_large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValidationError("validation_error", details=["the file must be UTF-8 text"]) from error
    result = context.audits.register_csv(principal.subject, text)
    return {
        "processed": result.processed,
        "rejected": [{"line": r.line, "code": r.code, "detail": r.detail} for r in result.rejected],
    }


@router.get("/metrics", summary="Counting error by bus, route, day part and model version")
def metrics(
    start: date,
    end: date,
    route_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9-]{1,10}$"),
    _: Principal = Depends(AUDITORS),
    context: ApplicationContext = Depends(get_context),
) -> dict[str, Any]:
    return Serializer.metrics(context.audits.metrics(start, end, route_id))
