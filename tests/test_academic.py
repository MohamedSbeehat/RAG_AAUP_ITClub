import json
from pathlib import Path

import pymupdf
from PIL import Image, PngImagePlugin

from rag_aaup.academic.domain import parse_curriculum
from rag_aaup.ingestion.loaders import load_document


def test_parser_keeps_zero_credits_and_excludes_free_course_blacklist():
    html = """<nav class="breadcrumb"><a>الرئيسية</a><a>بكالوريوس اختبار</a></nav>
    <h2>مجموع الساعات ( 128 ساعة معتمدة )</h2><h2>متطلبات الجامعة</h2>
    <p>على الطالب اجتياز جميع المساقات</p><table><tr><td class="course-id">000011110</td>
    <td>خدمة مجتمع</td><td class="d-none">Hidden description must not become a table column</td>
    <td>1</td><td>-</td><td>0</td><td>-</td></tr></table>
    <h2>مساقات حرة</h2><p>إجتياز ( 6 ) ساعات لا تشمل المساقات التالية</p>
    <table><tr><td class="course-id">123456789</td><td>مساق مستثنى</td><td>3</td><td>-</td><td>3</td><td>000011110</td></tr></table>"""
    plan = parse_curriculum(html, {"major_id": "test", "slug": "Test", "url": "https://www.aaup.edu/test"})
    assert plan.required_credits == 128
    assert plan.groups[0].courses[0].credits == "0"
    assert plan.groups[1].kind == "excluded"
    assert "ليست قائمة مساقات مطلوبة" in plan.groups[1].rule()


def test_embedded_reference_text_preserves_arabic_and_page_location(tmp_path: Path):
    path = tmp_path / "reference.pdf"
    text = "المساق: تركيب البيانات. رمز المساق: 240112031. الساعات المعتمدة: 3."
    with pymupdf.open() as document:
        document.new_page()
        document.embfile_add(
            "campusiq-reference.json",
            json.dumps(
                {
                    "format": "campusiq.curriculum.v1",
                    "pages": [{"page": 1, "section": "متطلبات التخصص", "text": text}],
                }
            ).encode(),
        )
        document.save(path)
    pages = load_document(path)
    assert pages[0].text == text
    assert pages[0].page == 1


def test_generated_plan_image_is_searchable_without_cloud_ocr(tmp_path: Path):
    path = tmp_path / "plan.png"
    text = "الخطة الدراسية، المساق 240112031، تركيب البيانات، 3 ساعات."
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt(
        "campusiq_reference",
        json.dumps(
            {"format": "campusiq.curriculum.v1", "page": 7, "section": "الخطة", "text": text},
            ensure_ascii=False,
        ),
    )
    Image.new("RGB", (200, 100), "white").save(path, pnginfo=metadata)
    page = load_document(path)[0]
    assert page.text == text and page.page == 7
