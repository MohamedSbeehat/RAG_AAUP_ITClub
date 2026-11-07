import json

from rag_aaup.config import PROJECT_ROOT, get_settings
from rag_aaup.indexing.database import Database
from rag_aaup.retrieval.service import HybridRetriever


def main() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    retriever = HybridRetriever(settings, database)
    cases_path = PROJECT_ROOT / "data" / "eval" / "aaup_questions.jsonl"
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    passed = 0
    skipped = 0
    for case in cases:
        expected_source = case["expected_source"]
        if not any(document.relative_path.endswith(expected_source) for document in database.documents()):
            print(f"SKIP {case['id']}: index {expected_source} first")
            skipped += 1
            continue

        results = retriever.search(case["question"])
        expected_page = case.get("expected_page")
        must_contain = case.get("must_contain", [])
        found = any(
            expected_source in result.source
            and (expected_page is None or result.page == expected_page)
            and all(term.casefold() in result.text.casefold() for term in must_contain)
            for result in results
        )
        print(f"{'PASS' if found else 'FAIL'} {case['id']}")
        passed += int(found)

    evaluated = len(cases) - skipped
    if evaluated == 0:
        raise RuntimeError("No evaluation cases could run; index the official AAUP source files first.")
    print(f"Evidence retrieval: {passed}/{evaluated} passed; {skipped} skipped.")
    if passed != evaluated:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
