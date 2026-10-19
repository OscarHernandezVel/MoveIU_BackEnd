"""Health checks: liveness (process is up) and readiness (dependencies answer)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core_service.clock import Clock
from core_service.persistence.documents import DocumentStore
from core_service.persistence.relational import Database
from core_service.routing import RouteCatalog


class HealthService:
    def __init__(
        self,
        database: Database,
        store: DocumentStore,
        catalog: RouteCatalog,
        clock: Clock,
        ai_status: Callable[[], dict[str, Any]],
        version: str,
        details: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._database = database
        self._store = store
        self._catalog = catalog
        self._clock = clock
        self._ai_status = ai_status
        self._version = version
        self._details = details
        self._started = clock.now()

    def liveness(self) -> dict[str, Any]:
        return {"status": "up", "version": self._version, "uptime_seconds": round(self._clock.now() - self._started, 1)}

    def readiness(self) -> dict[str, Any]:
        checks = {
            "relational_database": self._safe(self._database.ping),
            "document_store": self._safe(self._store.ping),
            "route_catalog": len(self._catalog.route_ids()) > 0,
        }
        report: dict[str, Any] = {
            "status": "ready" if all(checks.values()) else "degraded",
            "checks": checks,
            "ai": self._ai_status(),
            "version": self._version,
        }
        if self._details is not None:
            report["data"] = self._safe_details()
        return report

    def _safe_details(self) -> dict[str, Any]:
        try:
            return self._details() if self._details is not None else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _safe(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 - a broken dependency is a "false", not a crash
            return False
