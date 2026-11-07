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
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "LLM_TIMEOUT_SECONDS"):
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


def test_auto_uses_groq_when_gemini_quota_or_service_fails(monkeypatch) -> None:
    source = SearchResult("S1", "Evidence", "guide.md", "Guide", 1, "", "", True, 1.0)
    settings = Settings(
        llm_provider="auto",
        gemini_api_key="gemini-key",
        gemini_fallback_models="",
        groq_api_key="groq-key",
    )
    gemini = Mock(side_effect=service.GenerationError("quota exceeded"))
    groq = Mock(return_value="Verified answer [S1]")
    ollama = Mock(side_effect=AssertionError("Ollama should not be needed"))
    monkeypatch.setattr(service, "_generate_gemini", gemini)
    monkeypatch.setattr(service, "_generate_groq", groq)
    monkeypatch.setattr(service, "_generate_ollama", ollama)

    answer = service.generate_answer(settings, "Admission requirements?", [source], [])

    assert answer == service.GeneratedAnswer("Verified answer [S1]", "groq")
    gemini.assert_called_once()
    groq.assert_called_once()
    ollama.assert_not_called()


def test_groq_request_uses_configured_model_key_and_timeout(monkeypatch) -> None:
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": " Verified answer [S1] "}}]}
    client = Mock()
    client.post.return_value = response
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=False)
    create_client = Mock(return_value=manager)
    monkeypatch.setattr(service.httpx, "Client", create_client)
    settings = Settings(groq_api_key="groq-key", groq_model="openai/gpt-oss-20b", llm_timeout_seconds=12)

    assert service._generate_groq(settings, "prompt") == "Verified answer [S1]"
    create_client.assert_called_once_with(timeout=12)
    client.post.assert_called_once_with(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": "Bearer groq-key"},
        json={"model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "prompt"}]},
    )
    response.raise_for_status.assert_called_once()


def test_groq_key_is_loaded_from_env_without_exposing_it(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=auto\nGROQ_API_KEY=groq-secret\nGROQ_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )

    settings = generation_settings(Settings(_env_file=None), env_file)

    assert settings.groq_api_key.get_secret_value() == "groq-secret"
    assert settings.groq_model == "openai/gpt-oss-20b"
    assert "groq-secret" not in repr(settings)


def _quota_error(message: str = "Quota exceeded. Please retry in 1h0m0s.") -> service.GenerationError:
    error = service.GenerationError(message)
    error.code = 429
    return error


def test_gemini_quota_moves_to_next_free_model_and_skips_exhausted_one(monkeypatch) -> None:
    source = SearchResult("S1", "Evidence", "guide.md", "Guide", 1, "", "", True, 1.0)
    settings = Settings(
        llm_provider="gemini",
        gemini_api_key="gemini-key",
        gemini_model="main-flash",
        gemini_fallback_models="lite-a, lite-b",
    )
    monkeypatch.setattr(service, "_cooldowns", {})

    def fake_gemini(_settings, _prompt, model=None):
        if model == "main-flash":
            raise _quota_error()
        return f"Answer from {model} [S1]"

    gemini = Mock(side_effect=fake_gemini)
    monkeypatch.setattr(service, "_generate_gemini", gemini)

    first = service.generate_answer(settings, "ما شروط القبول؟", [source], [])
    second = service.generate_answer(settings, "ما شروط القبول؟", [source], [])

    assert first == service.GeneratedAnswer("Answer from lite-a [S1]", "gemini")
    assert second == first
    called_models = [call.args[2] for call in gemini.call_args_list]
    # main-flash is tried once, then skipped while its quota is still exhausted.
    assert called_models == ["main-flash", "lite-a", "lite-a"]


def test_all_providers_failing_reports_each_real_reason(monkeypatch) -> None:
    source = SearchResult("S1", "Evidence", "guide.md", "Guide", 1, "", "", True, 1.0)
    settings = Settings(
        llm_provider="auto",
        gemini_api_key="gemini-key",
        gemini_fallback_models="",
        groq_api_key="groq-secret",
    )
    monkeypatch.setattr(service, "_cooldowns", {})
    monkeypatch.setattr(service, "_generate_gemini", Mock(side_effect=_quota_error()))
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    unauthorized = httpx.HTTPStatusError(
        "401 groq-secret", request=request, response=httpx.Response(401, request=request)
    )
    monkeypatch.setattr(service, "_generate_groq", Mock(side_effect=unauthorized))
    ollama = Mock(side_effect=AssertionError("Ollama is not running and must not be tried"))
    monkeypatch.setattr(service, "_generate_ollama", ollama)
    monkeypatch.setattr(service, "_ollama_running", lambda _settings: False)

    with pytest.raises(service.ProviderUnavailableError) as failure:
        service.generate_answer(settings, "كم نسبة الغياب المسموح بها؟", [source], [])

    message = str(failure.value)
    assert "Gemini" in message and "429" in message
    assert "Groq" in message and "401" in message
    assert "Ollama" not in message
    assert "groq-secret" not in message
    ollama.assert_not_called()


def test_retry_delay_is_read_from_provider_message() -> None:
    error = RuntimeError("Generate content requests per day limit exceeded. Please retry in 18h8m0.6s.")
    assert service._retry_after_seconds(error) == pytest.approx(18 * 3600 + 8 * 60 + 0.6)
    assert service._retry_after_seconds(RuntimeError("no hint")) == service._DEFAULT_COOLDOWN_SECONDS


def test_unavailable_providers_return_retrieved_sources_instead_of_error(
    live_config_client, monkeypatch
) -> None:
    client, env_file = live_config_client
    env_file.write_text("LLM_PROVIDER=gemini\nGEMINI_API_KEY=test-key\n", encoding="utf-8")
    generate = Mock(side_effect=service.ProviderUnavailableError("تعذر توليد الإجابة حالياً:\n- Gemini: 429"))
    monkeypatch.setattr("rag_aaup.assistant.generate_answer", generate)

    response = client.post("/api/ask", json={"question": "شروط القبول"})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "sources_fallback"
    assert "429" in body["answer"]
    assert body["sources"][0]["text"] == "Verified admission excerpt"
