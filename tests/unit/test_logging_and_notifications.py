"""Log redaction and the team e-mail channel."""

from __future__ import annotations

import json
import logging
import smtplib
from typing import Any

import pytest

from core_service.config import NotificationSettings
from core_service.logging_config import JsonFormatter, configure_logging, redact, redact_value
from core_service.notifications import LoggingTeamNotifier, SmtpTeamNotifier, build_team_notifier


class TestRedaction:
    def test_emails_and_bearer_tokens_are_masked_in_free_text(self) -> None:
        text = redact("user ana@example.com sent Bearer abc.def-123 ok")
        assert "ana@example.com" not in text and "abc.def-123" not in text and "[REDACTED_EMAIL]" in text

    @pytest.mark.parametrize("key", ["password", "Authorization", "push_token", "api_key", "email", "jwt_secret"])
    def test_sensitive_fields_are_masked_by_name(self, key: str) -> None:
        assert redact_value(key, "value") == "[REDACTED]"
        assert redact_value("bus_id", "1045") == "1045"

    def test_the_json_formatter_never_emits_secrets(self) -> None:
        record = logging.LogRecord("core", logging.INFO, __file__, 1, "login of %s", ("ana@example.com",), None)
        record.password = "Str0ngPassw0rd"  # extra field added through ``extra=``
        record.bus_id = "1045"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["password"] == "[REDACTED]" and payload["bus_id"] == "1045"
        assert "ana@example.com" not in payload["message"] and payload["level"] == "INFO"

    def test_exceptions_are_included_and_redacted(self) -> None:
        try:
            raise ValueError("bad value for ana@example.com")
        except ValueError:
            import sys

            record = logging.LogRecord("core", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError" in payload["exception"] and "ana@example.com" not in payload["exception"]

    def test_configure_logging_installs_one_json_handler(self) -> None:
        root = logging.getLogger()
        saved = root.handlers[:], root.level
        try:
            configure_logging("warning")
            assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, JsonFormatter)
            assert root.level == logging.WARNING
        finally:
            root.handlers, root.level = saved[0], saved[1]


class FakeSmtp:
    instances: list[FakeSmtp] = []
    fail = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        if FakeSmtp.fail:
            raise smtplib.SMTPConnectError(421, "down")
        self.host, self.port, self.sent, self.login_args, self.tls = host, port, [], None, False
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def starttls(self, context: Any) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: Any) -> None:
        self.sent.append(message)


class TestTeamNotifier:
    @pytest.fixture(autouse=True)
    def fake_smtp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeSmtp.instances, FakeSmtp.fail = [], False
        monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)

    def test_without_smtp_settings_the_notice_only_goes_to_the_log(self) -> None:
        notifier = build_team_notifier(NotificationSettings())
        assert isinstance(notifier, LoggingTeamNotifier) and notifier.send("s", "b") is False

    def test_an_email_is_sent_over_tls_with_login(self) -> None:
        settings = NotificationSettings(
            team_emails=("a@x.co", "b@x.co"), smtp_host="smtp.x.co", smtp_user="u", smtp_password="p"
        )
        notifier = build_team_notifier(settings)
        assert isinstance(notifier, SmtpTeamNotifier)
        assert notifier.send("Asunto\r\nBcc: evil@x.co", "cuerpo") is True
        smtp = FakeSmtp.instances[0]
        message = smtp.sent[0]
        assert smtp.tls and smtp.login_args == ("u", "p") and message["To"] == "a@x.co, b@x.co"
        assert "\n" not in message["Subject"] and "\r" not in message["Subject"]  # no header injection

    def test_a_smtp_failure_returns_false_instead_of_raising(self) -> None:
        FakeSmtp.fail = True
        notifier = SmtpTeamNotifier("smtp.x.co", 587, "", "", "from@x.co", ["a@x.co"])
        assert notifier.send("s", "b") is False
