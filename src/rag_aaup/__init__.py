r"""RAG system for the AAUP IT Club RAG competition.

Pipeline (each stage lives in its own subpackage):

    ingestion  -> processing -> indexing -> retrieval -> generation
                                                     \-> evaluation (scores the whole chain)
"""

__version__ = "0.1.0"
