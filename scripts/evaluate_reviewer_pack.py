"""Exercise the real API with public synthetic documents, real embeddings, and optionally a real LLM."""

import argparse
import hashlib
import json
import re
import secrets
import tempfile
import time
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from rag_aaup import api
from rag_aaup.assistant import contextual_query
from rag_aaup.config import PROJECT_ROOT, Settings
from rag_aaup.processing.chunking import normalize_for_search
from rag_aaup.tenancy import COOKIE

PACK = PROJECT_ROOT / "reviewer_pack"
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
REFUSAL = (
    r"لا (?:اجد|تتوفر|يتوفر|يوجد|توجد|تتضمن|تحدد|تحتوي|يمكنني|استطيع|يتضمن|توفر|املك|تقدم)|"
    r"لم (?:يرد|ترد|اجد)|غير (?:مذكور|موجود|متاح|متوفر)|خارج|ليست? (?:ضمن|من|في)"
)
SMOKE_IDS = {
    "D01",
    "D06",
    "P01",
    "P03",
    "P04",
    "T01",
    "T06",
    "M01",
    "F01",
    "F02",
    "F05",
    "F06",
    "N01",
    "N02",
    "O01",
    "O06",
}


def select_cases(cases, profile="full", case_ids=None):
    selected = (
        set(case_ids.split(","))
        if case_ids
        else (SMOKE_IDS if profile == "smoke" else {case["id"] for case in cases})
    )
    unknown = selected - {case["id"] for case in cases}
    if unknown:
        raise ValueError(f"Unknown case IDs: {', '.join(sorted(unknown))}")
    # Include every earlier turn in a selected case's conversation, in original order.
    # Otherwise selecting a follow-up alone could falsely test a missing conversation.
    required = set(selected)
    for index, case in enumerate(cases):
        if case["id"] in selected and case.get("conversation"):
            required.update(
                earlier["id"]
                for earlier in cases[:index]
                if earlier.get("conversation") == case["conversation"]
            )
    return [case for case in cases if case["id"] in required]


def normalized(text):
    return normalize_for_search(text.translate(DIGITS))


def source_name(source):
    return source["source"].split(",", 1)[0]


def check_response(case, response, mode):
    """Transparent heuristic checks, not a semantic judge or proof of factual faithfulness."""
    text = normalized(response["answer"])
    sources = response.get("sources", [])
    used = set(re.findall(r"\[(S\d+)\]", response["answer"]))
    available = {item["citation"] for item in sources}
    checks = {}
    if mode == "retrieval":
        checks["required_evidence_files"] = set(case.get("required_sources", [])) <= {
            source_name(item) for item in sources
        }
        return checks
    arabic = len(re.findall(r"[\u0600-\u06ff]", response["answer"]))
    latin = len(re.findall("[A-Za-z]", response["answer"]))
    checks["response_language"] = (
        latin > 5 and arabic / max(arabic + latin, 1) < 0.2 if case.get("language") == "en" else arabic > 5
    )
    checks["no_invented_citation_ids"] = used <= available
    checks["expected_fact_patterns"] = all(
        re.search(pattern, text, flags=re.IGNORECASE) for pattern in case.get("patterns", [])
    )
    checks["no_forbidden_claim_patterns"] = not any(
        re.search(pattern, text, flags=re.IGNORECASE) for pattern in case.get("forbidden_patterns", [])
    )
    if case["kind"] == "answer":
        checks["has_cited_evidence"] = bool(used) and response["provider"] != "abstain"
        checks["required_cited_files"] = set(case.get("required_sources", [])) <= {
            source_name(item) for item in sources if item["citation"] in used
        }
        checks["pdf_page_location"] = all(item["page"] == 1 for item in sources if ".pdf" in item["source"])
    elif case["kind"] == "refusal":
        checks["explicit_refusal"] = bool(re.search(REFUSAL, text))
        if case.get("scope_required"):
            checks["explains_document_scope"] = bool(re.search(r"مستند|ملفات|وثائق|نطاق|اكاديمي", text))
    else:
        checks["asks_for_clarification"] = bool(re.search(r"[?؟]|تزويد|حدد|احتاج|تخبر|معرفه|اذكر", text))
        checks["no_guessed_final_limit"] = not re.search(
            r"(?:يمكنك تسجيل|حدك النهائي|بامكانك تسجيل)\D{0,10}(?:12|15|18|21)\b", text
        )
    return {key: bool(value) for key, value in checks.items()}


def verify_pack():
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["sha256"].items():
        if hashlib.sha256((PACK / "documents" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Fixture changed: {name}; review the cases and rebuild the fixture manifest")
    return manifest


def write_report(report, output):
    report["answered"] = sum(item["status"] in {"PASS", "FAIL"} for item in report["results"])
    report["errors"] = sum(item["status"] == "ERROR" for item in report["results"])
    report["failed_checks"] = sum(item["status"] == "FAIL" for item in report["results"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = [
        "# نتائج حزمة اختبار المقيّمين",
        "",
        f"- وقت التشغيل UTC: `{report['started_at']}`",
        f"- الوضع: `{report['mode']}`؛ النقل: `{report['transport']}`",
        f"- المجموعة: `{report.get('profile', 'full')}`؛ عدد حالات الحزمة الأصلية: 42",
        f"- النموذج: `{report['model']}`؛ التضمين: `{report['embedding_model']}`",
        f"- ناجح: **{report['passed']}/{report['assessed']} محاولة**؛ غير مقاس بهذا الوضع: {report['not_assessed']}",
        f"- استجابات وصلت: {report['answered']}؛ أخطاء تشغيل/مزود: {report['errors']}؛ إجابات خالفت الفحوص: {report['failed_checks']}",
        "- البيانات وهمية عامة، وليست لوائح AAUP. لا يحتوي التقرير على مفاتيح أو جلسات.",
        "",
        (
            "النجاح هنا لفحوص آلية معلنة: أنماط الحقائق واللغة والمصادر وروابط الملفات والامتناع. "
            "لا يثبت ذلك دعم كل عبارة دلالياً؛ راجع الإجابة الكاملة مقابل خانة المتوقع. "
            "وضع retrieval لا يقيس الإجابات أو الامتناع أو حوار المتابعة كاملاً."
        ),
        "",
        "| النوع | ناجح | جرت محاولته |",
        "| --- | ---: | ---: |",
    ]
    for category, counts in report["categories"].items():
        rows.append(f"| {report['category_labels'][category]} | {counts['passed']} | {counts['assessed']} |")
    rows += ["", "## فحوص إضافية", ""]
    rows += [f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in report["integration_checks"].items()]
    for result in report["results"]:
        rows += [
            "",
            f"## {result['id']} - {result['status']}",
            "",
            f"**السؤال:** {result['question']}",
            "",
            f"**المتوقع:** {result['expected']}",
            "",
        ]
        if result.get("answer"):
            rows += ["**الإجابة الفعلية:**", "", result["answer"], ""]
        if result.get("error"):
            rows += [f"خطأ التشغيل: {result['error']}", ""]
        if result.get("checks"):
            rows += [
                "الفحوص: "
                + "، ".join(f"{key}={'PASS' if value else 'FAIL'}" for key, value in result["checks"].items())
            ]
        rows += [
            "",
            "المصادر: " + ("؛ ".join(item["source"] for item in result.get("sources", [])) or "لا توجد"),
        ]
    markdown = "\n".join(line.rstrip() for line in "\n".join(rows).splitlines()) + "\n"
    output.with_suffix(".md").write_text(markdown, encoding="utf-8")


def run(args):
    manifest = verify_pack()
    suite = json.loads((PACK / "cases.json").read_text(encoding="utf-8"))
    cases = select_cases(suite["cases"], args.profile, args.case_ids)
    settings = Settings()
    if (
        args.mode == "live"
        and settings.llm_provider == "gemini"
        and not settings.gemini_api_key
        and not args.base_url
    ):
        raise ValueError("Live mode needs GEMINI_API_KEY in the local .env (never commit it)")
    started = datetime.now(UTC).isoformat()
    results, histories, conversations = [], {}, {}
    category_counts = {key: Counter(assessed=0, passed=0) for key in suite["categories"]}
    report = {
        "schema": "campusiq.reviewer-results.v1",
        "profile": "custom" if args.case_ids else args.profile,
        "selected_case_ids": [case["id"] for case in cases],
        "started_at": started,
        "mode": args.mode,
        "transport": "HTTP" if args.base_url else "in-process FastAPI TestClient",
        "model": settings.gemini_model if args.mode == "live" else "not called",
        "embedding_model": settings.embedding_model,
        "fixture_sha256": manifest["sha256"],
        "cases_sha256": hashlib.sha256((PACK / "cases.json").read_bytes()).hexdigest(),
        "code_sha256": hashlib.sha256(
            b"".join(path.read_bytes() for path in sorted((PROJECT_ROOT / "src").rglob("*.py")))
        ).hexdigest(),
        "category_labels": suite["categories"],
        "integration_checks": {},
        "results": results,
    }
    with ExitStack() as stack:
        if args.base_url:
            client = stack.enter_context(httpx.Client(base_url=args.base_url, timeout=100, trust_env=False))
        else:
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="campusiq-reviewer-")))
            isolated = settings.model_copy(
                update={
                    "multi_tenant_enabled": True,
                    "workspace_root": temporary / "companies",
                    "raw_dir": temporary / "legacy",
                    "database_path": temporary / "legacy.sqlite3",
                    "qdrant_path": temporary / "legacy.qdrant",
                    "qdrant_url": None,
                    "qdrant_api_key": None,
                    "app_password": None,
                }
            )
            stack.enter_context(patch.object(api, "get_settings", return_value=isolated))
            client = stack.enter_context(TestClient(api.app))
        config = client.get("/api/config")
        config.raise_for_status()
        if not config.json().get("multi_tenant"):
            raise ValueError(
                "Reviewer runner requires MULTI_TENANT_ENABLED=true; it never clears an existing company"
            )
        checks = report["integration_checks"]
        checks["unauthenticated_documents_denied"] = client.get("/api/documents").status_code == 401

        def register(label):
            response = client.post(
                "/api/auth/register",
                json={
                    "username": "reviewer-" + secrets.token_hex(6),
                    "password": secrets.token_urlsafe(24),
                    "company_name": label,
                    "knowledge_mode": "academic",
                },
            )
            response.raise_for_status()
            return {"Cookie": f"{COOKIE}={response.cookies.get(COOKIE)}"}

        first = register("Synthetic reviewer university")
        client.headers.update(first)
        try:
            checks["new_company_starts_empty"] = client.get("/api/documents").json() == []
            upload = client.post(
                "/api/documents",
                files=[
                    ("files", (name, (PACK / "documents" / name).read_bytes())) for name in manifest["sha256"]
                ],
            )
            upload.raise_for_status()
            checks["fixture_upload_and_index"] = (
                not upload.json()["failed"] and len(client.get("/api/documents").json()) == 5
            )
            name = next(iter(manifest["sha256"]))
            duplicate = client.post(
                "/api/documents", files={"files": ("duplicate.pdf", (PACK / "documents" / name).read_bytes())}
            )
            duplicate.raise_for_status()
            checks["duplicate_content_skipped"] = duplicate.json()["skipped_duplicates"] == ["duplicate.pdf"]
            cached_links, last_ask = {}, 0.0
            for case in cases:
                result = {key: case[key] for key in ("id", "category", "question", "expected")}
                results.append(result)
                if args.mode == "retrieval" and case["kind"] != "answer":
                    result["status"] = "NOT_ASSESSED"
                    continue
                conversation = case.get("conversation", case["id"])
                query = case["question"]
                if args.mode == "retrieval":
                    query = contextual_query(query, histories.get(conversation, []))
                body = {
                    "question": query,
                    "mode": "answer" if args.mode == "live" else "sources",
                    "conversation_id": conversations.get(conversation),
                }
                # Respect the production limit (30 questions/minute/company), without disabling it.
                time.sleep(max(0, 2.1 - (time.monotonic() - last_ask)))
                last_ask, before = time.monotonic(), time.monotonic()
                response = client.post("/api/ask", json=body)
                result["latency_seconds"] = round(time.monotonic() - before, 3)
                category_counts[case["category"]]["assessed"] += 1
                if response.status_code != 200:
                    result.update(
                        status="ERROR",
                        error=f"HTTP {response.status_code}: {response.json().get('detail', 'request failed')}",
                    )
                    print(f"{case['id']}: ERROR HTTP {response.status_code}", flush=True)
                    continue
                answer = response.json()
                conversations[conversation] = answer["conversation_id"]
                histories.setdefault(conversation, []).append(("user", case["question"]))
                result.update(answer=answer["answer"], provider=answer["provider"])
                verdicts = check_response(case, answer, args.mode)
                for source in answer["sources"]:
                    url = source.get("url")
                    if url not in cached_links:
                        download = client.get(url) if url else None
                        digest = (
                            hashlib.sha256(download.content).hexdigest()
                            if download and download.status_code == 200
                            else None
                        )
                        cached_links[url] = (
                            digest == manifest["sha256"].get(source_name(source)) and digest is not None
                        )
                    verdicts["local_file_links_match_fixture"] = (
                        verdicts.get("local_file_links_match_fixture", True) and cached_links[url]
                    )
                result["sources"] = [
                    {key: source[key] for key in ("citation", "source", "page", "section", "text")}
                    for source in answer["sources"]
                ]
                result["checks"] = verdicts
                result["status"] = "PASS" if all(verdicts.values()) else "FAIL"
                category_counts[case["category"]]["passed"] += result["status"] == "PASS"
                print(f"{case['id']}: {result['status']} ({result['latency_seconds']}s)", flush=True)
            # A separate new company must not download A's files, and clearing B must preserve A.
            files_a = client.get("/api/documents").json()
            second = register("Independent synthetic company")
            checks["other_company_starts_empty"] = client.get("/api/documents", headers=second).json() == []
            checks["cross_company_file_denied"] = (
                client.get(files_a[0]["url"], headers=second).status_code == 404
            )
            client.post(
                "/api/documents",
                headers=second,
                files={"files": ("isolated.txt", b"B_ONLY_REVIEWER_MARKER_85139")},
            ).raise_for_status()
            client.request(
                "DELETE", "/api/documents", headers=second, json={"confirmation": "DELETE"}
            ).raise_for_status()
            checks["clear_other_company_preserves_first"] = (
                len(client.get("/api/documents", headers=first).json()) == 5
            )
            client.post("/api/auth/logout", headers=second).raise_for_status()
        finally:
            # Only random accounts created by this run are touched. Existing accounts remain unchanged.
            client.request(
                "DELETE", "/api/documents", headers=first, json={"confirmation": "DELETE"}
            ).raise_for_status()
            checks["own_clear_removes_all_indexed_files"] = (
                client.get("/api/documents", headers=first).json() == []
            )
            client.post("/api/auth/logout", headers=first).raise_for_status()
            checks["logout_revokes_session"] = client.get("/api/documents", headers=first).status_code == 401
    report["categories"] = category_counts
    report["assessed"] = sum(item["assessed"] for item in category_counts.values())
    report["passed"] = sum(item["passed"] for item in category_counts.values())
    report["not_assessed"] = len(results) - report["assessed"]
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_report(report, args.output)
    print(
        f"Result: {report['passed']}/{report['assessed']}; not assessed: {report['not_assessed']}; reports: {args.output}.json/.md"
    )
    return 0 if report["passed"] == report["assessed"] and all(report["integration_checks"].values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("retrieval", "live"), default="retrieval")
    parser.add_argument(
        "--profile",
        choices=("full", "smoke"),
        default="full",
        help="smoke selects 16 cases covering all seven categories; full selects 42",
    )
    parser.add_argument(
        "--case-ids",
        help="Optional comma-separated IDs; earlier conversation turns are included automatically",
    )
    parser.add_argument(
        "--base-url", help="Optional running multi-company HTTP server; otherwise use an isolated API process"
    )
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs/reviewer-results")
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
