from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from rag_aaup import api
from rag_aaup.config import Settings, generation_settings
from rag_aaup.generation import service
from rag_aaup.indexing.database import Database
from rag_aaup.processing.chunking import Chunk
from rag_aaup.retrieval.service import SearchResult


@pytest.fixture
def live_config_client(tmp_path: Path, monkeypatch) -> Generator[tuple[TestClient, Path]]:
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=gemini\nGEMINI_API_KEY=\n", encoding="utf-8")
    for key in ("GEMINI_API_KEY", "LLM_PROVIDER", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        _env_file=None,
        raw_dir=tmp_path / "raw",
        database_path=tmp_path / "index.sqlite3",
        llm_provider="gemini",
        remove_deleted_documents=False,
    )
    database = Database.from_settings(settings)
    database.replace_document(
        "guide.md",
        "guide",
        "Guide",
        "hash",
        1,
        1,
        [Chunk("Verified admission excerpt", 2, "Admission", "academic")],
        np.asarray([[1, 0]], dtype=np.float32),
    )
    database.close()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "generation_settings", lambda base: generation_settings(base, env_file))
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    with TestClient(api.app) as client:
        yield client, env_file


def test_missing_gemini_key_is_reported_before_loading_embeddings(live_config_client, monkeypatch) -> None:
    client, _ = live_config_client
    encode = Mock(side_effect=AssertionError("Embedding model must not load for missing credentials"))
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", encode)
    status = client.get("/api/status")
    assert not status.json()["generation_configured"]
    response = client.post("/api/ask", json={"question": "Admission requirements?"})
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]
    encode.assert_not_called()


def test_sources_only_mode_works_repeatedly_without_llm(live_config_client, monkeypatch) -> None:
    client, _ = live_config_client
    generate = Mock(side_effect=AssertionError("Sources mode must not call an LLM"))
    monkeypatch.setattr("rag_aaup.assistant.generate_answer", generate)
    for _ in range(2):
        response = client.post("/api/ask", json={"question": "شروط القبول", "mode": "sources"})
        assert response.status_code == 200
        assert response.json()["provider"] == "sources_only"
        assert "ليست إجابة" in response.json()["answer"]
        assert response.json()["sources"][0]["text"] == "Verified admission excerpt"
    generate.assert_not_called()


def test_adding_key_activates_gemini_without_restart(live_config_client, monkeypatch) -> None:
    client, env_file = live_config_client
    assert not client.get("/api/status").json()["generation_configured"]
    env_file.write_text("LLM_PROVIDER=gemini\nGEMINI_API_KEY=test-key\n", encoding="utf-8")
    generate = Mock(return_value=service.GeneratedAnswer("Verified answer [S1]", "gemini"))
    monkeypatch.setattr("rag_aaup.assistant.generate_answer", generate)
    status = client.get("/api/status")
    assert status.json()["generation_configured"]
    assert "test-key" not in status.text
    response = client.post("/api/ask", json={"question": "Admission requirements?"})
    assert response.status_code == 200
    assert response.json()["provider"] == "gemini"
    assert generate.call_args.args[0].gemini_api_key.get_secret_value() == "test-key"


def test_request_recovers_after_generation_failure(live_config_client, monkeypatch) -> None:
    client, env_file = live_config_client
    env_file.write_text("LLM_PROVIDER=gemini\nGEMINI_API_KEY=test-key\n", encoding="utf-8")
    generate = Mock(
        side_effect=[
            service.GenerationError("Temporary failure"),
            service.GeneratedAnswer("Verified answer [S1]", "gemini"),
        ]
    )
    monkeypatch.setattr("rag_aaup.assistant.generate_answer", generate)
    assert client.post("/api/ask", json={"question": "Admission requirements?"}).status_code == 503
    assert client.post("/api/ask", json={"question": "Admission requirements?"}).status_code == 200


def test_whitespace_question_is_rejected(live_config_client) -> None:
    client, _ = live_config_client
    assert client.post("/api/ask", json={"question": "  ", "mode": "sources"}).status_code == 422


def test_provider_timeout_is_localized_without_leaking_credentials(monkeypatch) -> None:
    source = SearchResult("S1", "Evidence", "guide.md", "Guide", 1, "", "", True, 1.0)
    monkeypatch.setattr(service, "_generate_gemini", Mock(side_effect=httpx.ReadTimeout("private-key")))
    with pytest.raises(service.GenerationError) as failure:
        service.generate_answer(Settings(llm_provider="gemini"), "ما شروط القبول؟", [source], [])
    assert "private-key" not in str(failure.value)
    assert "تعذر" in str(failure.value)


def test_gemini_has_bounded_timeout_and_closes_client(monkeypatch) -> None:
    client = Mock()
    client.models.generate_content.return_value.text = "Verified answer [S1]"
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=False)
    create_client = Mock(return_value=manager)
    monkeypatch.setattr(service.genai, "Client", create_client)
    settings = Settings(gemini_api_key="test-key", llm_timeout_seconds=12)
    assert service._generate_gemini(settings, "prompt") == "Verified answer [S1]"
    options = create_client.call_args.kwargs["http_options"]
    assert options.timeout == 12000
    assert options.retry_options.attempts == 1
    manager.__exit__.assert_called_once()
