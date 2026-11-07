import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from rag_aaup.config import Settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.embeddings import encode_documents
from rag_aaup.ingestion.loaders import load_document
from rag_aaup.processing.chunking import chunk_pages

logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".txt", ".md"}


@dataclass
class IndexReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class IndexingService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def sync(self) -> IndexReport:
        self.settings.raw_dir.mkdir(parents=True, exist_ok=True)
        report = IndexReport()
        paths = sorted(
            path
            for path in self.settings.raw_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        for path in paths:
            relative_path = path.relative_to(self.settings.raw_dir).as_posix()
            try:
                self._index_file(path, relative_path, report)
            except (OSError, ValueError, RuntimeError) as error:
                message = f"{type(error).__name__}: {error}"
                report.failed[relative_path] = message
                logger.exception("Could not index document %s", relative_path)
        return report

    def _index_file(self, path: Path, relative_path: str, report: IndexReport) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if self.database.has_unchanged_document(relative_path, digest):
            report.unchanged.append(relative_path)
            return

        pages = load_document(path)
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("No readable text found; scanned PDFs require OCR before indexing")
        embeddings = encode_documents([chunk.text for chunk in chunks])
        stat = path.stat()
        family_key, version_sort = document_version(path.stem, stat.st_mtime_ns)
        existed = any(document.relative_path == relative_path for document in self.database.documents())
        self.database.replace_document(
            relative_path=relative_path,
            family_key=family_key,
            title=path.stem,
            sha256=digest,
            modified_ns=stat.st_mtime_ns,
            version_sort=version_sort,
            chunks=chunks,
            embeddings=embeddings,
        )
        (report.updated if existed else report.added).append(relative_path)
        logger.info("Indexed %s (%d chunks)", relative_path, len(chunks))


def document_version(stem: str, modified_ns: int) -> tuple[str, int]:
    normalized = re.sub(r"[^a-zA-Z0-9\u0600-\u06FF]+", "-", stem.casefold()).strip("-")
    years = [int(year) for year in re.findall(r"20\d{2}", stem)]
    tagged_versions = [
        int(number) for number in re.findall(r"(?i)(?:v(?:ersion)?|rev(?:ision)?)[-_ ]*(\d+)", stem)
    ]
    version = max(years or tagged_versions, default=modified_ns // 10**9)

    family = re.sub(
        r"(?i)(?:[-_ ]+(?:v(?:ersion)?|rev(?:ision)?)[-_ ]*\d+|[-_ ]+20\d{2}(?:[-_ ]+20?\d{2})?)$",
        "",
        stem,
    )
    family_key = re.sub(r"[^a-zA-Z0-9\u0600-\u06FF]+", "-", family.casefold()).strip("-")
    return family_key or normalized, version


__all__ = ["IndexReport", "IndexingService", "document_version"]
