"""AI analysis service: orchestrates context, model, tools, validation and persistence.

Flow: build a private numeric context -> ask the model (allowing a few read-only tool rounds)
-> validate the JSON strictly (one correction retry) -> *ground* the result (drop findings that
mention routes or buses that do not exist in the data) -> store findings and an audit trail.
If anything fails, the rule-based analyzer answers instead and the report says why.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from core_service.ai.circuit_breaker import CircuitBreaker, CircuitOpenError
from core_service.ai.context_builder import ContextBuilder
from core_service.ai.llm_client import LLMClient, LLMError, LLMResponse
from core_service.ai.prompts import (
    FINAL_ANSWER_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_correction_prompt,
    build_user_prompt,
)
from core_service.ai.rule_analyzer import RuleBasedAnalyzer
from core_service.ai.schemas import (
    AIFinding,
    AnalysisOutput,
    AnalysisRequest,
    AnalysisResponseValidator,
    Anomaly,
    InvalidAnalysisError,
)
from core_service.ai.tools import ToolRegistry
from core_service.clock import Clock
from core_service.config import AISettings
from core_service.metrics import MetricsRegistry
from core_service.models import FindingKind
from core_service.persistence.documents import DocumentStore
from core_service.services.findings_service import FindingsService

log = logging.getLogger(__name__)
RUNS_COLLECTION = "ai_runs"
RUN_TTL_SECONDS = 30 * 24 * 3600.0


@dataclass(frozen=True)
class AnalysisReport:
    run_id: str
    mode: str  # "ai" | "rules"
    degraded_reason: str | None
    summary: str
    findings: tuple[AIFinding, ...]
    dropped_findings: int
    recorded_findings: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "degraded_reason": self.degraded_reason,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "dropped_findings": self.dropped_findings,
            "recorded_findings": self.recorded_findings,
        }


class _AIFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AIAnalysisService:
    def __init__(
        self,
        llm: LLMClient | None,
        tools: ToolRegistry,
        breaker: CircuitBreaker,
        context_builder: ContextBuilder,
        fallback: RuleBasedAnalyzer,
        findings: FindingsService,
        store: DocumentStore,
        clock: Clock,
        metrics: MetricsRegistry,
        settings: AISettings,
        validator: AnalysisResponseValidator | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._breaker = breaker
        self._context_builder = context_builder
        self._fallback = fallback
        self._findings = findings
        self._store = store
        self._clock = clock
        self._metrics = metrics
        self._settings = settings
        self._validator = validator or AnalysisResponseValidator()

    @property
    def circuit_state(self) -> str:
        return self._breaker.state.value

    @property
    def available(self) -> bool:
        return self._llm is not None and self._settings.enabled

    # ------------------------------------------------------------------ public
    def analyze(self, request: AnalysisRequest) -> AnalysisReport:
        context = self._context_builder.build(request)  # may raise PrivacyViolationError: never fall back silently
        mode, degraded, output = "ai", None, None
        usage = {"input_tokens": 0, "output_tokens": 0}
        if not self.available:
            mode, degraded = "rules", "ai_disabled"
        else:
            try:
                output = self._breaker.call(lambda: self._ask_model(context, usage))
            except CircuitOpenError:
                mode, degraded = "rules", "circuit_open"
            except _AIFailure as failure:
                mode, degraded = "rules", failure.reason
            except LLMError as error:
                mode, degraded = "rules", "provider_error_retryable" if error.retryable else "provider_error"
        if output is None:
            self._metrics.increment("ai_fallbacks", reason=degraded or "unknown")
            output = self._fallback.analyze(context)
        grounded, dropped = self._ground(output, context)
        run_id = str(uuid.uuid4())
        recorded = self._record(grounded, source="ai" if mode == "ai" else "rules")
        self._store_run(run_id, request, mode, degraded, context, grounded, dropped, usage)
        self._metrics.increment("ai_analyses", mode=mode)
        return AnalysisReport(run_id, mode, degraded, output.summary, grounded, dropped, recorded)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        runs = sorted(self._store.find(RUNS_COLLECTION), key=lambda d: d["created_at"], reverse=True)
        return runs[:limit]

    # ------------------------------------------------------------ model round trip
    def _ask_model(self, context: dict[str, Any], usage: dict[str, int]) -> AnalysisOutput:
        assert self._llm is not None
        messages: list[dict[str, Any]] = [{"role": "user", "content": build_user_prompt(context)}]
        tool_specs = self._tools.specs()
        corrected = False
        nudged = False
        tool_rounds = 0
        while True:
            allow_tools = tool_rounds < self._settings.max_tool_rounds
            response = self._llm.complete(
                SYSTEM_PROMPT, messages, tool_specs if allow_tools else [], self._settings.max_tokens
            )
            usage["input_tokens"] += response.input_tokens
            usage["output_tokens"] += response.output_tokens
            if response.tool_calls and allow_tools:
                tool_rounds += 1
                self._append_tool_round(messages, response)
                continue
            if response.tool_calls:  # the model insists on tools after the limit
                if nudged:
                    raise _AIFailure("tool_loop")
                nudged = True
                messages.append({"role": "assistant", "content": response.text or "(no answer)"})
                messages.append({"role": "user", "content": FINAL_ANSWER_PROMPT})
                continue
            try:
                return self._validator.parse(response.text)
            except InvalidAnalysisError as error:
                if corrected:
                    self._metrics.increment("ai_invalid_answers")
                    raise _AIFailure("invalid_answer") from error
                corrected = True
                messages.append({"role": "assistant", "content": response.text or "{}"})
                messages.append({"role": "user", "content": build_correction_prompt(error.errors)})

    def _append_tool_round(self, messages: list[dict[str, Any]], response: LLMResponse) -> None:
        messages.append({"role": "assistant", "content": list(response.content)})
        results = []
        for call in response.tool_calls:
            outcome = self._tools.execute(call.name, call.arguments)
            self._metrics.increment("ai_tool_calls", tool=call.name, ok=str(outcome.ok).lower())
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _dump(outcome.content),
                    "is_error": not outcome.ok,
                }
            )
        messages.append({"role": "user", "content": results})

    # ------------------------------------------------------------------ grounding
    @staticmethod
    def _ground(output: AnalysisOutput, context: dict[str, Any]) -> tuple[tuple[AIFinding, ...], int]:
        """Keep only findings that refer to routes/buses present in the analysed data."""
        buses_by_route: dict[str, set[str]] = {
            route["route_id"]: {bus["bus_id"] for bus in route["buses"]} for route in context["routes"]
        }
        known_routes = set(context["scope"]["routes"]) | set(buses_by_route)
        all_buses = set().union(*buses_by_route.values()) if buses_by_route else set()
        kept: list[AIFinding] = []
        for finding in output.findings:
            if finding.affected_route is not None and finding.affected_route not in known_routes:
                continue
            if finding.affected_bus_id is not None:
                if finding.affected_bus_id not in all_buses:
                    continue
                if finding.affected_route is not None and finding.affected_bus_id not in buses_by_route.get(
                    finding.affected_route, set()
                ):
                    continue
            kept.append(finding)
        return tuple(kept), len(output.findings) - len(kept)

    # ------------------------------------------------------------------ persistence
    def _record(self, findings: tuple[AIFinding, ...], source: str) -> int:
        recorded = 0
        for finding in findings:
            if finding.detected_anomaly is Anomaly.NONE:
                continue
            description = (
                f"[{finding.detected_anomaly.value} -> {finding.recommended_action.value}] {finding.justification}"[
                    :500
                ]
            )
            stored = self._findings.record(
                bus_id=finding.affected_bus_id,
                kind=FindingKind.AI_RECOMMENDATION,
                severity=finding.severity,
                description=description,
                source=source,
                dedupe_seconds=1800.0,
            )
            recorded += 1 if stored is not None else 0
        return recorded

    def _store_run(
        self,
        run_id: str,
        request: AnalysisRequest,
        mode: str,
        degraded: str | None,
        context: dict[str, Any],
        findings: tuple[AIFinding, ...],
        dropped: int,
        usage: dict[str, int],
    ) -> None:
        """Audit trail of every analysis: who asked, what the model saw and answered."""
        self._store.put(
            RUNS_COLLECTION,
            run_id,
            {
                "run_id": run_id,
                "created_at": self._clock.now(),
                "requested_by": request.requested_by,
                "mode": mode,
                "degraded_reason": degraded,
                "model": getattr(self._llm, "model", None) if mode == "ai" else None,
                "prompt_version": PROMPT_VERSION,
                "buses_analysed": sum(len(r["buses"]) for r in context["routes"]),
                "findings": [f.to_dict() for f in findings],
                "dropped_findings": dropped,
                "usage": usage,
            },
            ttl_seconds=RUN_TTL_SECONDS,
        )


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
