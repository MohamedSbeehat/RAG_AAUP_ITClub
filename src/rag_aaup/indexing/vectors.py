"""Qdrant dense search, with persistent local and remote server backends."""

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

import numpy as np
from qdrant_client import QdrantClient, models

if TYPE_CHECKING:
    from rag_aaup.indexing.database import StoredChunk

_BATCH_SIZE = 128


class VectorStoreError(RuntimeError):
    """The vector index is unavailable or incompatible with the embeddings."""


def validate_vector(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all() or not np.any(vector):
        raise ValueError("Embeddings must be finite, nonzero, one-dimensional vectors")
    return vector


class QdrantVectorStore:
    def __init__(
        self,
        path: Path,
        collection: str = "academic_chunks",
        url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ):
        self.path = path
        self.collection = collection
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self._client: QdrantClient | None = None

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            if self.url:
                self._client = QdrantClient(url=self.url, api_key=self.api_key, timeout=self.timeout)
            else:
                self.path.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(self.path), force_disable_check_same_thread=True)
        return self._client

    @contextmanager
    def _operation(self) -> Generator[None]:
        try:
            yield
        except VectorStoreError:
            raise
        except Exception as error:
            # Do not return client exception text, which can contain endpoint credentials.
            raise VectorStoreError(
                f"Qdrant operation failed ({type(error).__name__}). Check the configured endpoint, "
                "credentials, and storage access. Local mode requires a single application process."
            ) from error

    def _ensure_collection(self, dimension: int) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
        config = self.client.get_collection(self.collection).config.params.vectors
        if not isinstance(config, models.VectorParams) or (
            config.size != dimension or config.distance != models.Distance.COSINE
        ):
            raise VectorStoreError(
                "Qdrant collection has an incompatible vector dimension or distance. "
                "Restore the embedding model or choose a new QDRANT_COLLECTION and re-index all documents."
            )

    def exists(self) -> bool:
        with self._operation():
            return self.client.collection_exists(self.collection)

    @staticmethod
    def _scope(database_id: str) -> models.Filter:
        return models.Filter(
            must=[models.FieldCondition(key="database_id", match=models.MatchValue(value=database_id))]
        )

    def synchronize(self, chunks: list["StoredChunk"], database_id: str) -> None:
        """Reconcile cached embeddings without recomputing them; retry safely after a partial write."""
        with self._operation():
            if chunks:
                dimensions = {validate_vector(chunk.embedding).size for chunk in chunks}
                if len(dimensions) != 1:
                    raise VectorStoreError("Cached embeddings have mixed dimensions; re-index all documents.")
                self._ensure_collection(dimensions.pop())
            elif not self.client.collection_exists(self.collection):
                return

            existing: dict[str, str | None] = {}
            offset = None
            while True:
                points, offset = self.client.scroll(
                    collection_name=self.collection,
                    scroll_filter=self._scope(database_id),
                    limit=_BATCH_SIZE,
                    offset=offset,
                    with_payload=["fingerprint"],
                    with_vectors=False,
                )
                existing.update((str(point.id), (point.payload or {}).get("fingerprint")) for point in points)
                if offset is None:
                    break

            current_ids: set[str] = set()
            pending: list[models.PointStruct] = []
            for chunk in chunks:
                point_id = str(uuid5(UUID(database_id), str(chunk.chunk_id)))
                current_ids.add(point_id)
                payload = {
                    "database_id": database_id,
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document.document_id,
                    "source": chunk.document.relative_path,
                    "title": chunk.document.title,
                    "sha256": chunk.document.sha256,
                    "page": chunk.page,
                    "section": chunk.section,
                    "topic": chunk.topic,
                }
                fingerprint = hashlib.sha256(
                    json.dumps([payload, chunk.text], sort_keys=True, ensure_ascii=False).encode("utf-8")
                    + chunk.embedding.tobytes()
                ).hexdigest()
                if existing.get(point_id) == fingerprint:
                    continue
                pending.append(
                    models.PointStruct(
                        id=point_id,
                        vector=chunk.embedding.tolist(),
                        payload={**payload, "fingerprint": fingerprint},
                    )
                )
                if len(pending) == _BATCH_SIZE:
                    self.client.upsert(self.collection, points=pending, wait=True)
                    pending = []
            if pending:
                self.client.upsert(self.collection, points=pending, wait=True)

            stale_ids = sorted(existing.keys() - current_ids)
            for start in range(0, len(stale_ids), _BATCH_SIZE):
                self.client.delete(
                    self.collection,
                    points_selector=models.PointIdsList(points=stale_ids[start : start + _BATCH_SIZE]),
                    wait=True,
                )

    def search(
        self,
        vector: np.ndarray,
        database_id: str,
        limit: int,
        chunk_ids: list[int] | None = None,
        document_ids: list[int] | None = None,
        group_by_document: bool = False,
    ) -> list[tuple[int, float]]:
        vector = validate_vector(vector)
        if limit < 1 or chunk_ids == [] or document_ids == []:
            return []
        with self._operation():
            if not self.client.collection_exists(self.collection):
                raise VectorStoreError("Qdrant collection is missing. Run rag-aaup index to restore it.")
            self._ensure_collection(vector.size)
            scope = self._scope(database_id)
            for key, ids in (("chunk_id", chunk_ids), ("document_id", document_ids)):
                if ids is not None:
                    scope.must.append(models.FieldCondition(key=key, match=models.MatchAny(any=ids)))
            arguments = {
                "collection_name": self.collection,
                "query": vector.tolist(),
                "query_filter": scope,
                "limit": limit,
                "with_payload": ["chunk_id"],
                "with_vectors": False,
            }
            if group_by_document:
                groups = self.client.query_points_groups(
                    **arguments, group_by="document_id", group_size=1
                ).groups
                hits = [group.hits[0] for group in groups if group.hits]
            else:
                hits = self.client.query_points(**arguments).points
        return [(int(hit.payload["chunk_id"]), float(hit.score)) for hit in hits]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
