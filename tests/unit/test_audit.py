"""Audit: metrics, registration (live and offline), CSV import, dataset export and weekly report."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from core_service.audit import (
    AuditMetricsCalculator,
    AuditPoint,
    AuditService,
    TrainingDatasetExporter,
    WeeklyAuditReport,
)
from core_service.audit.metrics_calculator import daypart
from core_service.clock import FakeClock
from core_service.config import OccupancySettings
from core_service.errors import NotFoundError, ValidationError
from core_service.i18n import MessageCatalog
from core_service.metrics import MetricsRegistry
from core_service.models import GeoPoint, Role, User
from core_service.notifications import TeamNotifier
from core_service.occupancy import ReadingHistoryStore
from core_service.persistence.documents import InMemoryDocumentStore
from core_service.persistence.relational import (
    AuditRepository,
    CatalogRepository,
    Migrator,
    TelemetryRepository,
    UserRepository,
)
from core_service.routing import RouteCatalog
from factories import demo_buses, demo_data, demo_stops, demo_track, make_reading
from pg import make_database

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=TZ).timestamp()  # a Monday, 08:00 local


def point(
    bus: str, day: int, hour: int, manual: int, model: int, version: str = "occ-1.2", route: str = "C5"
) -> AuditPoint:
    return AuditPoint(bus, route, datetime(2026, 9, day, hour, 0, tzinfo=TZ), version, manual, model)


class TestMetricsCalculator:
    calculator = AuditMetricsCalculator(threshold_percentage=5.0)

    def test_error_per_sample(self) -> None:
        sample = point("1045", 21, 7, manual=40, model=44)
        assert sample.absolute_error == 4
        assert sample.percentage_error == pytest.approx(10.0)
        assert point("1045", 21, 7, manual=0, model=2).percentage_error == 200.0  # no division by zero

    def test_groups_are_sorted_worst_first(self) -> None:
        report = self.calculator.calculate(
            [
                point("1045", 21, 7, 40, 44),
                point("1045", 22, 18, 40, 43),
                point("1102", 21, 11, 30, 30),
                point("1102", 22, 11, 30, 33, version="occ-1.3"),
            ]
        )
        assert report.total_samples == 4
        assert [g.key for g in report.by_bus] == ["1045", "1102"]
        assert report.by_bus[0].mean_absolute_error == 3.5
        assert report.by_bus[0].mean_percentage_error == 8.75
        assert {g.key for g in report.by_daypart} == {"peak", "valley"}
        assert [g.key for g in report.by_model_version] == ["occ-1.3", "occ-1.2"]
        assert [g.key for g in report.by_route] == ["C5"]
        assert report.overall_percentage_error == pytest.approx(round((10 + 7.5 + 0 + 10) / 4, 2))

    def test_recalibration_needs_two_bad_audits_in_a_row(self) -> None:
        bad_twice = [point("1045", 21, 7, 40, 44), point("1045", 22, 7, 40, 43)]
        bad_then_good = [point("1102", 21, 7, 40, 44), point("1102", 22, 7, 40, 40), point("1102", 23, 7, 40, 44)]
        report = self.calculator.calculate(bad_twice + bad_then_good)
        assert report.buses_to_recalibrate == ["1045"]

    def test_same_day_samples_are_averaged_into_one_audit(self) -> None:
        one_day = [point("1045", 21, 7, 40, 44), point("1045", 21, 12, 40, 50)]
        assert self.calculator.calculate(one_day).buses_to_recalibrate == []

    def test_empty_input(self) -> None:
        report = self.calculator.calculate([])
        assert report.total_samples == 0 and report.overall_percentage_error == 0.0 and report.by_bus == []

    def test_daypart_boundaries(self) -> None:
        assert daypart(datetime(2026, 9, 21, 6, 0)) == "peak"
        assert daypart(datetime(2026, 9, 21, 8, 59)) == "peak"
        assert daypart(datetime(2026, 9, 21, 9, 0)) == "valley"
        assert daypart(datetime(2026, 9, 21, 17, 0)) == "peak"
        assert daypart(datetime(2026, 9, 21, 20, 0)) == "valley"


@dataclass
class Rig:
    clock: FakeClock
    service: AuditService
    telemetry: TelemetryRepository
    histories: ReadingHistoryStore
    store: InMemoryDocumentStore
    auditor_id: str
    audits: AuditRepository


class RecordingNotifier(TeamNotifier):
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> bool:
        self.messages.append((subject, body))
        return True


@pytest.fixture
def rig() -> Rig:
    clock = FakeClock(NOW)
    db = make_database()
    Migrator(db, clock).migrate()
    catalog_repo = CatalogRepository(db)
    for stop in demo_stops().values():
        catalog_repo.save_stop(stop)
    route = demo_data()["routes"][0]
    catalog_repo.save_route(
        route["route_id"], route["name"], [GeoPoint(lat, lng) for lng, lat in route["path"]], route["stops"]
    )
    for bus in demo_buses():
        catalog_repo.save_bus(bus)
    UserRepository(db).create(User("auditor-1", "Aud", "aud@example.com", None, "hash", Role.AUDITOR, NOW))
    catalog = RouteCatalog()
    catalog.replace_all([demo_track()], demo_buses())
    telemetry, histories, store, audits = (
        TelemetryRepository(db),
        ReadingHistoryStore(),
        InMemoryDocumentStore(clock),
        AuditRepository(db),
    )
    service = AuditService(
        audits,
        telemetry,
        histories,
        catalog,
        store,
        AuditMetricsCalculator(5.0),
        clock,
        OccupancySettings(),
        MetricsRegistry(),
        utc_offset_hours=-5,
    )
    return Rig(clock, service, telemetry, histories, store, "auditor-1", audits)


class TestRegistration:
    def test_live_count_is_compared_with_the_current_reading(self, rig: Rig) -> None:
        rig.histories.get("1045").push(make_reading(ts=NOW - 2, seated=30, standing=6, doors_total=37), 0.4)
        sample = rig.service.register(rig.auditor_id, "1045", "P-018", 31, 6)
        assert (sample.model_seated, sample.model_standing, sample.doors_total) == (30, 6, 37)
        assert sample.manual_total == 37 and sample.model_version == "occ-1.2"

    def test_training_document_is_written_with_features_and_labels(self, rig: Rig) -> None:
        rig.histories.get("1045").push(make_reading(ts=NOW - 2, seated=30, standing=6), 0.4)
        sample = rig.service.register(rig.auditor_id, "1045", "P-018", 31, 6)
        document = rig.store.get("training_samples", sample.sample_id)
        assert document is not None
        assert document["labels"] == {"seated": 31, "standing": 6}
        assert document["features"]["seated"] == 30 and document["features"]["capacity_seats"] == 41
        assert "name" not in json.dumps(document) and "email" not in json.dumps(document)

    def test_a_silent_bus_cannot_be_audited_live(self, rig: Rig) -> None:
        with pytest.raises(ValidationError) as none_yet:
            rig.service.register(rig.auditor_id, "1045", "P-018", 1, 1)
        assert none_yet.value.code == "audit_bus_silent"
        rig.histories.get("1045").push(make_reading(ts=NOW - 500), 0.4)
        with pytest.raises(ValidationError):
            rig.service.register(rig.auditor_id, "1045", "P-018", 1, 1)

    def test_offline_count_uses_the_closest_stored_telemetry(self, rig: Rig) -> None:
        rig.telemetry.save(make_reading(ts=NOW - 100, seated=20, standing=2), 45)
        rig.telemetry.save(make_reading(ts=NOW - 103, seated=25, standing=3), 57)
        sample = rig.service.register(rig.auditor_id, "1045", "P-018", 21, 2, at=NOW - 99)
        assert sample.model_seated == 20 and sample.sampled_at == NOW - 100
        with pytest.raises(ValidationError) as caught:
            rig.service.register(rig.auditor_id, "1045", "P-018", 1, 1, at=NOW - 5000)
        assert caught.value.code == "audit_no_data"

    def test_input_validation(self, rig: Rig) -> None:
        rig.histories.get("1045").push(make_reading(ts=NOW), 0.4)
        with pytest.raises(ValidationError):
            rig.service.register(rig.auditor_id, "1045", "P-018", -1, 0)
        with pytest.raises(NotFoundError):
            rig.service.register(rig.auditor_id, "9999", "P-018", 1, 1)
        with pytest.raises(ValidationError):
            rig.service.register(rig.auditor_id, "1045", "NOPE", 1, 1)

    def test_a_failure_writing_sql_removes_the_training_document(self, rig: Rig) -> None:
        rig.histories.get("1045").push(make_reading(ts=NOW), 0.4)
        with pytest.raises(Exception):  # noqa: B017, PT011 - the auditor id violates the foreign key
            rig.service.register("not-a-user", "1045", "P-018", 1, 1)
        assert rig.store.find("training_samples") == []  # the LIFO rollback undid the document


class TestCsvImport:
    def test_valid_and_invalid_rows_are_reported_separately(self, rig: Rig) -> None:
        rig.telemetry.save(make_reading(ts=NOW - 100, seated=20, standing=2), 45)
        stamp = datetime.fromtimestamp(NOW - 100, timezone.utc).isoformat().replace("+00:00", "Z")
        text = "\n".join(
            [
                "bus_id,stop_id,timestamp,manual_seated,manual_standing",
                f"1045,P-018,{stamp},21,2",
                f"9999,P-018,{stamp},21,2",
                f"1045,P-018,{stamp},abc,2",
                "1045,P-018,2026-01-01T00:00:00,1,1",
                f"1045,P-018,{stamp},-3,2",
            ]
        )
        result = rig.service.register_csv(rig.auditor_id, text)
        assert result.processed == 1
        assert [(e.line, e.code) for e in result.rejected] == [
            (3, "bus_not_found"),
            (4, "invalid_row"),
            (5, "validation_error"),
            (6, "validation_error"),
        ]

    def test_missing_columns_are_rejected(self, rig: Rig) -> None:
        with pytest.raises(ValidationError):
            rig.service.register_csv(rig.auditor_id, "bus_id,stop_id\n1045,P-018")

    def test_row_limit(self, rig: Rig) -> None:
        rig.service._max_csv_rows = 2  # noqa: SLF001
        stamp = "2026-09-28T12:00:00Z"
        text = "bus_id,stop_id,timestamp,manual_seated,manual_standing\n" + "\n".join(
            f"1045,P-018,{stamp},1,1" for _ in range(5)
        )
        result = rig.service.register_csv(rig.auditor_id, text)
        assert result.rejected[-1].code == "too_many_rows"


class TestMetricsEndpointLogic:
    def test_metrics_between_dates_and_by_route(self, rig: Rig) -> None:
        rig.histories.get("1045").push(make_reading(ts=NOW - 1, seated=30, standing=6, doors_total=36), 0.4)
        rig.service.register(rig.auditor_id, "1045", "P-018", 30, 6)
        today = date(2026, 9, 28)
        report = rig.service.metrics(today, today)
        assert report.total_samples == 1 and report.overall_percentage_error == 0.0
        assert rig.service.metrics(today, today, route_id="C1").total_samples == 0
        assert rig.service.metrics(today - timedelta(days=5), today - timedelta(days=1)).total_samples == 0
        with pytest.raises(ValidationError):
            rig.service.metrics(today, today - timedelta(days=1))


class TestDatasetExport:
    def test_jsonl_is_ordered_filterable_and_privacy_safe(self, rig: Rig) -> None:
        for offset, version in ((-30, "occ-1.2"), (-10, "occ-1.3"), (-20, "occ-1.2")):
            rig.histories.get("1045").push(make_reading(ts=NOW + offset, model_version=version), 0.4)
            rig.service.register(rig.auditor_id, "1045", "P-018", 10, 0)
        exporter = TrainingDatasetExporter(rig.store)
        lines = list(exporter.export_jsonl())
        assert [json.loads(line)["captured_at"] for line in lines] == sorted(
            json.loads(line)["captured_at"] for line in lines
        )
        assert len(lines) == 3
        assert len(list(exporter.export_jsonl(model_version="occ-1.3"))) == 1
        assert len(list(exporter.export_jsonl(since=NOW - 25))) == 2
        assert exporter.summary() == {"occ-1.2": 2, "occ-1.3": 1}
        assert all(set(json.loads(line)) >= {"features", "labels", "model_version"} for line in lines)


class TestWeeklyReport:
    def build(self, rig: Rig) -> tuple[WeeklyAuditReport, RecordingNotifier]:
        notifier = RecordingNotifier()
        return WeeklyAuditReport(rig.service, notifier, rig.store, MessageCatalog(), rig.clock, 5.0), notifier

    def test_sent_once_on_monday_after_seven(self, rig: Rig) -> None:
        report, notifier = self.build(rig)
        rig.histories.get("1045").push(make_reading(ts=NOW - 86_400 * 2, seated=30, standing=6, doors_total=36), 0.4)
        rig.clock.set(NOW - 86_400 * 2)
        rig.service.register(rig.auditor_id, "1045", "P-018", 32, 6)
        rig.clock.set(NOW)
        assert report.run_if_due() is True
        assert report.run_if_due() is False  # already sent this week
        subject, body = notifier.messages[0]
        assert "Reporte semanal" in subject and "2026-09-21" in subject
        assert "Muestras: 1" in body and "Por bus:" in body and "1045" in body

    def test_not_due_on_other_days_or_before_seven(self, rig: Rig) -> None:
        report, notifier = self.build(rig)
        rig.clock.set(NOW + 86_400)  # Tuesday
        assert report.run_if_due() is False
        rig.clock.set(datetime(2026, 10, 5, 6, 30, tzinfo=TZ).timestamp())  # next Monday, 06:30
        assert report.run_if_due() is False
        assert notifier.messages == []

    def test_english_report(self, rig: Rig) -> None:
        report, _ = self.build(rig)
        subject, body = report.build(date(2026, 9, 28), locale="en")
        assert subject.startswith("[Bus capacity] Weekly accuracy report")
        assert "Buses to recalibrate" in body and "none" in body
