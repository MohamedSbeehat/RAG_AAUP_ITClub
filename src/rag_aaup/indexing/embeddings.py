from functools import lru_cache

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

from rag_aaup.config import get_settings


@lru_cache
def get_model() -> SentenceTransformer:
    settings = get_settings()
    device = settings.embedding_device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        return SentenceTransformer(settings.embedding_model, device=device, local_files_only=True)
    except OSError:
        return SentenceTransformer(settings.embedding_model, device=device)


def encode_documents(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    prefixed = [f"passage: {text}" for text in texts]
    return (
        get_model()
        .encode(
            prefixed,
            batch_size=32,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        .astype(np.float32)
    )


def encode_query(text: str) -> np.ndarray:
    return (
        get_model()
        .encode(
            [f"query: {text}"],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
        .astype(np.float32)
    )
