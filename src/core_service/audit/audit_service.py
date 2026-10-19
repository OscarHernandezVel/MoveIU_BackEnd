"""Manual counts compared with the model counts (PB-14)."""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core_service.audit.metrics_calculator import AuditMetricsCalculator, AuditPoint, MetricsReport
from core_service.clock import Clock
from core_service.config import OccupancySettings
from core_service.errors import ValidationError
from core_service.metrics import MetricsRegistry
from core_service.models import AuditSample, BusCapacity
from core_service.occupancy import ReadingHistoryStore
from core_service.persistence.documents import DocumentStore
from core_service.persistence.relational import AuditRepository, TelemetryRepository
from core_service.routing import RouteCatalog
from core_service.services.saga import Saga

TRAINING_COLLECTION = "training_samples"
CSV_COLUMNS = ("bus_id", "stop_id", "timestamp", "manual_seated", "manual_standing")


@dataclass(frozen=True)
class RowError:
    line: int
    code: str
    detail: str = ""


@dataclass(frozen=True)
class CsvResult:
    processed: int
    rejected: list[RowError]


class AuditService:
    def __init__(
        self,
        audits: AuditRepository,
        telemetry: TelemetryRepository,
        histories: ReadingHistoryStore,
        catalog: RouteCatalog,
        store: DocumentStore,
        calculator: AuditMetricsCalculator,
        clock: Clock,
        occupancy_settings: OccupancySettings,
        metrics: MetricsRegistry,
        utc_offset_hours: int = -5,
        max_csv_rows: int = 5_000,
    ) -> None:
        self._audits = audits
        self._telemetry = telemetry
        self._histories = histories
        self._catalog = catalog
        self._store = store
        self._calculator = calculator
        self._clock = clock
        self._occupancy = occupancy_settings
        self._metrics = metrics
        self._tz = timezone(timedelta(hours=utc_offset_hours))
        self._max_csv_rows = max_csv_rows

    @property
    def timezone(self) -> timezone:
        return self._tz

    # ---------------------------------------------------------------- register
    def register(
        self,
        auditor_id: str,
        bus_id: str,
        stop_id: str,
        manual_seated: int,
        manual_standing: int,
        at: float | None = None,
    ) -> AuditSample:
        """Record a manual count. Without ``at`` it is compared with the bus' live reading;
        with ``at`` (a count saved offline) with the stored telemetry closest to that moment."""
        if manual_seated < 0 or manual_standing < 0:
            raise ValidationError("validation_error", details=["counts must not be negative"])
        bus = self._catalog.require_bus(bus_id)
        track = self._catalog.find_track(bus.route_id)
        if track is None or track.find_stop(stop_id) is None:
            raise ValidationError("stop_not_in_route", params={"route_id": bus.route_id or "-", "stop_id": stop_id})

        features, sampled_at, model_seated, model_standing, doors_total, confidence, version = self._model_side(
            bus_id, at
        )
        sample = AuditSample(
            sample_id=str(uuid.uuid4()),
            bus_id=bus_id,
            stop_id=stop_id,
            auditor_id=auditor_id,
            manual_seated=manual_seated,
            manual_standing=manual_standing,
            model_seated=model_seated,
            model_standing=model_standing,
            doors_total=doors_total,
            confidence=confidence,
            model_version=version,
            sampled_at=sampled_at,
        )
        features.update(self._capacity_features(bus.capacity))
        training_document = {
            "sample_id": sample.sample_id,
            "captured_at": sampled_at,
            "bus_id": bus_id,
            "model_version": version,
            "features": features,
            "labels": {"seated": manual_seated, "standing": manual_standing},
            "error": {"absolute": abs(sample.model_total - sample.manual_total)},
        }
        with Saga() as saga:  # document + SQL row are written together or not at all
            saga.step(
                lambda: self._store.put(TRAINING_COLLECTION, sample.sample_id, training_document),
                compensate=lambda: self._store.delete(TRAINING_COLLECTION, sample.sample_id),
            )
            saga.step(lambda: self._audits.add(sample))
        self._metrics.increment("audits_registered")
        return sample

    def _model_side(self, bus_id: str, at: float | None) -> tuple[dict[str, Any], float, int, int, int, float, str]:
        if at is None:
            entry = self._histories.latest_entry(bus_id)  # survives a terminal reset and works on any replica
            if entry is None or self._clock.now() - entry.reading.ts > self._occupancy.no_data_after_seconds:
                raise ValidationError("audit_bus_silent", params={"bus_id": bus_id})
            r = entry.reading
            features = {
                "seated": r.seated,
                "standing": r.standing,
                "standing_out_of_position": r.standing_out_of_position,
                "children_in_arms": r.children_in_arms,
                "wheelchair_occupied": r.wheelchair_occupied,
                "doors_total": r.doors_total,
                "confidence": r.confidence,
                "approximate_count": r.approximate_count,
                "speed_kmh": r.speed_kmh,
                "local_hour": datetime.fromtimestamp(r.ts, self._tz).hour,
            }
            return (
                features,
                r.ts,
                r.seated,
                r.standing + r.standing_out_of_position,
                r.doors_total,
                r.confidence,
                r.model_version,
            )
        row = self._telemetry.nearest(bus_id, at, tolerance_seconds=10.0)
        if row is None:
            raise ValidationError("audit_no_data", params={"bus_id": bus_id})
        features = {
            "seated": row["seated"],
            "standing": row["standing"],
            "standing_out_of_position": row["standing_out_of_position"],
            "doors_total": row["doors_total"],
            "confidence": row["confidence"],
            "approximate_count": bool(row["approximate_count"]),
            "local_hour": datetime.fromtimestamp(row["ts"], self._tz).hour,
        }
        standing = row["standing"] + row["standing_out_of_position"]
        return features, row["ts"], row["seated"], standing, row["doors_total"], row["confidence"], row["model_version"]

    @staticmethod
    def _capacity_features(capacity: BusCapacity) -> dict[str, int]:
        return {
            "capacity_seats": capacity.seats,
            "capacity_standing": capacity.standing_positions,
            "capacity_wheelchair": capacity.wheelchair_spaces,
        }

    # --------------------------------------------------------------------- CSV
    def register_csv(self, auditor_id: str, text: str) -> CsvResult:
        """Bulk load: header ``bus_id,stop_id,timestamp,manual_seated,manual_standing`` (ISO-8601 time)."""
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or not set(CSV_COLUMNS) <= {name.strip() for name in reader.fieldnames}:
            raise ValidationError("validation_error", details=[f"the CSV needs the columns {', '.join(CSV_COLUMNS)}"])
        processed = 0
        rejected: list[RowError] = []
        for number, row in enumerate(reader, start=2):  # the header is line 1
            if processed + len(rejected) >= self._max_csv_rows:
                rejected.append(RowError(number, "too_many_rows"))
                break
            try:
                clean = {key.strip(): (value or "").strip() for key, value in row.items() if key}
                self.register(
                    auditor_id,
                    clean["bus_id"],
                    clean["stop_id"],
                    int(clean["manual_seated"]),
                    int(clean["manual_standing"]),
                    at=self._parse_time(clean["timestamp"]),
                )
                processed += 1
            except ValidationError as error:
                rejected.append(RowError(number, error.code, "; ".join(error.details)))
            except Exception as error:  # noqa: BLE001 - bad rows are reported, never abort the file
                rejected.append(RowError(number, getattr(error, "code", "invalid_row"), type(error).__name__))
        return CsvResult(processed, rejected)

    @staticmethod
    def _parse_time(value: str) -> float:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        moment = datetime.fromisoformat(normalized)
        if moment.tzinfo is None:
            raise ValidationError("validation_error", details=["timestamp needs a time zone"])
        return moment.timestamp()

    # ----------------------------------------------------------------- metrics
    def metrics(self, start: date, end: date, route_id: str | None = None) -> MetricsReport:
        if end < start:
            raise ValidationError("audit_invalid_range")
        begin = datetime(start.year, start.month, start.day, tzinfo=self._tz).timestamp()
        finish = datetime(end.year, end.month, end.day, tzinfo=self._tz).timestamp() + 86_400
        points = [
            AuditPoint(
                bus_id=sample.bus_id,
                route_id=route,
                local_time=datetime.fromtimestamp(sample.sampled_at, self._tz),
                model_version=sample.model_version,
                manual_total=sample.manual_total,
                model_total=sample.model_total,
            )
            for sample, route in self._audits.list_between(begin, finish, route_id)
        ]
        return self._calculator.calculate(points)
