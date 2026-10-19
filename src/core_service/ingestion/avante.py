"""Adapter for the public data of Pasto's transit system (SETP Avante) as published in ``rutas.js``.

Source: the community project *RutiPasto* (https://github.com/AnderHolden/RutiPasto, file
``data/rutas.js``), which compiles the official route guide of Avante SETP, the municipality of Pasto and
OpenStreetMap: 23 routes (C1-C16, E1-E7), each with an outbound ("ida") and a return ("retorno") trip, the
GPS trace of each trip and its stops with official codes.

Licence: the repository declares none, and part of the geometry derives from OpenStreetMap (ODbL, which
requires attribution). The adapter therefore copies the provenance into every package it produces. Check
the terms before redistributing the data outside your organisation.

Data-quality notes found while validating the file (why the loader repairs instead of trusting):

* the same stop code can be spelled differently on different routes ("Calle 17 Carrera19" / "Calle 17
  Carrera 19") - the first spelling is kept when the coordinates agree;
* a stop can be listed twice on a loop, or project *behind* the previous stop on routes that revisit a street;
* some routes (C5 in the file) have stops kilometres away from their own trace; the loader rejects those stops.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from core_service.ingestion.package import CatalogPackage, CatalogPackageError, parse_package
from core_service.models import GeoPoint, haversine_m

SOURCE_URL = "https://github.com/AnderHolden/RutiPasto/blob/main/data/rutas.js"
ATTRIBUTION = (
    "SETP Avante (Pasto), Alcaldía de Pasto y colaboradores de OpenStreetMap (ODbL), "
    "compilado por el proyecto RutiPasto"
)
LICENSE_NOTE = "El repositorio de origen no declara licencia: confirme los términos antes de redistribuir."
SAME_PLACE_M = 5.0


def parse_rutas_js(text: str) -> list[dict[str, Any]]:
    """Extract the ``RUTAS_PASTO`` array from the JavaScript file (it is plain JSON) or read a .json file."""
    stripped = text.lstrip()
    if stripped.startswith("["):
        start = len(text) - len(stripped)
    else:
        marker = text.find("RUTAS_PASTO")
        start = text.find("[", text.find("=", marker)) if marker >= 0 else -1
    if start < 0:
        raise CatalogPackageError(["rutas.js: the RUTAS_PASTO array was not found"])
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise CatalogPackageError([f"rutas.js: cannot read the route array: {error.msg}"]) from error
    if not isinstance(data, list):
        raise CatalogPackageError(["rutas.js: RUTAS_PASTO is not a list"])
    return [item for item in data if isinstance(item, dict)]


def _trace(points: Iterable[Any]) -> list[list[float]]:
    """RutiPasto stores ``[lat, lng]``; the package uses GeoJSON order ``[lng, lat]``."""
    return [[float(p[1]), float(p[0])] for p in points]


def load_avante_routes(
    text: str,
    route_ids: Sequence[str],
    *,
    id_prefix: str = "AV-",
    retrieved_at: str = "",
) -> CatalogPackage:
    """Build a catalogue package with the requested routes (no buses: the source has no fleet data)."""
    by_id = {str(item.get("id")): item for item in parse_rutas_js(text)}
    missing = [rid for rid in route_ids if rid not in by_id]
    if missing:
        raise CatalogPackageError([f"routes not found in the source: {', '.join(missing)}"])

    stops: dict[str, dict[str, Any]] = {}
    renamed = unverified = 0
    routes: list[dict[str, Any]] = []
    for rid in route_ids:
        item = by_id[rid]
        entry: dict[str, Any] = {
            "route_id": rid,
            "name": f"{item.get('origen', '?')} - {item.get('destino', '?')}"[:100],
        }
        for direction, stops_key, trace_key in (
            ("outbound", "paradasIda", "coordenadasIda"),
            ("return", "paradasRetorno", "coordenadasRetorno"),
        ):
            ids: list[str] = []
            for raw in item.get(stops_key, []):
                if not raw.get("coordenadaVerificada", True):
                    unverified += 1
                    continue
                stop_id = f"{id_prefix}{raw['id']}"
                lat, lng, name = float(raw["lat"]), float(raw["lng"]), str(raw["nombre"]).strip()
                known = stops.get(stop_id)
                if known is None:
                    stops[stop_id] = {"stop_id": stop_id, "name": name[:100], "lat": lat, "lng": lng}
                else:
                    if haversine_m(GeoPoint(known["lat"], known["lng"]), GeoPoint(lat, lng)) > SAME_PLACE_M:
                        raise CatalogPackageError(
                            [f"stop code {raw['id']} appears at two different places (route {rid} {direction})"]
                        )
                    renamed += known["name"] != name[:100]
                ids.append(stop_id)
            trace = _trace(item.get(trace_key, []))
            if direction == "outbound":
                entry["path"], entry["stops"] = trace, ids
            else:
                entry["return_path"], entry["return_stops"] = trace, ids
        routes.append(entry)

    notes = []
    if renamed:
        notes.append(f"{renamed} stop names spelled differently between routes (first spelling kept)")
    if unverified:
        notes.append(f"{unverified} stops without verified coordinates were left out")
    provenance = {
        "source": SOURCE_URL,
        "retrieved_at": retrieved_at,
        "attribution": ATTRIBUTION,
        "license_note": LICENSE_NOTE,
        "notes": "; ".join(notes),
    }
    return parse_package(
        {"format": "core-catalog/1", "provenance": provenance, "stops": list(stops.values()), "routes": routes}
    )
