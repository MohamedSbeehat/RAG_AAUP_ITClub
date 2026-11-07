import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    knowledge_mode: Literal["academic", "organization"] = "academic"
    organization_name: str = "AAUP"
    assistant_name: str = "مساعد المستندات"
    app_username: str = "admin"
    app_password: SecretStr | None = None
    multi_tenant_enabled: bool = False
    workspace_root: Path = PROJECT_ROOT / "workspaces"
    session_hours: int = Field(default=12, ge=1, le=168)
    max_upload_size_mb: int = Field(default=25, ge=1, le=500)
    remove_deleted_documents: bool = True
    ocr_tessdata_path: Path = PROJECT_ROOT / "data" / "models" / "tessdata"
    ocr_language: str = "ara+eng"
    database_path: Path = PROJECT_ROOT / "indexes" / "rag.sqlite3"
    qdrant_path: Path | None = None
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = Field(default="academic_chunks", min_length=1)
    qdrant_timeout_seconds: int = Field(default=30, gt=0)
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_device: str = "auto"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    # Comma-separated free models tried after GEMINI_MODEL; each has its own daily free quota.
    gemini_fallback_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-20b"
    ollama_model: str = "qwen2.5:3b"
    llm_provider: str = "auto"
    llm_timeout_seconds: int = Field(default=40, ge=1, le=60)
    refresh_interval_seconds: int = Field(default=15, ge=1)
    minimum_dense_score: float = 0.28
    retrieval_limit: int = 6
    retrieval_candidate_limit: int = Field(default=64, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def generation_settings(settings: Settings, env_file: Path | None = None) -> Settings:
    """Reload only provider settings, leaving the active index and embedding model unchanged."""
    fields = {
        "gemini_api_key",
        "gemini_model",
        "gemini_fallback_models",
        "groq_api_key",
        "groq_model",
        "ollama_model",
        "llm_provider",
        "llm_timeout_seconds",
    }
    path = env_file or Path(settings.model_config["env_file"])
    file_values = {key.casefold(): value for key, value in dotenv_values(path).items()}
    environment = {key.casefold(): value for key, value in os.environ.items()}
    overrides = {
        key: environment.get(key, file_values.get(key))
        for key in fields
        if environment.get(key, file_values.get(key)) is not None
    }
    return Settings.model_validate({**settings.model_dump(), **overrides})
