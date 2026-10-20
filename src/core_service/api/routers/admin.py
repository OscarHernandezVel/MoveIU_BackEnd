"""Operations reserved for administrators."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from core_service.api.dependencies import get_context, require_roles
from core_service.container import ApplicationContext
from core_service.models import Role

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_roles(Role.ADMIN))])


@router.post("/catalog/reload", summary="Reload routes, stops and buses from the database")
def reload_catalog(context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    """Applies a catalogue ingestion (``python -m core_service.ingestion``) without restarting.

    It affects the instance that receives the call; with several replicas call each one (or restart them)."""
    report = context.catalog_service.reload()
    return {
        "routes_loaded": report.routes_loaded,
        "buses_loaded": report.buses_loaded,
        "invalid_routes": report.invalid_routes,
        "warnings": report.warnings,
    }
