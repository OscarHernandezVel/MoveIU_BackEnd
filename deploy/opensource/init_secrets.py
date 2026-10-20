"""Prepara un servidor para el despliegue autoalojado (docker-compose.prod.yml).

Genera, sin sobrescribir lo que ya exista:

* ``secrets/``  secretos del servicio y contraseñas de PostgreSQL / Valkey (un archivo por valor);
* ``certs/``    una autoridad certificadora interna y certificados TLS para PostgreSQL y Valkey;
* ``runtime/valkey.conf`` con TLS, contraseña, AOF y ``noeviction``;
* ``.env``      dominio, correo de Let's Encrypt y contraseña del superusuario de PostgreSQL.

Uso, en el servidor y desde esta carpeta (como root para fijar los dueños de los archivos)::

    sudo python3 init_secrets.py --domain capacidadbus.co --email operaciones@capacidadbus.co

Requiere ``openssl``. ``--force`` regenera todo (cambia contraseñas: solo antes del primer arranque).
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRETS = HERE / "secrets"
CERTS = HERE / "certs"
RUNTIME = HERE / "runtime"

# Usuarios dentro de los contenedores que deben poder leer cada archivo.
UID_CORE = 10001     # backend/Dockerfile
UID_POSTGRES = 1000  # timescale/timescaledb-ha (verifique: docker run --rm timescale/timescaledb-ha:pg16 id)
UID_VALKEY = 999     # valkey/valkey:8-alpine (verifique: docker run --rm valkey/valkey:8-alpine id valkey)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def chown(path: Path, uid: int | None) -> None:
    if uid is not None and is_root():
        os.chown(path, uid, uid)


def write(path: Path, content: str, *, uid: int | None, mode: int = 0o600, force: bool = False) -> str:
    """Crea el archivo si no existe y devuelve su valor (el existente si ya estaba)."""
    if path.exists() and not force:
        print(f"  se conserva  {path.relative_to(HERE)}")
        return path.read_text(encoding="utf-8").strip()
    path.write_text(content + "\n", encoding="utf-8")
    os.chmod(path, mode)
    chown(path, uid)
    print(f"  creado       {path.relative_to(HERE)}")
    return content


def openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def make_certs(force: bool) -> None:
    CERTS.mkdir(exist_ok=True)
    ca_key, ca_crt = CERTS / "ca.key", CERTS / "ca.crt"
    if force or not ca_crt.exists():
        openssl("req", "-x509", "-new", "-nodes", "-newkey", "rsa:3072", "-days", "3650",
                "-subj", "/CN=capacidadbus-internal-ca", "-keyout", str(ca_key), "-out", str(ca_crt))
        print("  creado       certs/ca.crt")
    os.chmod(ca_key, 0o600)
    os.chmod(ca_crt, 0o644)
    for name, uid in (("postgres", UID_POSTGRES), ("valkey", UID_VALKEY)):
        key, csr, crt, ext = (CERTS / f"{name}.{suffix}" for suffix in ("key", "csr", "crt", "ext"))
        if crt.exists() and not force:
            print(f"  se conserva  certs/{name}.crt")
            continue
        # El nombre del servicio en Docker (postgres / valkey) va en el certificado: permite verify-full.
        ext.write_text(f"subjectAltName=DNS:{name},DNS:localhost\n", encoding="utf-8")
        openssl("req", "-new", "-nodes", "-newkey", "rsa:3072", "-subj", f"/CN={name}",
                "-keyout", str(key), "-out", str(csr))
        openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                "-days", "825", "-extfile", str(ext), "-out", str(crt))
        csr.unlink()
        ext.unlink()
        os.chmod(key, 0o600)
        chown(key, uid)
        os.chmod(crt, 0o644)
        print(f"  creado       certs/{name}.crt")


VALKEY_CONF = """# Generado por init_secrets.py. Valkey solo con TLS y contraseña, persistencia AOF.
bind 0.0.0.0
port 0
tls-port 6379
tls-cert-file /certs/valkey.crt
tls-key-file /certs/valkey.key
tls-ca-cert-file /certs/ca.crt
tls-auth-clients no
tls-protocols "TLSv1.2 TLSv1.3"
requirepass {password}
appendonly yes
appendfsync everysec
aof-use-rdb-preamble yes
dir /data
maxmemory 512mb
maxmemory-policy noeviction
tcp-keepalive 60
# El backend no usa estos comandos: se desactivan para limitar el daño si la contraseña se filtra.
rename-command FLUSHALL ""
rename-command FLUSHDB ""
rename-command CONFIG ""
rename-command DEBUG ""
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", required=True, help="dominio base: se usan app.<dominio> y api.<dominio>")
    parser.add_argument("--email", required=True, help="correo para Let's Encrypt")
    parser.add_argument("--force", action="store_true", help="regenerar todo (antes del primer arranque)")
    args = parser.parse_args()

    if shutil.which("openssl") is None:
        print("Falta openssl (apt install openssl).", file=sys.stderr)
        return 1
    if not is_root():
        print("Aviso: sin root no se pueden fijar los dueños de los archivos; los contenedores podrían no leerlos.")

    for folder in (SECRETS, RUNTIME):
        folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(SECRETS, 0o755)  # los archivos internos siguen siendo 0600 y de su contenedor
    force = args.force

    print("Certificados internos:")
    make_certs(force)

    print("Secretos:")
    write(SECRETS / "jwt_secret", secrets.token_urlsafe(48), uid=UID_CORE, force=force)
    write(SECRETS / "ingest_key", secrets.token_urlsafe(48), uid=UID_CORE, force=force)
    app_pw = write(SECRETS / "pg_app_password", secrets.token_hex(24), uid=UID_POSTGRES, force=force)
    migrator_pw = write(SECRETS / "pg_migrator_password", secrets.token_hex(24), uid=UID_POSTGRES, force=force)
    write(SECRETS / "pg_reader_password", secrets.token_hex(24), uid=UID_POSTGRES, force=force)
    valkey_pw = write(SECRETS / "valkey_password", secrets.token_hex(32), uid=None, force=force)

    tls = "sslmode=verify-full&sslrootcert=/certs/ca.crt"
    write(SECRETS / "database_url", f"postgresql://core_app:{app_pw}@postgres:5432/core?{tls}",
          uid=UID_CORE, force=force)
    write(SECRETS / "database_migration_url", f"postgresql://core_migrator:{migrator_pw}@postgres:5432/core?{tls}",
          uid=UID_CORE, force=force)
    write(SECRETS / "redis_url", f"rediss://:{valkey_pw}@valkey:6379/0", uid=UID_CORE, force=force)
    # Opcionales: vacíos = función desactivada. Edítelos después si los necesita.
    write(SECRETS / "smtp_password", "", uid=UID_CORE, force=False)
    write(SECRETS / "ai_api_key", "", uid=UID_CORE, force=False)

    print("Configuración:")
    write(RUNTIME / "valkey.conf", VALKEY_CONF.format(password=valkey_pw), uid=UID_VALKEY, force=force)
    env = "\n".join([
        f"DOMAIN={args.domain}",
        f"ACME_EMAIL={args.email}",
        f"POSTGRES_PASSWORD={secrets.token_hex(24)}",
        "TEAM_EMAILS=",
        "SMTP_HOST=",
        "SMTP_PORT=587",
        "SMTP_USER=",
        f"SMTP_SENDER=no-responder@{args.domain}",
        "AI_ENABLED=false",
    ])
    write(HERE / ".env", env, uid=None, force=force)

    print("\nListo. Siguiente paso: docker compose -f docker-compose.prod.yml up -d --build")
    print("La clave de ingesta para los buses (EDGE_INGEST_KEY) está en secrets/ingest_key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
