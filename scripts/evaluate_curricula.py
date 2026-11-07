"""Reproducible local retrieval checks against factual curriculum tables, without an LLM."""

import argparse
import json
import re
import shutil

import pymupdf

from rag_aaup.academic.sync import AcademicReferences
from rag_aaup.config import PROJECT_ROOT, Settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import IndexingService
from rag_aaup.retrieval.service import HybridRetriever


def write_cases():
    cases = []
    for record in AcademicReferences().state()["programs"].values():
        path = PROJECT_ROOT / record["file"]
        with pymupdf.open(path) as document:
            payload = json.loads(document.embfile_get("campusiq-reference.json"))
        plan = payload["plan"]
        base = {
            "expected_source": path.name,
            "source_content_hash": record["content_hash"],
            "source_url": record["source_url"],
        }

        def add(question, terms, base=base):
            cases.append(
                {**base, "id": f"curriculum-{len(cases) + 1:02}", "question": question, "must_contain": terms}
            )

        for question in (
            f"كم إجمالي الساعات المطلوبة لإنهاء {plan['title']}؟",
            f"Total credit hours for {plan['title']}?",
        ):
            add(question, [f"إجمالي الساعات اللازمة للدرجة حسب المصدر: {plan['required_credits']}"])
        core = [
            course
            for group in plan["groups"]
            if group["kind"] == "required" and "التخصص" in group["title"]
            for course in group["courses"]
        ]
        for course in core[:4]:
            add(
                f"كم ساعة معتمدة للمساق {course['code']} {course['name']}؟",
                [f"رقم المساق: {course['code']}.", f"الساعات المعتمدة: {course['credits']}."],
            )
        prerequisites = [course for course in core if re.search(r"\d{8,12}", course["prerequisites"])]
        for course in prerequisites[:5]:
            add(
                f"شو المتطلبات السابقة للمساق {course['code']} {course['name']}؟",
                [
                    f"رقم المساق: {course['code']}.",
                    f"المتطلبات السابقة كما وردت بالمصدر: {course['prerequisites']}.",
                ],
            )
        for group in plan["groups"]:
            if group["kind"] == "elective":
                add(
                    f"كم ساعة اختياري أختار من {group['title']} في {plan['title']}؟",
                    [f"المطلوب اختيار {group['selection_credits']} ساعات"],
                )
            if group["kind"] == "excluded":
                for course in group["courses"][:2]:
                    add(
                        f"هل المساق {course['code']} محسوب من المساقات الحرة؟",
                        ["مستثنى من المساقات الحرة", f"رقم المساق: {course['code']}."],
                    )
        for course in core[4:7]:
            add(
                f"How many credit hours does course {course['code']} have?",
                [f"رقم المساق: {course['code']}.", f"الساعات المعتمدة: {course['credits']}."],
            )
    assert len(cases) == 50, f"Source structure changed; review test generation ({len(cases)} cases)"
    path = PROJECT_ROOT / "data/eval/curriculum_questions.jsonl"
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-cases", action="store_true")
    args = parser.parse_args()
    if args.write_cases:
        write_cases()
    cases = [
        json.loads(line)
        for line in (PROJECT_ROOT / "data/eval/curriculum_questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    current_hashes = {
        record["file"].replace("\\", "/").rsplit("/", 1)[-1]: record["content_hash"]
        for record in AcademicReferences().state()["programs"].values()
    }
    if any(case["source_content_hash"] != current_hashes.get(case["expected_source"]) for case in cases):
        raise RuntimeError(
            "Official curriculum snapshot changed; review the sources before regenerating test cases with --write-cases"
        )
    directory = PROJECT_ROOT / ".cache/curriculum-evaluation/input"
    directory.mkdir(parents=True, exist_ok=True)
    for record in AcademicReferences().state()["programs"].values():
        source = PROJECT_ROOT / record["file"]
        shutil.copyfile(source, directory / source.name)
    settings = Settings(
        _env_file=None,
        raw_dir=directory,
        database_path=directory.parent / "evaluation.sqlite3",
        multi_tenant_enabled=False,
    )
    database = Database.from_settings(settings)
    try:
        report = IndexingService(settings, database).sync()
        assert not report.failed, report.failed
        by_name = {record.relative_path: record.document_id for record in database.documents()}
        retriever, passed = HybridRetriever(settings, database), 0
        for case in cases:
            results = retriever.search(case["question"], document_id=by_name[case["expected_source"]])
            found = any(
                all(term in item.text for term in case["must_contain"]) and item.page is not None
                for item in results
            )
            print(("PASS " if found else "FAIL ") + case["id"])
            passed += int(found)
        print(
            f"Local curriculum evidence: {passed}/{len(cases)}. Table/prerequisite recall within selected local document; not an LLM faithfulness score."
        )
        if passed != len(cases):
            raise SystemExit(1)
    finally:
        database.close()


if __name__ == "__main__":
    main()
