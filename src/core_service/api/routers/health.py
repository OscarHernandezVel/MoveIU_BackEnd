"""Health and metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core_service.api.dependencies import get_context, require_roles
from core_service.container import ApplicationContext
from core_service.models import Principal, Role

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="The process is up")
def live(context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return context.health.liveness()


@router.get("/health/ready", summary="Dependencies answer")
def ready(context: ApplicationContext = Depends(get_context)) -> JSONResponse:
    report = context.health.readiness()
    return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503)


@router.get("/metrics", summary="Operational counters and latencies")
def metrics(
    _: Principal = Depends(require_roles(Role.ADMIN)), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    return context.metrics.snapshot()
