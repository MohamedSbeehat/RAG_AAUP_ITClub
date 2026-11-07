import uuid
from dataclasses import dataclass
from re import findall, search

from rag_aaup.config import Settings
from rag_aaup.generation.service import (
    ABSTAIN_AR,
    ABSTAIN_EN,
    GeneratedAnswer,
    GenerationError,
    ProviderUnavailableError,
    generate_answer,
)
from rag_aaup.indexing.database import Database
from rag_aaup.processing.chunking import normalize_for_search
from rag_aaup.retrieval.service import HybridRetriever, SearchResult


def contextual_query(query: str, history: list[tuple[str, str]]) -> str:
    """Resolve short follow-ups using recent user questions, without recycling generated claims."""
    normalized = normalize_for_search(query)
    follow_up = search(
        r"^(?:and\b|what about\b|how about\b|what if\b|also\b|it\b|that\b|"
        r"طيب\b|طب\b|ماذا عن\b|واذا\b|اذا\b|وهل\b|وكمان\b|وشو\b|"
        r"وقديش\b|وكم\b|ولو\b|وهسا\b|وهذا\b|وهذه\b)",
        normalized,
    )
    questions = [message for role, message in history if role == "user"][-3:]
    if follow_up and questions:
        # Put the current question first for embedding models with a bounded context window.
        prior = "\n".join(questions)[-3000:]
        return f"{query}\nسياق أسئلة المستخدم السابقة:\n{prior}"
    return query


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

    def answer(
        self,
        query: str,
        conversation_id: str | None = None,
        generation_config: Settings | None = None,
        document_id: int | None = None,
    ) -> AssistantResponse:
        conversation_id = conversation_id or str(uuid.uuid4())
        history = self.database.recent_messages(conversation_id)
        retrieval_query = contextual_query(query, history)
        sources = (
            self.retriever.search(retrieval_query, document_id=document_id)
            if document_id is not None
            else self.retriever.search(retrieval_query)
        )
        try:
            generated: GeneratedAnswer = generate_answer(
                generation_config or self.settings, query, sources, history
            )
        except ProviderUnavailableError as error:
            # Retrieval worked but no free LLM answered (quota, invalid key, no internet).
            # Show the retrieved excerpts with their sources instead of an empty error.
            # Not saved to history: it is a notice, not an answer to build follow-ups on.
            arabic = any("؀" <= char <= "ۿ" for char in query)
            note = (
                "\n\nهذه أقرب المقتطفات من المستندات بدون صياغة؛ افتح المصدر للتحقق."
                if arabic
                else "\n\nBelow are the closest excerpts from the documents, unedited; open each source to verify."
            )
            return AssistantResponse(conversation_id, str(error) + note, "sources_fallback", sources)
        if generated.provider != "abstain":
            used = set(findall(r"\[(S\d+)\]", generated.answer))
            allowed = {source.citation for source in sources}
            if not used:
                arabic = any("\u0600" <= char <= "\u06ff" for char in query)
                generated = GeneratedAnswer(ABSTAIN_AR if arabic else ABSTAIN_EN, "abstain")
                sources = []
            elif used - allowed:
                raise GenerationError(
                    "تعذر التحقق من إحالات الإجابة إلى المستندات. حاول مجدداً أو استخدم البحث في المصادر."
                )
            else:
                sources = [source for source in sources if source.citation in used]
        self.database.save_exchange(conversation_id, query, generated.answer)
        return AssistantResponse(conversation_id, generated.answer, generated.provider, sources)
