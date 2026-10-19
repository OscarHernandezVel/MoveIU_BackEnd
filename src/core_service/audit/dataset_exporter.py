"""Exports the labelled samples used to retrain the on-board models (PB-12).

Samples live in the document store because their features change as models evolve (a schema-less
fit). The export is JSON Lines, ordered by capture time, and contains counts only: no image, no
name, no e-mail ever reaches the dataset.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from core_service.audit.audit_service import TRAINING_COLLECTION
from core_service.data_structures import Array
from core_service.persistence.documents import DocumentStore


class TrainingDatasetExporter:
    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    def export_jsonl(self, since: float | None = None, model_version: str | None = None) -> Iterator[str]:
        def wanted(document: dict[str, Any]) -> bool:
            if since is not None and document["captured_at"] < since:
                return False
            return model_version is None or document["model_version"] == model_version

        documents = Array.from_iterable(self._store.find(TRAINING_COLLECTION, wanted))
        for document in documents.merge_sorted(key=lambda d: d["captured_at"]):
            yield json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def summary(self) -> dict[str, int]:
        """Number of labelled samples per model version."""
        counts: dict[str, int] = {}
        for document in self._store.find(TRAINING_COLLECTION):
            counts[document["model_version"]] = counts.get(document["model_version"], 0) + 1
        return counts
