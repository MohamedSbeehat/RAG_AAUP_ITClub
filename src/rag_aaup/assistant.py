import uuid
from dataclasses import dataclass
from re import search

from rag_aaup.config import Settings
from rag_aaup.generation.service import GeneratedAnswer, generate_answer
from rag_aaup.indexing.database import Database
from rag_aaup.retrieval.service import HybridRetriever, SearchResult


@dataclass(frozen=True)
class AssistantResponse:
    conversation_id: str
    answer: str
    provider: str
    sources: list[SearchResult]


class AcademicAssistant:
    def __init__(self, settings: Settings, database: Database, retriever: HybridRetriever):
        self.settings = settings
        self.database = database
        self.retriever = retriever

    def answer(self, query: str, conversation_id: str | None = None) -> AssistantResponse:
        conversation_id = conversation_id or str(uuid.uuid4())
        history = self.database.recent_messages(conversation_id)
        retrieval_query = query
        is_follow_up = search(
            r"(?i)^(?:and\b|what about\b|how about\b|what if\b|also\b|it\b|that\b|"
            r"طيب\b|ماذا عن\b|وإذا\b|وهل\b|وكمان\b|وهذا\b|وهذه\b)",
            query.strip(),
        )
        if is_follow_up:
            previous_question = next(
                (message for role, message in reversed(history) if role == "user"),
                None,
            )
            if previous_question:
                retrieval_query = f"{previous_question}\n{query}"
        sources = self.retriever.search(retrieval_query)
        generated: GeneratedAnswer = generate_answer(self.settings, query, sources, history)
        self.database.save_exchange(conversation_id, query, generated.answer)
        return AssistantResponse(conversation_id, generated.answer, generated.provider, sources)
