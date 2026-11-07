import argparse
import logging
from pathlib import Path

import uvicorn

from rag_aaup.config import get_settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import IndexingService


def doctor(check_provider: bool) -> None:
    settings = get_settings()
    print(f"Organization: {settings.organization_name}; mode: {settings.knowledge_mode}")
    print(f"Documents directory: {settings.raw_dir.resolve()}")
    print(f"Vector backend: Qdrant ({'server' if settings.qdrant_url else 'local'})")
    print(f"Provider: {settings.llm_provider}; Gemini model: {settings.gemini_model}")
    print(f"Gemini key configured: {bool(settings.gemini_api_key)} (value hidden)")
    print(f"Workspace password configured: {bool(settings.app_password)}")
    if check_provider:
        from google.genai.errors import APIError

        from rag_aaup.generation.service import GenerationError, _generate_gemini, _generate_ollama

        try:
            if settings.llm_provider == "ollama":
                result = _generate_ollama(settings, "Reply with exactly: OK")
            else:
                result = _generate_gemini(settings, "Reply with exactly: OK")
            if not result:
                raise GenerationError("Empty provider response")
            print("Live generation check: passed")
        except (APIError, OSError, RuntimeError) as error:
            print(
                f"Live generation check: failed ({type(error).__name__}, code={getattr(error, 'code', '-')})"
            )
            raise SystemExit(1) from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Organization document RAG workspace")
    parser.add_argument("command", choices=("serve", "index", "doctor", "sync-academic", "setup-ocr"))
    parser.add_argument(
        "--install-dir", type=Path, help="sync-academic: copy references to this local folder"
    )
    parser.add_argument(
        "--check-provider", action="store_true", help="Doctor: make a small live generation call"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "sync-academic":
        from rag_aaup.academic.sync import AcademicReferences

        references = AcademicReferences()
        state = references.sync(force=True)
        print(f"Programs: {len(state['programs'])}; errors: {state['errors']}")
        if state["errors"]:
            raise SystemExit(1)
        if args.install_dir:
            print("Installed:", references.install(args.install_dir))
        return
    if args.command == "setup-ocr":
        import httpx

        target = get_settings().ocr_tessdata_path
        target.mkdir(parents=True, exist_ok=True)
        for language in ("ara", "eng"):
            path = target / f"{language}.traineddata"
            if not path.exists():
                response = httpx.get(
                    f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{language}.traineddata",
                    timeout=60,
                )
                response.raise_for_status()
                temporary = path.with_suffix(".downloading")
                temporary.write_bytes(response.content)
                temporary.replace(path)
            print(f"OCR language ready: {language}")
        return

    if args.command == "doctor":
        doctor(args.check_provider)
        return

    if args.command == "index":
        settings = get_settings()
        database = Database.from_settings(settings)
        try:
            report = IndexingService(settings, database).sync()
        finally:
            database.close()
        print(
            f"Added: {len(report.added)}, updated: {len(report.updated)}, "
            f"unchanged: {len(report.unchanged)}, removed: {len(report.removed)}, failed: {len(report.failed)}"
        )
        for path, error in report.failed.items():
            print(f"FAILED {path}: {error}")
        if report.failed:
            raise SystemExit(1)
        return

    settings = get_settings()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not (
        settings.multi_tenant_enabled or (settings.app_password and settings.app_password.get_secret_value())
    ):
        parser.error(
            "Set APP_PASSWORD before exposing the workspace beyond localhost; use HTTPS in deployment."
        )
    uvicorn.run("rag_aaup.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
