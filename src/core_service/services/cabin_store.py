"""Which positions of the cabin are occupied right now (the data behind the 3D twin of the bus).

The on-board computer never sends an image (PB-N07): it sends the *ids* of the occupied positions
(``S02-L1``, ``P04``...), the same ids engraved on the model and calibrated in ``bus_positions``. This store
keeps the newest list per bus for a short time, so a stale picture is never shown as if it were current.
"""

from __future__ import annotations

from dataclasses import dataclass

from core_service.clock import Clock
from core_service.persistence.documents import DocumentStore

COLLECTION = "cabin"
#: Three missed reports (3 s each) and the picture is no longer "now".
FRESH_SECONDS = 15.0
#: The document disappears on its own: nothing about a bus that stopped reporting stays around.
TTL_SECONDS = 60.0


@dataclass(frozen=True)
class CabinSnapshot:
    bus_id: str
    ts: float
    occupied: tuple[str, ...]
    received_at: float

    def age(self, now: float) -> float:
        return max(0.0, now - self.received_at)


class CabinStore:
    def __init__(self, store: DocumentStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def put(self, bus_id: str, ts: float, occupied: list[str]) -> None:
        document = {"ts": ts, "occupied": sorted(set(occupied)), "received_at": self._clock.now()}
        self._store.put(COLLECTION, bus_id, document, ttl_seconds=TTL_SECONDS)

    def get(self, bus_id: str) -> CabinSnapshot | None:
        document = self._store.get(COLLECTION, bus_id)
        if document is None:
            return None
        return CabinSnapshot(
            bus_id, float(document["ts"]), tuple(str(p) for p in document["occupied"]), float(document["received_at"])
        )

    def is_fresh(self, snapshot: CabinSnapshot) -> bool:
        return snapshot.age(self._clock.now()) <= FRESH_SECONDS
