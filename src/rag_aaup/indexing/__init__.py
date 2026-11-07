"""Indexing: build the search indexes from the chunks.

- Dense index: embeddings + vector store (e.g. FAISS)
- Sparse index: BM25 over normalized / stemmed Arabic text

Indexes are saved to indexes/ (rebuildable, not committed to git).
"""
