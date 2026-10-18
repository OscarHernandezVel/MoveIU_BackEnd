"""Relational persistence (PostgreSQL + TimescaleDB)."""

from core_service.persistence.relational.alert_repository import AlertRepository
from core_service.persistence.relational.audit_repository import AuditRepository, TelemetryRepository
from core_service.persistence.relational.catalog_repository import CatalogRepository, RouteDefinition
from core_service.persistence.relational.database import Database
from core_service.persistence.relational.finding_repository import FindingRepository
from core_service.persistence.relational.migrations import MIGRATIONS, Migrator
from core_service.persistence.relational.spatial import SpatialManager, SpatialStatus
from core_service.persistence.relational.timescale import TimescaleManager, TimescaleStatus
from core_service.persistence.relational.user_repository import DeviceRepository, UserRepository

__all__ = [
    "MIGRATIONS",
    "AlertRepository",
    "AuditRepository",
    "CatalogRepository",
    "Database",
    "DeviceRepository",
    "FindingRepository",
    "Migrator",
    "RouteDefinition",
    "SpatialManager",
    "SpatialStatus",
    "TelemetryRepository",
    "TimescaleManager",
    "TimescaleStatus",
    "UserRepository",
]
