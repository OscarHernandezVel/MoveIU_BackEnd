"""Ingestion of the network catalogue (stops, routes, buses and their interior maps)."""

from core_service.ingestion.avante import load_avante_routes
from core_service.ingestion.loader import CatalogLoader, IngestReport
from core_service.ingestion.package import (
    FORMAT,
    CatalogPackage,
    CatalogPackageError,
    PackageBus,
    PackageRoute,
    load_package,
    parse_package,
)

__all__ = [
    "FORMAT",
    "CatalogLoader",
    "CatalogPackage",
    "CatalogPackageError",
    "IngestReport",
    "PackageBus",
    "PackageRoute",
    "load_avante_routes",
    "load_package",
    "parse_package",
]
