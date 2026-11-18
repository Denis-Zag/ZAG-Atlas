"""Хранение данных одной компании и аудит команд."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
DATA = Path(os.getenv("ATLAS_DATA_DIR", str(ROOT / "data")))
DEFAULTS = {"company_name": "", "company_description": "",
            "MAX_TITLE_CHARS": 200, "MAX_UPLOAD_MB": 10,
            "MAX_DOCUMENT_CHARS": 100000, "MAX_QUESTION_CHARS": 2000,
            "MAX_CONTEXT_CHARS": 20000, "SESSION_IDLE_MINUTES": 30, "SESSION_MAX_HOURS": 8}


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


@contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "atlas.sqlite3", timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def initialize():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
          text TEXT NOT NULL, created_by TEXT NOT NULL, filename TEXT,
          method TEXT NOT NULL, warnings TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snippets(
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
          document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          snippet_text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_snippets_document ON snippets(document_id);
        CREATE TABLE IF NOT EXISTS qa_runs(
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, question TEXT NOT NULL,
          answer TEXT NOT NULL, sources_json TEXT NOT NULL,
          needs_review INTEGER NOT NULL, error TEXT,
          confidence TEXT NOT NULL, created_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_runs(
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL,
          input TEXT, output TEXT, status TEXT NOT NULL, error TEXT,
          duration_ms INTEGER NOT NULL);
        """)


def settings():
    path = DATA / "settings.json"
    if not path.exists():
        return DEFAULTS.copy()
    return {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}


def save_settings(values):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "settings.json"
    temp = DATA / ("settings-" + uid() + ".tmp")
    temp.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def rows(sql, params=()):
    with db() as con:
        return [dict(row) for row in con.execute(sql, params)]


def audit(action, inp, out, status, error, duration):
    with db() as con:
        con.execute("INSERT INTO audit_runs VALUES(?,?,?,?,?,?,?,?)",
                    (uid(), now(), action, json.dumps(inp, ensure_ascii=False),
                     json.dumps(out, ensure_ascii=False), status, error, duration))
