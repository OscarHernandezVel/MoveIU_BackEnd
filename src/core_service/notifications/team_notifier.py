"""E-mail notifications to the technical team (findings, weekly audit report)."""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from abc import ABC, abstractmethod
from collections.abc import Sequence
from email.message import EmailMessage

from core_service.config import NotificationSettings

log = logging.getLogger(__name__)


class TeamNotifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> bool:
        """Send a message to the team; returns whether it was actually sent."""


class LoggingTeamNotifier(TeamNotifier):
    """Used when no SMTP server is configured: the notice only goes to the log."""

    def send(self, subject: str, body: str) -> bool:
        log.info("Team notice (no SMTP configured): %s", subject)
        return False


class SmtpTeamNotifier(TeamNotifier):
    def __init__(self, host: str, port: int, user: str, password: str, sender: str, recipients: Sequence[str]) -> None:
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._sender = sender
        self._recipients = list(recipients)

    def send(self, subject: str, body: str) -> bool:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message["Subject"] = re.sub(r"[\x00-\x1f\x7f]+", " ", subject)[:200]  # header injection guard (CR, LF, ...)
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException):
            log.exception("Could not send the team e-mail")
            return False


def build_team_notifier(settings: NotificationSettings) -> TeamNotifier:
    if settings.smtp_host and settings.team_emails:
        return SmtpTeamNotifier(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password,
            settings.sender,
            settings.team_emails,
        )
    return LoggingTeamNotifier()
