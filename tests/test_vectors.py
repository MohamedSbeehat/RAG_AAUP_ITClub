from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from rag_aaup.config import Settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.vectors import QdrantVectorStore, VectorStoreError
from rag_aaup.processing.chunking import Chunk
from rag_aaup.retrieval.service import HybridRetriever


@pytest.fixture
def database(tmp_path: Path) -> Generator[Database]:
    database = Database(tmp_path / "rag.sqlite3")
    try:
        yield database
    finally:
        database.close()


def add_document(
    database: Database,
    texts: list[str],
    embeddings: list[list[float]],
    source: str = "guide.md",
    family: str = "guide",
    version: int = 1,
) -> None:
    database.replace_document(
        source,
        family,
        "Academic guide",
        str(version),
        version,
        version,
        [Chunk(text, 3, "Registration", "academic") for text in texts],
        np.asarray(embeddings, dtype=np.float32),
    )


def test_legacy_index_migrates_without_embedding_or_losing_history(database: Database) -> None:
    add_document(database, ["Admission requirements"], [[2, 0]])
    database.save_exchange("existing-chat", "Admission?", "See the guide.")
    # Restore the schema from before Qdrant was introduced, retaining the cached embeddings.
    with database.connect() as connection:
        for trigger in ("chunks_vector_insert", "chunks_vector_delete", "chunks_vector_update"):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE index_metadata")
    path = database.path
    database.close()

    migrated = Database(path)
    try:
        hits = migrated.dense_search(np.asarray([1, 0]), limit=5)
        assert len(hits) == 1
        assert hits[0][1] == pytest.approx(1.0)
        assert migrated.chunks_by_ids([hits[0][0]])[0].page == 3
        assert migrated.recent_messages("existing-chat") == [
            ("user", "Admission?"),
            ("assistant", "See the guide."),
        ]
    finally:
        migrated.close()


def test_persistence_and_unchanged_vectors_are_not_rewritten(database: Database, monkeypatch) -> None:
    add_document(database, ["Admission requirements"], [[1, 0]])
    database.sync_vectors()
    database_id = database.database_id
    path = database.path
    database.close()
    reopened = Database(path)
    try:
        upsert = Mock(wraps=reopened.vectors.client.upsert)
        monkeypatch.setattr(reopened.vectors.client, "upsert", upsert)
        reopened.sync_vectors()
        reopened.sync_vectors()
        assert reopened.database_id == database_id
        assert reopened.dense_search(np.asarray([1, 0]), 1)[0][1] == pytest.approx(1.0)
        upsert.assert_not_called()
    finally:
        reopened.close()


def test_replacement_removes_old_vectors_and_handles_reused_chunk_ids(database: Database) -> None:
    add_document(database, ["Old admission", "Old fee", "Old deadline"], [[1, 0]] * 3)
    database.sync_vectors()
    add_document(database, ["Current regulation"], [[0, 1]], version=2)
    hits = database.dense_search(np.asarray([1, 0]), 10)
    assert len(hits) == 1
    assert hits[0][1] == pytest.approx(0.0)
    chunk = database.chunks_by_ids([hits[0][0]])[0]
    assert chunk.text == "Current regulation"
    assert database.vectors.client.count(database.vectors.collection, exact=True).count == 1


def test_failed_sync_blocks_search_and_recovers_after_restart(database: Database, monkeypatch) -> None:
    add_document(database, ["Old admission"], [[1, 0]])
    database.sync_vectors()
    add_document(database, ["Current regulation"], [[0, 1]], version=2)
    with monkeypatch.context() as patch:
        patch.setattr(database.vectors.client, "upsert", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(VectorStoreError, match="Qdrant operation failed"):
            database.dense_search(np.asarray([1, 0]), 10)
    path = database.path
    database.close()
    reopened = Database(path)
    try:
        hits = reopened.dense_search(np.asarray([0, 1]), 10)
        assert len(hits) == 1
        assert hits[0][1] == pytest.approx(1.0)
        assert reopened.chunks_by_ids([hits[0][0]])[0].text == "Current regulation"
    finally:
        reopened.close()


def test_deleted_collection_is_recovered_from_cached_embeddings(database: Database) -> None:
    add_document(database, ["Admission requirements"], [[1, 0]])
    database.sync_vectors()
    database.vectors.client.delete_collection(database.vectors.collection)
    assert database.dense_search(np.asarray([1, 0]), 1)[0][1] == pytest.approx(1.0)


def test_batched_stale_deletion_recovers_after_partial_failure(database: Database, monkeypatch) -> None:
    add_document(database, ["Old evidence"] * 260, [[1, 0]] * 260)
    database.sync_vectors()
    add_document(database, ["Current evidence"] * 3, [[0, 1]] * 3, version=2)
    delete = database.vectors.client.delete
    calls = 0

    def fail_second_delete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted after the first batch")
        return delete(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(database.vectors.client, "delete", fail_second_delete)
        with pytest.raises(VectorStoreError):
            database.sync_vectors()
        assert not database.vectors_ready
    hits = database.dense_search(np.asarray([0, 1]), 10)
    assert len(hits) == 3
    assert all(score == pytest.approx(1.0) for _, score in hits)
    assert database.vectors.client.count(database.vectors.collection, exact=True).count == 3


def test_shared_collection_keeps_database_namespaces_separate(tmp_path: Path) -> None:
    store = QdrantVectorStore(tmp_path / "shared-qdrant")
    first = Database(tmp_path / "first.sqlite3", store)
    second = Database(tmp_path / "second.sqlite3", store)
    try:
        add_document(first, ["First university"], [[1, 0]])
        add_document(second, ["Second university"], [[0, 1]])
        first.sync_vectors()
        second.sync_vectors()
        assert first.database_id != second.database_id
        assert store.client.count(store.collection, exact=True).count == 2
        first_hits = first.dense_search(np.asarray([1, 0]), 10)
        second_hits = second.dense_search(np.asarray([1, 0]), 10)
        assert len(first_hits) == len(second_hits) == 1
        assert first_hits[0][1] == pytest.approx(1.0)
        assert second_hits[0][1] == pytest.approx(0.0)
    finally:
        first.close()
        second.close()


def test_dimension_change_is_explicit_and_does_not_delete_index(database: Database) -> None:
    add_document(database, ["Admission requirements"], [[1, 0]])
    database.sync_vectors()
    with pytest.raises(VectorStoreError, match="incompatible vector dimension"):
        database.dense_search(np.asarray([1, 0, 0]), 1)
    assert database.dense_search(np.asarray([1, 0]), 1)[0][1] == pytest.approx(1.0)


@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 0], [float("inf"), 0]])
def test_invalid_vectors_do_not_overwrite_document(database: Database, vector: list[float]) -> None:
    add_document(database, ["Admission requirements"], [[1, 0]])
    with pytest.raises(ValueError, match="finite, nonzero"):
        add_document(database, ["Invalid replacement"], [vector], version=2)
    assert database.all_chunks()[0].text == "Admission requirements"


def test_retrieval_uses_qdrant_without_loading_sqlite_vectors(database: Database, monkeypatch) -> None:
    add_document(database, ["Semantic evidence"], [[2, 0]])
    database.sync_vectors()
    monkeypatch.setattr(database, "all_chunks", Mock(side_effect=AssertionError("full vector scan")))
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    query_points = Mock(wraps=database.vectors.client.query_points)
    monkeypatch.setattr(database.vectors.client, "query_points", query_points)
    results = HybridRetriever(Settings(), database).search("unmatched wording")
    assert results[0].text == "Semantic evidence"
    assert results[0].page == 3
    assert results[0].citation == "S1"
    assert query_points.called
    assert all(chunk.embedding is None for chunk in database.chunks_by_ids([1]))


def test_lexical_only_evidence_outside_dense_candidates_is_retained(database: Database, monkeypatch) -> None:
    add_document(database, ["General evidence"] * 8 + ["CS404 prerequisite"], [[1, 0]] * 8 + [[0, 1]])
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    retriever = HybridRetriever(Settings(retrieval_candidate_limit=4), database)
    assert retriever.search("CS404 prerequisite", limit=1)[0].text == "CS404 prerequisite"


def test_current_version_is_found_when_older_chunks_crowd_candidate_pool(
    database: Database, monkeypatch
) -> None:
    add_document(database, ["Older evidence"] * 8, [[1, 0]] * 8, source="guide_2025.md", version=2025)
    add_document(database, ["Current evidence"], [[0.6, 0.8]], source="guide_2026.md", version=2026)
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    retriever = HybridRetriever(Settings(retrieval_candidate_limit=4), database)
    results = retriever.search("unmatched wording", limit=1)
    assert results[0].text == "Current evidence"
    assert results[0].is_latest_version
