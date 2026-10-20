"""Simulates the on-board hardware of every active bus of a catalogue package, all at the same time.

Usage (after loading the catalogue with ``python -m core_service.ingestion`` and starting the service)::

    python simulator/simulate_fleet.py --key <CORE_INGEST_KEY> --package demo_data/pasto_c1_c4.json

Each bus drives its route back and forth, starting at a different point so that the buses of one route
do not travel together. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from pathlib import Path

from simulate_bus import interpolate, send

DEFAULT_PACKAGE = Path(__file__).resolve().parents[1] / "demo_data" / "pasto_c1_c4.json"


def drive(
    base_url: str,
    key: str,
    bus: dict[str, object],
    path: list[tuple[float, float]],
    args: argparse.Namespace,
    offset: float,
) -> None:
    rng = random.Random(str(bus["bus_id"]))
    seats, standing = int(bus["seats"]), int(bus["standing_positions"])  # type: ignore[call-overload]
    occupied = rng.randint(3, 12)
    for _ in range(args.loops):
        for direction in (1, -1):
            for step in range(args.steps + 1):
                base = (step / args.steps + offset) % 1.0
                progress = base if direction == 1 else 1.0 - base
                occupied = max(0, min(seats + standing + 3, occupied + rng.choice((-2, -1, 0, 1, 2))))
                seated = min(occupied, seats)
                on_foot = min(occupied - seated, standing)
                lat, lng = interpolate(path, progress)
                status = send(
                    base_url,
                    key,
                    str(bus["bus_id"]),
                    {
                        "ts": time.time(),
                        "lat": lat,
                        "lng": lng,
                        "seated": seated,
                        "standing": on_foot,
                        "doors_total": seated + on_foot,
                        "confidence": 0.93,
                        "model_version": "occ-sim-1",
                        "speed_kmh": 25.0,
                    },
                )
                label = f"{bus['bus_id']} ({bus['route_id']})"
                print(f"{label} progress={progress:4.2f} occupied={seated + on_foot:2d} -> {status}")
                time.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--key", required=True, help="value of CORE_INGEST_KEY")
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--steps", type=int, default=80, help="readings per one-way trip")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    parser.add_argument("--loops", type=int, default=1, help="number of round trips per bus")
    args = parser.parse_args()

    data = json.loads(Path(args.package).read_text(encoding="utf-8"))
    paths = {r["route_id"]: [(lat, lng) for lng, lat in r["path"]] for r in data["routes"]}
    buses = [b for b in data["buses"] if b.get("status", "active") == "active" and b.get("route_id") in paths]
    per_route: dict[str, int] = {}
    threads = []
    for bus in buses:
        index = per_route.get(bus["route_id"], 0)
        per_route[bus["route_id"]] = index + 1
        thread = threading.Thread(
            target=drive, args=(args.base_url, args.key, bus, paths[bus["route_id"]], args, 0.31 * index), daemon=True
        )
        threads.append(thread)
        thread.start()
    print(f"Simulating {len(threads)} buses on {len(paths)} routes...")
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
