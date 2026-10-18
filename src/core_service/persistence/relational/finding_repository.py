"""Technical findings."""

from __future__ import annotations

from collections.abc import Sequence

from core_service.models import Finding, FindingKind, FindingSeverity
from core_service.persistence.relational.database import Database, Row


def _finding(row: Row) -> Finding:
    return Finding(
        finding_id=row["finding_id"],
        bus_id=row["bus_id"],
        kind=FindingKind(row["kind"]),
        severity=FindingSeverity(row["severity"]),
        description=row["description"],
        source=row["source"],
        created_at=row["created_at"],
        status=row["status"],
        notified_at=row["notified_at"],
    )


class FindingRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, finding: Finding) -> None:
        self._db.execute(
            "INSERT INTO technical_findings (finding_id, bus_id, kind, severity, description, source, status, "
            "notified_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                finding.finding_id,
                finding.bus_id,
                finding.kind.value,
                finding.severity.value,
                finding.description,
                finding.source,
                finding.status,
                finding.notified_at,
                finding.created_at,
            ),
        )

    def list_recent(self, limit: int = 50, status: str | None = None, source: str | None = None) -> list[Finding]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query_all(
            f"SELECT * FROM technical_findings {where} ORDER BY created_at DESC LIMIT %s",  # noqa: S608 - clauses are constants
            (*params, limit),
        )
        return [_finding(r) for r in rows]

    def list_unnotified_since(self, since: float) -> list[Finding]:
        rows = self._db.query_all(
            "SELECT * FROM technical_findings WHERE notified_at IS NULL AND created_at >= %s ORDER BY created_at",
            (since,),
        )
        return [_finding(r) for r in rows]

    def mark_notified(self, finding_ids: Sequence[str], now: float) -> None:
        with self._db.transaction():
            for finding_id in finding_ids:
                self._db.execute(
                    "UPDATE technical_findings SET notified_at = %s WHERE finding_id = %s", (now, finding_id)
                )
