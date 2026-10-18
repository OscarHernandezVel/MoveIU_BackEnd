"""Users and devices."""

from __future__ import annotations

from core_service.errors import ConflictError
from core_service.models import Device, Role, User
from core_service.persistence.relational.database import Database, Row, UniqueViolation


def _user(row: Row) -> User:
    return User(
        user_id=row["user_id"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        password_hash=row["password_hash"],
        role=Role(row["role"]),
        created_at=row["created_at"],
    )


class UserRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, user: User) -> None:
        try:
            self._db.execute(
                "INSERT INTO users (user_id, name, email, phone, password_hash, role, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user.user_id, user.name, user.email, user.phone, user.password_hash, user.role.value, user.created_at),
            )
        except UniqueViolation as error:
            raise ConflictError("email_taken") from error

    def find_by_email(self, email: str) -> User | None:
        row = self._db.query_one("SELECT * FROM users WHERE lower(email) = lower(%s)", (email,))
        return None if row is None else _user(row)

    def find_by_id(self, user_id: str) -> User | None:
        row = self._db.query_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return None if row is None else _user(row)

    def set_role(self, user_id: str, role: Role) -> bool:
        if role is Role.GUEST:
            raise ValueError("guest is not a persistent role")
        return self._db.execute("UPDATE users SET role = %s WHERE user_id = %s", (role.value, user_id)) > 0


class DeviceRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, device: Device, now: float) -> None:
        self._db.execute(
            "INSERT INTO devices (device_id, user_id, push_token, platform, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (device_id) DO UPDATE SET push_token = excluded.push_token, "
            "platform = excluded.platform, updated_at = excluded.updated_at",
            (device.device_id, device.user_id, device.push_token, device.platform, now, now),
        )

    def find(self, device_id: str) -> Device | None:
        row = self._db.query_one("SELECT * FROM devices WHERE device_id = %s", (device_id,))
        if row is None:
            return None
        return Device(row["device_id"], row["push_token"], row["platform"], row["user_id"])

    def update_push_token(self, device_id: str, push_token: str, now: float) -> bool:
        return (
            self._db.execute(
                "UPDATE devices SET push_token = %s, updated_at = %s WHERE device_id = %s", (push_token, now, device_id)
            )
            > 0
        )

    def link_user(self, device_id: str, user_id: str, now: float) -> bool:
        """Attach a guest device (and therefore its alerts) to an account."""
        return (
            self._db.execute(
                "UPDATE devices SET user_id = %s, updated_at = %s WHERE device_id = %s", (user_id, now, device_id)
            )
            > 0
        )
