"""Operación diaria del despliegue autoalojado en un servidor pequeño (opción A, 2 vCPU).

Envuelve ``docker compose -f docker-compose.prod.yml -f docker-compose.2vcpu.yml`` para que nadie tenga
que recordar comandos largos. Ejecutar en el servidor, desde esta carpeta::

    python3 ops.py check                       # ¿el servidor y los archivos están listos?
    python3 ops.py up                          # construir y arrancar; espera a que todo esté sano
    python3 ops.py status                      # contenedores, consumo y salud pública
    python3 ops.py post-install                # permisos del rol de solo lectura (tras el primer arranque)
    python3 ops.py ingest                      # simula la carga del catálogo de demostración
    python3 ops.py ingest --apply              # la aplica y recarga las réplicas
    python3 ops.py ingest --file rutas.json --apply
    python3 ops.py make-admin admin@dominio.co
    python3 ops.py backup                      # copia inmediata (además del cron)
    python3 ops.py update                      # git pull + reconstruir + reiniciar el backend
    python3 ops.py logs core                   # registros en vivo de un servicio
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
COMPOSE = [
    "docker", "compose",
    "-f", str(HERE / "docker-compose.prod.yml"),
    "-f", str(HERE / "docker-compose.2vcpu.yml"),
]
REQUIRED_FILES = [
    ".env", "runtime/valkey.conf", "certs/ca.crt", "certs/postgres.crt", "certs/postgres.key",
    "certs/valkey.crt", "certs/valkey.key", "secrets/jwt_secret", "secrets/ingest_key",
    "secrets/database_url", "secrets/database_migration_url", "secrets/redis_url",
    "secrets/pg_app_password", "secrets/pg_migrator_password", "secrets/pg_reader_password",
    "secrets/smtp_password", "secrets/ai_api_key",
]
EXPECTED_OWNERS = {"secrets/jwt_secret": 10001, "certs/postgres.key": 1000, "certs/valkey.key": 999}
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DEFAULT_PACKAGE = "demo_data/pasto_c1_c4.json"


# ----------------------------------------------------------------------------------------- utilidades

def ok(msg: str) -> None:
    print(f"  [ok]    {msg}")


def warn(msg: str) -> None:
    print(f"  [aviso] {msg}")


def fail(msg: str) -> None:
    print(f"  [falla] {msg}")


def run(args: list[str], *, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    shown = args[len(COMPOSE):] if args[: len(COMPOSE)] == COMPOSE else args
    print("> " + " ".join(shown))
    return subprocess.run(args, check=check, text=True, **kwargs)  # type: ignore[call-overload]


def compose(*args: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return run([*COMPOSE, *args], check=check, **kwargs)


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = HERE / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def meminfo_mb(field: str) -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def services_health() -> list[dict[str, str]]:
    """Estado de los contenedores (docker compose ps en JSON: una línea por contenedor o un arreglo)."""
    out = subprocess.run([*COMPOSE, "ps", "--format", "json"], capture_output=True, text=True, check=False)
    text = out.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wait_healthy(timeout: float = 240.0) -> bool:
    print(f"Esperando a que los servicios estén sanos (máximo {int(timeout)} s)...")
    deadline = time.monotonic() + timeout
    pending: list[str] = []
    while time.monotonic() < deadline:
        rows = services_health()
        pending = [
            str(row.get("Service")) + ":" + str(row.get("Health") or row.get("State"))
            for row in rows
            if row.get("Service") in {"core", "postgres", "valkey"} and row.get("Health") != "healthy"
        ]
        if rows and not pending:
            print("Todos sanos.")
            return True
        time.sleep(5)
    print("Algún servicio no quedó sano a tiempo: " + ", ".join(pending), file=sys.stderr)
    print("Revise:  python3 ops.py logs core   (o postgres / valkey)", file=sys.stderr)
    return False


def public_health(domain: str) -> str:
    url = f"https://api.{domain}/api/v1/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:  # noqa: S310 - URL del dominio propio
            return f"{url} -> {response.status}"
    except urllib.error.HTTPError as exc:
        return f"{url} -> {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return f"{url} -> sin respuesta ({exc})"


# ------------------------------------------------------------------------------------------- comandos

def cmd_check(_: argparse.Namespace) -> int:
    problems = 0
    print("Servidor:")
    cpus = os.cpu_count() or 0
    if cpus >= 2:
        ok(f"{cpus} vCPU")
    else:
        fail(f"{cpus} vCPU (mínimo 2)")
        problems += 1
    ram, swap = meminfo_mb("MemTotal"), meminfo_mb("SwapTotal")
    if ram < 0:
        warn("no se pudo leer la memoria (no es Linux)")
    elif ram >= 3500:
        ok(f"{ram} MB de RAM")
    elif ram >= 1800:
        warn(f"{ram} MB de RAM: active la variante de 2 GB en .env (ver docker-compose.2vcpu.yml)")
    else:
        fail(f"{ram} MB de RAM: insuficiente (mínimo 2 GB)")
        problems += 1
    if swap >= 1024:
        ok(f"{swap} MB de swap")
    elif swap >= 0:
        warn(f"{swap} MB de swap (recomendado 2048; lo crea setup_server.sh)")
    free_gb = shutil.disk_usage(HERE).free // 1024**3
    if free_gb >= 20:
        ok(f"{free_gb} GB libres en disco")
    else:
        warn(f"{free_gb} GB libres en disco (recomendado 20 o más)")

    print("Docker:")
    if shutil.which("docker") is None:
        fail("docker no está instalado (sudo bash setup_server.sh)")
        return 1
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False)
    if version.returncode == 0:
        ok(version.stdout.strip())
    else:
        fail("falta el complemento docker compose v2")
        problems += 1
    daemon = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if daemon.returncode == 0:
        ok("el servicio docker responde")
    else:
        fail("docker no responde o su usuario no está en el grupo docker")
        problems += 1

    print("Archivos generados por init_secrets.py:")
    missing = [name for name in REQUIRED_FILES if not (HERE / name).exists()]
    if missing:
        fail("faltan: " + ", ".join(missing))
        fail("ejecute: sudo python3 init_secrets.py --domain <dominio> --email <correo>")
        problems += 1
    else:
        ok("secretos, certificados, valkey.conf y .env presentes")
        for name, uid in EXPECTED_OWNERS.items():
            owner = (HERE / name).stat().st_uid
            if owner == uid:
                ok(f"{name} pertenece al uid {uid}")
            else:
                warn(f"{name} pertenece al uid {owner}, se esperaba {uid} (ejecute init_secrets.py con sudo)")

    domain = read_env().get("DOMAIN", "")
    print("DNS:")
    if not domain:
        fail("DOMAIN no está en .env")
        problems += 1
    for host in (f"app.{domain}", f"api.{domain}") if domain else ():
        try:
            addresses = sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443)})
            ok(f"{host} -> {', '.join(addresses)} (debe ser la IP pública de este servidor)")
        except socket.gaierror:
            fail(f"{host} no resuelve: cree el registro A en su DNS")
            problems += 1

    print()
    print("Listo para: python3 ops.py up" if not problems else f"{problems} problema(s) por resolver.")
    return 1 if problems else 0


def cmd_up(_: argparse.Namespace) -> int:
    compose("up", "-d", "--build", "--remove-orphans")
    return 0 if wait_healthy() else 1


def cmd_status(_: argparse.Namespace) -> int:
    compose("ps")
    print()
    table = "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
    subprocess.run(["docker", "stats", "--no-stream", "--format", table], check=False)
    domain = read_env().get("DOMAIN")
    if domain:
        print()
        print("Salud pública: " + public_health(domain))
    return 0


def cmd_post_install(_: argparse.Namespace) -> int:
    compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "core",
            "-f", "/sql/02-reader-grants.sql")
    print("Permisos de core_reader aplicados.")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    mount: list[str] = []
    target = args.file
    host_file = Path(args.file)
    if host_file.is_file():
        # Archivo del servidor: se monta en el contenedor de un solo uso.
        mount = ["-v", f"{host_file.resolve()}:/tmp/catalog.json:ro"]
        target = "/tmp/catalog.json"
    base = ["run", "--rm", "--no-deps", *mount, "core", "python", "-m", "core_service.ingestion",
            "--source", "package", "--file", target]
    compose(*base, "--dry-run")
    if not args.apply:
        suffix = "" if args.file == DEFAULT_PACKAGE else f" --file {args.file}"
        print(f"\nEso fue una simulación. Para cargar: python3 ops.py ingest --apply{suffix}")
        return 0
    compose(*base)
    print("Recargando el catálogo en todas las réplicas...")
    compose("restart", "core")
    return 0 if wait_healthy() else 1


# psql sustituye la variable email como literal SQL escapado; el correo nunca se concatena al texto del SQL.
MAKE_ADMIN_SQL = "UPDATE users SET role = 'admin' WHERE lower(email) = lower(:" + "'email');\n"


def cmd_make_admin(args: argparse.Namespace) -> int:
    email = args.email.strip()
    if not EMAIL.match(email):
        print("Correo inválido.", file=sys.stderr)
        return 2
    result = compose("exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-v", f"email={email}",
                     "-U", "postgres", "-d", "core", input=MAKE_ADMIN_SQL, capture_output=True)
    if (result.stdout or "").strip().endswith("UPDATE 1"):
        print(f"{email} ahora es administrador (debe cerrar sesión y volver a entrar).")
        return 0
    print(f"No existe la cuenta {email}: regístrela primero (POST /api/v1/auth/register).", file=sys.stderr)
    return 1


def cmd_backup(_: argparse.Namespace) -> int:
    return run([sys.executable, str(HERE / "backup_db.py")], check=False).returncode


def cmd_update(args: argparse.Namespace) -> int:
    if not args.no_pull:
        if not (REPO / ".git").exists():
            print("El proyecto no es un repositorio git: copie la versión nueva y use --no-pull.")
            return 1
        run(["git", "-C", str(REPO), "pull", "--ff-only"])
    compose("build", "core")
    compose("up", "-d", "--no-deps", "core")
    return 0 if wait_healthy() else 1


def cmd_logs(args: argparse.Namespace) -> int:
    services = [args.service] if args.service else []
    try:
        return compose("logs", "-f", "--tail", str(args.tail), *services, check=False).returncode
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verifica servidor, Docker, archivos y DNS").set_defaults(func=cmd_check)
    sub.add_parser("up", help="construye y arranca todo").set_defaults(func=cmd_up)
    sub.add_parser("status", help="contenedores, consumo y salud").set_defaults(func=cmd_status)
    sub.add_parser("post-install", help="permisos del rol de solo lectura").set_defaults(func=cmd_post_install)
    ingest = sub.add_parser("ingest", help="carga rutas, paradas y buses (simula salvo --apply)")
    ingest.add_argument("--file", default=DEFAULT_PACKAGE,
                        help="paquete core-catalog/1: ruta dentro de la imagen o archivo del servidor")
    ingest.add_argument("--apply", action="store_true", help="aplicar los cambios")
    ingest.set_defaults(func=cmd_ingest)
    admin = sub.add_parser("make-admin", help="da el rol admin a una cuenta ya registrada")
    admin.add_argument("email")
    admin.set_defaults(func=cmd_make_admin)
    sub.add_parser("backup", help="copia de la base de datos ahora").set_defaults(func=cmd_backup)
    update = sub.add_parser("update", help="actualiza el backend a la última versión")
    update.add_argument("--no-pull", action="store_true", help="no ejecutar git pull")
    update.set_defaults(func=cmd_update)
    logs = sub.add_parser("logs", help="registros en vivo")
    logs.add_argument("service", nargs="?", choices=["core", "postgres", "valkey", "caddy", "uptime-kuma"])
    logs.add_argument("--tail", type=int, default=100)
    logs.set_defaults(func=cmd_logs)
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(f"El comando falló con código {exc.returncode}.", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
