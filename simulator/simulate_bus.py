"""Simulates the on-board hardware of one bus driving along a route (development helper).

Usage::

    CORE_SEED_DEMO=true CORE_INGEST_KEY=<key> python -m core_service        # terminal 1
    python simulator/simulate_bus.py --key <key> --bus 1045 --loops 2        # terminal 2

Only the standard library is used, so it runs anywhere Python runs.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

DEMO_FILE = Path(__file__).resolve().parents[1] / "demo_data" / "route_c5.json"


def load_path(route_id: str, file: Path = DEMO_FILE) -> list[tuple[float, float]]:
    data = json.loads(file.read_text(encoding="utf-8"))
    route = next(r for r in data["routes"] if r["route_id"] == route_id)
    return [(lat, lng) for lng, lat in route["path"]]


def interpolate(path: list[tuple[float, float]], progress: float) -> tuple[float, float]:
    """Point at ``progress`` (0-1) along the polyline, by segment length."""
    lengths = [math.dist(a, b) for a, b in zip(path, path[1:], strict=False)]
    target = max(0.0, min(1.0, progress)) * sum(lengths)
    for (a, b), length in zip(zip(path, path[1:], strict=False), lengths, strict=True):
        if target <= length or length == 0:
            t = 0.0 if length == 0 else target / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        target -= length
    return path[-1]


def send(base_url: str, key: str, bus_id: str, body: dict[str, object]) -> str:
    request = urllib.request.Request(  # noqa: S310 - local development helper
        f"{base_url}/api/v1/internal/buses/{bus_id}/telemetry",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Ingest-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - local development helper
            return json.loads(response.read())["status"]
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--key", required=True, help="value of CORE_INGEST_KEY")
    parser.add_argument("--bus", default="1045")
    parser.add_argument("--route", default="C5")
    parser.add_argument("--package", default=str(DEMO_FILE), help="catalogue package (demo_data/*.json) with the route")
    parser.add_argument("--steps", type=int, default=60, help="readings per one-way trip")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    parser.add_argument("--loops", type=int, default=1, help="number of round trips")
    parser.add_argument("--seats", type=int, default=41)
    parser.add_argument("--standing", type=int, default=8)
    args = parser.parse_args()

    path = load_path(args.route, Path(args.package))
    rng = random.Random(7)
    occupied = 8
    for _ in range(args.loops):
        for direction in (1, -1):
            for step in range(args.steps + 1):
                progress = step / args.steps if direction == 1 else 1 - step / args.steps
                occupied = max(0, min(args.seats + args.standing + 4, occupied + rng.choice((-2, -1, 0, 1, 2, 3))))
                seated = min(occupied, args.seats)
                standing = min(occupied - seated, args.standing)
                lat, lng = interpolate(path, progress)
                status = send(
                    args.base_url,
                    args.key,
                    args.bus,
                    {
                        "ts": time.time(),
                        "lat": lat,
                        "lng": lng,
                        "seated": seated,
                        "standing": standing,
                        "doors_total": seated + standing,
                        "confidence": 0.93,
                        "model_version": "occ-sim-1",
                        "speed_kmh": 28.0,
                    },
                )
                print(f"{args.bus} progress={progress:4.2f} seated={seated:2d} standing={standing:2d} -> {status}")
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
