"""Telemetry ingestion from the on-board hardware (machine-to-machine, API-key protected)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from core_service.api.dependencies import get_context, require_ingest_key
from core_service.api.schemas import TelemetryBatchIn, TelemetryIn
from core_service.container import ApplicationContext
from core_service.errors import DomainError
from core_service.models import GeoPoint, Reading
from core_service.services.cabin_store import CabinStore
from core_service.services.occupancy_service import ProcessStatus

router = APIRouter(prefix="/internal", tags=["ingestion"], dependencies=[Depends(require_ingest_key)])


def to_reading(bus_id: str, body: TelemetryIn, received_at: float) -> Reading:
    return Reading(
        bus_id=bus_id,
        ts=body.ts,
        location=GeoPoint(body.lat, body.lng),
        seated=body.seated,
        standing=body.standing,
        doors_total=body.doors_total,
        confidence=body.confidence,
        model_version=body.model_version,
        standing_out_of_position=body.standing_out_of_position,
        children_in_arms=body.children_in_arms,
        wheelchair_occupied=body.wheelchair_occupied,
        approximate_count=body.approximate_count,
        speed_kmh=body.speed_kmh,
        received_at=received_at,
    )


@router.post("/buses/{bus_id}/telemetry", summary="One reading")
def ingest(bus_id: str, body: TelemetryIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    outcome = context.occupancy.process(to_reading(bus_id, body, context.clock.now()))
    if outcome.status is ProcessStatus.APPLIED and body.cabin is not None:
        CabinStore(context.store, context.clock).put(bus_id, body.ts, body.cabin.occupied)
    return {"status": outcome.status.value}


@router.post("/telemetry/batch", summary="Buffered readings (offline bursts), up to 500")
def ingest_batch(body: TelemetryBatchIn, context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    now = context.clock.now()
    items: list[dict[str, Any]] = []
    valid: list[tuple[int, Reading]] = []
    for index, item in enumerate(body.readings):
        try:
            valid.append((index, to_reading(item.bus_id, item, now)))
        except DomainError as error:  # an invalid item is reported; the rest still goes through
            items.append({"index": index, "status": "rejected", "code": error.code})
    results = context.occupancy.process_batch([reading for _, reading in valid])
    cabins = CabinStore(context.store, context.clock)
    for (index, _), result in zip(valid, results, strict=True):
        if result.outcome is not None:
            items.append({"index": index, "status": result.outcome.status.value})
            item = body.readings[index]
            if result.outcome.status is ProcessStatus.APPLIED and item.cabin is not None:
                cabins.put(item.bus_id, item.ts, item.cabin.occupied)  # chronological: the newest applied one stays
        else:
            items.append({"index": index, "status": "rejected", "code": result.error_code})
    items.sort(key=lambda entry: entry["index"])
    rejected = sum(1 for entry in items if entry["status"] == "rejected")
    return {"accepted": len(items) - rejected, "rejected": rejected, "items": items}
