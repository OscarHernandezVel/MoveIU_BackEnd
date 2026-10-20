"""Copia lógica diaria de PostgreSQL (pg_dump, formato custom) con rotación.

Programe en cron (crontab -e del usuario que administra Docker)::

    15 3 * * *  cd /opt/capacidadbus/deploy/opensource && /usr/bin/python3 backup_db.py >> backups/backup.log 2>&1

Para restaurar con TimescaleDB use ``timescaledb_pre_restore()`` y ``timescaledb_post_restore()`` (ver
docs/despliegue-opensource.md). Para tener copia fuera del servidor sincronice ``backups/`` con restic.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = HERE / "backups"
KEEP_DAYS = 14
COMPOSE = ["docker", "compose", "-f", str(HERE / "docker-compose.prod.yml")]


def main() -> int:
    DEST.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DEST / f"core-{stamp}.dump"
    partial = target.with_suffix(".partial")
    with partial.open("wb") as out:
        result = subprocess.run(
            [*COMPOSE, "exec", "-T", "postgres", "pg_dump", "-U", "postgres", "-d", "core", "-Fc"],
            stdout=out,
            check=False,
        )
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        print(f"{stamp} ERROR: pg_dump terminó con código {result.returncode}", file=sys.stderr)
        return 1
    partial.rename(target)
    target.chmod(0o600)
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for old in DEST.glob("core-*.dump"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            removed += 1
    print(f"{stamp} OK {target.name} ({target.stat().st_size // 1024} KiB), {removed} copias antiguas borradas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
