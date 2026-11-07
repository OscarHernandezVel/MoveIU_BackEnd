"""Rebuild routes from Avante's official stop lists (``demo_data/avante_oficial_2024.json``) into the catalogue package.

RutiPasto's traces are wrong for some routes (C5 cut in two and mixed with the start of another route; C12 is
really two routes, C12A and C12B). The official route guide of Avante lists the stops of each direction in order,
and RutiPasto has verified coordinates for almost all of them. This script:

1. places every official stop with those coordinates (aliases and a few manual points cover the rest; a stop
   that cannot be placed is skipped and reported: the bus still drives past it);
2. draws each direction along the streets with OSRM (OpenStreetMap routing), stop after stop;
3. writes the routes into the package, retires the ones they replace and moves their buses.

    python scripts/build_official_routes.py --rutas rutas.js            # updates demo_data/pasto_todas.json
    python scripts/build_official_routes.py --rutas rutas.js --dry-run  # only reports

Only the standard library is used. The OSRM public server is a demo service: run this by hand, not in CI.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "demo_data" / "avante_oficial_2024.json"
PACKAGE = ROOT / "demo_data" / "pasto_todas.json"
FLEET = ROOT / "demo_data" / "fleet_pasto_todas.json"
CHUNK = 40  # waypoints per OSRM request (the demo server refuses very long lists)
OUTLIER_M = 1500.0  # a placed stop this far from both neighbours is a wrong match, not the route
#: The routes the official ones replace, and where their buses go.
REPLACES = {"C12": ["C12A", "C12B"]}


def norm(name: str) -> str:
    text = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().upper()
    text = re.sub(r"\bAV(ENIDA)?\b\.?", "AV", text)
    text = re.sub(r"\bCRA\b", "CARRERA", text)
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", text).split())


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * 111_320, (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0])))


def load_rutipasto(path: Path) -> dict[str, list[dict]]:
    """Verified stops of RutiPasto by normalised name (one name can have several physical stops)."""
    text = path.read_text(encoding="utf-8")
    start = text.find("[", text.find("=", text.find("RUTAS_PASTO")))
    routes, _ = json.JSONDecoder().raw_decode(text[start:])
    by_name: dict[str, list[dict]] = {}
    for route in routes:
        for leg in ("paradasIda", "paradasRetorno"):
            for stop in route[leg]:
                if stop.get("coordenadaVerificada") is False:
                    continue
                entry = {"stop_id": f"AV-{stop['id']}", "name": stop["nombre"], "lat": stop["lat"], "lng": stop["lng"]}
                if all(e["stop_id"] != entry["stop_id"] for e in by_name.get(norm(stop["nombre"]), [])):
                    by_name.setdefault(norm(stop["nombre"]), []).append(entry)
    return by_name


def place(names: list[str], catalog: dict[str, list[dict]], official: dict, report: list[str]) -> list[dict]:
    aliases = {norm(k): norm(v) for k, v in official.get("aliases", {}).items()}
    manual = {norm(k): v for k, v in official.get("manual_stops", {}).items()}
    options: list[tuple[str, list[dict]]] = []
    for name in names:
        key = aliases.get(norm(name), norm(name))
        if key in catalog:
            options.append((name, catalog[key]))
        elif norm(name) in manual:
            m = manual[norm(name)]
            stop_id = "AV-M" + re.sub(r"[^A-Z0-9]", "", norm(name))[:14]
            options.append((name, [{"stop_id": stop_id, "name": name, "lat": m["lat"], "lng": m["lng"], "manual": True}]))
            report.append(f"  ubicación manual: {name} ({m['note']})")
        else:
            report.append(f"  sin coordenadas, se omite: {name}")
    placed = [{**c, "official_name": name} for (name, _), c in zip(options, choose(options))]
    kept = []
    for index, stop in enumerate(placed):
        here = (stop["lat"], stop["lng"])
        around = [placed[j] for j in (index - 1, index + 1) if 0 <= j < len(placed)]
        if not stop.get("manual") and around and all(dist_m(here, (o["lat"], o["lng"])) > OUTLIER_M for o in around):
            report.append(f"  descartada por estar lejos del recorrido: {stop['official_name']} ({stop['stop_id']})")
            continue
        if kept and kept[-1]["stop_id"] == stop["stop_id"]:
            continue  # two official names on the same physical stop
        kept.append(stop)
    return kept


def osrm(service: str, points: list[dict], query: str) -> dict:
    coords = ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in points)
    url = f"https://router.project-osrm.org/{service}/v1/driving/{coords}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "MoveIU-catalogo/1.0 (proyecto universitario)"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed public service
        data = json.loads(response.read())
    time.sleep(1.0)  # the demo server allows about one request per second
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM {service}: {data.get('code')} {data.get('message', '')}")
    return data


def choose(options: list[tuple[str, list[dict]]]) -> list[dict]:
    """One candidate per stop, minimising the distance *by road* of the whole trip (Viterbi).

    The same stop name usually exists on both sides of a divided avenue; the wrong side costs the bus a 1-2 km
    loop to come back. Road distances come from OSRM's table service, in windows of WINDOW stops."""
    window = 15
    road: dict[tuple[str, str], float] = {}
    for start in range(0, len(options) - 1, window - 1):
        group = [c for _, cands in options[start : start + window] for c in cands]
        unique = list({c["stop_id"]: c for c in group}.values())
        if len(unique) < 2:
            continue
        matrix = osrm("table", unique, "annotations=distance")["distances"]
        for i, a in enumerate(unique):
            for j, b in enumerate(unique):
                if matrix[i][j] is not None:
                    road[(a["stop_id"], b["stop_id"])] = matrix[i][j]

    def leg(a: dict, b: dict) -> float:
        return road.get((a["stop_id"], b["stop_id"]), 5 * dist_m((a["lat"], a["lng"]), (b["lat"], b["lng"])))

    cost = [0.0] * len(options[0][1])
    back: list[list[int]] = []
    for i in range(1, len(options)):
        prev, cands = options[i - 1][1], options[i][1]
        step = [min(range(len(prev)), key=lambda p, c=c: cost[p] + leg(prev[p], c)) for c in cands]
        cost = [cost[p] + leg(prev[p], c) for p, c in zip(step, cands)]
        back.append(step)
    best = min(range(len(cost)), key=cost.__getitem__)
    chosen = [best]
    for step in reversed(back):
        chosen.append(step[chosen[-1]])
    return [cands[k] for (_, cands), k in zip(options, reversed(chosen))]


def road_path(stops: list[dict], report: list[str]) -> list[list[float]]:
    """Street trace through the stops in order, as ``[lng, lat]`` (GeoJSON order, like the package)."""
    path: list[list[float]] = []
    for start in range(0, len(stops) - 1, CHUNK - 1):
        chunk = stops[start : start + CHUNK]
        data = osrm("route", chunk, "overview=full&geometries=geojson&continue_straight=true")
        points = [[round(lng, 6), round(lat, 6)] for lng, lat in data["routes"][0]["geometry"]["coordinates"]]
        for leg, (a, b) in zip(data["routes"][0]["legs"], zip(chunk, chunk[1:])):
            straight = dist_m((a["lat"], a["lng"]), (b["lat"], b["lng"]))
            if leg["distance"] > 3 * straight + 400:  # one-way streets or a misplaced stop: worth a look
                report.append(f"  desvío: {a['official_name']} -> {b['official_name']}: {leg['distance'] / 1000:.1f} km por calles, {straight / 1000:.1f} km en línea recta")
        path.extend(points[1:] if path else points)
    return path


def length_km(path: list[list[float]]) -> float:
    return sum(dist_m((a[1], a[0]), (b[1], b[0])) for a, b in zip(path, path[1:])) / 1000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rutas", required=True, help="rutas.js de RutiPasto (coordenadas de las paradas)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    official = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    catalog = load_rutipasto(Path(args.rutas))
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    stops = {s["stop_id"]: s for s in package["stops"]}
    routes = {r["route_id"]: r for r in package["routes"]}
    for route_id, spec in official["routes"].items():
        report: list[str] = []
        legs = {leg: place(spec[leg], catalog, official, report) for leg in ("outbound", "return")}
        paths = {leg: road_path(placed, report) for leg, placed in legs.items()}
        print(f"{route_id} {spec['name']}")
        for leg in ("outbound", "return"):
            print(f"  {leg}: {len(legs[leg])} de {len(spec[leg])} paradas, {length_km(paths[leg]):.1f} km por calles")
        print("\n".join(report) or "  todas las paradas ubicadas")
        for placed in legs.values():
            for s in placed:
                stops.setdefault(s["stop_id"], {"stop_id": s["stop_id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"]})
        routes[route_id] = {
            "route_id": route_id,
            "name": spec["name"],
            "path": paths["outbound"],
            "stops": [s["stop_id"] for s in legs["outbound"]],
            "return_path": paths["return"],
            "return_stops": [s["stop_id"] for s in legs["return"]],
        }
    retired = sorted(set(package.get("retired_routes", [])) | set(REPLACES))
    for old in REPLACES:
        routes.pop(old, None)
    package["routes"] = sorted(routes.values(), key=lambda r: (r["route_id"][0], int(re.sub(r"\D", "", r["route_id"]) or 0), r["route_id"]))
    package["stops"] = list(stops.values())
    package["retired_routes"] = retired
    package["provenance"]["official_routes"] = official["source"]
    moved = 0
    for target in (package, json.loads(FLEET.read_text(encoding="utf-8"))):
        for old, new in REPLACES.items():
            for i, bus in enumerate(b for b in target["buses"] if b.get("route_id") == old):
                bus["route_id"] = new[i % len(new)]
                moved += target is package
        if target is not package and not args.dry_run:
            FLEET.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Retiradas: {', '.join(retired)} · buses movidos: {moved}")
    if not args.dry_run:
        PACKAGE.write_text(json.dumps(package, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"Paquete actualizado: {PACKAGE.relative_to(ROOT)} ({len(package['routes'])} rutas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
