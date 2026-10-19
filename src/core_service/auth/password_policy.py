"""Password rules of the user story 1.1: at least 8 characters, one digit and one uppercase letter."""

from __future__ import annotations

from core_service.errors import ValidationError


class PasswordPolicy:
    def __init__(self, min_length: int = 8, max_length: int = 128) -> None:
        self.min_length = min_length
        self.max_length = max_length  # also bounds the work of the key-derivation function

    def violations(self, password: str) -> list[str]:
        """Message keys of every rule the password breaks (empty when it is acceptable)."""
        problems: list[str] = []
        if len(password) < self.min_length:
            problems.append("password_too_short")
        if len(password) > self.max_length:
            problems.append("password_too_long")
        if not any(ch.isdigit() for ch in password):
            problems.append("password_no_digit")
        if not any(ch.isupper() for ch in password):
            problems.append("password_no_uppercase")
        return problems

    def validate(self, password: str) -> None:
        problems = self.violations(password)
        if problems:
            raise ValidationError(
                "password_policy",
                params={"min_length": self.min_length, "max_length": self.max_length},
                details=problems,
            )
