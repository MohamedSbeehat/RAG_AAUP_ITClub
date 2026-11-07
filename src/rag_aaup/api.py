import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from rag_aaup.assistant import AcademicAssistant
from rag_aaup.config import Settings, get_settings
from rag_aaup.generation.service import GenerationError
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import SUPPORTED_SUFFIXES, IndexingService, IndexReport
from rag_aaup.retrieval.service import HybridRetriever

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    database: Database
    indexer: IndexingService
    assistant: AcademicAssistant


def _runtime() -> Runtime:
    settings = get_settings()
    database = Database(settings.database_path)
    indexer = IndexingService(settings, database)
    assistant = AcademicAssistant(settings, database, HybridRetriever(settings, database))
    return Runtime(settings, database, indexer, assistant)


async def _refresh_loop(runtime: Runtime) -> None:
    while True:
        await asyncio.sleep(runtime.settings.refresh_interval_seconds)
        report: IndexReport = await asyncio.to_thread(runtime.indexer.sync)
        if report.failed:
            logger.error("Document refresh completed with failures: %s", report.failed)
        if report.added or report.updated:
            logger.info("Document refresh: added=%s updated=%s", report.added, report.updated)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    runtime = _runtime()
    app.state.runtime = runtime
    initial_report = await asyncio.to_thread(runtime.indexer.sync)
    if initial_report.failed:
        logger.error("Initial document indexing had failures: %s", initial_report.failed)
    refresh_task = asyncio.create_task(_refresh_loop(runtime), name="document-refresh")
    try:
        yield
    finally:
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task


app = FastAPI(
    title="AAUP Smart Academic Advisor",
    description="Arabic/English document-grounded assistant for admissions and registration.",
    version="0.1.0",
    lifespan=_lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)


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
        "failed": report.failed,
    }


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>المرشد الأكاديمي الذكي</title>
  <style>
    :root{font-family:system-ui,"Segoe UI",sans-serif;color:#17233b;background:#f3f6fb}
    body{margin:0}.wrap{max-width:850px;margin:6vh auto;padding:24px}
    header,.panel{background:white;border:1px solid #dfe6f0;border-radius:16px;padding:24px;margin-bottom:16px}
    h1{margin-top:0;color:#153a66}p{line-height:1.7}.muted{color:#5d6b82}
    textarea{box-sizing:border-box;width:100%;min-height:110px;padding:14px;border:1px solid #bdc9db;border-radius:10px;font:inherit}
    button{margin-top:12px;padding:11px 22px;background:#1459a6;color:white;border:0;border-radius:9px;font:inherit;cursor:pointer}
    button:disabled{opacity:.6;cursor:wait}#answer{white-space:pre-wrap;line-height:1.8}
    .source{border-top:1px solid #e4e9f1;padding-top:10px;margin-top:10px;font-size:.95rem}
    .error{color:#a22525}
  </style>
</head>
<body><main class="wrap">
  <header><h1>المرشد الأكاديمي الذكي</h1>
    <p class="muted">اسأل عن القبول والتسجيل. الإجابات تعتمد على المستندات المفهرسة وتعرض مصادرها.</p></header>
  <section class="panel">
    <form id="form"><label for="question">سؤالك / Your question</label>
      <textarea id="question" required minlength="2" placeholder="مثال: ما الوثائق المطلوبة للتسجيل؟"></textarea>
      <button id="submit" type="submit">اسأل</button></form>
  </section>
  <section id="result" class="panel" hidden><h2>الإجابة</h2><div id="answer"></div>
    <div id="sources"></div></section>
</main>
<script>
const form=document.querySelector("#form"),button=document.querySelector("#submit");
let conversationId=crypto.randomUUID();
form.addEventListener("submit",async event=>{
  event.preventDefault();button.disabled=true;button.textContent="جارٍ البحث...";
  const result=document.querySelector("#result"),answer=document.querySelector("#answer"),sources=document.querySelector("#sources");
  result.hidden=false;answer.textContent="";sources.replaceChildren();
  try{
    const response=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({question:document.querySelector("#question").value,conversation_id:conversationId})});
    const data=await response.json();if(!response.ok)throw new Error(data.detail||"تعذر إكمال الطلب");
    conversationId=data.conversation_id;answer.textContent=data.answer;
    for(const source of data.sources){const item=document.createElement("p");item.className="source";
      item.textContent=`[${source.citation}] ${source.source}${source.is_latest_version?"":" (نسخة أقدم - مرجع احتياطي)"}`;
      sources.append(item)}
  }catch(error){answer.className="error";answer.textContent=error.message}
  finally{button.disabled=false;button.textContent="اسأل"}
});
</script></body></html>"""


@app.get("/api/health")
async def health() -> dict[str, object]:
    runtime = _get_runtime()
    return {
        "status": "ok",
        "indexed_documents": runtime.database.document_count(),
        "raw_directory": str(runtime.settings.raw_dir),
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
            "title": document.title,
            "source": document.relative_path,
            "version": document.version_sort,
            "is_latest": latest[document.family_key] == document.document_id,
        }
        for document in all_documents
    ]


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
    for file in files:
        filename = Path(file.filename or "").name
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=400, detail="Every upload must have a valid filename")
        destination = runtime.settings.raw_dir / filename
        content = await file.read()
        await asyncio.to_thread(destination.write_bytes, content)
        stored.append(filename)
        await file.close()

    report = await asyncio.to_thread(runtime.indexer.sync)
    return {"uploaded": stored, **_report_dict(report)}


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, object]:
    runtime = _get_runtime()
    try:
        response = await asyncio.to_thread(
            runtime.assistant.answer,
            request.question,
            request.conversation_id or str(uuid.uuid4()),
        )
    except GenerationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {
        "conversation_id": response.conversation_id,
        "answer": response.answer,
        "provider": response.provider,
        "sources": [
            {
                "citation": source.citation,
                "source": source.source,
                "title": source.title,
                "page": source.page,
                "section": source.section,
                "topic": source.topic,
                "is_latest_version": source.is_latest_version,
            }
            for source in response.sources
        ],
    }
