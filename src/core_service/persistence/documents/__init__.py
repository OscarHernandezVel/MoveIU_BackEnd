"""Non-relational (document) persistence."""

from core_service.persistence.documents.append_only import AppendOnlyDocumentStore
from core_service.persistence.documents.in_memory import InMemoryDocumentStore
from core_service.persistence.documents.redis_client import RedisOptions, build_redis_client, parse_sentinels
from core_service.persistence.documents.redis_store import RedisDocumentStore
from core_service.persistence.documents.store import Document, DocumentStore

__all__ = [
    "AppendOnlyDocumentStore",
    "Document",
    "DocumentStore",
    "InMemoryDocumentStore",
    "RedisDocumentStore",
    "RedisOptions",
    "build_redis_client",
    "parse_sentinels",
]
