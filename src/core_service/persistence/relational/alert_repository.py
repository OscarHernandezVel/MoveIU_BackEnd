"""Proximity alerts (source of truth; the in-memory index is rebuilt from here)."""

from __future__ import annotations

from core_service.models import AlertStatus, Direction, ProximityAlert
from core_service.persistence.relational.database import Database, Row


def _alert(row: Row) -> ProximityAlert:
    return ProximityAlert(
        alert_id=row["alert_id"],
        device_id=row["device_id"],
        route_id=row["route_id"],
        direction=Direction(row["direction"]),
        stop_id=row["stop_id"],
        stops_before=row["stops_before"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        bus_id=row["bus_id"],
        status=AlertStatus(row["status"]),
        sent_at=row["sent_at"],
    )


class AlertRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, alert: ProximityAlert) -> None:
        self._db.execute(
            "INSERT INTO alerts (alert_id, device_id, route_id, direction, stop_id, bus_id, stops_before, "
            "status, created_at, expires_at, sent_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                alert.alert_id,
                alert.device_id,
                alert.route_id,
                alert.direction.value,
                alert.stop_id,
                alert.bus_id,
                alert.stops_before,
                alert.status.value,
                alert.created_at,
                alert.expires_at,
                alert.sent_at,
            ),
        )

    def find(self, alert_id: str) -> ProximityAlert | None:
        row = self._db.query_one("SELECT * FROM alerts WHERE alert_id = %s", (alert_id,))
        return None if row is None else _alert(row)

    def count_active(self, device_id: str, now: float) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM alerts WHERE device_id = %s AND status = 'active' AND expires_at > %s",
            (device_id, now),
        )
        return 0 if row is None else int(row["n"])

    def list_active_for_device(self, device_id: str, now: float) -> list[ProximityAlert]:
        rows = self._db.query_all(
            "SELECT * FROM alerts WHERE device_id = %s AND status = 'active' AND expires_at > %s "
            "ORDER BY created_at DESC",
            (device_id, now),
        )
        return [_alert(r) for r in rows]

    def list_all_active(self, now: float) -> list[ProximityAlert]:
        return [
            _alert(r)
            for r in self._db.query_all(
                "SELECT * FROM alerts WHERE status = 'active' AND expires_at > %s ORDER BY created_at", (now,)
            )
        ]

    def transition(self, alert_id: str, expected: AlertStatus, new: AlertStatus, sent_at: float | None = None) -> bool:
        """Compare-and-set of the status: only one caller can win a transition.

        Returning an alert to ``ACTIVE`` (rollback of a failed delivery) clears ``sent_at``."""
        return (
            self._db.execute(
                "UPDATE alerts SET status = %s, "
                "sent_at = CASE WHEN %s::text = 'active' THEN NULL ELSE COALESCE(%s, sent_at) END "
                "WHERE alert_id = %s AND status = %s",
                (new.value, new.value, sent_at, alert_id, expected.value),
            )
            > 0
        )

    def cancel(self, alert_id: str, device_id: str) -> ProximityAlert | None:
        """Cancel an active alert owned by ``device_id``; returns it, or None if not found.

        One ``UPDATE ... RETURNING``: if a delivery or an expiry wins the race, this matches no row."""
        rows = self._db.write_returning(
            "UPDATE alerts SET status = 'cancelled' "
            "WHERE alert_id = %s AND device_id = %s AND status = 'active' RETURNING *",
            (alert_id, device_id),
        )
        return _alert(rows[0]) if rows else None

    def expire_due(self, now: float) -> list[ProximityAlert]:
        """Mark active alerts whose time is over as expired and return exactly those rows.

        Atomic: an alert delivered or cancelled concurrently is never overwritten."""
        rows = self._db.write_returning(
            "UPDATE alerts SET status = 'expired' WHERE status = 'active' AND expires_at <= %s RETURNING *", (now,)
        )
        return [_alert(r) for r in rows]
