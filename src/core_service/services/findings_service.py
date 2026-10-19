"""Technical findings: anomalies that deserve a human look.

High-severity findings are e-mailed to the team immediately; the rest go into a daily digest.
Every finding is de-duplicated per bus and kind so a persistent fault does not flood the team.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from core_service.clock import Clock
from core_service.config import OccupancySettings
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.models import Finding, FindingKind, FindingSeverity
from core_service.notifications import TeamNotifier
from core_service.occupancy.count_fusion import FusionReason
from core_service.persistence.documents import DocumentStore
from core_service.persistence.relational import FindingRepository
from core_service.services.events import BusWentSilent, OccupancyUpdated

log = logging.getLogger(__name__)
DEDUPE_COLLECTION = "dedupe"


class FindingsService:
    def __init__(
        self,
        findings: FindingRepository,
        store: DocumentStore,
        notifier: TeamNotifier,
        catalog: MessageCatalog,
        clock: Clock,
        settings: OccupancySettings,
        metrics: MetricsRegistry,
    ) -> None:
        self._findings = findings
        self._store = store
        self._notifier = notifier
        self._catalog = catalog
        self._clock = clock
        self._settings = settings
        self._metrics = metrics

    def record(
        self,
        *,
        bus_id: str | None,
        kind: FindingKind,
        severity: FindingSeverity,
        description: str,
        source: str = "rules",
        dedupe_seconds: float = 3600.0,
    ) -> Finding | None:
        """Store a finding once per bus/kind within ``dedupe_seconds``; returns it (or None when duplicate)."""
        key = f"finding:{kind.value}:{bus_id or '-'}"
        if not self._store.set_if_absent(DEDUPE_COLLECTION, key, {"at": self._clock.now()}, ttl_seconds=dedupe_seconds):
            return None
        finding = Finding(
            finding_id=str(uuid.uuid4()),
            bus_id=bus_id,
            kind=kind,
            severity=severity,
            description=description,
            source=source,
            created_at=self._clock.now(),
        )
        self._findings.create(finding)
        self._metrics.increment("findings_recorded", kind=kind.value, severity=severity.value)
        if severity is FindingSeverity.HIGH:
            subject = self._catalog.render("finding.alert.subject", description=description)
            if self._notifier.send(subject, description):
                self._findings.mark_notified([finding.finding_id], self._clock.now())
        return finding

    # ------------------------------------------------------------ event handlers
    def on_occupancy_updated(self, event: OccupancyUpdated) -> None:
        state, reason = event.state, event.fusion_reason
        if reason is FusionReason.DISAGREEMENT_SUSTAINED:
            self.record(
                bus_id=state.bus_id,
                kind=FindingKind.COUNT_MISMATCH,
                severity=FindingSeverity.MEDIUM,
                description=self._catalog.render(
                    "finding.count_mismatch",
                    bus_id=state.bus_id,
                    threshold=self._settings.door_gap_threshold_people,
                    seconds=round(self._settings.door_gap_window_seconds),
                ),
            )
        elif reason is FusionReason.LOW_CONFIDENCE:
            self.record(
                bus_id=state.bus_id,
                kind=FindingKind.LOW_CONFIDENCE,
                severity=FindingSeverity.MEDIUM,
                description=self._catalog.render(
                    "finding.low_confidence", bus_id=state.bus_id, min_confidence=self._settings.min_confidence
                ),
            )

    def on_bus_silent(self, event: BusWentSilent) -> None:
        self.record(
            bus_id=event.bus_id,
            kind=FindingKind.SILENT_BUS,
            severity=FindingSeverity.HIGH,
            description=self._catalog.render(
                "finding.silent_bus",
                bus_id=event.bus_id,
                route_id=event.route_id or "-",
                seconds=round(event.silent_for_seconds),
            ),
        )

    # ------------------------------------------------------------------ digest
    def send_daily_digest(self) -> bool:
        """E-mail the findings of the last 24 hours that were not sent yet (scheduled job)."""
        pending = self._findings.list_unnotified_since(self._clock.now() - timedelta(days=1).total_seconds())
        if not pending:
            return False
        body = "\n".join(
            f"{f.severity.value} | {f.kind.value} | bus {f.bus_id or '-'} | {f.description}" for f in pending
        )
        subject = self._catalog.render("finding.digest.subject", count=len(pending))
        if self._notifier.send(subject, body):
            self._findings.mark_notified([f.finding_id for f in pending], self._clock.now())
            return True
        return False
