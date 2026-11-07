import re
from dataclasses import dataclass

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
    document_id: int | None = None


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
        limit = self.settings.retrieval_limit if limit is None else limit
        if limit < 1 or not query.strip():
            return []
        with self.database.search_snapshot():
            return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        documents = self.database.documents()
        if not documents:
            return []
        query_embedding = encode_query(query)
        candidate_limit = max(self.settings.retrieval_candidate_limit, limit * 4)
        dense_score_by_id = dict(self.database.dense_search(query_embedding, candidate_limit))
        lexical_query = _fts_query(query)
        lexical_scores = (
            dict(self.database.lexical_search(lexical_query, candidate_limit)) if lexical_query else {}
        )
        candidate_ids = dense_score_by_id.keys() | lexical_scores.keys()
        candidates = self.database.chunks_by_ids(sorted(candidate_ids))

        latest = _latest_documents(documents)
        latest_ids = {document.document_id for document in latest.values()}
        latest_candidate_ids = sorted({latest[chunk.document.family_key].document_id for chunk in candidates})
        # Check the best current evidence per candidate family. A large older edition must not
        # crowd its newer edition out of the bounded candidate pool.
        dense_score_by_id.update(
            self.database.dense_search(
                query_embedding,
                len(latest_candidate_ids),
                document_ids=latest_candidate_ids,
                group_by_document=True,
            )
        )
        if lexical_query:
            for document_id in latest_candidate_ids:
                lexical_scores.update(self.database.lexical_search(lexical_query, 1, [document_id]))

        lexical_only_ids = sorted(lexical_scores.keys() - dense_score_by_id.keys())
        if lexical_only_ids:
            dense_score_by_id.update(
                self.database.dense_search(query_embedding, len(lexical_only_ids), chunk_ids=lexical_only_ids)
            )
        chunks = self.database.chunks_by_ids(sorted(dense_score_by_id))
        dense_rank = {
            chunk_id: rank
            for rank, chunk_id in enumerate(
                sorted(dense_score_by_id, key=lambda key: (-dense_score_by_id[key], key)), start=1
            )
        }
        lexical_rank = {
            chunk_id: rank
            for rank, chunk_id in enumerate(
                sorted(lexical_scores, key=lambda key: (lexical_scores[key], key)), start=1
            )
        }

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
        for citation_number, (score, chunk, _) in enumerate(scored[:limit], start=1):
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
                    document_id=chunk.document.document_id,
                )
            )
        return results


__all__ = ["HybridRetriever", "SearchResult"]
