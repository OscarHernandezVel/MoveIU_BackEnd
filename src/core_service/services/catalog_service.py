"""Loads routes and buses from the relational database into the in-memory :class:`RouteCatalog`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core_service.metrics import MetricsRegistry
from core_service.models import Direction
from core_service.persistence.relational import CatalogRepository, RouteDefinition
from core_service.routing import PathGeometry, RouteCatalog, RouteTrack, return_geometry, stop_progress

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReloadReport:
    routes_loaded: int
    buses_loaded: int
    invalid_routes: list[str] = field(default_factory=list)
    #: Routes that load fine but the service cannot model exactly (e.g. a return trip with its own stops).
    warnings: list[str] = field(default_factory=list)


class CatalogService:
    def __init__(self, repository: CatalogRepository, catalog: RouteCatalog, metrics: MetricsRegistry) -> None:
        self._repository = repository
        self._catalog = catalog
        self._metrics = metrics

    def reload(self) -> ReloadReport:
        """Rebuild the catalog. A route with bad data is skipped (and reported), never fatal."""
        tracks: list[RouteTrack] = []
        invalid: list[str] = []
        warnings: list[str] = []
        for definition in self._repository.load_routes():
            try:
                track = RouteTrack(
                    definition.route_id, definition.name, PathGeometry(definition.path), definition.stops
                )
                tracks.append(track)
                self._sync_progress(definition, track)
                if not definition.return_is_mirror:
                    warnings.append(
                        f"{definition.route_id}: the return trip has its own trace or stops; the service "
                        "models it as the outbound trace walked backwards"
                    )
            except Exception as error:  # noqa: BLE001 - one broken route must not take the others down
                log.error("Route %s is invalid and was skipped: %s", definition.route_id, error)
                invalid.append(definition.route_id)
                self._metrics.increment("catalog_invalid_routes")
        buses = self._repository.load_buses()
        self._catalog.replace_all(tracks, buses)
        return ReloadReport(len(tracks), len(buses), invalid, warnings)

    def _sync_progress(self, definition: RouteDefinition, track: RouteTrack) -> None:
        """Keep ``route_stops.progress`` equal to the position of each stop along the trace of its direction
        (only differences are written)."""
        back = return_geometry(definition.path, definition.return_path)
        wanted_by_direction = (
            (Direction.OUTBOUND, {rs.stop.stop_id: rs.progress for rs in track.stops()}),
            (Direction.RETURN, stop_progress(back, definition.return_stops)),
        )
        for direction, wanted in wanted_by_direction:
            stored = self._repository.stop_progress(definition.route_id, direction)
            changed = {
                stop_id: value
                for stop_id, value in wanted.items()
                if stored.get(stop_id) is None or abs(stored[stop_id] - value) > 1e-9  # type: ignore[operator]
            }
            if changed:
                self._repository.set_stop_progress(definition.route_id, direction, changed)
