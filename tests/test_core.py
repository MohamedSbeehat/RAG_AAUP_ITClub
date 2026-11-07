from pathlib import Path

import numpy as np

from rag_aaup.config import Settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import IndexingService, document_version
from rag_aaup.ingestion.loaders import TextPage
from rag_aaup.processing.chunking import Chunk, chunk_pages, normalize_for_search
from rag_aaup.retrieval.service import HybridRetriever


def test_arabic_normalization_and_chunk_metadata() -> None:
    assert normalize_for_search("إِجْراءات الـقَـبـول") == "اجراءات القبول"
    chunks = chunk_pages([TextPage("عمادة القبول والتسجيل تطبق الأنظمة والسياسات الأكاديمية.", page=4)])
    assert len(chunks) == 1
    assert chunks[0].page == 4
    assert chunks[0].topic == "القبول والتسجيل"


def test_version_name_groups_revisions() -> None:
    older_family, older_version = document_version("AAUP_Bachelor_Instructions_2025", 10)
    newer_family, newer_version = document_version("AAUP_Bachelor_Instructions_2026", 20)
    assert older_family == newer_family == "aaup-bachelor-instructions"
    assert newer_version > older_version


def test_incremental_index_uses_official_aaup_corpus(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    source = project_root / "data" / "raw" / "AAUP_Official_Test_Corpus_2026-11-07.md"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source_path = raw_dir / source.name
    corpus = source.read_text(encoding="utf-8")
    source_path.write_text(corpus, encoding="utf-8")
    settings = Settings(raw_dir=raw_dir, database_path=tmp_path / "indexes" / "rag.sqlite3")
    database = Database(settings.database_path)

    def fake_encode(texts: list[str]) -> np.ndarray:
        return np.asarray(
            [[1, 0] if "19 credit hours" in text else [0, 1] for text in texts],
            dtype=np.float32,
        )

    monkeypatch.setattr("rag_aaup.indexing.service.encode_documents", fake_encode)
    monkeypatch.setattr(
        "rag_aaup.retrieval.service.encode_query",
        lambda _: np.asarray([1, 0], dtype=np.float32),
    )
    indexer = IndexingService(settings, database)

    first_report = indexer.sync()
    assert first_report.added == [source_path.name]
    assert not first_report.failed
    assert indexer.sync().unchanged == [source_path.name]

    results = HybridRetriever(settings, database).search("ordinary semester maximum credit hours")
    assert any("19" in result.text for result in results)
    assert all(result.source.startswith(source_path.name) for result in results)

    source_path.write_text(f"{corpus}\n", encoding="utf-8")
    update_report = indexer.sync()
    assert update_report.updated == [source_path.name]
    assert database.document_count() == 1


def test_newer_version_preferred_and_old_version_is_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    corpus = (project_root / "data" / "raw" / "AAUP_Official_Test_Corpus_2026-11-07.md").read_text(
        encoding="utf-8"
    )
    admission_fact = (
        "the Doctor of Medicine row lists the scientific secondary-school branch "
        "and a minimum average of 90%."
    )
    tuition_fact = "This corpus does not contain a verified per-credit fee for any program."
    assert admission_fact in corpus
    assert tuition_fact in corpus

    settings = Settings(
        raw_dir=tmp_path / "raw",
        database_path=tmp_path / "indexes" / "rag.sqlite3",
    )
    database = Database(settings.database_path)
    database.replace_document(
        "aaup/admission-source.md",
        "aaup-admission-source",
        "AAUP admission source",
        "older",
        1,
        1,
        [Chunk(admission_fact, None, "Admission", "القبول والتسجيل")],
        np.asarray([[1, 0]], dtype=np.float32),
    )
    database.replace_document(
        "aaup/current-source.md",
        "aaup-admission-source",
        "AAUP current source",
        "current",
        2,
        2,
        [Chunk(tuition_fact, None, "Fees", "الرسوم والمنح")],
        np.asarray([[0, 1]], dtype=np.float32),
    )
    monkeypatch.setattr(
        "rag_aaup.retrieval.service.encode_query",
        lambda _: np.asarray([1, 0], dtype=np.float32),
    )

    retriever = HybridRetriever(settings, database)
    fallback = retriever.search("Doctor of Medicine admission average")
    assert fallback[0].source.startswith("aaup/admission-source.md")
    assert not fallback[0].is_latest_version

    database.replace_document(
        "aaup/current-source.md",
        "aaup-admission-source",
        "AAUP current source",
        "current-updated",
        2,
        2,
        [Chunk(admission_fact, None, "Admission", "القبول والتسجيل")],
        np.asarray([[1, 0]], dtype=np.float32),
    )
    latest = retriever.search("Doctor of Medicine admission average")
    assert latest[0].source.startswith("aaup/current-source.md")
    assert latest[0].is_latest_version
