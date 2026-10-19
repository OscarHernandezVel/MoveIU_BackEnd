"""AI building blocks: schema validation, privacy guard, tools, breaker, client and rule analyzer."""

from __future__ import annotations

import json

import pytest

from core_service.ai import (
    AIFinding,
    AnalysisResponseValidator,
    Anomaly,
    AnthropicMessagesClient,
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    InvalidAnalysisError,
    LLMError,
    PrivacyGuard,
    RecommendedAction,
    RuleBasedAnalyzer,
    Tool,
    ToolRegistry,
)
from core_service.ai.tools import ToolArgumentError, validate_arguments
from core_service.clock import FakeClock
from core_service.errors import PrivacyViolationError

VALID = {
    "summary": "Todo normal.",
    "findings": [
        {
            "recommended_action": "review_camera",
            "affected_route": "C5",
            "detected_anomaly": "low_model_confidence",
            "severity": "high",
            "affected_bus_id": "1045",
            "confidence": 0.8,
            "justification": "Confianza baja.",
        }
    ],
}


class TestResponseValidator:
    validator = AnalysisResponseValidator()

    def test_valid_answer_is_parsed_into_typed_objects(self) -> None:
        output = self.validator.parse(json.dumps(VALID))
        assert output.summary == "Todo normal."
        finding = output.findings[0]
        assert finding.recommended_action is RecommendedAction.REVIEW_CAMERA
        assert finding.detected_anomaly is Anomaly.LOW_MODEL_CONFIDENCE and finding.confidence == 0.8

    def test_json_inside_a_code_fence_or_with_leading_text_is_accepted(self) -> None:
        fenced = "```json\n" + json.dumps(VALID) + "\n```"
        assert len(self.validator.parse(fenced).findings) == 1
        assert len(self.validator.parse("Aquí tienes: " + json.dumps(VALID)).findings) == 1

    @pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]", "42"])
    def test_non_json_answers_are_rejected(self, text: str) -> None:
        with pytest.raises(InvalidAnalysisError):
            self.validator.parse(text)

    def test_unknown_enum_values_and_extra_keys_are_rejected(self) -> None:
        bad = json.loads(json.dumps(VALID))
        bad["findings"][0]["recommended_action"] = "delete_database"
        with pytest.raises(InvalidAnalysisError) as caught:
            self.validator.validate(bad)
        assert any("recommended_action" in e for e in caught.value.errors)
        extra = {**VALID, "note": "x"}
        with pytest.raises(InvalidAnalysisError):
            self.validator.validate(extra)
        nested = json.loads(json.dumps(VALID))
        nested["findings"][0]["shell"] = "rm -rf /"
        with pytest.raises(InvalidAnalysisError):
            self.validator.validate(nested)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("confidence", 1.5),
            ("confidence", "high"),
            ("confidence", True),
            ("affected_route", "route five"),
            ("affected_bus_id", "bus; DROP TABLE"),
            ("justification", "x" * 501),
            ("severity", "critical"),
        ],
    )
    def test_field_constraints(self, key: str, value: object) -> None:
        bad = json.loads(json.dumps(VALID))
        bad["findings"][0][key] = value
        with pytest.raises(InvalidAnalysisError):
            self.validator.validate(bad)

    def test_required_keys_and_limits(self) -> None:
        missing = {"summary": "s", "findings": [{"affected_route": "C5"}]}
        with pytest.raises(InvalidAnalysisError) as caught:
            self.validator.validate(missing)
        assert "missing" in str(caught.value)
        with pytest.raises(InvalidAnalysisError):
            self.validator.validate({"summary": "s", "findings": [VALID["findings"][0]] * 11})
        with pytest.raises(InvalidAnalysisError):
            self.validator.validate({"summary": 3, "findings": []})

    def test_optional_fields_have_safe_defaults(self) -> None:
        minimal = {
            "summary": "s",
            "findings": [{"recommended_action": "none", "affected_route": None, "detected_anomaly": "none"}],
        }
        finding = self.validator.validate(minimal).findings[0]
        assert finding.severity.value == "medium" and finding.confidence == 0.5 and finding.affected_bus_id is None


class TestPrivacyGuard:
    guard = PrivacyGuard()

    def test_clean_numeric_payload_passes(self) -> None:
        self.guard.assert_clean({"routes": [{"route_id": "C5", "buses": [{"bus_id": "1045", "occupancy_pct": 50}]}]})

    @pytest.mark.parametrize("key", ["email", "phone", "device_id", "user_id", "push_token", "password", "image"])
    def test_forbidden_keys_are_blocked_at_any_depth(self, key: str) -> None:
        with pytest.raises(PrivacyViolationError):
            self.guard.assert_clean({"routes": [{"deep": {key: "x"}}]})

    @pytest.mark.parametrize("text", ["write to ana@example.com", "call +573001234567", "3001234567"])
    def test_personal_looking_values_are_blocked(self, text: str) -> None:
        with pytest.raises(PrivacyViolationError):
            self.guard.assert_clean({"query": text})

    def test_sanitize_text_strips_control_characters_and_truncates(self) -> None:
        assert PrivacyGuard.sanitize_text("hola\x00\n\tmundo   ok") == "hola mundo ok"
        assert len(PrivacyGuard.sanitize_text("a" * 900, 100)) == 100


class TestToolRegistry:
    def make(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            Tool(
                "echo",
                "Echo a bus id.",
                {
                    "type": "object",
                    "required": ["bus_id"],
                    "properties": {
                        "bus_id": {"type": "string", "maxLength": 5},
                        "n": {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                    "additionalProperties": False,
                },
                lambda args: {"bus": args["bus_id"], "n": args.get("n", 1)},
            )
        )
        registry.register(Tool("boom", "Fails.", {"type": "object"}, lambda args: 1 / 0))
        registry.register(Tool("leak", "Leaks.", {"type": "object"}, lambda args: {"email": "a@b.co"}))
        registry.register(Tool("many", "Lots.", {"type": "object"}, lambda args: list(range(500))))
        return registry

    def test_specs_are_sorted_and_duplicates_rejected(self) -> None:
        registry = self.make()
        assert registry.names() == ["boom", "echo", "leak", "many"]
        assert {s["name"] for s in registry.specs()} == set(registry.names())
        with pytest.raises(ValueError):
            registry.register(Tool("echo", "again", {"type": "object"}, lambda a: 1))

    def test_successful_call(self) -> None:
        result = self.make().execute("echo", {"bus_id": "1045", "n": 2})
        assert result.ok and result.content == {"bus": "1045", "n": 2}

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"bus_id": 5},
            {"bus_id": "toolong"},
            {"bus_id": "a", "n": 0},
            {"bus_id": "a", "n": 9},
            {"bus_id": "a", "n": True},
            {"bus_id": "a", "extra": 1},
            "not-a-dict",
        ],
    )
    def test_invalid_arguments_become_error_results_not_exceptions(self, arguments: object) -> None:
        result = self.make().execute("echo", arguments)
        assert not result.ok and "error" in result.content

    def test_unknown_failing_and_leaking_tools_are_contained(self) -> None:
        registry = self.make()
        assert not registry.execute("nope", {}).ok
        failing = registry.execute("boom", {})
        assert not failing.ok and "division" not in json.dumps(failing.content)  # internal detail not exposed
        assert not registry.execute("leak", {}).ok  # personal data never reaches the model

    def test_long_results_are_truncated(self) -> None:
        assert len(self.make().execute("many", {}).content) == 50

    def test_validate_arguments_direct(self) -> None:
        with pytest.raises(ToolArgumentError):
            validate_arguments({"required": ["a"]}, {})
        assert validate_arguments({"properties": {"x": {"enum": [1, 2]}}}, {"x": 1}) == {"x": 1}
        with pytest.raises(ToolArgumentError):
            validate_arguments({"properties": {"x": {"enum": [1, 2]}}}, {"x": 3})


class TestCircuitBreaker:
    def make(self) -> tuple[CircuitBreaker, FakeClock]:
        clock = FakeClock()
        return CircuitBreaker(clock, failure_threshold=3, window=5, recovery_seconds=30), clock

    @staticmethod
    def fail() -> None:
        raise LLMError("down")

    def test_closed_breaker_passes_results_through(self) -> None:
        breaker, _ = self.make()
        assert breaker.call(lambda: 7) == 7 and breaker.state is BreakerState.CLOSED

    def test_opens_after_the_threshold_and_rejects_without_calling(self) -> None:
        breaker, _ = self.make()
        for _ in range(3):
            with pytest.raises(LLMError):
                breaker.call(self.fail)
        assert breaker.state is BreakerState.OPEN
        calls = []
        with pytest.raises(CircuitOpenError) as caught:
            breaker.call(lambda: calls.append(1))
        assert calls == [] and caught.value.code == "ai_unavailable"

    def test_successes_in_between_keep_it_closed_only_while_below_threshold(self) -> None:
        breaker, _ = self.make()
        for _ in range(2):
            with pytest.raises(LLMError):
                breaker.call(self.fail)
        breaker.call(lambda: 1)
        assert breaker.state is BreakerState.CLOSED
        with pytest.raises(LLMError):
            breaker.call(self.fail)
        assert breaker.state is BreakerState.OPEN  # 3 failures inside the window of 5

    def test_half_open_probe_success_closes_and_failure_reopens(self) -> None:
        breaker, clock = self.make()
        for _ in range(3):
            with pytest.raises(LLMError):
                breaker.call(self.fail)
        clock.advance(31)
        assert breaker.state is BreakerState.HALF_OPEN
        with pytest.raises(LLMError):
            breaker.call(self.fail)  # probe fails -> open again, timer restarted
        assert breaker.state is BreakerState.OPEN
        clock.advance(10)
        assert breaker.state is BreakerState.OPEN
        clock.advance(25)
        assert breaker.call(lambda: "ok") == "ok" and breaker.state is BreakerState.CLOSED

    def test_only_one_probe_at_a_time(self) -> None:
        breaker, clock = self.make()
        for _ in range(3):
            with pytest.raises(LLMError):
                breaker.call(self.fail)
        clock.advance(31)
        seen: list[str] = []

        def probe() -> str:
            with pytest.raises(CircuitOpenError):  # a second caller during the probe is rejected
                breaker.call(lambda: "second")
            seen.append("ok")
            return "first"

        assert breaker.call(probe) == "first" and seen == ["ok"]

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(FakeClock(), failure_threshold=5, window=3)


class TestAnthropicClient:
    @staticmethod
    def transport(status: int, payload: dict, seen: list | None = None):  # noqa: ANN205
        def send(url: str, headers: dict, body: bytes, timeout: float):  # noqa: ANN202
            if seen is not None:
                seen.append((url, headers, json.loads(body), timeout))
            return status, payload

        return send

    def test_request_shape_and_response_parsing(self) -> None:
        seen: list = []
        payload = {
            "content": [
                {"type": "text", "text": "hola"},
                {"type": "tool_use", "id": "t1", "name": "get_bus_state", "input": {"bus_id": "1"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
        client = AnthropicMessagesClient("key", transport=self.transport(200, payload, seen))
        response = client.complete("sys", [{"role": "user", "content": "q"}], [{"name": "t"}], 200)
        url, headers, body, _ = seen[0]
        assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "key"
        assert body["model"] == "claude-opus-5-5" and body["system"] == "sys" and body["tools"] == [{"name": "t"}]
        assert response.text == "hola" and response.tool_calls[0].name == "get_bus_state"
        assert (response.input_tokens, response.output_tokens) == (12, 3)

    def test_tools_are_omitted_when_empty(self) -> None:
        seen: list = []
        client = AnthropicMessagesClient("key", transport=self.transport(200, {"content": []}, seen))
        client.complete("s", [], [], 10)
        assert "tools" not in seen[0][2]

    @pytest.mark.parametrize(
        ("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (401, False)]
    )
    def test_http_errors_are_classified(self, status: int, retryable: bool) -> None:
        client = AnthropicMessagesClient("key", transport=self.transport(status, {"error": "secret prompt echo"}))
        with pytest.raises(LLMError) as caught:
            client.complete("s", [], [], 10)
        assert caught.value.retryable is retryable and "secret" not in str(caught.value)

    def test_malformed_payload_is_an_error(self) -> None:
        client = AnthropicMessagesClient("key", transport=self.transport(200, {"nope": 1}))
        with pytest.raises(LLMError):
            client.complete("s", [], [], 10)

    def test_construction_guards(self) -> None:
        with pytest.raises(ValueError):
            AnthropicMessagesClient("")
        with pytest.raises(ValueError):
            AnthropicMessagesClient("key", base_url="http://insecure.example.com")


def bus(**overrides: object) -> dict:
    base = {
        "bus_id": "1045",
        "state": "available_seats",
        "occupancy_pct": 40,
        "confidence": 0.95,
        "approximate_count": False,
        "seconds_since_reading": 5.0,
        "recent_occupancy_pct": [40, 41, 40],
    }
    return {**base, **overrides}


def context(*buses: dict) -> dict:
    return {"routes": [{"route_id": "C5", "buses": list(buses)}], "scope": {"routes": ["C5"]}}


class TestRuleBasedAnalyzer:
    analyzer = RuleBasedAnalyzer(min_confidence=0.6, stale_seconds=60)

    def test_healthy_fleet_has_no_findings(self) -> None:
        output = self.analyzer.analyze(context(bus()))
        assert output.findings == () and "no anomalies" in output.summary

    def test_silent_bus(self) -> None:
        finding = self.analyzer.analyze(context(bus(seconds_since_reading=300.0))).findings[0]
        assert (
            finding.detected_anomaly is Anomaly.BUS_OFFLINE
            and finding.recommended_action is RecommendedAction.CHECK_BUS_OFFLINE
        )
        assert self.analyzer.analyze(context(bus(state="no_data"))).findings[0].detected_anomaly is Anomaly.BUS_OFFLINE

    def test_low_confidence(self) -> None:
        finding = self.analyzer.analyze(context(bus(confidence=0.4))).findings[0]
        assert finding.detected_anomaly is Anomaly.LOW_MODEL_CONFIDENCE and finding.affected_route == "C5"

    def test_sustained_overcrowding_needs_three_consecutive_samples(self) -> None:
        assert self.analyzer.analyze(context(bus(recent_occupancy_pct=[120, 130, 90]))).findings == ()
        finding = self.analyzer.analyze(context(bus(recent_occupancy_pct=[120, 130, 110, 50]))).findings[0]
        assert finding.detected_anomaly is Anomaly.SUSTAINED_OVERCROWDING

    def test_occlusion_hint_for_a_full_bus_with_approximate_count(self) -> None:
        finding = self.analyzer.analyze(context(bus(approximate_count=True, occupancy_pct=95))).findings[0]
        assert finding.detected_anomaly is Anomaly.HIGH_DENSITY_OCCLUSION and isinstance(finding, AIFinding)

    def test_output_is_capped_at_ten_findings(self) -> None:
        buses = [bus(bus_id=str(i), confidence=0.1) for i in range(30)]
        assert len(self.analyzer.analyze(context(*buses)).findings) == 10
