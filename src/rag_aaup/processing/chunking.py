import re
import unicodedata
from dataclasses import dataclass

from rag_aaup.ingestion.loaders import TextPage

_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_WHITESPACE = re.compile(r"\s+")
_TOPICS = {
    "القبول والتسجيل": (
        "admission",
        "registration",
        "قبول",
        "تسجيل",
        "التحاق",
        "طلب الالتحاق",
    ),
    "التجسير والتحويل": ("bridging", "transfer", "تجسير", "انتقال", "تحويل"),
    "الخطط الدراسية": ("study plan", "curriculum", "course plan", "خطة دراسية", "مساق", "متطلب"),
    "الرسوم والمنح": ("tuition", "fee", "scholarship", "رسوم", "منحة", "تقسيط"),
    "الامتحانات والتقييم": ("exam", "grade", "assessment", "امتحان", "علامة", "تقييم"),
    "الإنذارات والتخرج": ("warning", "graduation", "probation", "إنذار", "تخرج", "معدل تراكمي"),
}


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int | None
    section: str
    topic: str


def normalize_for_search(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _DIACRITICS.sub("", text).replace("\u0640", "")
    text = re.sub(r"[إأآٱ]", "ا", text)
    text = text.replace("ى", "ي")
    return _WHITESPACE.sub(" ", text).strip().lower()


def infer_topic(text: str, section: str) -> str:
    searchable = normalize_for_search(f"{section} {text}")
    for topic, keywords in _TOPICS.items():
        if any(normalize_for_search(keyword) in searchable for keyword in keywords):
            return topic
    return section or "غير مصنف"


def _split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    if max_chars < 1 or overlap < 0 or overlap >= max_chars:
        raise ValueError("max_chars must be positive and overlap must be in [0, max_chars)")

    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            cut = paragraph.rfind(" ", 0, max_chars + 1)
            cut = cut if cut > max_chars // 2 else max_chars
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[max(0, cut - overlap) :].strip()

        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def chunk_pages(
    pages: list[TextPage],
    max_chars: int = 1000,
    overlap: int = 140,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        for text in _split_text(page.text, max_chars, overlap):
            chunks.append(
                Chunk(
                    text=text,
                    page=page.page,
                    section=page.section,
                    topic=infer_topic(text, page.section),
                )
            )
    return chunks


__all__ = ["Chunk", "chunk_pages", "infer_topic", "normalize_for_search"]
