"""Deterministic factual tables and prerequisite diagrams; never generate academic facts with an LLM."""

import json
import re
from dataclasses import asdict
from pathlib import Path

import arabic_reshaper
import pymupdf
from bidi.algorithm import get_display
from PIL import Image, PngImagePlugin
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from rag_aaup.academic.domain import CourseGroup, StudyPlan

REFERENCE_ATTACHMENT = "campusiq-reference.json"
W, H = landscape(A4)
M = 36
BLUE = HexColor("#153451")
TEAL = HexColor("#117d83")


def _font() -> str:
    if "ReferenceArabic" not in pdfmetrics.getRegisteredFontNames():
        paths = [Path("C:/Windows/Fonts/arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
        path = next((path for path in paths if path.exists()), None)
        if path is None:
            raise ValueError("An Arabic-capable Arial or DejaVu Sans TTF is required")
        pdfmetrics.registerFont(TTFont("ReferenceArabic", str(path)))
    return "ReferenceArabic"


def _visual(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def _lines(text: str, width: float, size: float = 11) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if pdfmetrics.stringWidth(_visual(word), _font(), size) > width:
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if current and pdfmetrics.stringWidth(_visual(current + char), _font(), size) > width:
                    lines.append(current)
                    current = ""
                current += char
            continue
        candidate = f"{current} {word}".strip()
        if current and pdfmetrics.stringWidth(_visual(candidate), _font(), size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + ([current] if current else [""])


def _right(canvas: Canvas, text: str, x: float, y: float, size: float = 11, color=BLUE):
    canvas.setFillColor(color)
    canvas.setFont(_font(), size)
    canvas.drawRightString(x, y, _visual(text))


def _wrapped(canvas: Canvas, text: str, x: float, y: float, width: float, size: float = 11):
    for line in _lines(text, width, size):
        _right(canvas, line, x, y, size)
        y -= size * 1.5
    return y


def _course_text(plan: StudyPlan, group: CourseGroup, course) -> str:
    prefix = "مستثنى من المساقات الحرة، وليس مساقاً مطلوباً" if group.kind == "excluded" else group.rule()
    return (
        f"البرنامج: {plan.title}. المجموعة: {group.title}. {prefix}\n"
        f"رقم المساق: {course.code}. اسم المساق: {course.name}. "
        f"الساعات المعتمدة: {course.credits or 'غير مذكور'}. "
        f"الساعات النظرية: {course.lecture or 'غير مذكور'}. العملية: {course.practical or 'غير مذكور'}. "
        f"المتطلبات السابقة كما وردت بالمصدر: {course.prerequisites or 'غير مذكور'}."
    )


def build_reference(plan: StudyPlan, output: Path, snapshot_at: str) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    font = _font()
    pages: list[dict] = [
        {
            "kind": "cover",
            "title": "بيانات المصدر",
            "text": (
                f"البرنامج: {plan.title}\nإجمالي الساعات اللازمة للدرجة حسب المصدر: {plan.required_credits} ساعة معتمدة.\n"
                f"المصدر الرسمي: {plan.source_url}\nتاريخ إعداد النسخة المرجعية: {snapshot_at}.\n"
                "نسخة مرجعية أعدها CampusIQ من جداول الموقع، وليست لائحة أو وثيقة أصدرتها الجامعة. "
                "حالة القبول وسنة نفاذ الخطة والدفعة غير مثبتة بهذه الصفحة. لا تستنتج الرسوم أو مواعيد الامتحانات منها."
            ),
        }
    ]
    widths = [92, 246, 62, 56, 56, W - 2 * M - 512]
    for group in plan.groups:
        batch, used = [], 0
        for course in group.courses:
            values = [
                course.code,
                course.name,
                course.credits or "غير مذكور",
                course.lecture or "-",
                course.practical or "-",
                course.prerequisites or "غير مذكور",
            ]
            row_height = max(
                30, max(len(_lines(value, width - 14)) for value, width in zip(values, widths)) * 17 + 12
            )
            if used + row_height > 350 and batch:
                pages.append({"kind": "table", "title": group.title, "group": group, "rows": batch})
                batch, used = [], 0
            batch.append((course, values, row_height))
            used += row_height
        if batch:
            pages.append({"kind": "table", "title": group.title, "group": group, "rows": batch})
    # Every non-advisory prerequisite edge is represented. Unresolved relationships remain literal text.
    course_map = {
        course.code: course for group in plan.groups if group.kind != "advisory" for course in group.courses
    }
    edges = []
    for group in plan.groups:
        if group.kind in {"excluded", "advisory"}:
            continue
        for course in group.courses:
            if re.search(r"\d{8,12}", course.prerequisites):
                edges.append(course)
    for start in range(0, len(edges), 5):
        pages.append({"kind": "tree", "title": "خريطة المتطلبات السابقة", "edges": edges[start : start + 5]})
    temporary = output.with_suffix(".building.pdf")
    canvas = Canvas(str(temporary), pagesize=(W, H), invariant=1)
    canvas.setTitle(plan.title + " - CampusIQ reference")
    canvas.setAuthor("CampusIQ - independent reference compilation")
    logical_pages = []
    for number, page in enumerate(pages, 1):
        canvas.setFillColor(BLUE)
        canvas.rect(0, H - 76, W, 76, fill=1, stroke=0)
        _right(canvas, "CampusIQ | مرجع الخطط الدراسية", W - M, H - 27, 17, white)
        _right(canvas, plan.title, W - M, H - 52, 11, white)
        canvas.setFillColor(TEAL)
        canvas.setFont(font, 9)
        canvas.drawString(M, H - 27, "LOCAL REFERENCE / AAUP")
        _right(canvas, page["title"], W - M, H - 104, 16)
        text = f"{plan.title}\nالمصدر الرسمي: {plan.source_url}\nالنسخة المرجعية: {snapshot_at}\n"
        if page["kind"] == "cover":
            canvas.setFillColor(HexColor("#edf5f7"))
            canvas.roundRect(M, H - 220, W - 2 * M, 85, 12, fill=1, stroke=0)
            _right(canvas, f"متطلب الدرجة: {plan.required_credits} ساعة معتمدة", W - M - 20, H - 171, 23)
            _right(canvas, "منقول من إجمالي الخطة، وليس مجموع جميع بدائل الاختيار", W - M - 20, H - 199, 11)
            y = _wrapped(canvas, page["text"], W - M, H - 250, W - 2 * M, 12)
            _wrapped(
                canvas,
                "الجداول التالية تحفظ رموز المساقات وأسماءها وساعاتها ومتطلباتها. القوائم المستثناة مميزة بوضوح. "
                "الخريطة الشجرية تنظيم آلي لعلاقات المصدر؛ لا تثبت موعد طرح المساق أو السماح بالتسجيل المتزامن.",
                W - M,
                y - 20,
                W - 2 * M,
                11,
            )
            text += page["text"]
        elif page["kind"] == "table":
            group = page["group"]
            _wrapped(canvas, group.rule(), W - M, H - 128, W - 2 * M, 10)
            y = H - 177
            headers = ["رمز المساق", "اسم المساق", "معتمدة", "نظري", "عملي", "متطلبات سابقة"]
            x = W - M
            for label, width in zip(headers, widths):
                canvas.setFillColor(TEAL if group.kind != "excluded" else HexColor("#98521c"))
                canvas.rect(x - width, y, width, 30, fill=1, stroke=0)
                _right(canvas, label, x - 7, y + 10, 11, white)
                x -= width
            text += group.rule() + "\n"
            for index, (course, values, height) in enumerate(page["rows"]):
                y -= height
                canvas.setFillColor(HexColor("#f0f5fa") if index % 2 == 0 else white)
                canvas.rect(M, y, W - 2 * M, height, fill=1, stroke=0)
                x = W - M
                for value, width in zip(values, widths):
                    _wrapped(canvas, value, x - 7, y + height - 17, width - 14, 11)
                    x -= width
                canvas.setStrokeColor(HexColor("#dce6ef"))
                canvas.line(M, y, W - M, y)
                text += _course_text(plan, group, course) + "\n\n"
        else:
            _right(
                canvas,
                "كل سهم يعني علاقة متطلب سابقة مذكورة؛ لا يُفسر وحده هل الشروط بدائل أو مجتمعة.",
                W - M,
                H - 127,
                10,
            )
            text += "خريطة مشتقة من المتطلبات السابقة المعلنة، وليست مخططاً رسمياً صادراً عن الجامعة.\n"
            for index, course in enumerate(page["edges"]):
                y = H - 195 - index * 72
                for x, width in ((M, 340), (W - M - 300, 300)):
                    canvas.setFillColor(HexColor("#edf5f7"))
                    canvas.roundRect(x, y - 22, width, 52, 8, fill=1, stroke=0)
                _right(canvas, f"{course.code} | {course.name}", W - M - 10, y + 9, 10)
                _right(canvas, f"{course.credits} ساعات معتمدة", W - M - 10, y - 9, 10)
                codes = re.findall(r"\d{8,12}", course.prerequisites)
                labels = [
                    f"{code}: {course_map[code].name}" if code in course_map else code for code in codes
                ]
                _wrapped(canvas, " / ".join(labels), M + 330, y + 12, 320, 9)
                canvas.setStrokeColor(TEAL)
                canvas.line(M + 345, y + 4, W - M - 306, y + 4)
                canvas.line(M + 345, y + 4, M + 355, y + 10)
                canvas.line(M + 345, y + 4, M + 355, y - 2)
                text += f"المساق {course.code} {course.name}. المتطلبات السابقة كما وردت: {course.prerequisites}.\n"
        _right(
            canvas,
            "مرجع مشتق مستقل - المصدر الرسمي متاح عبر الرابط - أرقام الصفحات تخص هذا المرجع",
            W - M,
            30,
            8,
        )
        canvas.linkURL(plan.source_url, (M, 15, W - M, 42), relative=0)
        canvas.setFillColor(BLUE)
        canvas.setFont(font, 8)
        canvas.drawString(M, 30, f"{number} / {len(pages)}")
        logical_pages.append({"page": number, "section": page["title"], "text": text})
        canvas.showPage()
    canvas.save()
    with pymupdf.open(temporary) as document:
        payload = {
            "format": "campusiq.curriculum.v1",
            "plan": asdict(plan),
            "snapshot_at": snapshot_at,
            "pages": logical_pages,
        }
        document.embfile_add(
            REFERENCE_ATTACHMENT,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            filename=REFERENCE_ATTACHMENT,
        )
        ready = output.with_suffix(".ready.pdf")
        document.save(ready, garbage=4, deflate=True, no_new_id=True)
        ready.replace(output)
    temporary.unlink()
    return payload


def export_images(pdf: Path, payload: dict, directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf) as document:
        for record in payload["pages"]:
            path = directory / f"page-{record['page']:02}.png"
            pixmap = document[record["page"] - 1].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            metadata = PngImagePlugin.PngInfo()
            metadata.add_itxt(
                "campusiq_reference", json.dumps({"format": payload["format"], **record}, ensure_ascii=False)
            )
            image.save(path, pnginfo=metadata)
            paths.append(path)
    return paths
