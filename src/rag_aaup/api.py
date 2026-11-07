import asyncio
import base64
import binascii
import hmac
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from rag_aaup.assistant import AcademicAssistant
from rag_aaup.config import Settings, generation_settings, get_settings
from rag_aaup.generation.service import GenerationError, generation_available
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import SUPPORTED_SUFFIXES, IndexingService, IndexReport
from rag_aaup.indexing.vectors import VectorStoreError
from rag_aaup.retrieval.service import HybridRetriever, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    database: Database
    indexer: IndexingService
    assistant: AcademicAssistant


def _runtime() -> Runtime:
    settings = get_settings()
    database = Database.from_settings(settings)
    indexer = IndexingService(settings, database)
    assistant = AcademicAssistant(settings, database, HybridRetriever(settings, database))
    return Runtime(settings, database, indexer, assistant)


async def _refresh_loop(runtime: Runtime) -> None:
    while True:
        await asyncio.sleep(runtime.settings.refresh_interval_seconds)
        sync_task = asyncio.create_task(asyncio.to_thread(runtime.indexer.sync))
        try:
            report: IndexReport = await asyncio.shield(sync_task)
        except asyncio.CancelledError:
            # to_thread keeps running after cancellation. Finish before closing local Qdrant.
            await sync_task
            raise
        if report.failed:
            logger.error("Document refresh completed with failures: %s", report.failed)
        if report.added or report.updated:
            logger.info("Document refresh: added=%s updated=%s", report.added, report.updated)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    runtime = _runtime()
    app.state.runtime = runtime
    refresh_task = None
    try:
        initial_report = await asyncio.to_thread(runtime.indexer.sync)
        if initial_report.failed:
            logger.error("Initial document indexing had failures: %s", initial_report.failed)
        refresh_task = asyncio.create_task(_refresh_loop(runtime), name="document-refresh")
        yield
    finally:
        try:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
        finally:
            await asyncio.to_thread(runtime.database.close)
            app.state.runtime = None


app = FastAPI(
    title="Document RAG Workspace",
    description="Arabic/English answers grounded in the configured organization's documents.",
    version="0.1.0",
    lifespan=_lifespan,
)


@app.middleware("http")
async def access_control(request: Request, call_next):
    runtime = getattr(app.state, "runtime", None)
    settings = runtime.settings if runtime else get_settings()
    if settings.app_password and settings.app_password.get_secret_value():
        valid = False
        try:
            scheme, credentials = request.headers.get("Authorization", "").split(" ", 1)
            username, password = base64.b64decode(credentials, validate=True).decode("utf-8").split(":", 1)
            valid = (
                scheme.casefold() == "basic"
                and hmac.compare_digest(username.encode(), settings.app_username.encode())
                and hmac.compare_digest(password.encode(), settings.app_password.get_secret_value().encode())
            )
        except (ValueError, UnicodeError, binascii.Error):
            pass
        if not valid:
            return JSONResponse(
                status_code=401,
                content={"detail": "Workspace sign-in required"},
                headers={"WWW-Authenticate": 'Basic realm="Document workspace", charset="UTF-8"'},
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=2, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)
    mode: Literal["answer", "sources"] = "answer"


def _get_runtime() -> Runtime:
    runtime = getattr(app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Application runtime is not ready")
    return runtime


def _report_dict(report: IndexReport) -> dict[str, object]:
    return {
        "added": report.added,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "removed": report.removed,
        "failed": report.failed,
    }


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health(response: Response) -> dict[str, object]:
    runtime = _get_runtime()
    ready = runtime.database.vectors_ready
    response.status_code = 200 if ready else 503
    return {
        "status": "ok" if ready else "degraded",
        "indexed_documents": runtime.database.document_count(),
        "raw_directory": str(runtime.settings.raw_dir),
        "vector_backend": "qdrant",
        "vector_mode": "server" if runtime.settings.qdrant_url else "local",
        "vector_ready": ready,
    }


@app.get("/api/status")
async def status(response: Response) -> dict[str, object]:
    runtime = _get_runtime()
    settings = generation_settings(runtime.settings)
    response.headers["Cache-Control"] = "no-store"
    return {
        "provider": settings.llm_provider,
        "generation_configured": generation_available(settings),
        "request_timeout_seconds": settings.llm_timeout_seconds + 30,
        "assistant_name": runtime.settings.assistant_name,
        "organization_name": runtime.settings.organization_name,
        "knowledge_mode": runtime.settings.knowledge_mode,
    }


@app.get("/api/documents")
async def documents() -> list[dict[str, object]]:
    runtime = _get_runtime()
    all_documents = runtime.database.documents()
    latest = {}
    by_id = {document.document_id: document for document in all_documents}
    for document in all_documents:
        current_id = latest.get(document.family_key)
        current = by_id.get(current_id)
        if current is None or (document.version_sort, document.modified_ns) > (
            current.version_sort,
            current.modified_ns,
        ):
            latest[document.family_key] = document.document_id
    return [
        {
            "document_id": document.document_id,
            "title": document.title,
            "source": document.relative_path,
            "version": document.version_sort,
            "is_latest": latest[document.family_key] == document.document_id,
            "url": f"/api/documents/{document.document_id}/file",
        }
        for document in all_documents
    ]


@app.get("/api/workspace")
async def workspace() -> dict[str, object]:
    runtime = _get_runtime()
    return {
        "directory": str(runtime.settings.raw_dir.resolve()),
        "document_count": runtime.database.document_count(),
        "chunk_count": runtime.database.chunk_count(),
        "is_syncing": runtime.indexer.is_syncing,
        "last_synced_at": runtime.indexer.last_synced_at,
        "refresh_interval_seconds": runtime.settings.refresh_interval_seconds,
        "supported_extensions": sorted(SUPPORTED_SUFFIXES),
        "last_report": _report_dict(runtime.indexer.last_report) if runtime.indexer.last_report else None,
    }


@app.get("/api/documents/{document_id}/file")
async def source_file(document_id: int) -> FileResponse:
    runtime = _get_runtime()
    document = next((item for item in runtime.database.documents() if item.document_id == document_id), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Document is not indexed")
    root = runtime.settings.raw_dir.resolve()
    path = (root / document.relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Source document is unavailable")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@app.post("/api/reindex")
async def reindex() -> dict[str, object]:
    runtime = _get_runtime()
    report = await asyncio.to_thread(runtime.indexer.sync)
    return _report_dict(report)


@app.post("/api/documents")
async def upload_documents(files: Annotated[list[UploadFile], File()]) -> dict[str, object]:
    runtime = _get_runtime()
    invalid = [
        file.filename or "(unnamed)"
        for file in files
        if Path(file.filename or "").suffix.lower() not in SUPPORTED_SUFFIXES
    ]
    if invalid:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported document type. Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}",
        )

    runtime.settings.raw_dir.mkdir(parents=True, exist_ok=True)
    stored: list[str] = []
    size_limit = runtime.settings.max_upload_size_mb * 1024 * 1024
    for file in files:
        filename = Path(file.filename or "").name
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=400, detail="Every upload must have a valid filename")
        destination = runtime.settings.raw_dir / filename
        if not destination.resolve().is_relative_to(runtime.settings.raw_dir.resolve()):
            raise HTTPException(status_code=400, detail="Invalid upload destination")
        temporary = destination.with_name(f".{uuid.uuid4().hex}.upload")
        total = 0
        try:
            with temporary.open("wb") as output:
                while content := await file.read(1024 * 1024):
                    total += len(content)
                    if total > size_limit:
                        raise HTTPException(
                            status_code=413, detail="File exceeds the configured upload limit"
                        )
                    output.write(content)
            await asyncio.to_thread(temporary.replace, destination)
            stored.append(filename)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    report = await asyncio.to_thread(runtime.indexer.sync)
    return {"uploaded": stored, **_report_dict(report)}


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, object]:
    runtime = _get_runtime()
    settings = generation_settings(runtime.settings)
    if request.mode == "answer" and not generation_available(settings) and runtime.database.document_count():
        raise HTTPException(
            status_code=503,
            detail=(
                "توليد الإجابات غير مفعّل: أضف مفتاح Gemini إلى GEMINI_API_KEY في ملف .env واحفظه. "
                "سيُقرأ تلقائياً دون إعادة تشغيل. يمكنك الآن اختيار البحث في المصادر."
            ),
        )
    try:
        timeout = settings.llm_timeout_seconds + 30
        if request.mode == "sources":
            sources = await asyncio.wait_for(
                asyncio.to_thread(runtime.assistant.retriever.search, request.question), timeout=timeout
            )
            arabic = any("\u0600" <= char <= "\u06ff" for char in request.question)
            return {
                "conversation_id": request.conversation_id or str(uuid.uuid4()),
                "provider": "sources_only",
                "answer": (
                    "هذه مقتطفات مسترجعة للمراجعة، وليست إجابة مولّدة أو تأكيداً بأن كل مقتطف يجيب عن السؤال."
                    if arabic
                    else "These are retrieved excerpts for review, not a generated answer or verified claims."
                )
                if sources
                else ("لا توجد مقتطفات كافية في الفهرس." if arabic else "No supporting excerpts were found."),
                "sources": [_source_dict(source) for source in sources],
            }
        response = await asyncio.wait_for(
            asyncio.to_thread(
                runtime.assistant.answer,
                request.question,
                request.conversation_id or str(uuid.uuid4()),
                settings,
            ),
            timeout=timeout,
        )
    except TimeoutError as error:
        raise HTTPException(
            status_code=504,
            detail="استغرق الطلب وقتاً أطول من المسموح. حاول مجدداً أو استخدم البحث في المصادر.",
        ) from error
    except (GenerationError, VectorStoreError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {
        "conversation_id": response.conversation_id,
        "answer": response.answer,
        "provider": response.provider,
        "sources": [_source_dict(source) for source in response.sources],
    }


def _source_dict(source: SearchResult) -> dict[str, object]:
    return {
        "citation": source.citation,
        "source": source.source,
        "title": source.title,
        "page": source.page,
        "section": source.section,
        "topic": source.topic,
        "is_latest_version": source.is_latest_version,
        "text": source.text,
        "url": (
            f"/api/documents/{source.document_id}/file"
            + (f"#page={source.page}" if source.page is not None else "")
        )
        if source.document_id is not None
        else None,
    }
