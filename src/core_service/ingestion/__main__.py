"""Command line: ``python -m core_service.ingestion`` loads the network catalogue into PostgreSQL.

Examples (messages for the operator are in Spanish)::

    # 1. Convert the public Pasto data into a package file (no database needed) and review it
    python -m core_service.ingestion --source avante --file rutas.js --routes C1,C2,C3,C4 \\
        --fleet demo_data/fleet_pasto.json --write-package demo_data/pasto_c1_c4.json

    # 2. Check what would change (nothing is written)
    python -m core_service.ingestion --source package --file demo_data/pasto_c1_c4.json --dry-run

    # 3. Load it (needs CORE_DATABASE_URL; DML rights are enough, the schema must already be migrated)
    python -m core_service.ingestion --source package --file demo_data/pasto_c1_c4.json
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from core_service.config import _secret
from core_service.errors import ServiceUnavailableError
from core_service.ingestion.avante import load_avante_routes
from core_service.ingestion.loader import CatalogLoader, IngestReport
from core_service.ingestion.package import CatalogPackage, CatalogPackageError, load_package
from core_service.persistence.relational import CatalogRepository, Database

EXIT_OK, EXIT_ERRORS, EXIT_USAGE = 0, 1, 2
REQUIRED_SCHEMA_VERSION = 3


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _merge(base: CatalogPackage, extra: CatalogPackage) -> CatalogPackage:
    """Add the routes, stops and buses of ``extra`` (the fleet) to ``base`` (``extra`` wins on repeated ids).
    Provenance of both is kept: the fleet's entries are prefixed with ``fleet_``."""
    stops = {**base.stops, **extra.stops}
    routes = {r.route_id: r for r in [*base.routes, *extra.routes]}
    buses = {b.bus.bus_id: b for b in [*base.buses, *extra.buses]}
    return CatalogPackage(
        stops,
        list(routes.values()),
        list(buses.values()),
        {**base.provenance, **{f"fleet_{k}": v for k, v in extra.provenance.items()}},
    )


def _print_report(report: IngestReport) -> None:
    mode = "SIMULACIÓN (no se escribió nada)" if report.dry_run else ("APLICADO" if report.applied else "NO APLICADO")
    print(f"Resultado: {mode}")
    print(f"  Paradas : {report.stops_created} nuevas, {report.stops_updated} actualizadas")
    print(f"  Rutas   : {report.routes_created} nuevas, {report.routes_updated} actualizadas (ambos sentidos)")
    print(f"  Buses   : {report.buses_created} nuevos, {report.buses_updated} actualizados")
    print(f"  Posiciones del mapa interior: {report.positions_written}")
    for title, items in (("Correcciones", report.repairs), ("Avisos", report.warnings), ("Errores", report.errors)):
        if items:
            print(f"{title} ({len(items)}):")
            for line in items[:25]:
                print(f"  - {line}")
            if len(items) > 25:
                print(f"  ... y {len(items) - 25} más")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga rutas, paradas y buses en PostgreSQL.")
    parser.add_argument("--source", choices=("package", "avante"), required=True)
    parser.add_argument("--file", required=True, help="paquete .json o rutas.js de Avante/RutiPasto")
    parser.add_argument("--routes", default="", help="rutas a tomar de la fuente 'avante', separadas por comas")
    parser.add_argument("--fleet", default="", help="paquete .json con los buses (y sus mapas interiores)")
    parser.add_argument(
        "--write-package", default="", help="guarda el paquete resultante y termina (sin base de datos)"
    )
    parser.add_argument("--dry-run", action="store_true", help="valida y muestra los cambios sin escribir")
    parser.add_argument("--strict", action="store_true", help="rechaza rutas que necesiten correcciones")
    parser.add_argument(
        "--database-url", default="", help="por defecto, CORE_DATABASE_URL (o el archivo de CORE_DATABASE_URL_FILE)"
    )
    parser.add_argument("--schema", default=os.environ.get("CORE_DATABASE_SCHEMA", ""))
    args = parser.parse_args(argv)

    try:
        if args.source == "avante":
            route_ids = [r.strip() for r in args.routes.split(",") if r.strip()]
            if not route_ids:
                print("Indique --routes (por ejemplo C1,C2,C3,C4) con la fuente 'avante'.", file=sys.stderr)
                return EXIT_USAGE
            package = load_avante_routes(_read(args.file), route_ids, retrieved_at=date.today().isoformat())
        else:
            package = load_package(_read(args.file))
        if args.fleet:
            package = _merge(package, load_package(_read(args.fleet)))
    except OSError as error:
        print(f"No se pudo leer el archivo: {error}", file=sys.stderr)
        return EXIT_USAGE
    except CatalogPackageError as error:
        print("El paquete no es válido:", file=sys.stderr)
        for problem in error.problems[:30]:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_USAGE

    if args.write_package:
        Path(args.write_package).write_text(package.dumps(), encoding="utf-8")
        print(
            f"Paquete guardado en {args.write_package}: {len(package.routes)} rutas, "
            f"{len(package.stops)} paradas, {len(package.buses)} buses."
        )
        return EXIT_OK

    url = args.database_url or _secret(os.environ, "CORE_DATABASE_URL")
    if not url:
        print("Falta la base de datos: use --database-url, CORE_DATABASE_URL o CORE_DATABASE_URL_FILE.",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        database = Database(
            url,
            schema=args.schema or None,
            min_size=1,
            max_size=1,
            application_name="core-ingestion",
            connect_timeout_seconds=15.0,
            pool_timeout_seconds=15.0,
        )
    except ServiceUnavailableError:
        print(
            "No se pudo conectar a la base de datos. Revise que el contenedor esté «healthy» "
            "(docker compose -f docker-compose.dev.yml ps) y que la URL sea correcta "
            "(pruebe 127.0.0.1 en lugar de localhost).",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        row = database.query_one("SELECT max(version) AS version FROM schema_migrations")
        if row is None or (row["version"] or 0) < REQUIRED_SCHEMA_VERSION:
            print(
                f"El esquema está desactualizado (se necesita la migración {REQUIRED_SCHEMA_VERSION}): "
                "ejecute python scripts/init_dev_db.py (o inicie el servicio una vez con el rol dueño del esquema).",
                file=sys.stderr,
            )
            return EXIT_USAGE
        report = CatalogLoader(database, CatalogRepository(database)).ingest(
            package, dry_run=args.dry_run, strict=args.strict
        )
    finally:
        database.close()
    _print_report(report)
    if report.applied:
        print("Para que el servicio en marcha lo use: POST /api/v1/admin/catalog/reload (o reinícielo).")
    return EXIT_OK if report.ok else EXIT_ERRORS


if __name__ == "__main__":
    sys.exit(main())
