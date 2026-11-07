import json
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

import pymupdf
from docx import Document
from docx.document import Document as DocumentType
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from PIL import Image

from rag_aaup.config import get_settings


@dataclass(frozen=True)
class TextPage:
    text: str
    page: int | None = None
    section: str = ""


class UnsupportedDocumentError(ValueError):
    pass


def _docx_blocks(document: DocumentType):
    body = document.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def load_pdf(path: Path) -> list[TextPage]:
    pages: list[TextPage] = []
    with pymupdf.open(path) as document:
        if "campusiq-reference.json" in document.embfile_names():
            payload = json.loads(document.embfile_get("campusiq-reference.json").decode("utf-8"))
            records = payload.get("pages", [])
            if payload.get("format") != "campusiq.curriculum.v1" or len(records) != len(document):
                raise ValueError("Invalid curriculum reference payload")
            return [
                TextPage(record["text"], page=index, section=record["section"])
                for index, record in enumerate(records, 1)
            ]
        for page_number, page in enumerate(document, start=1):
            text = page.get_text("text", sort=True).strip()
            if not text or text.count("\ufffd") > len(text) // 4:
                settings = get_settings()
                if not settings.ocr_tessdata_path.exists():
                    raise ValueError("Scanned PDF needs OCR models; run rag-aaup setup-ocr")
                textpage = page.get_textpage_ocr(
                    language=settings.ocr_language, tessdata=str(settings.ocr_tessdata_path), full=True
                )
                text = page.get_text(textpage=textpage).strip()
            table_rows = [
                " | ".join(cell or "" for cell in row).strip()
                for table in page.find_tables().tables
                for row in table.extract()
            ]
            table_text = "\n".join(row for row in table_rows if row.strip())
            if table_text:
                text = f"{text}\n\n{table_text}".strip()
            if text:
                pages.append(TextPage(text=text, page=page_number))
    return pages


def load_image(path: Path) -> list[TextPage]:
    with Image.open(path) as image:
        if image.width * image.height > 30_000_000:
            raise ValueError("Image exceeds the OCR pixel limit")
        embedded = image.info.get("campusiq_reference")
        if embedded:
            record = json.loads(embedded)
            if record.get("format") == "campusiq.curriculum.v1":
                return [TextPage(record["text"], page=int(record["page"]), section=record["section"])]
    settings = get_settings()
    if not settings.ocr_tessdata_path.exists():
        raise ValueError("Image requires OCR models; run rag-aaup setup-ocr")
    pixmap = pymupdf.Pixmap(str(path))
    content = pixmap.pdfocr_tobytes(language=settings.ocr_language, tessdata=str(settings.ocr_tessdata_path))
    with pymupdf.open(stream=content, filetype="pdf") as document:
        text = document[0].get_text().strip()
    if not text:
        raise ValueError("OCR could not read this image")
    return [TextPage(text, page=1, section="استخراج OCR - يحتاج مراجعة الأرقام والرموز من الصورة")]


def load_docx(path: Path) -> list[TextPage]:
    document = Document(path)
    sections: list[TextPage] = []
    active_heading = ""
    active_lines: list[str] = []

    def flush() -> None:
        if active_lines:
            sections.append(TextPage("\n".join(active_lines), section=active_heading))
            active_lines.clear()

    for block in _docx_blocks(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            if block.style and block.style.name.lower().startswith("heading"):
                flush()
                active_heading = text
            else:
                active_lines.append(text)
        else:
            for row in block.rows:
                cells = [cell.text.strip().replace("\n", " / ") for cell in row.cells]
                if any(cells):
                    active_lines.append(" | ".join(cells))
    flush()
    return sections


def load_xlsx(path: Path) -> list[TextPage]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    pages: list[TextPage] = []
    try:
        for sheet in workbook.worksheets:
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if values:
                    pages.append(TextPage(" | ".join(values), section=f"{sheet.title}, row {row_number}"))
    finally:
        workbook.close()
    return pages


def load_text(path: Path) -> list[TextPage]:
    return [TextPage(path.read_text(encoding="utf-8-sig").strip())]


def load_markdown(path: Path) -> list[TextPage]:
    sections: list[TextPage] = []
    active_heading = ""
    active_lines: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            if active_lines:
                sections.append(TextPage("\n".join(active_lines).strip(), section=active_heading))
            active_heading = heading.group(1).strip()
            active_lines = []
        else:
            active_lines.append(line)
    if active_lines:
        sections.append(TextPage("\n".join(active_lines).strip(), section=active_heading))
    return [section for section in sections if section.text]


def load_document(path: Path) -> list[TextPage]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return load_pdf(path)
    if suffix == ".docx":
        return load_docx(path)
    if suffix == ".xlsx":
        return load_xlsx(path)
    if suffix == ".md":
        return load_markdown(path)
    if suffix == ".txt":
        return load_text(path)
    if suffix in {".png", ".jpg", ".jpeg"}:
        return load_image(path)
    raise UnsupportedDocumentError(f"Unsupported document type: {suffix or '(no extension)'}")


__all__ = [
    "BadZipFile",
    "TextPage",
    "UnsupportedDocumentError",
    "load_document",
    "load_markdown",
]
