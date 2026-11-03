"""Simulates the on-board hardware of every active bus of a catalogue package, all at the same time.

Usage (after loading the catalogue with ``python -m core_service.ingestion`` and starting the service)::

    python simulator/simulate_fleet.py --key <CORE_INGEST_KEY>                       # all 23 routes, forever
    python simulator/simulate_fleet.py --key-file secrets/ingest_key --minutes 10    # stop after 10 minutes

On the server it runs as the ``simulator`` service of docker-compose (``python3 ops.py sim start``).

Each bus drives its route there and back (the return trace when the package has one), starting at a
different point, at a city speed with stops, and people get on and off following the hour of the day in
Pasto (rush hours fuller). Every reading goes through the real ingestion (``/internal/telemetry/batch``),
so live map, "this bus or the next one", alerts, findings and the AI all see the same buses. Buses with the
41-seat layout also send the ids of the occupied positions, the data behind the 3D twin. Only the standard
library is used.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PACKAGE = Path(__file__).resolve().parents[1] / "demo_data" / "pasto_todas.json"
PASTO = timezone(timedelta(hours=-5))
MAX_BATCH = 500

# Ids of the 41-seat cabin (rows 1-10 L1 L2 | aisle P | R2 R1, doors at rows 1 and 10, rear bench of 5): the same
# ids the edge agent and the 3D twin use.
SEATS = [f"S{r:02d}-{s}" for r in range(1, 11) for s in (("L1", "L2") if r in (1, 10) else ("L1", "L2", "R2", "R1"))]
SEATS += [f"S11-T{t}" for t in range(1, 6)]
STANDS = [f"P{r:02d}" for r in range(2, 10)]


def load_factor(now: datetime) -> float:
    """Share of the capacity a bus carries at mid-route at this hour (morning, noon and evening rush)."""
    hour = now.hour + now.minute / 60
    rush = max(math.exp(-((hour - h) ** 2) / 0.9) for h in (6.8, 12.5, 18.0))
    return 0.25 + 0.95 * rush if 5 <= hour < 22 else 0.08


def measure(path: list[tuple[float, float]]) -> list[float]:
    """Cumulative length in metres of a ``(lat, lng)`` polyline."""
    cum = [0.0]
    for (lat1, lng1), (lat2, lng2) in zip(path, path[1:], strict=False):
        dy = (lat2 - lat1) * 111_320
        dx = (lng2 - lng1) * 111_320 * math.cos(math.radians(lat1))
        cum.append(cum[-1] + math.hypot(dx, dy))
    return cum


def point_at(path: list[tuple[float, float]], cum: list[float], s: float) -> tuple[float, float]:
    i = 1
    while i < len(cum) - 1 and cum[i] < s:
        i += 1
    span = cum[i] - cum[i - 1]
    t = 0.0 if span == 0 else (s - cum[i - 1]) / span
    (lat1, lng1), (lat2, lng2) = path[i - 1], path[i]
    return lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t


def settle(taken: set[str], pool: list[str], goal: int, rng: random.Random) -> None:
    """Changes few positions at a time, as people really move: the 3D cabin does not reshuffle every reading."""
    while len(taken) < goal:
        taken.add(rng.choice([p for p in pool if p not in taken]))
    while len(taken) > goal:
        taken.remove(rng.choice(sorted(taken)))


@dataclass
class SimBus:
    bus_id: str
    seats: int
    standing_positions: int
    trips: list[tuple[list[tuple[float, float]], list[float]]]  # outbound and return
    rng: random.Random
    trip: int = 0
    s: float = 0.0
    people: int = 0
    dwell: int = 0
    seat_ids: set[str] = field(default_factory=set)
    stand_ids: set[str] = field(default_factory=set)

    @property
    def twin_layout(self) -> bool:
        return self.seats == len(SEATS) and self.standing_positions == len(STANDS)

    def step(self, seconds: float, factor: float) -> dict[str, object]:
        path, cum = self.trips[self.trip]
        speed_kmh = 0.0
        if self.dwell > 0:
            self.dwell -= 1
        else:
            speed_kmh = self.rng.uniform(14, 30)
            self.s += speed_kmh / 3.6 * seconds
            if self.s >= cum[-1]:
                self.trip = (self.trip + 1) % len(self.trips)
                self.s, self.dwell = 0.0, 3  # terminal: a short wait before the next trip
                path, cum = self.trips[self.trip]
            elif self.rng.random() < 0.25:
                self.dwell = 1  # a stop on the way
        progress = self.s / cum[-1] if cum[-1] else 0.0
        capacity = self.seats + self.standing_positions
        target = capacity * factor * (0.4 + 0.6 * math.sin(math.pi * progress))
        if self.dwell:  # people get on and off only at stops
            self.people += round(max(-4, min(4, (target - self.people) * 0.5 + self.rng.uniform(-1.5, 1.5))))
        self.people = max(0, min(capacity + 4, self.people))
        seated = min(self.people, self.seats)
        standing = min(self.people - seated, self.standing_positions)
        lat, lng = point_at(path, cum, self.s)
        reading: dict[str, object] = {
            "bus_id": self.bus_id, "ts": time.time(), "lat": round(lat, 6), "lng": round(lng, 6),
            "seated": seated, "standing": standing, "standing_out_of_position": self.people - seated - standing,
            "doors_total": self.people, "confidence": 0.93, "model_version": "occ-sim-2", "speed_kmh": round(speed_kmh, 1),
        }
        if self.twin_layout:
            settle(self.seat_ids, SEATS, seated, self.rng)
            settle(self.stand_ids, STANDS, standing, self.rng)
            reading["cabin"] = {"occupied": sorted(self.seat_ids | self.stand_ids)}
        return reading


def load_fleet(package: Path, seed: int) -> list[SimBus]:
    data = json.loads(package.read_text(encoding="utf-8"))
    trips: dict[str, list[tuple[list[tuple[float, float]], list[float]]]] = {}
    for route in data["routes"]:
        outbound = [(lat, lng) for lng, lat in route["path"]]
        back = [(lat, lng) for lng, lat in route["return_path"]] if route.get("return_path") else outbound[::-1]
        trips[route["route_id"]] = [(p, measure(p)) for p in (outbound, back) if len(p) >= 2]
    fleet: list[SimBus] = []
    per_route: dict[str, int] = {}
    for bus in data["buses"]:
        route_trips = trips.get(bus.get("route_id", ""))
        if bus.get("status", "active") != "active" or not route_trips:
            continue
        index = per_route.get(bus["route_id"], 0)
        per_route[bus["route_id"]] = index + 1
        rng = random.Random(f"{seed}-{bus['bus_id']}")
        sim = SimBus(str(bus["bus_id"]), int(bus["seats"]), int(bus["standing_positions"]), route_trips, rng)
        sim.trip = index % len(route_trips)  # the second bus of a route starts on the return trip
        sim.s = route_trips[sim.trip][1][-1] * rng.uniform(0.1, 0.6)
        sim.people = rng.randint(4, 14)
        fleet.append(sim)
    return fleet


def send_batch(base_url: str, key: str, readings: list[dict[str, object]]) -> str:
    request = urllib.request.Request(  # noqa: S310 - the URL comes from the operator
        f"{base_url}/api/v1/internal/telemetry/batch",
        data=json.dumps({"readings": readings}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Ingest-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            body = json.loads(response.read())
            return f"{body['accepted']} aceptadas, {body['rejected']} rechazadas"
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except OSError as error:  # the service restarts or is not up yet: keep trying
        return f"sin conexión ({error})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument("--key", help="value of CORE_INGEST_KEY")
    key.add_argument("--key-file", help="file with the ingest key (the docker secret)")
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between readings (the edge sends every 3 s)")
    parser.add_argument("--minutes", type=float, default=0, help="stop after this many minutes (0 = forever)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    ingest_key = args.key or Path(args.key_file).read_text(encoding="utf-8").strip()
    fleet = load_fleet(Path(args.package), args.seed)
    print(f"Simulando {len(fleet)} buses cada {args.interval:g} s contra {args.base_url}", flush=True)
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else math.inf
    while time.monotonic() < deadline:
        started = time.monotonic()
        factor = load_factor(datetime.now(PASTO))
        readings = [bus.step(args.interval, factor) for bus in fleet]
        batches = [readings[i : i + MAX_BATCH] for i in range(0, len(readings), MAX_BATCH)]
        results = [send_batch(args.base_url, ingest_key, batch) for batch in batches]
        people = sum(int(r["doors_total"]) for r in readings)  # type: ignore[call-overload]
        print(f"{datetime.now(PASTO):%H:%M:%S} carga {factor:4.2f} · {people} pasajeros · {'; '.join(results)}", flush=True)
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
