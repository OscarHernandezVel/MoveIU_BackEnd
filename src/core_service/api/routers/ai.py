"""AI analysis, technical findings and the training dataset."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from core_service.ai import AnalysisRequest
from core_service.api.dependencies import get_context, require_roles
from core_service.api.schemas import AnalysisIn
from core_service.api.serializers import Serializer
from core_service.container import ApplicationContext
from core_service.errors import ValidationError
from core_service.models import GeoPoint, Principal, Role

router = APIRouter(tags=["ai"])
STAFF = require_roles(Role.AUDITOR, Role.ADMIN)
ADMIN = require_roles(Role.ADMIN)


@router.post("/ai/analysis", summary="Analyse the fleet (AI, or rules when the AI is not available)")
def analyse(
    body: AnalysisIn, principal: Principal = Depends(STAFF), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    for route_id in body.route_ids:
        if context.route_catalog.find_track(route_id) is None:
            raise ValidationError("ai_invalid_request", details=[f"unknown route {route_id}"])
    center = GeoPoint(body.center.lat, body.center.lng) if body.center else None
    request = AnalysisRequest(body.query, tuple(body.route_ids), center, body.radius_km, requested_by=principal.subject)
    return context.ai.analyze(request).to_dict()


@router.get("/ai/runs", summary="Audit trail of past analyses")
def runs(
    limit: int = Query(default=20, ge=1, le=100),
    _: Principal = Depends(ADMIN),
    context: ApplicationContext = Depends(get_context),
) -> dict[str, Any]:
    return {"runs": context.ai.list_runs(limit)}


@router.get("/findings", summary="Technical findings (rules and AI)")
def findings(
    limit: int = Query(default=50, ge=1, le=200),
    status: Literal["open", "reviewed"] | None = None,
    source: Literal["rules", "ai"] | None = None,
    _: Principal = Depends(STAFF),
    context: ApplicationContext = Depends(get_context),
) -> dict[str, Any]:
    items = context.findings_repository.list_recent(limit, status=status, source=source)
    return {"findings": [Serializer.finding(f) for f in items]}


@router.get("/ai/training-dataset/summary", summary="Labelled samples per model version")
def dataset_summary(
    _: Principal = Depends(ADMIN), context: ApplicationContext = Depends(get_context)
) -> dict[str, Any]:
    return {"samples_by_model_version": context.dataset_exporter.summary()}


@router.get("/ai/training-dataset", summary="Labelled samples as NDJSON (no personal data)")
def dataset(
    since: float | None = Query(default=None, gt=0),
    model_version: str | None = Query(default=None, max_length=40),
    _: Principal = Depends(ADMIN),
    context: ApplicationContext = Depends(get_context),
) -> StreamingResponse:
    lines: Iterator[str] = context.dataset_exporter.export_jsonl(since, model_version)
    return StreamingResponse((line + "\n" for line in lines), media_type="application/x-ndjson")
