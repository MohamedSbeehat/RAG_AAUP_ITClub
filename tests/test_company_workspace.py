from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from rag_aaup import api
from rag_aaup.assistant import AcademicAssistant
from rag_aaup.config import Settings
from rag_aaup.generation.service import GeneratedAnswer, GenerationError, _prompt
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import IndexingService
from rag_aaup.processing.chunking import Chunk
from rag_aaup.retrieval.service import HybridRetriever


@pytest.fixture
def company(tmp_path: Path, monkeypatch):
    settings = Settings(
        _env_file=None,
        raw_dir=tmp_path / "documents",
        database_path=tmp_path / "company.sqlite3",
        knowledge_mode="organization",
        organization_name="Example Company",
        app_password=None,
    )
    monkeypatch.setattr(
        "rag_aaup.indexing.service.encode_documents",
        lambda texts: np.asarray([[1, 0]] * len(texts), dtype=np.float32),
    )
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    return settings


def test_drop_update_and_remove_file_syncs_both_indexes(company: Settings) -> None:
    database = Database.from_settings(company)
    try:
        indexer = IndexingService(company, database)
        company.raw_dir.mkdir()
        path = company.raw_dir / "policy.txt"
        path.write_text("Support staff respond within four working hours.", encoding="utf-8")
        assert indexer.sync().added == ["policy.txt"]
        assert database.chunk_count() == 1
        path.write_text("Support staff respond within two working hours.", encoding="utf-8")
        assert indexer.sync().updated == ["policy.txt"]
        assert "two" in HybridRetriever(company, database).search("support")[0].text
        path.unlink()
        assert indexer.sync().removed == ["policy.txt"]
        assert database.document_count() == database.chunk_count() == 0
        assert not database.lexical_search('"Support"', 10)
        assert database.vectors.client.count(database.vectors.collection, exact=True).count == 0
        assert indexer.last_synced_at
    finally:
        database.close()


def test_missing_folder_preserves_existing_index(company: Settings) -> None:
    database = Database.from_settings(company)
    try:
        company.raw_dir.mkdir()
        (company.raw_dir / "policy.txt").write_text("Support policy", encoding="utf-8")
        indexer = IndexingService(company, database)
        indexer.sync()
        company.raw_dir.rename(company.raw_dir.with_name("temporarily-offline"))
        report = indexer.sync()
        assert "[directory]" in report.failed
        assert database.document_count() == 1
    finally:
        database.close()


def test_departments_with_same_filename_are_independent(company: Settings) -> None:
    for department in ("HR", "Finance"):
        directory = company.raw_dir / department
        directory.mkdir(parents=True)
        (directory / "Guide_2026.txt").write_text(f"{department} policy", encoding="utf-8")
    database = Database.from_settings(company)
    try:
        assert not IndexingService(company, database).sync().failed
        assert len({document.family_key for document in database.documents()}) == 2
        assert len(HybridRetriever(company, database).search("policy")) == 2
    finally:
        database.close()


def test_file_changing_during_embedding_is_retried(company: Settings, monkeypatch) -> None:
    company.raw_dir.mkdir()
    path = company.raw_dir / "policy.txt"
    path.write_text("Initial policy", encoding="utf-8")

    def copying_file(texts):
        path.write_text("Copy still in progress; content changed.", encoding="utf-8")
        return np.asarray([[1, 0]] * len(texts), dtype=np.float32)

    monkeypatch.setattr("rag_aaup.indexing.service.encode_documents", copying_file)
    database = Database.from_settings(company)
    try:
        assert "policy.txt" in IndexingService(company, database).sync().failed
        assert database.document_count() == 0
    finally:
        database.close()


def test_company_upload_source_download_and_limits(company: Settings, monkeypatch) -> None:
    settings = company.model_copy(update={"max_upload_size_mb": 1})
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    with TestClient(api.app) as client:
        content = b"Support staff respond within four working hours."
        upload = client.post("/api/documents", files={"files": ("policy.txt", content, "text/plain")})
        assert upload.status_code == 200
        assert upload.json()["added"] == ["policy.txt"]
        status = client.get("/api/workspace").json()
        assert status["document_count"] == status["chunk_count"] == 1
        search = client.post("/api/ask", json={"question": "support", "mode": "sources"}).json()
        source = search["sources"][0]
        assert client.get(source["url"]).content == content
        oversized = client.post(
            "/api/documents", files={"files": ("policy.txt", b"x" * (1024 * 1024 + 1), "text/plain")}
        )
        assert oversized.status_code == 413
        assert (company.raw_dir / "policy.txt").read_bytes() == content
        assert not list(company.raw_dir.glob("*.upload"))


def test_workspace_password_protects_ui_and_api(company: Settings, monkeypatch) -> None:
    from pydantic import SecretStr

    settings = company.model_copy(update={"app_password": SecretStr("workspace-secret")})
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/api/documents").status_code == 401
        assert client.get("/api/health", auth=("admin", "wrong")).status_code == 401
        response = client.get("/api/health", auth=("admin", "workspace-secret"))
        assert response.status_code == 200
        assert "workspace-secret" not in response.text


def test_company_prompt_does_not_use_academic_role(company: Settings) -> None:
    prompt = _prompt("What is the support policy?", [], [], company)
    assert "Example Company" in prompt
    assert "academic-advising assistant" not in prompt


def test_unknown_citations_are_rejected_without_saving_exchange(company: Settings, monkeypatch) -> None:
    database = Database.from_settings(company)
    try:
        database.replace_document(
            "policy.txt",
            "policy",
            "Policy",
            "hash",
            1,
            1,
            [Chunk("Support policy", None, "", "")],
            np.asarray([[1, 0]], dtype=np.float32),
        )
        monkeypatch.setattr(
            "rag_aaup.assistant.generate_answer", Mock(return_value=GeneratedAnswer("Wrong [S999]", "gemini"))
        )
        assistant = AcademicAssistant(company, database, HybridRetriever(company, database))
        with pytest.raises(GenerationError):
            assistant.answer("support", "invalid-citation-test")
        assert database.recent_messages("invalid-citation-test") == []
    finally:
        database.close()


def test_uncited_generation_becomes_safe_abstention(company: Settings, monkeypatch) -> None:
    database = Database.from_settings(company)
    try:
        database.replace_document(
            "policy.txt",
            "policy",
            "Policy",
            "hash",
            1,
            1,
            [Chunk("Support policy", None, "", "")],
            np.asarray([[1, 0]], dtype=np.float32),
        )
        monkeypatch.setattr(
            "rag_aaup.assistant.generate_answer",
            Mock(return_value=GeneratedAnswer("An unsupported claim", "gemini")),
        )
        response = AcademicAssistant(company, database, HybridRetriever(company, database)).answer("support")
        assert response.provider == "abstain"
        assert response.sources == []
        assert "unsupported claim" not in response.answer
    finally:
        database.close()
