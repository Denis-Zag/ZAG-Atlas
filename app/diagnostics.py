"""Технические JSON-логи без содержимого запросов и секретов."""
import json
import logging
import os
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path


request_id = ContextVar("atlas_request_id", default=None)
logger = logging.getLogger("atlas")
FIELDS = {
    "component", "service", "operation", "method", "route", "http_status",
    "duration_ms", "status", "audit_status", "qa_id", "document_id",
    "search_mode", "reason_code", "source_count", "snippet_count", "characters",
    "pages", "ocr_pages", "import_method", "input_count", "recovered",
}


def exception_details(exc):
    """Сохраняем цепочку и стек, исключая сообщения, исходные строки и locals."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        frames = [
            {"file": Path(frame.f_code.co_filename).name,
             "function": frame.f_code.co_name, "line": line}
            for frame, line in list(traceback.walk_tb(exc.__traceback__))[-20:]
        ]
        item = {"type": type(exc).__name__, "frames": frames}
        for name in ("errno", "winerror", "verify_code"):
            value = getattr(exc, name, None)
            if isinstance(value, int):
                item[name] = value
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            item["http_status"] = status
        chain.append(item)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return chain


class JsonFormatter(logging.Formatter):
    def format(self, record):
        event = record.msg if record.name == "atlas" else "library_event"
        if not isinstance(event, str) or not re.fullmatch(r"[a-z_]+", event):
            event = "application_event"
        result = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": event,
            "request_id": request_id.get(),
        }
        # Поля задаются кодом приложения; тела запросов и произвольные сообщения
        # сторонних библиотек сюда не передаются даже при LOG_LEVEL=DEBUG.
        for key, value in getattr(record, "details", {}).items():
            if key in FIELDS and isinstance(value, (str, int, float, bool, type(None))):
                result[key] = value
        if record.name != "atlas":
            result["component"] = record.name
        if record.exc_info and record.exc_info[1] is not None:
            result["exceptions"] = exception_details(record.exc_info[1])
        return json.dumps(result, ensure_ascii=False, allow_nan=False)


def configure():
    """Выводим диагностику в stdout; хранение и ротацию выполняет Docker."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logger.setLevel(level)
    # HTTP-библиотеки на DEBUG могут писать сетевые сведения. Уровень приложения
    # меняется отдельно от их подробности.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def log(level, event, *, exception=None, **details):
    info = (type(exception), exception, exception.__traceback__) if exception else None
    logger.log(level, event, extra={"details": details}, exc_info=info)
