"""Request and response contracts of the AI analysis.

The model must answer with a strictly formatted JSON object. Closed sets (enums) for actions
and anomalies mean the backend never acts on free text produced by a model, and any deviation
is rejected instead of being guessed at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core_service.models import FindingSeverity, GeoPoint

ROUTE_PATTERN = re.compile(r"^[A-Z]\d{1,2}$")
BUS_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,20}$")
MAX_FINDINGS = 10
MAX_TEXT = 500


class RecommendedAction(str, Enum):
    NONE = "none"
    MONITOR = "monitor"
    REVIEW_DOOR_SENSOR = "review_door_sensor"
    REVIEW_CAMERA = "review_camera"
    RECALIBRATE_BUS = "recalibrate_bus"
    CHECK_BUS_OFFLINE = "check_bus_offline"


class Anomaly(str, Enum):
    NONE = "none"
    DOOR_CAMERA_MISMATCH = "door_camera_mismatch"
    LOW_MODEL_CONFIDENCE = "low_model_confidence"
    SUSTAINED_OVERCROWDING = "sustained_overcrowding"
    BUS_OFFLINE = "bus_offline"
    HIGH_DENSITY_OCCLUSION = "high_density_occlusion"


@dataclass(frozen=True)
class AnalysisRequest:
    query: str
    route_ids: tuple[str, ...] = ()
    center: GeoPoint | None = None
    radius_km: float = 3.0
    requested_by: str = "system"


@dataclass(frozen=True)
class AIFinding:
    recommended_action: RecommendedAction
    affected_route: str | None
    detected_anomaly: Anomaly
    severity: FindingSeverity
    affected_bus_id: str | None
    confidence: float
    justification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_action": self.recommended_action.value,
            "affected_route": self.affected_route,
            "detected_anomaly": self.detected_anomaly.value,
            "severity": self.severity.value,
            "affected_bus_id": self.affected_bus_id,
            "confidence": self.confidence,
            "justification": self.justification,
        }


@dataclass(frozen=True)
class AnalysisOutput:
    findings: tuple[AIFinding, ...]
    summary: str


class InvalidAnalysisError(ValueError):
    """The model's answer does not satisfy the contract."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


#: Description of the expected answer, included in the prompt.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings", "summary"],
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_TEXT},
        "findings": {
            "type": "array",
            "maxItems": MAX_FINDINGS,
            "items": {
                "type": "object",
                "required": ["recommended_action", "affected_route", "detected_anomaly"],
                "properties": {
                    "recommended_action": {"enum": [a.value for a in RecommendedAction]},
                    "affected_route": {"type": ["string", "null"], "pattern": ROUTE_PATTERN.pattern},
                    "detected_anomaly": {"enum": [a.value for a in Anomaly]},
                    "severity": {"enum": [s.value for s in FindingSeverity]},
                    "affected_bus_id": {"type": ["string", "null"], "pattern": BUS_PATTERN.pattern},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "justification": {"type": "string", "maxLength": MAX_TEXT},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

_FINDING_KEYS = set(RESPONSE_SCHEMA["properties"]["findings"]["items"]["properties"])


class AnalysisResponseValidator:
    """Turns the model's text into a validated :class:`AnalysisOutput` (Pydantic-style, stdlib only)."""

    def parse(self, text: str) -> AnalysisOutput:
        return self.validate(self._extract_json(text))

    @staticmethod
    def _extract_json(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped).strip()
        start = stripped.find("{")
        if start < 0:
            raise InvalidAnalysisError(["the answer does not contain a JSON object"])
        try:
            value, _end = json.JSONDecoder().raw_decode(stripped[start:])
        except json.JSONDecodeError as error:
            raise InvalidAnalysisError([f"invalid JSON: {error.msg}"]) from error
        return value

    def validate(self, data: Any) -> AnalysisOutput:
        errors: list[str] = []
        if not isinstance(data, dict):
            raise InvalidAnalysisError(["the answer must be a JSON object"])
        extra = set(data) - {"findings", "summary"}
        if extra:
            errors.append(f"unexpected keys: {sorted(extra)}")
        summary = data.get("summary")
        if not isinstance(summary, str) or len(summary) > MAX_TEXT:
            errors.append(f"summary must be a string of at most {MAX_TEXT} characters")
        raw_findings = data.get("findings")
        findings: list[AIFinding] = []
        if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
            errors.append(f"findings must be a list with at most {MAX_FINDINGS} items")
        else:
            for index, raw in enumerate(raw_findings):
                finding = self._finding(raw, index, errors)
                if finding is not None:
                    findings.append(finding)
        if errors:
            raise InvalidAnalysisError(errors)
        return AnalysisOutput(tuple(findings), str(summary))

    @staticmethod
    def _finding(raw: Any, index: int, errors: list[str]) -> AIFinding | None:
        where = f"findings[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where} must be an object")
            return None
        problems_before = len(errors)
        missing = [k for k in ("recommended_action", "affected_route", "detected_anomaly") if k not in raw]
        if missing:
            errors.append(f"{where} is missing {missing}")
        unknown = set(raw) - _FINDING_KEYS
        if unknown:
            errors.append(f"{where} has unexpected keys {sorted(unknown)}")

        def enum_value(key: str, enum_type: type[Enum], default: str | None = None) -> Enum | None:
            value = raw.get(key, default)
            try:
                return enum_type(value)
            except ValueError:
                if key in raw or default is None:
                    errors.append(f"{where}.{key} must be one of {[e.value for e in enum_type]}")
                return None

        action = enum_value("recommended_action", RecommendedAction) if "recommended_action" in raw else None
        anomaly = enum_value("detected_anomaly", Anomaly) if "detected_anomaly" in raw else None
        severity = enum_value("severity", FindingSeverity, default="medium")

        route = raw.get("affected_route")
        if route is not None and not (isinstance(route, str) and ROUTE_PATTERN.match(route)):
            errors.append(f"{where}.affected_route must be a route code like C5 or null")
        bus = raw.get("affected_bus_id")
        if bus is not None and not (isinstance(bus, str) and BUS_PATTERN.match(bus)):
            errors.append(f"{where}.affected_bus_id is not a valid bus id")
        confidence = raw.get("confidence", 0.5)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{where}.confidence must be a number between 0 and 1")
        justification = raw.get("justification", "")
        if not isinstance(justification, str) or len(justification) > MAX_TEXT:
            errors.append(f"{where}.justification must be a string of at most {MAX_TEXT} characters")

        if len(errors) > problems_before or action is None or anomaly is None or severity is None:
            return None
        return AIFinding(
            recommended_action=action,  # type: ignore[arg-type]
            affected_route=route,
            detected_anomaly=anomaly,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            affected_bus_id=bus,
            confidence=float(confidence),
            justification=justification,
        )
