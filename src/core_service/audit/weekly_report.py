"""Weekly accuracy report e-mailed to the technical team (story 6.2, RF-14.3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from core_service.audit.audit_service import AuditService
from core_service.audit.metrics_calculator import MetricsReport
from core_service.clock import Clock
from core_service.i18n import MessageCatalog
from core_service.notifications import TeamNotifier
from core_service.persistence.documents import DocumentStore

DEDUPE_COLLECTION = "dedupe"


class WeeklyAuditReport:
    """Builds the report for the previous seven days and sends it once per week (Monday, 07:00 local)."""

    def __init__(
        self,
        audits: AuditService,
        notifier: TeamNotifier,
        store: DocumentStore,
        catalog: MessageCatalog,
        clock: Clock,
        threshold_percentage: float,
    ) -> None:
        self._audits = audits
        self._notifier = notifier
        self._store = store
        self._catalog = catalog
        self._clock = clock
        self._threshold = threshold_percentage

    def build(self, today: date, locale: str = "es") -> tuple[str, str]:
        start, end = today - timedelta(days=7), today - timedelta(days=1)
        report = self._audits.metrics(start, end)
        subject = self._catalog.render("report.audit.subject", locale, start=start.isoformat(), end=end.isoformat())
        return subject, self._body(report, locale)

    def _body(self, report: MetricsReport, locale: str) -> str:
        render = self._catalog.render
        buses = ", ".join(report.buses_to_recalibrate) or render("report.audit.none", locale)
        lines = [
            render("report.audit.summary", locale, samples=report.total_samples, error=report.overall_percentage_error),
            render("report.audit.recalibrate", locale, threshold=self._threshold, buses=buses),
            "",
            render("report.audit.by_bus", locale),
        ]
        for group in report.by_bus:
            lines.append(
                render(
                    "report.audit.line",
                    locale,
                    label=group.key,
                    samples=group.samples,
                    abs_error=group.mean_absolute_error,
                    pct_error=group.mean_percentage_error,
                )
            )
        lines += ["", render("report.audit.by_model", locale)]
        for group in report.by_model_version:
            lines.append(
                render(
                    "report.audit.line",
                    locale,
                    label=group.key,
                    samples=group.samples,
                    abs_error=group.mean_absolute_error,
                    pct_error=group.mean_percentage_error,
                )
            )
        return "\n".join(lines)

    def run_if_due(self) -> bool:
        """Send the report when it is Monday after 07:00 local time and it was not sent this week."""
        local_now = datetime.fromtimestamp(self._clock.now(), self._audits.timezone)
        if local_now.weekday() != 0 or local_now.hour < 7:
            return False
        year, week, _ = local_now.isocalendar()
        if not self._store.set_if_absent(
            DEDUPE_COLLECTION, f"weekly-audit-report:{year}-{week}", {"sent": True}, ttl_seconds=8 * 86_400
        ):
            return False
        subject, body = self.build(local_now.date())
        self._notifier.send(subject, body)
        return True
