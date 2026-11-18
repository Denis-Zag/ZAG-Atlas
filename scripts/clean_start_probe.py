"""Проверка только отдельной установки, созданной verify_clean_start.ps1."""
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


BASE = "http://127.0.0.1:8000"
QDRANT = "http://qdrant:6333"
DATA = Path("/app/data/atlas.sqlite3")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def database_rows(sql, parameters=()):
    with sqlite3.connect(DATA.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def read_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def initial():
    require(os.environ.get("SEARCH_MODE") == "hybrid", "SEARCH_MODE is not hybrid")
    require(read_json(BASE + "/health")["status"] == "ok", "API is not ready")
    with urllib.request.urlopen(BASE + "/", timeout=15) as response:
        html = response.read().decode("utf-8")
        require("Atlas" in html, "Web page missing")
    resources = re.findall(r'(?:src|href)=[\"\'](/static/[^\"\']+)', html)
    require(resources, "No static resources in HTML")
    for resource in resources:
        with urllib.request.urlopen(BASE + resource, timeout=15) as response:
            require(response.status == 200, "Static resource missing")
    counts = {
        table: database_rows("SELECT count(*) AS n FROM " + table)[0]["n"]
        for table in ("documents", "snippets", "qa_runs", "embedding_cache", "users")
    }
    require(all(counts[name] == 0 for name in counts if name != "users"),
            "Initial database or embedding cache is not empty")
    require(counts["users"] == 1, "Unexpected initial accounts")
    collections = read_json(QDRANT + "/collections")["result"]["collections"]
    require(not collections, "Initial Qdrant is not empty")
    tools = {}
    for command in (["tesseract", "--list-langs"], ["pdftoppm", "-v"]):
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        require(result.returncode == 0, "OCR executable unavailable")
        tools[command[0]] = result.stdout + result.stderr
    require("rus" in tools["tesseract"] and "eng" in tools["tesseract"],
            "OCR languages missing")
    return {"passed": True, "initial_counts": counts, "initial_collections": 0,
            "web_and_static": True, "ocr_rus_eng_and_poppler": True,
            "search_mode": "hybrid"}


class Session:
    def __init__(self):
        self.cookies = CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf = ""
        self.logged_in = False

    def request(self, path, body=None, raw=False, method=None):
        headers = {"X-Atlas-Request": "1"}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(BASE + path, data=encoded, headers=headers, method=method)
        with self.client.open(request, timeout=300) as response:
            value = response.read()
            require(response.headers.get("X-Audit-Status") != "failed", "Audit write failed")
            return value if raw else json.loads(value)


def smoke():
    session = Session()
    try:
        login = session.request("/auth/login", {"username": "admin",
                                               "password": os.environ["ADMIN_PASSWORD"]})
        session.logged_in = True
        require(login["must_change_password"], "Initial password change was not required")
        session.csrf = session.request("/session")["csrf_token"]
        try:
            session.request("/kb/documents")
        except urllib.error.HTTPError as error:
            require(error.code == 403, "Unexpected initial access status")
        else:
            raise AssertionError("Initial password change restriction missing")
        session.request("/auth/change-password", {
            "current_password": os.environ["ADMIN_PASSWORD"],
            "new_password": secrets.token_urlsafe(28),
        })
        session.csrf = session.request("/session")["csrf_token"]
        require(session.request("/kb/documents") == [], "Initial document list is not empty")
        settings = session.request("/settings")
        settings.update(company_name="Проверка чистого запуска",
                        company_description="Отдельная временная учебная установка")
        session.request("/settings", settings, method="PUT")
        text = "При создании заявки менеджер уточняет адрес и вид работ."
        saved = session.request("/kb/documents", {"title": "Контрольная инструкция", "text": text})
        require(saved.get("status") == "ok" and saved.get("document_id"), "Document was not saved")
        require(saved.get("index_status") != "pending", "Document indexing failed")
        documents = session.request("/kb/documents")
        require(len(documents) == 1 and documents[0]["id"] == saved["document_id"],
                "Document list mismatch")
        known = session.request("/kb/ask", {
            "question": "Какие сведения менеджер уточняет при создании заявки?"})
        require(known["needs_review"] is False and not known.get("technical_error"),
                "Known question did not produce a confirmed answer")
        require(known["sources"], "Known question has no sources")
        for source in known["sources"]:
            require(source["document_id"] == saved["document_id"]
                    and source["quote"] and source["quote"] in text, "Invalid source")
        unknown = session.request("/kb/ask", {
            "question": "Какую персональную скидку директор согласовал клиенту Иванову?"})
        require(unknown["needs_review"] is True and not unknown.get("technical_error")
                and not unknown["sources"] and unknown.get("error"),
                "Unknown question was not handled as insufficient knowledge")
        history = session.request("/kb/history")
        require(len(history) == 2 and any(row["id"] == unknown["qa_id"]
                                        and row["error"] for row in history), "History mismatch")
        exported = session.request("/kb/export?format=json")
        require(len(exported) == 2, "JSON export mismatch")
        csv_text = session.request("/kb/export?format=csv", raw=True).decode("utf-8-sig")
        require(len(list(csv.DictReader(io.StringIO(csv_text), delimiter=";"))) == 2,
                "CSV export mismatch")
        audits = session.request("/audit/runs")
        question_runs = [row for row in audits if row["action"] == "POST /kb/ask"]
        require(len(question_runs) == 2, "Question audit missing")
        details = [json.loads(row["output"]) for row in question_runs]
        require(all(row["meta"]["search_mode"] == "hybrid" for row in details),
                "Question did not use hybrid retrieval")
        known_meta = next(row["meta"] for row in details if row["qa_id"] == known["qa_id"])
        require(known_meta.get("semantic_candidates", 0) > 0, "No semantic retrieval candidates")
        collections = read_json(QDRANT + "/collections")["result"]["collections"]
        require(len(collections) == 1, "Expected one indexed collection")
        collection = read_json(QDRANT + "/collections/" + collections[0]["name"])["result"]
        require(collection["points_count"] == 1, "Qdrant points mismatch")
        return {"passed": True, "login_password_change": True, "company_settings": True,
                "document_created_and_listed": True, "known_answer": known,
                "unknown_answer": unknown, "history": True, "audit": True,
                "json_and_csv": True, "search_mode": "hybrid", "qdrant_points": 1,
                "qa_meta": details}
    finally:
        if session.logged_in:
            session.request("/auth/logout", {})
            try:
                session.request("/session")
            except urllib.error.HTTPError as error:
                require(error.code == 401, "Session was not revoked")
            else:
                raise AssertionError("Logout did not revoke the session")


def main():
    started = time.monotonic()
    try:
        require(re.fullmatch(r"atlas-clean-\d{8}-\d{6}-[a-f0-9]{8}",
                             os.environ.get("ATLAS_CLEAN_START", "")),
                "Probe is restricted to the isolated clean-start procedure")
        require(len(sys.argv) == 2 and sys.argv[1] in {"initial", "smoke"}, "Invalid probe mode")
        result = initial() if sys.argv[1] == "initial" else smoke()
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        # Не печатаем тела внешних ответов, окружение, cookie или пароли.
        message = str(error) if isinstance(error, AssertionError) else type(error).__name__
        print(json.dumps({"passed": False, "error": message}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
