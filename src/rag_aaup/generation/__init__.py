"""Generation: write the final answer from the retrieved chunks.

- LLM clients for free providers only (see docs/free-apis.md), with fallback and retry
- Prompt templates (Arabic / English) that force grounded answers with citations
- Abstain ("not found in the documents") when the context does not contain the answer
"""
