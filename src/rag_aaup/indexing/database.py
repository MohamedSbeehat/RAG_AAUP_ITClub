import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

import numpy as np

from rag_aaup.config import Settings
from rag_aaup.indexing.vectors import QdrantVectorStore, VectorStoreError, validate_vector
from rag_aaup.processing.chunking import Chunk


@dataclass(frozen=True)
class DocumentRecord:
    document_id: int
    relative_path: str
    family_key: str
    title: str
    sha256: str
    modified_ns: int
    version_sort: int


@dataclass(frozen=True)
class StoredChunk:
    chunk_id: int
    text: str
    page: int | None
    section: str
    topic: str
    document: DocumentRecord
    embedding: np.ndarray | None


class Database:
    def __init__(self, path: Path, vectors: QdrantVectorStore | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vectors = vectors or QdrantVectorStore(path.with_suffix(".qdrant"))
        self._index_lock = RLock()
        self._vector_revision: int | None = None
        self._has_vectors = False
        self._initialize()

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        return cls(
            settings.database_path,
            QdrantVectorStore(
                path=settings.qdrant_path or settings.database_path.with_suffix(".qdrant"),
                collection=settings.qdrant_collection,
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
                timeout=settings.qdrant_timeout_seconds,
            ),
        )

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id INTEGER PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    family_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    version_sort INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_family_idx
                    ON documents(family_key, version_sort DESC, modified_ns DESC);
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    page INTEGER,
                    section TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    embedding_dim INTEGER NOT NULL,
                    UNIQUE(document_id, ordinal)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    text,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id INTEGER PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_messages_conversation_idx
                    ON chat_messages(conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS index_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO index_metadata (key, value) VALUES ('revision', '0');
                CREATE TRIGGER IF NOT EXISTS chunks_vector_insert AFTER INSERT ON chunks BEGIN
                    UPDATE index_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision';
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_vector_delete AFTER DELETE ON chunks BEGIN
                    UPDATE index_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision';
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_vector_update AFTER UPDATE ON chunks BEGIN
                    UPDATE index_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision';
                END;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO index_metadata (key, value) VALUES ('database_id', ?)",
                (str(uuid4()),),
            )
            self.database_id = connection.execute(
                "SELECT value FROM index_metadata WHERE key = 'database_id'"
            ).fetchone()[0]

    def _revision(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM index_metadata WHERE key = 'revision'").fetchone()
            return int(row["value"])

    def sync_vectors(self) -> None:
        with self._index_lock:
            revision = self._revision()
            try:
                if self._vector_revision != revision or (self._has_vectors and not self.vectors.exists()):
                    chunks = self.all_chunks()
                    self.vectors.synchronize(chunks, self.database_id)
                    # Only checkpoint after successful Qdrant writes. Retry a failed sync before search.
                    self._vector_revision = revision
                    self._has_vectors = bool(chunks)
            except VectorStoreError:
                self._vector_revision = None
                raise

    @property
    def vectors_ready(self) -> bool:
        with self._index_lock:
            return self._vector_revision == self._revision()

    @contextmanager
    def search_snapshot(self) -> Generator[None]:
        """Serialize Qdrant local access and keep metadata stable during hybrid retrieval."""
        with self._index_lock:
            self.sync_vectors()
            yield

    def dense_search(
        self,
        vector: np.ndarray,
        limit: int,
        chunk_ids: list[int] | None = None,
        document_ids: list[int] | None = None,
        group_by_document: bool = False,
    ) -> list[tuple[int, float]]:
        with self.search_snapshot():
            try:
                return self.vectors.search(
                    vector, self.database_id, limit, chunk_ids, document_ids, group_by_document
                )
            except VectorStoreError:
                self._vector_revision = None
                raise

    def close(self) -> None:
        with self._index_lock:
            self.vectors.close()
            self._vector_revision = None

    def has_unchanged_document(self, relative_path: str, sha256: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sha256 FROM documents WHERE relative_path = ?", (relative_path,)
            ).fetchone()
        return row is not None and row["sha256"] == sha256

    def replace_document(
        self,
        relative_path: str,
        family_key: str,
        title: str,
        sha256: str,
        modified_ns: int,
        version_sort: int,
        chunks: list[Chunk],
        embeddings: np.ndarray,
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("Each chunk must have exactly one embedding")
        embeddings = np.asarray(embeddings, dtype=np.float32)
        if embeddings.ndim != 2:
            raise ValueError("Embeddings must be a matrix with one row per chunk")
        for vector in embeddings:
            validate_vector(vector)
        indexed_at = datetime.now(UTC).isoformat()

        with self._index_lock, self.connect() as connection:
            old = connection.execute(
                "SELECT document_id FROM documents WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            if old is not None:
                connection.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN "
                    "(SELECT chunk_id FROM chunks WHERE document_id = ?)",
                    (old["document_id"],),
                )
                connection.execute("DELETE FROM documents WHERE document_id = ?", (old["document_id"],))

            cursor = connection.execute(
                """
                INSERT INTO documents
                    (relative_path, family_key, title, sha256, modified_ns, version_sort, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (relative_path, family_key, title, sha256, modified_ns, version_sort, indexed_at),
            )
            document_id = cursor.lastrowid
            for ordinal, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                vector = np.asarray(embedding, dtype=np.float32)
                cursor = connection.execute(
                    """
                    INSERT INTO chunks
                        (document_id, ordinal, text, page, section, topic, embedding, embedding_dim)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        ordinal,
                        chunk.text,
                        chunk.page,
                        chunk.section,
                        chunk.topic,
                        vector.tobytes(),
                        vector.size,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)",
                    (cursor.lastrowid, chunk.text),
                )

    def documents(self) -> list[DocumentRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT document_id, relative_path, family_key, title, sha256, modified_ns, version_sort
                FROM documents ORDER BY family_key, version_sort DESC, modified_ns DESC
                """
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    def remove_document(self, relative_path: str) -> None:
        """Remove derived metadata and lexical rows; vector reconciliation removes its points."""
        with self._index_lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN "
                "(SELECT c.chunk_id FROM chunks c JOIN documents d USING(document_id) "
                "WHERE d.relative_path = ?)",
                (relative_path,),
            )
            connection.execute("DELETE FROM documents WHERE relative_path = ?", (relative_path,))

    def chunk_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def all_chunks(self) -> list[StoredChunk]:
        return self._chunks()

    def chunks_by_ids(self, chunk_ids: list[int]) -> list[StoredChunk]:
        """Fetch only candidate text/metadata; never read cached vector BLOBs on the search path."""
        return self._chunks(chunk_ids)

    def _chunks(self, chunk_ids: list[int] | None = None) -> list[StoredChunk]:
        if chunk_ids == []:
            return []
        embedding_columns = "c.embedding, c.embedding_dim" if chunk_ids is None else "NULL AS embedding"
        where = "" if chunk_ids is None else f"WHERE c.chunk_id IN ({','.join('?' for _ in chunk_ids)})"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.chunk_id, c.text, c.page, c.section, c.topic, {embedding_columns},
                       d.document_id, d.relative_path, d.family_key, d.title, d.sha256,
                       d.modified_ns, d.version_sort
                FROM chunks c JOIN documents d USING(document_id)
                {where}
                """,
                chunk_ids or [],
            ).fetchall()
        chunks: list[StoredChunk] = []
        for row in rows:
            vector = (
                np.frombuffer(row["embedding"], dtype=np.float32, count=row["embedding_dim"])
                if row["embedding"] is not None
                else None
            )
            chunks.append(
                StoredChunk(
                    chunk_id=row["chunk_id"],
                    text=row["text"],
                    page=row["page"],
                    section=row["section"],
                    topic=row["topic"],
                    document=_document_from_row(row),
                    embedding=vector,
                )
            )
        return chunks

    def lexical_search(
        self, query: str, limit: int, document_ids: list[int] | None = None
    ) -> list[tuple[int, float]]:
        if document_ids == []:
            return []
        where = ""
        parameters: list[str | int] = [query]
        if document_ids is not None:
            where = (
                "AND chunk_id IN (SELECT chunk_id FROM chunks WHERE document_id IN "
                f"({','.join('?' for _ in document_ids)}))"
            )
            parameters.extend(document_ids)
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunk_id, bm25(chunks_fts) AS score
                FROM chunks_fts WHERE chunks_fts MATCH ? {where} ORDER BY score LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [(int(row["chunk_id"]), float(row["score"])) for row in rows]

    def document_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def recent_messages(self, conversation_id: str, limit: int = 8) -> list[tuple[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM chat_messages
                WHERE conversation_id = ? ORDER BY message_id DESC LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [(row["role"], row["content"]) for row in reversed(rows)]

    def save_exchange(self, conversation_id: str, question: str, answer: str) -> None:
        created_at = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO chat_messages (conversation_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (conversation_id, "user", question, created_at),
                    (conversation_id, "assistant", answer, created_at),
                ],
            )


def _document_from_row(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        document_id=row["document_id"],
        relative_path=row["relative_path"],
        family_key=row["family_key"],
        title=row["title"],
        sha256=row["sha256"],
        modified_ns=row["modified_ns"],
        version_sort=row["version_sort"],
    )
