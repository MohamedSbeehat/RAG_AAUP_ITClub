import hashlib
import json
import re
import runpy
from pathlib import Path

import pytest

from rag_aaup.assistant import contextual_query
from rag_aaup.ingestion.loaders import load_document

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "reviewer_pack"


@pytest.mark.parametrize(
    "follow_up",
    [
        "واذا صار معدلي ٣٫٧؟",
        "وإذا صار معدلي 3.7؟",
        "وشو متطلبها السابق؟",
        "وكم ساعة إلها؟",
        "طيب ولو صرت سنة رابعة؟",
        "What if my GPA is higher?",
    ],
)
def test_palestinian_followups_retain_original_context_across_short_turns(follow_up):
    history = [
        ("user", "كم ساعة للبرمجة المتقدمة CS102؟"),
        ("assistant", "Invented assistant-only fact"),
        ("user", "وشو متطلبها؟"),
    ]
    query = contextual_query(follow_up, history)
    assert "CS102" in query
    assert follow_up in query
    assert "Invented assistant-only fact" not in query


def test_new_explicit_topic_does_not_inherit_an_unrelated_question():
    query = contextual_query("ما الحد الأدنى للحضور؟", [("user", "كم ساعة للبرمجة CS102؟")])
    assert "CS102" not in query


def test_public_fixture_integrity_and_case_coverage():
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((PACK / "documents" / name).read_bytes()).hexdigest() == digest
    suite = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    cases = suite["cases"]
    assert len(cases) == len({case["id"] for case in cases}) == 42
    assert {case["category"] for case in cases} == set(suite["categories"])
    assert sum(bool(case.get("dialect")) for case in cases) >= 25
    assert all(set(case.get("required_sources", [])) <= set(manifest["sha256"]) for case in cases)


def test_reviewer_pdfs_keep_arabic_rule_and_extract_the_real_table_row():
    attendance = load_document(PACK / "documents/01_Attendance_Policy_2026.pdf")
    assert attendance[0].page == 1 and "75%" in attendance[0].text and "3 أيام عمل" in attendance[0].text
    catalogue = load_document(PACK / "documents/04_Course_Catalog_2026.pdf")
    assert catalogue[0].page == 1
    assert re.search(r"CS102\s*\|\s*Advanced\s*Programming\s*\|\s*4\s*\|\s*CS101", catalogue[0].text)


def test_selecting_an_ambiguous_followup_also_runs_its_original_course_question():
    select = runpy.run_path(str(ROOT / "scripts/evaluate_reviewer_pack.py"))["select_cases"]
    cases = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))["cases"]
    assert [case["id"] for case in select(cases, case_ids="F06")] == ["T01", "F05", "F06"]
    with pytest.raises(ValueError, match="Unknown case"):
        select(cases, case_ids="TYPO")


def test_smoke_profile_covers_all_categories_with_at_most_sixteen_llm_calls():
    select = runpy.run_path(str(ROOT / "scripts/evaluate_reviewer_pack.py"))["select_cases"]
    suite = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    selected = select(suite["cases"], profile="smoke")
    assert len(selected) == 16
    assert {case["category"] for case in selected} == set(suite["categories"])
