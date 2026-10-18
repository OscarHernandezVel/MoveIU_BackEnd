"""Durable document store: in-memory index + append-only write-ahead log.

Every mutation is appended to a JSON Lines file. On start the log is replayed to rebuild
the index (expired documents are skipped). When the log grows much larger than the live
data it is compacted by writing a fresh file and atomically swapping it in.

A crash in the middle of a write can leave a truncated last line: replay skips lines it
cannot parse instead of failing, so the store always starts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import IO

from core_service.clock import Clock
from core_service.persistence.documents.in_memory import InMemoryDocumentStore, _Entry

log = logging.getLogger(__name__)


class AppendOnlyDocumentStore(InMemoryDocumentStore):
    def __init__(
        self,
        path: str | os.PathLike[str],
        clock: Clock | None = None,
        *,
        fsync: bool = False,
        compact_min_lines: int = 5_000,
        compact_ratio: int = 4,
    ) -> None:
        super().__init__(clock)
        self._path = Path(path)
        self._fsync = fsync
        self._compact_min_lines = compact_min_lines
        self._compact_ratio = compact_ratio
        self._lines_written = 0
        self.corrupt_lines = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._replay()
        self._handle: IO[str] | None = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    # ------------------------------------------------------------------ replay
    def _replay(self) -> None:
        if not self._path.exists():
            return
        now = self._clock.now()
        with open(self._path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    operation = record["op"]
                    collection, key = record["c"], record["k"]
                    if operation == "put":
                        expires_at = record.get("e")
                        if expires_at is not None and expires_at <= now:
                            self._collections.get(collection, {}).pop(key, None)
                        else:
                            self._collections.setdefault(collection, {})[key] = _Entry(record["p"], expires_at)
                    elif operation == "del":
                        self._collections.get(collection, {}).pop(key, None)
                    else:
                        raise ValueError(f"unknown operation {operation!r}")
                except (ValueError, KeyError, TypeError):
                    self.corrupt_lines += 1
                    log.warning("Skipping unreadable line in the document log")
                    continue
                self._lines_written += 1

    # ------------------------------------------------------------- persistence
    def _append(self, record: dict[str, object]) -> None:
        if self._handle is None:
            raise RuntimeError("the document store is closed")
        self._handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._handle.flush()
        if self._fsync:
            os.fsync(self._handle.fileno())
        self._lines_written += 1
        self._maybe_compact()

    def _record_put(self, collection: str, key: str, entry: _Entry) -> None:
        self._append({"op": "put", "c": collection, "k": key, "p": entry.payload, "e": entry.expires_at})

    def _record_delete(self, collection: str, key: str) -> None:
        self._append({"op": "del", "c": collection, "k": key})

    def _live_count(self) -> int:
        return sum(len(items) for items in self._collections.values())

    def _maybe_compact(self) -> None:
        if self._lines_written >= self._compact_min_lines and self._lines_written > self._compact_ratio * max(
            self._live_count(), 1
        ):
            self.compact()

    def compact(self) -> None:
        """Rewrite the log with only the live documents."""
        with self._lock:
            live = self._snapshot()
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(temporary, "w", encoding="utf-8") as out:
                for collection, key, entry in live:
                    record = {"op": "put", "c": collection, "k": key, "p": entry.payload, "e": entry.expires_at}
                    out.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
            if self._handle is not None:
                self._handle.close()
            os.replace(temporary, self._path)
            self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
            self._lines_written = len(live)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
