"""Retrieval: find the best chunks for a question.

- Hybrid search (dense + BM25) merged with Reciprocal Rank Fusion
- Optional query rewriting / expansion
- Reranking the top candidates with a cross-encoder
"""
