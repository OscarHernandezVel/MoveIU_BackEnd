"""Persistent records: technical findings and audit samples."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core_service.models.enums import FindingKind, FindingSeverity


@dataclass(frozen=True)
class Finding:
    """An anomaly worth a human look (rule-based or AI-generated)."""

    finding_id: str
    bus_id: str | None
    kind: FindingKind
    severity: FindingSeverity
    description: str
    source: str  # "rules" | "ai"
    created_at: float
    status: str = "open"
    notified_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "bus_id": self.bus_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "description": self.description,
            "source": self.source,
            "status": self.status,
            "notified_at": self.notified_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AuditSample:
    """A manual passenger count compared with the model count at the same instant."""

    sample_id: str
    bus_id: str
    stop_id: str
    auditor_id: str
    manual_seated: int
    manual_standing: int
    model_seated: int
    model_standing: int
    doors_total: int
    confidence: float
    model_version: str
    sampled_at: float

    @property
    def manual_total(self) -> int:
        return self.manual_seated + self.manual_standing

    @property
    def model_total(self) -> int:
        return self.model_seated + self.model_standing
