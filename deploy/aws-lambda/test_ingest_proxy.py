"""Prueba del Lambda de ingesta contra un backend falso local. Ejecutar: python test_ingest_proxy.py"""

from __future__ import annotations

import importlib
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

received: list[dict[str, object]] = []


class FakeBackend(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - nombre impuesto por http.server
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append({"path": self.path, "key": self.headers.get("X-Ingest-Key"), "body": body})
        status = 200 if self.headers.get("X-Ingest-Key") == "clave-buena" else 401
        payload = json.dumps({"status": "applied"} if status == 200 else {"error": {"code": "invalid_key"}})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args: object) -> None:
        pass


def event(path: str, key: str, method: str = "POST") -> dict[str, object]:
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": {"content-type": "application/json", "x-ingest-key": key, "host": "ignorado"},
        "body": json.dumps({"ts": 1790000000, "seated": 10}),
        "isBase64Encoded": False,
    }


def load_proxy(backend_url: str):  # type: ignore[no-untyped-def]
    os.environ["BACKEND_URL"] = backend_url
    os.environ["BACKEND_TIMEOUT_S"] = "2"
    import ingest_proxy

    return importlib.reload(ingest_proxy)


def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), FakeBackend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = load_proxy(f"http://127.0.0.1:{server.server_port}/")

    ok = proxy.handler(event("/api/v1/internal/buses/2101/telemetry", "clave-buena"), None)
    assert ok["statusCode"] == 200 and json.loads(ok["body"]) == {"status": "applied"}, ok
    assert received[-1]["path"] == "/api/v1/internal/buses/2101/telemetry"
    assert received[-1]["key"] == "clave-buena" and json.loads(received[-1]["body"])["seated"] == 10

    denied = proxy.handler(event("/api/v1/internal/telemetry/batch", "clave-mala"), None)
    assert denied["statusCode"] == 401, denied  # el backend decide; el Lambda no inventa respuestas

    calls = len(received)
    for path, method in (("/api/v1/auth/login", "POST"), ("/api/v1/internal/x", "GET"), ("/api/v1/internal/../x", "POST")):
        assert proxy.handler(event(path, "clave-buena", method), None)["statusCode"] == 404
    assert len(received) == calls, "las rutas que no son de ingesta no deben llegar al backend"

    server.shutdown()
    with socket.socket() as probe:  # un puerto libre: nadie escucha, como un EC2 apagado
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    down = load_proxy(f"http://127.0.0.1:{closed_port}").handler(event("/api/v1/internal/x", "clave-buena"), None)
    assert down["statusCode"] == 503 and "backend_unavailable" in down["body"], down
    print("ok: reenvío, 401 del backend, rutas bloqueadas y 503 con el backend apagado")


if __name__ == "__main__":
    main()
