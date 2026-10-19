"""Delivers an on-screen alert through every channel with retries, de-duplication and fallback.

Order of the pipeline for one alert:

1. **De-duplicate** by ``dedupe_key`` (atomic ``set_if_absent`` in the document store, so it
   also works across several service replicas).
2. **Admit** (optional callback, used for the rate limit of unsolicited alerts).
3. **Deliver** to each channel with exponential-backoff retries for retryable failures.
4. The alert counts as delivered when *at least one* channel succeeded. If none did, the
   de-duplication key is released (so a later attempt can succeed) and an error is raised so
   the caller can roll back its own state.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core_service.alerts.channels import ChannelDeliveryError, DeviceTarget, NotificationChannel
from core_service.alerts.screen_alert import ScreenAlert
from core_service.errors import ServiceUnavailableError
from core_service.metrics import MetricsRegistry
from core_service.persistence.documents import DocumentStore

log = logging.getLogger(__name__)

DEDUPE_COLLECTION = "dedupe"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2
    factor: float = 2.0
    max_delay: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def delay(self, failed_attempt: int) -> float:
        """Pause after the ``failed_attempt``-th failure (1-based)."""
        return min(self.max_delay, self.base_delay * (self.factor ** (failed_attempt - 1)))


@dataclass(frozen=True)
class ChannelResult:
    channel: str
    delivered: bool
    attempts: int
    error: str | None = None


@dataclass
class DispatchReport:
    results: list[ChannelResult] = field(default_factory=list)
    duplicate: bool = False
    suppressed: bool = False

    @property
    def delivered(self) -> bool:
        return any(result.delivered for result in self.results)

    @property
    def delivered_channels(self) -> list[str]:
        return [result.channel for result in self.results if result.delivered]


class DispatchFailedError(ServiceUnavailableError):
    default_code = "alert_not_deliverable"

    def __init__(self, report: DispatchReport) -> None:
        super().__init__()
        self.report = report


class AlertDispatcher:
    def __init__(
        self,
        channels: Sequence[NotificationChannel],
        store: DocumentStore,
        retry: RetryPolicy | None = None,
        metrics: MetricsRegistry | None = None,
        sleep: Callable[[float], None] = time.sleep,
        dedupe_ttl_seconds: float = 3600.0,
    ) -> None:
        if not channels:
            raise ValueError("at least one channel is required")
        self._channels = list(channels)
        self._store = store
        self._retry = retry or RetryPolicy()
        self._metrics = metrics
        self._sleep = sleep
        self._dedupe_ttl = dedupe_ttl_seconds

    def dispatch(
        self,
        target: DeviceTarget,
        alert: ScreenAlert,
        *,
        dedupe_ttl_seconds: float | None = None,
        admit: Callable[[], bool] | None = None,
    ) -> DispatchReport:
        ttl = dedupe_ttl_seconds or self._dedupe_ttl
        if not self._store.set_if_absent(
            DEDUPE_COLLECTION, alert.dedupe_key, {"at": alert.created_at}, ttl_seconds=ttl
        ):
            self._count("alerts_duplicate")
            return DispatchReport(duplicate=True)
        if admit is not None and not admit():
            self._store.delete(DEDUPE_COLLECTION, alert.dedupe_key)
            self._count("alerts_suppressed")
            return DispatchReport(suppressed=True)

        report = DispatchReport(results=[self._deliver(channel, target, alert) for channel in self._channels])
        if not report.delivered:
            self._store.delete(DEDUPE_COLLECTION, alert.dedupe_key)
            self._count("alerts_dispatch_failed")
            raise DispatchFailedError(report)
        self._count("alerts_dispatched", kind=alert.kind.value)
        return report

    def _deliver(self, channel: NotificationChannel, target: DeviceTarget, alert: ScreenAlert) -> ChannelResult:
        last_error: str | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                channel.deliver(target, alert)
                self._count("channel_delivered", channel=channel.name)
                return ChannelResult(channel.name, True, attempt)
            except ChannelDeliveryError as error:
                last_error = str(error)
                log.warning("Channel %s failed (attempt %s): %s", channel.name, attempt, error)
                if not error.retryable or attempt == self._retry.max_attempts:
                    break
                self._sleep(self._retry.delay(attempt))
            except Exception as error:  # noqa: BLE001 - a buggy channel must not stop the others
                last_error = f"unexpected {type(error).__name__}"
                log.exception("Channel %s crashed", channel.name)
                break
        self._count("channel_failed", channel=channel.name)
        return ChannelResult(channel.name, False, attempt, last_error)

    def _count(self, name: str, **labels: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(name, **labels)
