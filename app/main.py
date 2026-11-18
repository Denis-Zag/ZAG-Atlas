"""HTTP API и веб-панель ZAG Atlas."""
import csv
import io
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from . import ai, auth, diagnostics as diag, importer, search, vectors, storage as st


@asynccontextmanager
async def lifespan(app):
    diag.configure()
    try:
        st.initialize()
        auth.initialize()
        async with ai.make_client() as client:
            app.state.llm_client = client
            app.state.vector_index = vectors.VectorIndex(client) if vectors.enabled() else None
            diag.log(logging.INFO, "application_started", component="lifecycle",
                     search_mode="hybrid" if vectors.enabled() else "lexical")
            yield
    except Exception as exc:
        diag.log(logging.ERROR, "application_failed", component="lifecycle", exception=exc)
        raise
    finally:
        diag.log(logging.INFO, "application_stopped", component="lifecycle")


app = FastAPI(title="ZAG Atlas", version="0.1.0", lifespan=lifespan)
app.include_router(auth.router)
user = auth.user
admin = auth.admin
app.mount("/static", StaticFiles(directory=st.ROOT / "static"), name="static")


def checked(value, key, label):
    limit = st.settings()[key]
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, f"{label}: заполните поле")
    if len(value) > limit:
        raise HTTPException(422, f"{label}: {len(value)} символов, разрешено {limit}. "
                            f"Администратор: Настройки → Ограничения → {key}")
    return value.strip()


def redact(value):
    if isinstance(value, dict):
        return {k: ("***" if any(x in k.lower() for x in
                                ("password", "secret", "token", "api_key"))
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@app.middleware("http")
async def journal(request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
    start = time.perf_counter()
    inp = {"query": dict(request.query_params)}
    response = None
    error = None
    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                raise HTTPException(403, "Межсайтовый запрос запрещён")
            maximum = int(st.settings()["MAX_UPLOAD_MB"] * 1024 * 1024) + 65536
            chunks, length = [], 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > maximum:
                    raise HTTPException(413, "Размер запроса превышает MAX_UPLOAD_MB. "
                                        "Администратор: Настройки → Ограничения")
                chunks.append(chunk)
            request._body = b"".join(chunks)
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    inp = redact(json.loads(request._body))
                except ValueError:
                    inp = {"error": "Некорректный JSON"}
            else:
                inp = {"bytes": length, "type": request.headers.get("content-type", "")}
        response = await call_next(request)
    except HTTPException as exc:
        error = str(exc.detail)
        response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except Exception as exc:
        diag.log(logging.ERROR, "unexpected_error", component="api", exception=exc)
        error = "Непредвиденная ошибка сервера"
        response = JSONResponse({"detail": error}, status_code=500)
    error = getattr(request.state, "error", error)
    if response.status_code >= 400 and not error:
        error = "HTTP " + str(response.status_code)
    status = "error" if error and not getattr(request.state, "review", False) else (
        "needs_review" if getattr(request.state, "review", False) else "ok")
    request.state.audit_status = "ok"
    try:
        st.audit(request.method+" "+request.url.path,
                 {"actor": getattr(request.state, "actor", None), "data": inp,
                  "request_id": request.state.request_id},
                 getattr(request.state, "result", {"http_status": response.status_code}),
                 status, error, round((time.perf_counter()-start)*1000))
    except Exception as exc:
        # Операция могла уже сохраниться: не теряем её ответ и не провоцируем повтор.
        request.state.audit_status = "failed"
        response.headers["X-Audit-Status"] = "failed"
        diag.log(logging.ERROR, "audit_write_failed", component="storage", exception=exc)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response


@app.middleware("http")
async def request_diagnostics(request, call_next):
    supplied = request.headers.get("X-Request-ID")
    try:
        ident = str(UUID(supplied)) if supplied else st.uid()
    except ValueError:
        ident = st.uid()
    request.state.request_id = ident
    token = diag.request_id.set(ident)
    start = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            diag.log(logging.ERROR, "request_failed", component="api", exception=exc)
            response = JSONResponse({"detail": "Непредвиденная ошибка сервера"}, status_code=500)
        response.headers["X-Request-ID"] = ident
        review = getattr(request.state, "review", False)
        audit_status = getattr(request.state, "audit_status", "not_required")
        technical = bool(getattr(request.state, "error", None)) and not review
        level = (logging.ERROR if response.status_code >= 500 or audit_status == "failed"
                 or (technical and response.status_code < 400) else logging.WARNING
                 if response.status_code >= 400 or review else logging.INFO)
        # Не пишем каждый успешный healthcheck и запрос статики в технический поток.
        if level != logging.INFO or (request.url.path != "/health"
                                     and not request.url.path.startswith("/static/")):
            route = request.scope.get("route")
            diag.log(level, "request_completed", component="api", method=request.method,
                     route=getattr(route, "path", "<unmatched>"),
                     http_status=response.status_code, audit_status=audit_status,
                     status="error" if level == logging.ERROR else
                            "needs_review" if review else
                            "rejected" if response.status_code >= 400 else "ok",
                     duration_ms=round((time.perf_counter()-start)*1000))
        return response
    finally:
        diag.request_id.reset(token)


@app.exception_handler(RequestValidationError)
async def validation(request, exc):
    request.state.error = "Ошибка проверки входных данных"
    return JSONResponse({"detail": "Проверьте поля и типы данных запроса"}, status_code=422)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentInput(Input):
    title: str
    text: str
    import_id: str | None = None


class QuestionInput(Input):
    question: str


class ContextInput(QuestionInput):
    context: str


class SettingsInput(Input):
    company_name: str
    company_description: str
    MAX_TITLE_CHARS: int
    MAX_UPLOAD_MB: int
    MAX_DOCUMENT_CHARS: int
    MAX_QUESTION_CHARS: int
    MAX_CONTEXT_CHARS: int
    SESSION_IDLE_MINUTES: int = 30
    SESSION_MAX_HOURS: int = 8


# Предпросмотр хранится на диске, чтобы не доверять метаданным от браузера.
def import_path(token):
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise HTTPException(422, "Неверный идентификатор импорта")
    return st.DATA / "previews" / (token + ".json")


@app.get("/", response_class=HTMLResponse)
def home():
    return (st.ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/kb/documents/upload")
async def upload(file: UploadFile = File(...), actor=Depends(admin)):
    cfg = st.settings()
    data = await file.read(cfg["MAX_UPLOAD_MB"] * 1024 * 1024 + 1)
    await file.close()
    if len(data) > cfg["MAX_UPLOAD_MB"] * 1024 * 1024:
        raise HTTPException(413, "Превышен MAX_UPLOAD_MB. Администратор: Настройки → Ограничения")
    try:
        text, method, warnings = await run_in_threadpool(
            importer.extract, data, file.filename or "")
    except Exception as exc:
        diag.log(logging.WARNING, "document_import_failed", component="importer",
                 operation="extract", exception=exc)
        detail = str(exc) if isinstance(exc, (ValueError, UnicodeError)) else (
            "Не удалось прочитать документ. Проверьте формат и целостность файла")
        raise HTTPException(422, detail) from None
    checked(text, "MAX_DOCUMENT_CHARS", "Текст документа")
    diag.log(logging.INFO, "document_imported", component="importer",
             import_method=method, characters=len(text))
    token = secrets.token_hex(32)
    path = import_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Временные предпросмотры автоматически удаляются через час.
    for old in path.parent.glob("*.json"):
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    path.write_text(json.dumps({"text": text, "filename": file.filename,
                               "method": method, "warnings": warnings,
                               "owner": actor}, ensure_ascii=False), encoding="utf-8")
    return {"title": file.filename, "text": text, "import_id": token,
            "method": method, "warnings": warnings}


@app.post("/kb/documents")
async def add_document(payload: DocumentInput, request: Request, actor=Depends(admin)):
    title = checked(payload.title, "MAX_TITLE_CHARS", "Название")
    text = checked(payload.text, "MAX_DOCUMENT_CHARS", "Текст документа")
    imported = None
    path = None
    if payload.import_id:
        path = import_path(payload.import_id)
        if not path.exists() or time.time() - path.stat().st_mtime > 3600:
            raise HTTPException(422, "Предпросмотр истёк. Загрузите файл повторно")
        imported = json.loads(path.read_text(encoding="utf-8"))
        if imported["owner"] != actor:
            raise HTTPException(403, "Чужой предпросмотр")
    ident, date = st.uid(), st.now()
    with st.db() as con:
        con.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)",
                    (ident, date, title, text, actor,
                     imported["filename"] if imported else None,
                     imported["method"] if imported else "note",
                     json.dumps(imported["warnings"] if imported else [], ensure_ascii=False)))
        con.executemany("INSERT INTO snippets VALUES(?,?,?,?)",
                        [(st.uid(), date, ident, part) for part in search.split_text(text)])
    if path:
        path.unlink(missing_ok=True)
    result = {"status": "ok", "document_id": ident}
    if vectors.enabled():
        try:
            result["index"] = await request.app.state.vector_index.sync()
        except Exception as exc:
            diag.log(logging.ERROR, "index_sync_failed", component="vectors",
                     operation="add_document", document_id=ident, exception=exc)
            result["index_status"] = "pending"
            result["index_error"] = vectors.safe_error(exc)
            request.state.error = result["index_error"]
    request.state.result = result
    return result


@app.get("/kb/documents")
def documents(actor=Depends(user)):
    return st.rows("SELECT id,title,created_at,created_by,filename,method,warnings "
                   "FROM documents ORDER BY created_at DESC")


@app.get("/kb/documents/{ident}")
def document(ident: str, actor=Depends(user)):
    result = st.rows("SELECT * FROM documents WHERE id=?", (ident,))
    if not result:
        raise HTTPException(404, "Документ не найден")
    return result[0]


@app.delete("/kb/documents/{ident}")
async def delete_document(ident: str, request: Request, actor=Depends(admin)):
    with st.db() as con:
        count = con.execute("DELETE FROM documents WHERE id=?", (ident,)).rowcount
    if not count:
        raise HTTPException(404, "Документ не найден")
    if vectors.enabled():
        try:
            await request.app.state.vector_index.sync()
        except Exception as exc:
            diag.log(logging.ERROR, "index_sync_failed", component="vectors",
                     operation="delete_document", document_id=ident, exception=exc)
            request.state.error = vectors.safe_error(exc)
    return {"status": "ok"}


@app.post("/ai/answer_with_sources")
async def ai_answer(payload: ContextInput, request: Request, actor=Depends(user)):
    question = checked(payload.question, "MAX_QUESTION_CHARS", "Вопрос")
    context = payload.context
    if len(context) > st.settings()["MAX_CONTEXT_CHARS"]:
        raise HTTPException(422, "Превышен MAX_CONTEXT_CHARS")
    answer, error, meta = await ai.answer(question, context, client=request.app.state.llm_client)
    request.state.error = error
    request.state.review = answer.needs_review and not meta.get("technical_error")
    request.state.result = {"answer": answer.model_dump(), "meta": meta}
    return answer


@app.post("/kb/ask")
async def ask(payload: QuestionInput, request: Request, actor=Depends(user)):
    question = checked(payload.question, "MAX_QUESTION_CHARS", "Вопрос")
    supplied_id = request.headers.get("X-Request-ID")
    try:
        ident = str(UUID(supplied_id)) if supplied_id else request.state.request_id
    except ValueError:
        raise HTTPException(422, "Неверный идентификатор запроса") from None
    previous = st.rows("SELECT * FROM qa_runs WHERE id=?", (ident,))
    if previous:
        row = previous[0]
        if row["question"] != question or row["created_by"] != actor:
            raise HTTPException(409, "Идентификатор уже использован другим вопросом")
        request.state.error = row["error"]
        technical = bool(row["error"]) and row["answer"].startswith((
            "Поиск временно недоступен.", "Сервис ИИ временно недоступен."))
        result = {"answer": row["answer"], "sources": json.loads(row["sources_json"]),
                  "needs_review": bool(row["needs_review"]), "error": row["error"],
                  "technical_error": technical, "qa_id": ident}
        request.state.review = result["needs_review"] and not technical
        request.state.result = {**result, "recovered": True}
        diag.log(logging.INFO, "answer_recovered", component="api", qa_id=ident, recovered=True)
        return result
    search_meta, search_error = {}, None
    if vectors.enabled():
        try:
            found, search_meta = await request.app.state.vector_index.retrieve(
                question, st.settings()["MAX_CONTEXT_CHARS"])
        except Exception as exc:
            diag.log(logging.ERROR, "search_failed", component="vectors",
                     qa_id=ident, search_mode="hybrid", exception=exc)
            found, search_error = [], vectors.safe_error(exc)
    else:
        found = search.retrieve(question, st.rows(
            "SELECT snippets.*,documents.title FROM snippets JOIN documents "
            "ON documents.id=snippets.document_id"), st.settings()["MAX_CONTEXT_CHARS"])
    context = "\n\n".join(s["snippet_text"] for s in found)
    if search_error:
        answer, error = ai.fallback(search_error)
        answer.answer = "Поиск временно недоступен. Повторите вопрос позже или обратитесь к администратору."
        meta = {"technical_error": True, "search_mode": "hybrid"}
    else:
        answer, error, meta = await ai.answer(question, context, client=request.app.state.llm_client)
    meta.update(search_meta)
    sources = []
    if answer.sources:
        for source in answer.sources:
            matches = [s for s in found if source.quote in s["snippet_text"]]
            if not matches:
                answer, error = ai.fallback("Цитата не принадлежит найденному фрагменту")
                sources = []
                break
            for snippet in matches:
                item = {"document_id": snippet["document_id"], "quote": source.quote}
                if item not in sources:
                    sources.append(item)
    if not sources and not answer.needs_review:
        answer, error = ai.fallback("Нет подтверждённых источников")
    result = {"answer": answer.answer, "sources": sources,
              "needs_review": answer.needs_review, "error": error,
              "technical_error": bool(meta.get("technical_error")), "qa_id": ident}
    with st.db() as con:
        con.execute("INSERT INTO qa_runs VALUES(?,?,?,?,?,?,?,?,?)",
                    (ident, st.now(), question, answer.answer,
                     json.dumps(sources, ensure_ascii=False), int(answer.needs_review),
                     error, answer.confidence, actor))
    request.state.error = error
    request.state.review = answer.needs_review and not meta.get("technical_error")
    request.state.result = {"qa_id": ident, **result, "meta": meta}
    diag.log(logging.ERROR if meta.get("technical_error") else logging.WARNING
             if answer.needs_review else logging.INFO, "answer_completed",
             component="api", qa_id=ident, source_count=len(sources),
             search_mode="hybrid" if vectors.enabled() else "lexical",
             status="error" if meta.get("technical_error") else
                    "needs_review" if answer.needs_review else "ok")
    return result


@app.get("/kb/history")
def history(review: bool = False, actor=Depends(user)):
    return st.rows("SELECT * FROM qa_runs " +
                   ("WHERE needs_review=1 " if review else "") +
                   "ORDER BY created_at DESC LIMIT 100")


@app.get("/kb/history/{ident}")
def history_item(ident: str, actor=Depends(user)):
    result = st.rows("SELECT * FROM qa_runs WHERE id=?", (ident,))
    if not result:
        raise HTTPException(404, "Вопрос не найден")
    return result[0]


@app.get("/audit/runs")
def audits(questions_only: bool = False, actor=Depends(admin)):
    condition = "WHERE action=? " if questions_only else ""
    parameters = ("POST /kb/ask",) if questions_only else ()
    return st.rows("SELECT * FROM audit_runs " + condition +
                   "ORDER BY created_at DESC LIMIT 100", parameters)


@app.get("/settings")
def get_settings(actor=Depends(user)):
    return st.settings()


@app.put("/settings")
def put_settings(payload: SettingsInput, actor=Depends(admin)):
    values = payload.model_dump()
    if not values["company_name"].strip() or len(values["company_name"]) > 200:
        raise HTTPException(422, "Введите название компании до 200 символов")
    if len(values["company_description"]) > 2000:
        raise HTTPException(422, "Описание: не более 2000 символов")
    ceilings = {"MAX_TITLE_CHARS": 2000, "MAX_UPLOAD_MB": 50,
                "MAX_DOCUMENT_CHARS": 1000000, "MAX_QUESTION_CHARS": 10000,
                "MAX_CONTEXT_CHARS": 100000, "SESSION_IDLE_MINUTES": 1440, "SESSION_MAX_HOURS": 168}
    for key, ceiling in ceilings.items():
        if not 1 <= values[key] <= ceiling:
            raise HTTPException(422, f"{key}: допустимо от 1 до {ceiling}")
    if values["SESSION_IDLE_MINUTES"] > values["SESSION_MAX_HOURS"] * 60:
        raise HTTPException(422, "Бездействие не может превышать полное время сессии")
    st.save_settings(values)
    return values


def csv_safe(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


@app.get("/kb/export")
def export(format: str = "json", ident: str | None = None, actor=Depends(user)):
    records = st.rows("SELECT * FROM qa_runs " +
                      ("WHERE id=? " if ident else "") +
                      "ORDER BY created_at DESC", (ident,) if ident else ())
    if format == "json":
        for record in records:
            record["sources"] = json.loads(record.pop("sources_json"))
            record["needs_review"] = bool(record["needs_review"])
        return Response(json.dumps(records, ensure_ascii=False, indent=2),
                        media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="atlas-history.json"'})
    if format != "csv":
        raise HTTPException(422, "Выберите json или csv")
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    keys = ["created_at", "question", "answer", "needs_review", "sources_json", "error"]
    writer.writerow(keys)
    for record in records:
        writer.writerow([csv_safe(record[k]) for k in keys])
    return Response(out.getvalue().encode("utf-8-sig"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="atlas-history.csv"'})


@app.post("/kb/reindex")
async def reindex(request: Request, actor=Depends(admin)):
    if not vectors.enabled():
        raise HTTPException(409, "Векторный поиск выключен")
    try:
        result = await request.app.state.vector_index.sync()
        request.state.result = result
        return result
    except Exception as exc:
        diag.log(logging.ERROR, "index_sync_failed", component="vectors",
                 operation="reindex", exception=exc)
        raise HTTPException(503, vectors.safe_error(exc)) from None
