"""Loads a :class:`CatalogPackage` into PostgreSQL: stops, routes (both directions), buses and interior maps.

Guarantees
----------
* **Idempotent**: running the same package twice leaves the same data (upserts, no duplicates).
* **Atomic**: everything is written in one transaction; a failure changes nothing.
* **Checked with the service's own rules**: a route is only stored if the routing module accepts it (every
  stop within ``MAX_STOP_DISTANCE_M`` of the trace, stops in travel order, none repeated). Real-world
  data often breaks those rules a little (a stop listed twice on a loop, a stop that projects behind the
  previous one). In ``repair`` mode (default) the offending stops are dropped and every drop is reported;
  in ``strict`` mode any such issue rejects the route.
* **Calibration is never overwritten**: if a bus already has camera-calibrated positions and the package
  brings none, the calibrated ones stay.
* **Dry run**: validates and reports exactly what would change, without touching the database.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from core_service.ingestion.package import CatalogPackage, PackageBus, PackageRoute
from core_service.models import BusPosition, Direction, GeoPoint, Stop
from core_service.occupancy.layout import default_layout
from core_service.persistence.relational import CatalogRepository, Database
from core_service.routing import MAX_STOP_DISTANCE_M, PathGeometry, return_geometry, stop_progress

log = logging.getLogger(__name__)

#: Two stops with the same code closer than this are the same physical stop (only the name differs).
SAME_PLACE_M = 5.0


@dataclass
class IngestReport:
    applied: bool = False
    dry_run: bool = False
    stops_created: int = 0
    stops_updated: int = 0
    routes_created: int = 0
    routes_updated: int = 0
    buses_created: int = 0
    buses_updated: int = 0
    positions_written: int = 0
    #: Stops dropped or renamed to make the data consistent (one line each).
    repairs: list[str] = field(default_factory=list)
    #: Things the operator should know but that did not stop the load.
    warnings: list[str] = field(default_factory=list)
    #: Routes or buses that were rejected, with the reason.
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class _PreparedRoute:
    route: PackageRoute
    stops: list[Stop]
    return_stops: list[Stop]
    return_path: list[GeoPoint] | None = None


class CatalogLoader:
    def __init__(self, database: Database, repository: CatalogRepository) -> None:
        self._db = database
        self._repository = repository

    # ------------------------------------------------------------------ public
    def ingest(self, package: CatalogPackage, *, dry_run: bool = False, strict: bool = False) -> IngestReport:
        report = IngestReport(dry_run=dry_run)
        stops = dict(package.stops)
        prepared: list[_PreparedRoute] = []
        for route in package.routes:
            item = self._prepare_route(route, stops, strict, report)
            if item is not None:
                prepared.append(item)
        route_ids = {item.route.route_id for item in prepared}
        known_routes = {str(r["route_id"]) for r in self._db.query_all("SELECT route_id FROM routes")}
        buses = self._prepare_buses(package.buses, route_ids | known_routes, report)

        if strict and report.errors:
            report.warnings.append("strict mode: nothing was written because of the errors above")
            return report
        used = {s.stop_id for item in prepared for s in (*item.stops, *item.return_stops)}
        self._count(report, used, prepared, buses)
        if dry_run:
            return report
        with self._db.transaction():
            for stop_id in sorted(used):
                self._repository.save_stop(stops[stop_id])
            for item in prepared:
                self._write_route(item)
            for entry, positions in buses:
                self._repository.save_bus(entry.bus)
                if positions is not None:
                    self._repository.save_bus_positions(entry.bus.bus_id, positions)
                    report.positions_written += len(positions)
        report.applied = True
        log.info(
            "Catalogue ingested: %s routes, %s buses, %s stops",
            report.routes_created + report.routes_updated,
            report.buses_created + report.buses_updated,
            report.stops_created + report.stops_updated,
        )
        return report

    # --------------------------------------------------------------- routes
    def _prepare_route(
        self, route: PackageRoute, stops: dict[str, Stop], strict: bool, report: IngestReport
    ) -> _PreparedRoute | None:
        rid = route.route_id
        before = len(report.repairs)
        outbound = self._repair(rid, "outbound", PathGeometry(route.path), [stops[i] for i in route.stop_ids], report)
        back_geometry = return_geometry(route.path, route.return_path)
        if route.return_stop_ids is None:
            back_input = [stops[i] for i in reversed(route.stop_ids)]
        else:
            back_input = [stops[i] for i in route.return_stop_ids]
        back = self._repair(rid, "return", back_geometry, back_input, report)
        if strict and len(report.repairs) > before:
            report.errors.append(f"route {rid}: the data needs repairs and strict mode is on")
            return None
        if len(outbound) < 2:
            report.errors.append(f"route {rid}: fewer than 2 usable stops after checking them against the trace")
            return None
        back_path = route.return_path
        if len(back) < 2:
            report.warnings.append(f"route {rid}: no usable return stops; the outbound stops in reverse are used")
            back, back_path = list(reversed(outbound)), None
        if back_path is not None or [s.stop_id for s in back] != [s.stop_id for s in reversed(outbound)]:
            report.warnings.append(
                f"route {rid}: the return trip differs from the outbound one; the tables keep both, but the "
                "in-memory model serves the return trip as the outbound trace walked backwards"
            )
        return _PreparedRoute(route, outbound, back, back_path)

    @staticmethod
    def _repair(
        route_id: str, direction: str, geometry: PathGeometry, stops: Sequence[Stop], report: IngestReport
    ) -> list[Stop]:
        kept: list[Stop] = []
        seen: set[str] = set()
        last = -1.0
        for stop in stops:
            label = f"route {route_id} {direction}: stop {stop.stop_id} ({stop.name})"
            if stop.stop_id in seen:
                report.repairs.append(f"{label} is listed again; only its first appearance is kept")
                continue
            projection = geometry.project_after(stop.location, last)
            if projection.distance_m > MAX_STOP_DISTANCE_M:
                report.repairs.append(f"{label} is {projection.distance_m:.0f} m from the trace; dropped")
                continue
            if projection.progress <= last:
                report.repairs.append(f"{label} is out of order along the trace; dropped")
                continue
            kept.append(stop)
            seen.add(stop.stop_id)
            last = projection.progress
        return kept

    def _write_route(self, item: _PreparedRoute) -> None:
        route = item.route
        self._repository.save_route(
            route.route_id,
            route.name,
            route.path,
            [s.stop_id for s in item.stops],
            return_path=item.return_path,
            return_stop_ids=[s.stop_id for s in item.return_stops],
        )
        outbound = stop_progress(PathGeometry(route.path), item.stops)
        back = stop_progress(return_geometry(route.path, item.return_path), item.return_stops)
        self._repository.set_stop_progress(route.route_id, Direction.OUTBOUND, outbound)
        self._repository.set_stop_progress(route.route_id, Direction.RETURN, back)

    # ---------------------------------------------------------------- buses
    def _prepare_buses(
        self, buses: Sequence[PackageBus], routes: set[str], report: IngestReport
    ) -> list[tuple[PackageBus, list[BusPosition] | None]]:
        prepared: list[tuple[PackageBus, list[BusPosition] | None]] = []
        for entry in buses:
            bus = entry.bus
            if bus.route_id is not None and bus.route_id not in routes:
                report.errors.append(f"bus {bus.bus_id}: route {bus.route_id} does not exist or was rejected")
                continue
            positions = entry.positions
            if positions is None:
                existing = self._repository.load_bus_positions(bus.bus_id)
                if any(p.calibrated for p in existing):
                    if not self._same_counts(existing, entry):
                        report.errors.append(
                            f"bus {bus.bus_id}: it has calibrated positions that do not match the new capacity; "
                            "recalibrate it or send the new positions explicitly"
                        )
                        continue
                    report.warnings.append(f"bus {bus.bus_id}: calibrated positions kept")
                    prepared.append((entry, None))
                    continue
                try:
                    positions = default_layout(bus.capacity)
                except ValueError as error:
                    report.errors.append(f"bus {bus.bus_id}: {error}")
                    continue
            mismatch = self._capacity_mismatch(entry, positions)
            if mismatch:
                report.errors.append(f"bus {bus.bus_id}: {mismatch}")
                continue
            prepared.append((entry, positions))
        return prepared

    @staticmethod
    def _capacity_mismatch(entry: PackageBus, positions: Sequence[BusPosition]) -> str | None:
        counts: dict[str, int] = {}
        for position in positions:
            counts[position.kind.value] = counts.get(position.kind.value, 0) + 1
        capacity = entry.bus.capacity
        expected = {
            "seat": capacity.seats,
            "standing": capacity.standing_positions,
            "wheelchair": capacity.wheelchair_spaces,
        }
        wrong = [
            f"{k}: {counts.get(k, 0)} mapped vs {v} in the capacity"
            for k, v in expected.items()
            if counts.get(k, 0) != v
        ]
        return "positions do not match the capacity (" + "; ".join(wrong) + ")" if wrong else None

    def _same_counts(self, existing: Sequence[BusPosition], entry: PackageBus) -> bool:
        return self._capacity_mismatch(entry, existing) is None

    # -------------------------------------------------------------- reporting
    def _count(
        self,
        report: IngestReport,
        used: set[str],
        prepared: Sequence[_PreparedRoute],
        buses: Sequence[tuple[PackageBus, list[BusPosition] | None]],
    ) -> None:
        def existing(table: str, column: str) -> set[str]:
            return {str(r[column]) for r in self._db.query_all(f"SELECT {column} FROM {table}")}  # noqa: S608 - constants

        have_stops, have_routes, have_buses = (
            existing("stops", "stop_id"),
            existing("routes", "route_id"),
            existing("buses", "bus_id"),
        )
        report.stops_updated = len(used & have_stops)
        report.stops_created = len(used - have_stops)
        ids = {item.route.route_id for item in prepared}
        report.routes_updated = len(ids & have_routes)
        report.routes_created = len(ids - have_routes)
        bus_ids = {entry.bus.bus_id for entry, _ in buses}
        report.buses_updated = len(bus_ids & have_buses)
        report.buses_created = len(bus_ids - have_buses)
        if report.dry_run:
            report.positions_written = sum(len(p) for _, p in buses if p is not None)


__all__ = ["CatalogLoader", "IngestReport"]
