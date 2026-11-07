import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import sqlite3
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from rag_aaup.assistant import AcademicAssistant
from rag_aaup.config import PROJECT_ROOT, Settings, generation_settings, get_settings
from rag_aaup.generation.service import GenerationError, generation_available
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import SUPPORTED_SUFFIXES, IndexingService, IndexReport
from rag_aaup.indexing.vectors import VectorStoreError
from rag_aaup.retrieval.service import HybridRetriever, SearchResult
from rag_aaup.tenancy import COOKIE, Principal, TenantRegistry

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    database: Database
    indexer: IndexingService
    assistant: AcademicAssistant
    file_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ask_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    sync_guard: RLock = field(default_factory=RLock)


def _runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    database = Database.from_settings(settings)
    indexer = IndexingService(settings, database)
    assistant = AcademicAssistant(settings, database, HybridRetriever(settings, database))
    return Runtime(settings, database, indexer, assistant)


_request_runtime: ContextVar[Runtime | None] = ContextVar("request_runtime", default=None)
_request_principal: ContextVar[Principal | None] = ContextVar("request_principal", default=None)


async def _tenant_refresh_loop(registry: TenantRegistry):
    while True:
        await asyncio.sleep(registry.settings.refresh_interval_seconds)
        task = asyncio.create_task(asyncio.to_thread(registry.sync_all))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except (OSError, RuntimeError, ValueError):
            logger.exception("Workspace refresh failed; it will be retried")


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
    registry = TenantRegistry(runtime.settings, _runtime) if runtime.settings.multi_tenant_enabled else None
    app.state.registry = registry
    refresh_task = None
    try:
        initial_report = await asyncio.to_thread(runtime.indexer.sync)
        if initial_report.failed:
            logger.error("Initial document indexing had failures: %s", initial_report.failed)
        refresh_task = asyncio.create_task(
            _tenant_refresh_loop(registry) if registry else _refresh_loop(runtime), name="document-refresh"
        )
        yield
    finally:
        try:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
        finally:
            await asyncio.to_thread(runtime.database.close)
            if registry:
                await asyncio.to_thread(registry.close)
            app.state.runtime = None
            app.state.registry = None


app = FastAPI(
    title="Document RAG Workspace",
    description="Arabic/English answers grounded in the configured organization's documents.",
    version="0.1.0",
    lifespan=_lifespan,
)


@app.get("/api/project/{asset_name}")
def project_asset(asset_name: str):
    assets = {
        "presentation.pdf": ("docs/presentation/CampusIQ_Pitch_AR.pdf", "application/pdf"),
        "presentation.pptx": (
            "docs/presentation/CampusIQ_Pitch_AR.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        "documentation": ("docs/PROJECT_DOCUMENTATION_AR.md", "text/markdown; charset=utf-8"),
    }
    entry = assets.get(asset_name)
    if entry is None or not (PROJECT_ROOT / entry[0]).is_file():
        raise HTTPException(status_code=404, detail="ملف المشروع غير متاح")
    path, media_type = entry
    return FileResponse(
        PROJECT_ROOT / path,
        media_type=media_type,
        filename=Path(path).name,
        content_disposition_type="inline" if asset_name.endswith(".pdf") else "attachment",
    )


@app.middleware("http")
async def access_control(request: Request, call_next):
    runtime = getattr(app.state, "runtime", None)
    settings = runtime.settings if runtime else get_settings()
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse(status_code=403, content={"detail": "Cross-origin mutation is not allowed"})
    if settings.multi_tenant_enabled:
        public = {
            "/",
            "/static/workspace.js",
            "/api/config",
            "/api/health",
            "/api/auth/login",
            "/api/auth/register",
        }
        if request.url.path not in public:
            registry = getattr(app.state, "registry", None)
            if registry is None:
                return JSONResponse(status_code=503, content={"detail": "Workspace service is starting"})
            principal = await asyncio.to_thread(
                registry.accounts.authenticate, request.cookies.get(COOKIE, "")
            )
            if principal is None:
                return JSONResponse(status_code=401, content={"detail": "سجّل الدخول إلى حساب شركتك أولاً"})
            if (
                request.method == "POST"
                and request.url.path == "/api/ask"
                and not registry.allow_query(principal.tenant_id)
            ):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "وصلت مساحة الشركة إلى حد الطلبات خلال الدقيقة؛ حاول بعد قليل"},
                )
            own_runtime = await asyncio.to_thread(registry.runtime, principal.tenant_id)
            runtime_token = _request_runtime.set(own_runtime)
            principal_token = _request_principal.set(principal)
            try:
                response = await call_next(request)
                if request.method not in {"GET", "HEAD", "OPTIONS"} and response.status_code < 400:
                    registry.accounts.audit(principal, request.method + " " + request.url.path, "")
            finally:
                _request_runtime.reset(runtime_token)
                _request_principal.reset(principal_token)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            return response
    elif settings.app_password and settings.app_password.get_secret_value():
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
    document_id: int | None = Field(default=None, gt=0)


def _get_runtime() -> Runtime:
    if scoped := _request_runtime.get():
        return scoped
    runtime = getattr(app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Application runtime is not ready")
    return runtime


@app.get("/api/config")
async def public_config():
    runtime = getattr(app.state, "runtime", None)
    return {"multi_tenant": bool(runtime and runtime.settings.multi_tenant_enabled), "app_name": "CampusIQ"}


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=10, max_length=128)


class RegisterRequest(LoginRequest):
    company_name: str = Field(min_length=2, max_length=100)
    knowledge_mode: Literal["academic", "organization"] = "organization"


def _registry() -> TenantRegistry:
    registry = getattr(app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=404, detail="Company accounts are not enabled")
    return registry


def _set_session(response: Response, request: Request, principal: Principal):
    registry = _registry()
    token = registry.accounts.session(principal)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=registry.settings.session_hours * 3600,
    )


@app.post("/api/auth/register", status_code=201)
async def register(body: RegisterRequest, request: Request, response: Response):
    registry = _registry()
    if not registry.allow_auth(request.client.host if request.client else "local"):
        raise HTTPException(status_code=429, detail="انتظر قليلاً قبل محاولة أخرى")
    try:
        principal = await asyncio.to_thread(
            registry.accounts.register, body.username, body.password, body.company_name, body.knowledge_mode
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم بالفعل") from None
    _set_session(response, request, principal)
    return asdict(principal)


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    registry = _registry()
    if not registry.allow_auth(request.client.host if request.client else "local"):
        raise HTTPException(status_code=429, detail="انتظر قليلاً قبل محاولة أخرى")
    principal = await asyncio.to_thread(registry.accounts.login, body.username, body.password)
    if principal is None:
        raise HTTPException(status_code=401, detail="بيانات الدخول غير صحيحة")
    _set_session(response, request, principal)
    return asdict(principal)


@app.get("/api/auth/me")
async def me():
    principal = _request_principal.get()
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign-in required")
    return asdict(principal)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    _registry().accounts.logout(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE)
    return {"status": "signed_out"}


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
    return (Path(__file__).parent / "web" / "workspace.html").read_text(encoding="utf-8")


@app.get("/static/workspace.js")
async def workspace_script():
    return FileResponse(Path(__file__).parent / "web" / "workspace.js", media_type="text/javascript")


@app.get("/api/health")
async def health(response: Response) -> dict[str, object]:
    runtime = _get_runtime()
    if runtime.settings.multi_tenant_enabled and _request_principal.get() is None:
        return {"status": "ok", "multi_tenant": True}
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
        "tenant_id": _request_principal.get().tenant_id if _request_principal.get() else None,
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
            "url": _file_url(document.document_id, content_hash=document.sha256),
        }
        for document in all_documents
    ]


@app.get("/api/workspace")
async def workspace() -> dict[str, object]:
    runtime = _get_runtime()
    return {
        "directory": str(runtime.settings.raw_dir.resolve()),
        "tenant_id": _request_principal.get().tenant_id if _request_principal.get() else None,
        "document_count": runtime.database.document_count(),
        "chunk_count": runtime.database.chunk_count(),
        "is_syncing": runtime.indexer.is_syncing,
        "last_synced_at": runtime.indexer.last_synced_at,
        "refresh_interval_seconds": runtime.settings.refresh_interval_seconds,
        "supported_extensions": sorted(SUPPORTED_SUFFIXES),
        "last_report": _report_dict(runtime.indexer.last_report) if runtime.indexer.last_report else None,
        "processing": {
            "file": runtime.indexer.current_file,
            "completed": runtime.indexer.completed_files,
            "total": runtime.indexer.total_files,
        },
    }


@app.get("/api/documents/{document_id}/file")
async def source_file(
    document_id: int, workspace: str | None = None, version: str | None = None
) -> FileResponse:
    runtime = _get_runtime()
    principal = _request_principal.get()
    if workspace is not None and (principal is None or workspace != principal.tenant_id):
        raise HTTPException(status_code=404, detail="Source link belongs to a different workspace")
    document = next((item for item in runtime.database.documents() if item.document_id == document_id), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Document is not indexed")
    if version is not None and version != document.sha256:
        raise HTTPException(
            status_code=409, detail="هذا المرجع يخص نسخة سابقة؛ أعد البحث لعرض النسخة الحالية"
        )
    root = runtime.settings.raw_dir.resolve()
    path = (root / document.relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Source document is unavailable")
    if hashlib.sha256(path.read_bytes()).hexdigest() != document.sha256:
        raise HTTPException(status_code=409, detail="الملف تغير ولم تكتمل فهرسته بعد؛ حاول بعد المزامنة")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@app.post("/api/reindex")
async def reindex() -> dict[str, object]:
    runtime = _get_runtime()
    async with runtime.file_lock:
        report = await asyncio.to_thread(runtime.indexer.sync)
    return _report_dict(report)


@app.post("/api/documents")
async def upload_documents(files: Annotated[list[UploadFile], File()]) -> dict[str, object]:
    runtime = _get_runtime()
    async with runtime.file_lock:
        return await _upload_files(files, runtime)


async def _upload_files(files: list[UploadFile], runtime: Runtime) -> dict[str, object]:
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
    skipped: list[str] = []
    known = {
        item.relative_path: item.sha256
        for item in runtime.database.documents()
        if (runtime.settings.raw_dir / item.relative_path).is_file()
    }
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
        digest = hashlib.sha256()
        try:
            with temporary.open("wb") as output:
                while content := await file.read(1024 * 1024):
                    total += len(content)
                    if total > size_limit:
                        raise HTTPException(
                            status_code=413, detail="File exceeds the configured upload limit"
                        )
                    output.write(content)
                    digest.update(content)
            if digest.hexdigest() in known.values():
                skipped.append(filename)
                continue
            await asyncio.to_thread(temporary.replace, destination)
            known[filename] = digest.hexdigest()
            stored.append(filename)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    report = await asyncio.to_thread(runtime.indexer.sync)
    return {"uploaded": stored, "skipped_duplicates": skipped, **_report_dict(report)}


class ClearRequest(BaseModel):
    confirmation: Literal["DELETE"]


def _quarantine(runtime: Runtime, document_ids: list[int] | None = None) -> list[str]:
    with runtime.sync_guard:
        principal = _request_principal.get()
        if principal:
            _registry().accounts.set_academic(principal.tenant_id, False)
        return _quarantine_files(runtime, document_ids)


def _quarantine_files(runtime: Runtime, document_ids: list[int] | None = None) -> list[str]:
    root = runtime.settings.raw_dir.resolve()
    trash = root.parent / ".rag_trash" / uuid.uuid4().hex
    if document_ids is None:
        paths = [
            path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ]
    else:
        paths = [
            root / item.relative_path
            for item in runtime.database.documents()
            if item.document_id in document_ids
        ]
    moved = []
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or path.is_symlink():
            continue
        target = trash / path.relative_to(root)
        if not target.resolve().is_relative_to(trash.resolve()):
            raise ValueError("Invalid trash destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(path.name)
    return moved


@app.delete("/api/documents")
async def clear_documents(body: ClearRequest):
    runtime = _get_runtime()
    async with runtime.file_lock:
        moved = await asyncio.to_thread(_quarantine, runtime)
        principal = _request_principal.get()
        if principal:
            _registry().accounts.set_academic(principal.tenant_id, False)
        report = await asyncio.to_thread(runtime.indexer.sync)
    return {"quarantined": moved, **_report_dict(report)}


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: int):
    runtime = _get_runtime()
    if not any(item.document_id == document_id for item in runtime.database.documents()):
        raise HTTPException(status_code=404, detail="Document not found in this workspace")
    async with runtime.file_lock:
        moved = await asyncio.to_thread(_quarantine, runtime, [document_id])
        principal = _request_principal.get()
        if principal:
            _registry().accounts.set_academic(principal.tenant_id, False)
        report = await asyncio.to_thread(runtime.indexer.sync)
    return {"quarantined": moved, **_report_dict(report)}


@app.get("/api/academic")
async def academic_status():
    return _registry().references.state()


@app.post("/api/academic/import")
async def import_academic_references():
    principal = _request_principal.get()
    if principal is None or principal.knowledge_mode != "academic":
        raise HTTPException(status_code=403, detail="اختر مساحة أكاديمية لاستيراد مراجع الجامعة")
    registry, runtime = _registry(), _get_runtime()
    async with runtime.file_lock:
        state = await asyncio.to_thread(registry.references.sync)
        if state.get("errors"):
            raise HTTPException(status_code=503, detail="تعذر تحديث بعض المصادر؛ راجع حالة المراجع")

        def install():
            with runtime.sync_guard:
                copied = registry.references.install(runtime.settings.raw_dir)
                registry.accounts.set_academic(principal.tenant_id, True)
                return copied, runtime.indexer.sync()

        copied, report = await asyncio.to_thread(install)
    return {"installed": copied, **_report_dict(report)}


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, object]:
    runtime = _get_runtime()
    if runtime.ask_slots.locked():
        raise HTTPException(status_code=429, detail="هناك طلبات قيد المعالجة؛ حاول بعد قليل")
    async with runtime.ask_slots:
        return await _answer_request(request)


async def _answer_request(request: AskRequest) -> dict[str, object]:
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
                asyncio.to_thread(
                    runtime.assistant.retriever.search, request.question, document_id=request.document_id
                ),
                timeout=timeout,
            )
            arabic = any("\u0600" <= char <= "\u06ff" for char in request.question)
            return {
                "conversation_id": request.conversation_id or str(uuid.uuid4()),
                "provider": "sources_only",
                "tenant_id": _request_principal.get().tenant_id if _request_principal.get() else None,
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
                request.document_id,
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
        "tenant_id": _request_principal.get().tenant_id if _request_principal.get() else None,
        "sources": [_source_dict(source) for source in response.sources],
    }


def _file_url(document_id: int, page: int | None = None, content_hash: str | None = None) -> str:
    principal = _request_principal.get()
    parameters = ([f"workspace={principal.tenant_id}"] if principal else []) + (
        [f"version={content_hash}"] if content_hash else []
    )
    return (
        f"/api/documents/{document_id}/file"
        + ("?" + "&".join(parameters) if parameters else "")
        + (f"#page={page}" if page is not None else "")
    )


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
        "url": _file_url(source.document_id, source.page, source.content_hash)
        if source.document_id is not None
        else None,
    }
