"""Language-model access behind a small interface.

The rest of the system only knows :class:`LLMClient`. Two implementations ship:

* :class:`OpenAICompatibleClient` - any server that speaks the OpenAI chat-completions protocol, which is what
  open-source model servers expose (vLLM, Ollama, llama.cpp, LocalAI...). This is the default of the open-source
  stack: the model is an open-weights one running on infrastructure the project controls.
* :class:`AnthropicMessagesClient` - the Anthropic Messages API, kept as an optional, pluggable provider.

Both go through an injectable transport, so tests replace them with a scripted fake (no network, no cost) and the
orchestrator never changes. Messages use provider-neutral content blocks (text / tool_use / tool_result); each
client translates them to its own wire format.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import ssl
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The model could not be used. ``retryable`` tells if trying again later can help."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    content: tuple[dict[str, Any], ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(ABC):
    model: str = "unknown"

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        """Return the model's next turn (final text and/or tool calls)."""


#: ``(url, headers, body, timeout) -> (http_status, decoded_json)``
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, Any]]]


def urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - https enforced by the client
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return error.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LLMError(f"network error: {error}") from error


def check_base_url(url: str, *, allow_private_http: bool) -> None:
    """HTTPS is mandatory, except for a model server on the same private network (container name, loopback, LAN)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("the AI base URL must be an http(s) URL")
    if parsed.scheme == "https":
        return
    if not allow_private_http or not _is_private_host(parsed.hostname):
        raise ValueError("the AI base URL must use https (plain http is only accepted for private hosts)")


def _is_private_host(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")) or "." not in host:
        return True  # "llm" (a Docker/Kubernetes service name), localhost, *.internal
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


@dataclass
class OpenAICompatibleClient(LLMClient):
    """Chat-completions with function calling against an open-source model server (vLLM, Ollama, ...).

    ``base_url`` includes the API version, e.g. ``http://llm:8000/v1`` (vLLM) or ``http://ollama:11434/v1``.
    The API key is optional: local servers usually run without one."""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 120.0  # a self-hosted model on modest hardware answers slower than a hosted one
    transport: Transport = field(default=urllib_transport)
    allow_private_http: bool = True

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("a model name is required")
        check_base_url(self.base_url, allow_private_http=self.allow_private_http)

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *self._messages(messages)],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
                }
                for t in tools
            ]
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        status, payload = self.transport(
            self.base_url.rstrip("/") + "/chat/completions",
            headers,
            json.dumps(body).encode("utf-8"),
            self.timeout_seconds,
        )
        if status == 429 or status >= 500:
            raise LLMError(f"model server unavailable (HTTP {status})", retryable=True)
        if status >= 400:
            raise LLMError(f"model server rejected the request (HTTP {status})", retryable=False)  # never log the body
        return self._parse(payload)

    # ------------------------------------------------------------------ translation
    @staticmethod
    def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-neutral blocks -> chat-completions messages (tool results become ``role: tool`` messages)."""
        wire: list[dict[str, Any]] = []
        for message in messages:
            role, content = message["role"], message["content"]
            if isinstance(content, str):
                wire.append({"role": role, "content": content})
                continue
            text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
            calls = [
                {
                    "id": str(b["id"]),
                    "type": "function",
                    "function": {"name": str(b["name"]), "arguments": json.dumps(b.get("input") or {})},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                wire.append(entry)
                continue
            for block in content:  # a user turn: tool results first (they must follow the tool calls), then text
                if block.get("type") == "tool_result":
                    wire.append(
                        {"role": "tool", "tool_call_id": str(block["tool_use_id"]), "content": str(block["content"])}
                    )
            if text:
                wire.append({"role": "user", "content": text})
        return wire

    @staticmethod
    def _parse(payload: dict[str, Any]) -> LLMResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("unexpected model server answer", retryable=False)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMError("unexpected model server answer", retryable=False)
        text = message.get("content") or ""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict):
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}  # a malformed call is answered by the tool layer as an argument error
            call = ToolCall(
                str(raw.get("id")), str(function.get("name")), arguments if isinstance(arguments, dict) else {}
            )
            calls.append(call)
            blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
        finish = str(choice.get("finish_reason") or "stop")
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish, finish)
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            tool_calls=tuple(calls),
            stop_reason="tool_use" if calls else stop,
            content=tuple(blocks),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


@dataclass
class AnthropicMessagesClient(LLMClient):
    api_key: str
    model: str = "claude-opus-5-5"
    base_url: str = "https://api.anthropic.com"
    timeout_seconds: float = 30.0
    transport: Transport = field(default=urllib_transport)
    api_version: str = "2023-06-01"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("an API key is required")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("the AI base URL must use https")

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
        }
        status, payload = self.transport(
            self.base_url.rstrip("/") + "/v1/messages", headers, json.dumps(body).encode("utf-8"), self.timeout_seconds
        )
        if status == 429 or status >= 500:
            raise LLMError(f"provider unavailable (HTTP {status})", retryable=True)
        if status >= 400:
            # never log the payload: it may echo prompt content
            raise LLMError(f"provider rejected the request (HTTP {status})", retryable=False)
        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict[str, Any]) -> LLMResponse:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise LLMError("unexpected provider answer", retryable=False)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        str(block.get("id")), str(block.get("name")), arguments if isinstance(arguments, dict) else {}
                    )
                )
        usage = payload.get("usage") or {}
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            stop_reason=str(payload.get("stop_reason", "end_turn")),
            content=tuple(b for b in blocks if isinstance(b, dict)),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
