import re
from dataclasses import dataclass

import numpy as np

from rag_aaup.config import Settings
from rag_aaup.indexing.database import Database, DocumentRecord, StoredChunk
from rag_aaup.indexing.embeddings import encode_query


@dataclass(frozen=True)
class SearchResult:
    citation: str
    text: str
    source: str
    title: str
    page: int | None
    section: str
    topic: str
    is_latest_version: bool
    score: float


def _fts_query(query: str) -> str:
    words = re.findall(r"[\w\u0600-\u06FF]+", query, flags=re.UNICODE)
    return " OR ".join(f'"{word}"' for word in words)


def _latest_documents(documents: list[DocumentRecord]) -> dict[str, DocumentRecord]:
    latest: dict[str, DocumentRecord] = {}
    for document in documents:
        current = latest.get(document.family_key)
        if current is None or (document.version_sort, document.modified_ns) > (
            current.version_sort,
            current.modified_ns,
        ):
            latest[document.family_key] = document
    return latest


class HybridRetriever:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        chunks = self.database.all_chunks()
        if not chunks or not query.strip():
            return []

        query_embedding = encode_query(query)
        dense_scores = np.asarray([float(np.dot(chunk.embedding, query_embedding)) for chunk in chunks])
        dense_order = np.argsort(-dense_scores)
        dense_rank = {int(chunks[index].chunk_id): rank for rank, index in enumerate(dense_order, start=1)}
        dense_score_by_id = {chunk.chunk_id: float(dense_scores[index]) for index, chunk in enumerate(chunks)}

        lexical_query = _fts_query(query)
        lexical = self.database.lexical_search(lexical_query, max(32, len(chunks))) if lexical_query else []
        lexical_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(lexical, start=1)}

        documents = self.database.documents()
        latest = _latest_documents(documents)
        latest_ids = {document.document_id for document in latest.values()}

        relevant_latest_families: set[str] = set()
        for chunk in chunks:
            similarity = dense_score_by_id[chunk.chunk_id]
            if chunk.document.document_id in latest_ids and (
                similarity >= self.settings.minimum_dense_score or chunk.chunk_id in lexical_rank
            ):
                relevant_latest_families.add(chunk.document.family_key)

        eligible: list[StoredChunk] = []
        for chunk in chunks:
            if (
                chunk.document.document_id in latest_ids
                or chunk.document.family_key not in relevant_latest_families
            ):
                eligible.append(chunk)

        limit = limit or self.settings.retrieval_limit
        scored: list[tuple[float, StoredChunk, float]] = []
        for chunk in eligible:
            similarity = dense_score_by_id[chunk.chunk_id]
            if similarity < self.settings.minimum_dense_score and chunk.chunk_id not in lexical_rank:
                continue
            reciprocal_rank = 1 / (60 + dense_rank[chunk.chunk_id])
            if chunk.chunk_id in lexical_rank:
                reciprocal_rank += 1 / (60 + lexical_rank[chunk.chunk_id])
            score = reciprocal_rank + max(similarity, 0) * 0.01
            scored.append((score, chunk, similarity))

        scored.sort(key=lambda item: (item[0], item[1].document.version_sort), reverse=True)
        results: list[SearchResult] = []
        for citation_number, (score, chunk, similarity) in enumerate(scored[:limit], start=1):
            page_suffix = f", p. {chunk.page}" if chunk.page is not None else ""
            section_suffix = f", {chunk.section}" if chunk.section else ""
            results.append(
                SearchResult(
                    citation=f"S{citation_number}",
                    text=chunk.text,
                    source=f"{chunk.document.relative_path}{page_suffix}{section_suffix}",
                    title=chunk.document.title,
                    page=chunk.page,
                    section=chunk.section,
                    topic=chunk.topic,
                    is_latest_version=chunk.document.document_id in latest_ids,
                    score=score,
                )
            )
        return results


__all__ = ["HybridRetriever", "SearchResult"]
