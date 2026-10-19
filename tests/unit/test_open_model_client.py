"""Open-weights model server (vLLM, Ollama...) behind the same LLMClient interface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core_service.ai import AnthropicMessagesClient, LLMError, OpenAICompatibleClient
from core_service.config import AISettings, Settings
from core_service.container import build_llm_client
from core_service.errors import ValidationError

TOOL = {
    "name": "get_bus_state",
    "description": "Estado de un bus",
    "input_schema": {"type": "object", "properties": {}},
}


def transport(status: int, payload: dict[str, Any], seen: list[Any] | None = None):  # noqa: ANN201
    def send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict[str, Any]]:
        if seen is not None:
            seen.append((url, headers, json.loads(body), timeout))
        return status, payload

    return send


def client(status: int = 200, payload: dict[str, Any] | None = None, seen: list[Any] | None = None, **kw: Any) -> Any:
    return OpenAICompatibleClient("http://llm:8000/v1", "qwen3", transport=transport(status, payload or {}, seen), **kw)


class TestRequest:
    def test_it_speaks_chat_completions_with_functions(self) -> None:
        seen: list[Any] = []
        payload = {
            "choices": [{"message": {"content": "hola"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2},
        }
        response = client(payload=payload, seen=seen).complete(
            "sistema", [{"role": "user", "content": "q"}], [TOOL], 300
        )
        url, headers, body, _ = seen[0]
        assert url == "http://llm:8000/v1/chat/completions" and "authorization" not in headers
        assert body["model"] == "qwen3" and body["max_tokens"] == 300
        assert body["messages"][0] == {"role": "system", "content": "sistema"}
        assert body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_bus_state",
                    "description": "Estado de un bus",
                    "parameters": TOOL["input_schema"],
                },
            }
        ]
        assert (response.text, response.stop_reason, response.input_tokens, response.output_tokens) == (
            "hola",
            "end_turn",
            9,
            2,
        )

    def test_an_optional_key_is_sent_as_a_bearer_token(self) -> None:
        seen: list[Any] = []
        client(payload={"choices": [{"message": {"content": "x"}}]}, seen=seen, api_key="k").complete("s", [], [], 5)
        assert seen[0][1]["authorization"] == "Bearer k" and "tools" not in seen[0][2]

    def test_tool_rounds_are_translated_both_ways(self) -> None:
        seen: list[Any] = []
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "get_bus_state", "arguments": '{"bus_id": "1045"}'},
        }
        payload = {"choices": [{"message": {"content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        response = client(payload=payload, seen=seen).complete("s", [{"role": "user", "content": "q"}], [TOOL], 50)
        assert response.stop_reason == "tool_use" and response.tool_calls[0].arguments == {"bus_id": "1045"}
        assert response.content == (
            {"type": "tool_use", "id": "c1", "name": "get_bus_state", "input": {"bus_id": "1045"}},
        )
        # the orchestrator sends that turn back: blocks become tool_calls / role=tool messages
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": list(response.content)},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"ok": true}', "is_error": False}],
            },
        ]
        client(payload={"choices": [{"message": {"content": "{}"}}]}, seen=seen).complete("s", history, [TOOL], 50)
        wire = seen[1][2]["messages"]
        assert wire[2] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_bus_state", "arguments": '{"bus_id": "1045"}'},
                }
            ],
        }
        assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}

    def test_malformed_tool_arguments_become_an_empty_call_not_a_crash(self) -> None:
        call = {"id": "c1", "function": {"name": "get_bus_state", "arguments": "{not json"}}
        payload = {"choices": [{"message": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        assert client(payload=payload).complete("s", [], [TOOL], 5).tool_calls[0].arguments == {}


class TestFailures:
    @pytest.mark.parametrize(
        ("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (401, False)]
    )
    def test_http_errors_are_classified_and_never_echo_the_answer(self, status: int, retryable: bool) -> None:
        with pytest.raises(LLMError) as caught:
            client(status, {"error": "secret prompt echo"}).complete("s", [], [], 5)
        assert caught.value.retryable is retryable and "secret" not in str(caught.value)

    @pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": "x"}]}])
    def test_unexpected_answers_are_errors(self, payload: dict[str, Any]) -> None:
        with pytest.raises(LLMError):
            client(payload=payload).complete("s", [], [], 5)

    @pytest.mark.parametrize(
        "url",
        ["http://llm:8000/v1", "http://localhost:11434/v1", "http://10.0.0.5:8000/v1", "https://models.example.org/v1"],
    )
    def test_https_is_required_except_on_private_hosts(self, url: str) -> None:
        OpenAICompatibleClient(url, "m")

    @pytest.mark.parametrize("url", ["http://models.example.org/v1", "http://8.8.8.8/v1", "ftp://llm/v1", "llm"])
    def test_plain_http_to_the_internet_is_refused(self, url: str) -> None:
        with pytest.raises(ValueError):
            OpenAICompatibleClient(url, "m")

    def test_a_model_name_is_required(self) -> None:
        with pytest.raises(ValueError):
            OpenAICompatibleClient("http://llm/v1", "")


class TestConfiguration:
    ENV = {
        "CORE_ENV": "development",
        "CORE_DATABASE_URL": "postgresql://core:s@localhost:5432/core",
        "CORE_AI_ENABLED": "true",
    }

    def test_the_open_provider_needs_a_server_and_a_model_but_no_key(self) -> None:
        env = {
            **self.ENV,
            "CORE_AI_PROVIDER": "openai_compat",
            "CORE_AI_BASE_URL": "http://llm:8000/v1",
            "CORE_AI_MODEL": "qwen3",
        }
        ai = Settings.from_env(env).ai
        assert isinstance(build_llm_client(ai), OpenAICompatibleClient)

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ({"CORE_AI_PROVIDER": "openai_compat"}, "CORE_AI_BASE_URL"),
            ({"CORE_AI_PROVIDER": "openai_compat", "CORE_AI_BASE_URL": "http://llm:8000/v1"}, "CORE_AI_MODEL"),
            ({"CORE_AI_PROVIDER": "magic"}, "CORE_AI_PROVIDER"),
            ({}, "CORE_AI_API_KEY"),
        ],
    )
    def test_incomplete_ai_settings_fail_at_start(self, extra: dict[str, str], message: str) -> None:
        with pytest.raises(ValidationError) as caught:
            Settings.from_env({**self.ENV, **extra})
        assert message in " ".join(caught.value.details)

    def test_anthropic_stays_available_as_an_optional_provider(self) -> None:
        ai = Settings.from_env({**self.ENV, "CORE_AI_API_KEY": "key"}).ai
        assert ai.provider == "anthropic" and isinstance(build_llm_client(ai), AnthropicMessagesClient)

    def test_serverless_database_switches(self) -> None:
        env = {
            **self.ENV,
            "CORE_AI_ENABLED": "false",
            "CORE_DB_MIGRATE_ON_START": "false",
            "CORE_DB_PREPARE": "false",
            "CORE_DB_STARTUP_OPTIONS": "false",
            "CRON_SECRET": "c" * 32,
        }
        settings = Settings.from_env(env)
        db = settings.database
        assert (db.migrate_on_start, db.prepare_statements, db.startup_options) == (False, False, False)
        assert settings.security.cron_secret == "c" * 32  # Vercel's own variable name is accepted
        assert AISettings().provider == "anthropic"
