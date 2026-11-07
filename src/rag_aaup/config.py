from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    raw_dir: Path = PROJECT_ROOT / "data" / "raw"
    database_path: Path = PROJECT_ROOT / "indexes" / "rag.sqlite3"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_device: str = "auto"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    ollama_model: str = "qwen2.5:3b"
    llm_provider: str = "auto"
    refresh_interval_seconds: int = 45
    minimum_dense_score: float = 0.28
    retrieval_limit: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()
