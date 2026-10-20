"""Follows routes over the real-time WebSocket, like the app's map would (manual testing).

Example (the service running on 127.0.0.1:8080)::

    python simulator/watch_stream.py --routes C1,C2 --seconds 30

It signs in as a guest (``POST /api/v1/auth/guest``), opens ``/ws/v1/buses/stream``, subscribes and prints
one line per frame: route, sequence, and the buses that appeared, changed or left.
Uses the ``websockets`` package (installed with ``uvicorn[standard]``).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import uuid

from websockets.sync.client import connect


def guest_token(base_url: str) -> str:
    body = json.dumps(
        {"device_id": f"watch-{uuid.uuid4().hex[:12]}", "platform": "android", "push_token": "simulator"}
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - URL given by the operator
        f"{base_url}/api/v1/auth/guest", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return str(json.loads(response.read())["access_token"])


def describe(message: dict) -> str:
    kind = message["type"]
    if kind == "snapshot":
        buses = ", ".join(f"{b['bus']} {b['pct']}% {b['state']}" for b in message["buses"]) or "sin buses"
        return f"[{message['route']} #{message['seq']}] estado completo: {buses}"
    if kind == "delta":
        changed = ", ".join(f"{b['bus']} {b['pct']}% ({b['lat']:.5f},{b['lng']:.5f})" for b in message["upsert"])
        gone = f" | salen: {', '.join(message['remove'])}" if message["remove"] else ""
        return f"[{message['route']} #{message['seq']}] cambios: {changed or '-'}{gone}"
    return json.dumps(message, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--routes", default="C5", help="rutas separadas por comas (máximo 5)")
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()
    token = guest_token(args.base_url)
    ws_url = args.base_url.replace("http", "ws", 1) + "/ws/v1/buses/stream"
    deadline = time.monotonic() + args.seconds
    with connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        ws.send(json.dumps({"action": "subscribe", "routes": args.routes.split(",")}))
        while time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                break
            if message["type"] != "heartbeat":
                print(describe(message), flush=True)


if __name__ == "__main__":
    main()
