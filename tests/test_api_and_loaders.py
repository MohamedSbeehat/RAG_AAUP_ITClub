from pathlib import Path

import pymupdf
from docx import Document
from fastapi.testclient import TestClient

from rag_aaup import api
from rag_aaup.assistant import AcademicAssistant
from rag_aaup.config import Settings
from rag_aaup.generation.service import GeneratedAnswer
from rag_aaup.indexing.database import Database
from rag_aaup.ingestion.loaders import load_docx, load_pdf


def test_pdf_and_docx_loaders_keep_source_locations(tmp_path: Path) -> None:
    pdf_path = tmp_path / "guide.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Admission requirements")
    xs, ys = [50, 250, 450], [100, 140, 180]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for row_index, y in enumerate([120, 160]):
        values = ["Course", "Credits"] if row_index == 0 else ["CS101", "3"]
        for column_index, x in enumerate(xs[:-1]):
            page.insert_text((x + 5, y), values[column_index])
    pdf.save(pdf_path)
    pdf.close()

    docx_path = tmp_path / "guide.docx"
    document = Document()
    document.add_heading("Registration", level=1)
    document.add_paragraph("Submit the form through the portal.")
    document.save(docx_path)

    pdf_pages = load_pdf(pdf_path)
    docx_sections = load_docx(docx_path)
    assert pdf_pages[0].page == 1
    assert "Admission requirements" in pdf_pages[0].text
    assert all(term in pdf_pages[0].text for term in ("CS101", "3", "Course", "Credits"))
    assert any(section.section == "Registration" for section in docx_sections)


def test_api_starts_and_abstains_without_indexed_sources(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        raw_dir=tmp_path / "raw",
        database_path=tmp_path / "indexes" / "test.sqlite3",
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)

    with TestClient(api.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["indexed_documents"] == 0

        response = client.post("/api/ask", json={"question": "When is the application deadline?"})
        assert response.status_code == 200
        assert response.json()["provider"] == "abstain"
        assert response.json()["sources"] == []

        unsupported = client.post(
            "/api/documents",
            files=[("files", ("notes.exe", b"not a document", "application/octet-stream"))],
        )
        assert unsupported.status_code == 415


def test_follow_up_question_reuses_previous_question(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        raw_dir=tmp_path / "raw",
        database_path=tmp_path / "indexes" / "test.sqlite3",
    )
    database = Database(settings.database_path)
    conversation_id = "follow-up-test"
    database.save_exchange(conversation_id, "What documents are needed for admission?", "A transcript.")

    class Retriever:
        query = ""

        def search(self, query: str):
            self.query = query
            return []

    retriever = Retriever()
    monkeypatch.setattr(
        "rag_aaup.assistant.generate_answer",
        lambda *_: GeneratedAnswer("Not found.", "abstain"),
    )
    assistant = AcademicAssistant(settings, database, retriever)
    assistant.answer("What about the deadline?", conversation_id)

    assert "What documents are needed for admission?" in retriever.query
    assert "What about the deadline?" in retriever.query
