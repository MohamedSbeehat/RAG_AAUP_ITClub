import hashlib
import json
import re
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Course:
    code: str
    name: str
    credits: str
    lecture: str = ""
    practical: str = ""
    prerequisites: str = ""


@dataclass(frozen=True)
class CourseGroup:
    title: str
    kind: str
    selection_credits: int | None
    courses: list[Course]

    def rule(self) -> str:
        if self.kind == "excluded":
            return (
                f"متطلب المساقات الحرة: {self.selection_credits} ساعات. "
                "المساقات أدناه مستثناة من الاختيار الحر وليست قائمة مساقات مطلوبة."
            )
        if self.kind == "elective":
            return f"المطلوب اختيار {self.selection_credits} ساعات معتمدة من مساقات هذه المجموعة."
        if self.kind == "advisory":
            return "توزيع إسترشادي من المصدر؛ رقم الجدول هنا توثيقي ولا يثبت موعد طرح المساق أو فصل تسجيله."
        return "المساقات الواردة في هذه المجموعة مطلوبة وفق جدول المصدر."


@dataclass(frozen=True)
class StudyPlan:
    major_id: str
    slug: str
    title: str
    source_url: str
    required_credits: int
    groups: list[CourseGroup]

    def semantic_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_curriculum(html: bytes | str, source: dict) -> StudyPlan:
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8" if isinstance(html, bytes) else None)
    breadcrumb = soup.select(".breadcrumb a")
    if not breadcrumb:
        raise ValueError("Official program breadcrumb not found; refusing to guess the program name")
    title = clean(breadcrumb[-1].get_text(" ", strip=True))
    if "بكالوريوس" not in title:
        raise ValueError("The source does not identify a bachelor program")
    heading = next((h for h in soup.find_all("h2") if "مجموع الساعات" in h.get_text()), None)
    total = re.search(r"\(\s*(\d+)\s+ساعة", heading.get_text() if heading else "")
    if total is None:
        raise ValueError("Official total credit requirement not found")
    groups: list[CourseGroup] = []
    advisory_number = 0
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        group_heading = table.find_previous(["h2", "h3", "h4"])
        group_title = clean(group_heading.get_text(" ", strip=True)) if group_heading else ""
        preceding = table.find_previous(["p", "h2", "h3", "h4"])
        raw_rule = clean(preceding.get_text(" ", strip=True)) if preceding else ""
        choice = re.search(r"\(\s*(\d+)\s*\)", raw_rule)
        selection = int(choice.group(1)) if choice else None
        core_rows = table.select("td.course-id")
        if core_rows:
            kind = "excluded" if "لا تشمل" in raw_rule else "elective" if selection is not None else "required"
            courses: list[Course] = []
            for cell in core_rows:
                row = cell.parent
                for hidden in row.select(".d-none"):
                    hidden.decompose()
                cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all("td", recursive=False)]
                if len(cells) != 6 or not re.fullmatch(r"\d{8,12}", cells[0]) or not cells[1]:
                    raise ValueError("Curriculum table structure changed; manual source review required")
                courses.append(Course(cells[0], cells[1], cells[4], cells[2], cells[3], cells[5]))
            if kind == "excluded":
                group_title += " - المساقات المستثناة"
            groups.append(CourseGroup(group_title, kind, selection, courses))
        else:
            # Keep advisory tables without inferring academic-year/semester labels from DOM order.
            courses = []
            for row in table.find_all("tr"):
                cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all("td", recursive=False)]
                if len(cells) == 3 and (re.fullmatch(r"\d{8,12}", cells[0]) or cells[0] == "-"):
                    courses.append(Course(cells[0], cells[1], cells[2], prerequisites="لم يرد في هذا الجدول"))
            if courses:
                advisory_number += 1
                groups.append(CourseGroup(f"التوزيع الإسترشادي - الجدول {advisory_number}", "advisory", None, courses))
    if not groups or not any(group.kind == "required" for group in groups):
        raise ValueError("No usable official curriculum tables found")
    return StudyPlan(source["major_id"], source["slug"], title, source["url"], int(total.group(1)), groups)
