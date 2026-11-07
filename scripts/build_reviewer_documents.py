"""Build small, public, synthetic reviewer PDFs. Never scrape or invent real university rules."""

import hashlib
import json
import shutil

import pymupdf
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from rag_aaup.academic.references import _font, _right, _wrapped
from rag_aaup.config import PROJECT_ROOT

OUT = PROJECT_ROOT / "output/pdf/reviewer_pack"
FIXTURES = PROJECT_ROOT / "reviewer_pack/documents"
QA = PROJECT_ROOT / "tmp/pdfs/reviewer_pack"
WIDTH, HEIGHT = A4


def header(canvas, subtitle):
    canvas.setFillColor(HexColor("#123256"))
    canvas.rect(0, HEIGHT - 105, WIDTH, 105, fill=1, stroke=0)
    _right(canvas, "CampusIQ | حزمة اختبار المقيّمين", WIDTH - 36, HEIGHT - 38, 19, white)
    _right(canvas, subtitle, WIDTH - 36, HEIGHT - 72, 13, white)
    _wrapped(
        canvas,
        "بيانات وهمية للاختبار فقط. لا تمثل AAUP أو أي جامعة حقيقية.",
        WIDTH - 36,
        HEIGHT - 135,
        WIDTH - 72,
        12,
    )
    _right(canvas, "نسخة الاختبار 1.0 | صفحة 1 من 1", WIDTH - 36, 34, 10)


def attendance():
    name = "01_Attendance_Policy_2026.pdf"
    path = OUT / name
    canvas = Canvas(str(path), pagesize=A4, invariant=1)
    canvas.setTitle("Synthetic reviewer fixture - Attendance")
    header(canvas, "جامعة التجربة | الحضور والغياب")
    sections = [
        ("الحد الأدنى للحضور", "الحد الأدنى لنسبة الحضور في المساق هو 75% من اللقاءات المقررة."),
        (
            "الحد المسموح للغياب",
            "الحد الأعلى المسموح للغياب هو 25% من اللقاءات. الوصول إلى 25% بالضبط لا يحقق شرط تجاوز الحد.",
        ),
        (
            "تجاوز الحد",
            "إذا تجاوز الغياب 25% يحرم الطالب من الامتحان النهائي وترصد له علامة صفر في المساق. تطبق هذه القاعدة على الغياب الذي لا يغطيه عذر مقبول.",
        ),
        (
            "مراجعة القرار",
            "يمكن تقديم طلب مراجعة قرار الحرمان خلال 3 أيام عمل من تاريخ التبليغ، إلى لجنة الأعذار عبر مدرس المساق. تقديم الطلب لا يعني قبوله تلقائياً.",
        ),
    ]
    y = HEIGHT - 190
    for title, text in sections:
        _right(canvas, title, WIDTH - 36, y, 15)
        y = _wrapped(canvas, text, WIDTH - 36, y - 26, WIDTH - 72, 12) - 28
    assert y > 70, "Attendance page overflow"
    canvas.save()
    # Same explicit logical-text convention as the application's Arabic reference export.
    # The catalogue PDF below deliberately has no such attachment and tests normal table extraction.
    with pymupdf.open(path) as document:
        document.embfile_add(
            "campusiq-reference.json",
            json.dumps(
                {
                    "format": "campusiq.curriculum.v1",
                    "pages": [
                        {
                            "section": "الحضور والغياب - بيانات وهمية",
                            "text": "جامعة التجربة - بيانات وهمية لا تمثل جامعة حقيقية.\n"
                            + "\n".join(f"{title}: {text}" for title, text in sections),
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        document.saveIncr()
    return path


def catalogue():
    path = OUT / "04_Course_Catalog_2026.pdf"
    canvas = Canvas(str(path), pagesize=A4, invariant=1)
    canvas.setTitle("Synthetic reviewer fixture - Course table")
    header(canvas, "جامعة التجربة | جدول المساقات")
    rows = [
        ["Code", "Course title", "Credits", "Prerequisite"],
        ["CS101", "Introduction to Programming", "3", "None"],
        ["CS102", "Advanced Programming", "4", "CS101"],
        ["CS201", "Data Structures", "3", "CS102"],
        ["NET201", "Computer Networks", "3", "CS102"],
        ["MATH101", "Calculus I", "3", "None"],
        ["LAB101", "Programming Lab", "1", "CS101"],
        ["PROJ401", "Graduation Project I", "2", "CS201"],
    ]
    widths = [75, 240, 60, 148]
    y = HEIGHT - 195
    for index, row in enumerate(rows):
        x = 36
        for value, width in zip(row, widths):
            canvas.setFillColor(
                HexColor("#123256") if index == 0 else HexColor("#edf3f9" if index % 2 else "#ffffff")
            )
            canvas.setStrokeColor(HexColor("#b8c7d9"))
            canvas.rect(x, y - 34, width, 34, fill=1, stroke=1)
            canvas.setFillColor(white if index == 0 else HexColor("#123256"))
            canvas.setFont(_font(), 10)
            canvas.drawString(x + 7, y - 22, value)
            x += width
        y -= 34
    _wrapped(
        canvas,
        "الأسماء الإنجليزية في الجدول تقابل: مقدمة البرمجة، البرمجة المتقدمة، هياكل البيانات، شبكات الحاسوب، التفاضل والتكامل 1، مختبر البرمجة، مشروع التخرج 1.",
        WIDTH - 36,
        y - 36,
        WIDTH - 72,
        12,
    )
    _wrapped(
        canvas,
        "الجدول للاختبار فقط. لا يثبت الفصل الذي تطرح فيه المادة أو السماح بالتسجيل المتزامن.",
        WIDTH - 36,
        y - 110,
        WIDTH - 72,
        11,
    )
    canvas.save()
    return path


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    for path in (attendance(), catalogue()):
        shutil.copyfile(path, FIXTURES / path.name)
        with pymupdf.open(path) as document:
            assert len(document) == 1
            document[0].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6)).save(QA / f"{path.stem}.png")
        print(f"Created public synthetic fixture: reviewer_pack/documents/{path.name}")
    manifest = {
        "schema": "campusiq.reviewer-fixtures.v1",
        "fixture_version": "1.0",
        "synthetic": True,
        "sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(FIXTURES.iterdir())
            if path.is_file()
        },
    }
    (PROJECT_ROOT / "reviewer_pack/manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
