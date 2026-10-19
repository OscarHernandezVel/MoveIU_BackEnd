"""Test doubles."""

from __future__ import annotations

from collections.abc import Callable

from core_service.alerts import (
    ChannelDeliveryError,
    DeviceTarget,
    NotificationChannel,
    PushGateway,
    PushPayload,
    ScreenAlert,
)


class RecordingPushGateway(PushGateway):
    """Records every push; can be told to fail a number of times."""

    def __init__(self, fail_times: int = 0, retryable: bool = True) -> None:
        self.sent: list[PushPayload] = []
        self.fail_times = fail_times
        self.retryable = retryable
        self.attempts = 0

    def send(self, payload: PushPayload) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ChannelDeliveryError("push provider unavailable", retryable=self.retryable)
        self.sent.append(payload)


class ScriptedChannel(NotificationChannel):
    """Channel whose behaviour is a callable, to test the dispatcher in isolation."""

    def __init__(self, name: str, behaviour: Callable[[int], None] | None = None) -> None:
        self.name = name
        self.behaviour = behaviour
        self.calls = 0
        self.delivered: list[ScreenAlert] = []

    def deliver(self, target: DeviceTarget, alert: ScreenAlert) -> None:
        self.calls += 1
        if self.behaviour is not None:
            self.behaviour(self.calls)
        self.delivered.append(alert)


# ------------------------------------------------------------------ AI and notifications
import json  # noqa: E402
from typing import Any  # noqa: E402

from core_service.ai import LLMClient, LLMError, LLMResponse, ToolCall  # noqa: E402
from core_service.notifications import TeamNotifier  # noqa: E402


class RecordingNotifier(TeamNotifier):
    def __init__(self, succeed: bool = True) -> None:
        self.messages: list[tuple[str, str]] = []
        self.succeed = succeed

    def send(self, subject: str, body: str) -> bool:
        self.messages.append((subject, body))
        return self.succeed


def text_reply(payload: Any) -> LLMResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return LLMResponse(text=text, content=({"type": "text", "text": text},), input_tokens=100, output_tokens=50)


def tool_reply(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> LLMResponse:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return LLMResponse(
        text="", tool_calls=(ToolCall(call_id, name, arguments),), stop_reason="tool_use", content=(block,)
    )


class ScriptedLLMClient(LLMClient):
    """Plays back a list of responses (or exceptions) and records every request."""

    model = "fake-model"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int
    ) -> LLMResponse:
        self.requests.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        if not self.script:
            raise LLMError("script exhausted", retryable=False)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step
