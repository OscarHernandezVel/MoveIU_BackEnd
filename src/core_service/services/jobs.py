"""Periodic background jobs (silent buses, expiry, purge, digests, weekly report).

``run_pending`` is deterministic (driven by the injected clock) so tests never sleep; the
optional background thread just calls it once per second.

With several replicas every one has the same schedule. A ``coordinator`` (the shared document
store) makes each job run **once per interval in the whole cluster**: before running, a replica
takes an atomic ``set_if_absent`` lease that lives for 80 % of the interval; replicas that lose the
race skip that round. If the winner crashes, the lease expires and the next round is taken by
someone else, so there is no single point of failure and no leader to elect.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from core_service.clock import Clock
from core_service.errors import DomainError
from core_service.metrics import MetricsRegistry
from core_service.persistence.documents import DocumentStore

log = logging.getLogger(__name__)


@dataclass
class PeriodicJob:
    name: str
    interval_seconds: float
    action: Callable[[], object]
    next_run: float = 0.0
    runs: int = 0
    failures: int = 0
    last_error: str | None = field(default=None, repr=False)


class JobRunner:
    def __init__(
        self, clock: Clock, metrics: MetricsRegistry, coordinator: DocumentStore | None = None, node_id: str = "node"
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._coordinator = coordinator
        self._node_id = node_id
        self._jobs: list[PeriodicJob] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def add(
        self, name: str, interval_seconds: float, action: Callable[[], object], *, run_immediately: bool = False
    ) -> PeriodicJob:
        if interval_seconds <= 0:
            raise ValueError("interval must be positive")
        first = self._clock.now() if run_immediately else self._clock.now() + interval_seconds
        job = PeriodicJob(name, interval_seconds, action, next_run=first)
        with self._lock:
            if any(existing.name == name for existing in self._jobs):
                raise ValueError(f"job '{name}' already exists")
            self._jobs.append(job)
        return job

    def jobs(self) -> list[PeriodicJob]:
        with self._lock:
            return list(self._jobs)

    def run_pending(self) -> list[str]:
        """Run every job that is due; a failing job is logged and retried at its next interval."""
        now = self._clock.now()
        executed: list[str] = []
        for job in self.jobs():
            if job.next_run > now:
                continue
            job.next_run = now + job.interval_seconds
            if not self._lease(job):
                self._metrics.increment("jobs_skipped_other_replica", job=job.name)
                continue
            job.runs += 1
            executed.append(job.name)
            try:
                job.action()
            except Exception as error:  # noqa: BLE001
                job.failures += 1
                job.last_error = type(error).__name__
                self._metrics.increment("job_failures", job=job.name)
                log.exception("Job %s failed", job.name)
        return executed

    def _lease(self, job: PeriodicJob) -> bool:
        if self._coordinator is None:
            return True
        try:
            return self._coordinator.set_if_absent(
                "job_lease", job.name, {"node": self._node_id}, ttl_seconds=max(1.0, job.interval_seconds * 0.8)
            )
        except DomainError:
            log.warning("Could not coordinate job %s: skipping this round", job.name)
            return False

    # ------------------------------------------------------------ background thread
    def start(self, tick_seconds: float = 1.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(tick_seconds):
                self.run_pending()

        self._thread = threading.Thread(target=loop, name="core-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
