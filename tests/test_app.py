"""Проверки контракта, прав, источников и сохранения данных."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["ADMIN_PASSWORD"] = "test-admin"
os.environ["STAFF_PASSWORD"] = "test-staff"
os.environ["PROXYAPI_API_KEY"] = ""
from fastapi.testclient import TestClient
from app import ai, auth as accounts, search, storage as st
from tests.session_client import SessionClient
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


class QualityTests(unittest.TestCase):
    def payload(self, **changes):
        return {"answer": "Нужно уточнить адрес.",
                "sources": [{"quote": "уточнить адрес"}],
                "confidence": "high", "needs_review": False, **changes}

    def test_invented_quote_rejected(self):
        answer, reason = ai.validate_answer(self.payload(), "Другой материал")
        self.assertTrue(answer.needs_review)
        self.assertEqual(answer.sources, [])
        self.assertTrue(reason)

    def test_sources_required(self):
        result, _ = ai.validate_answer(self.payload(sources=[]), "уточнить адрес")
        self.assertTrue(result.needs_review)

    def test_low_confidence_forces_review(self):
        result, _ = ai.validate_answer(self.payload(confidence="low"), "уточнить адрес")
        self.assertTrue(result.needs_review)

    def test_grounded_quote_accepted(self):
        result, reason = ai.validate_answer(self.payload(), "Нужно уточнить адрес.")
        self.assertFalse(result.needs_review)
        self.assertIsNone(reason)

    def test_empty_context_no_external_call(self):
        result, reason, meta = asyncio.run(ai.answer("Вопрос", ""))
        self.assertTrue(result.needs_review)
        self.assertNotIn("technical_error", meta)

    def test_missing_key_is_technical_error(self):
        result, reason, meta = asyncio.run(ai.answer("Вопрос", "Нужный контекст"))
        self.assertTrue(meta["technical_error"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.hash_patch = patch("app.auth.ITERATIONS", 1000)
        self.hash_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = st.DATA
        st.DATA = Path(self.temp.name)
        self.client = SessionClient(app)
        self.client.__enter__()
        self.admin = ("admin", "test-admin")
        self.staff = ("staff", "test-staff")
        with st.db() as con:
            con.execute("UPDATE users SET must_change_password=0 WHERE username='admin'")
            con.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                        ("staff", st.now(), "Тестовый сотрудник", "staff", accounts.password_hash("test-staff"), 0, 1))
        for credentials in (self.admin, self.staff):
            response = self.client.post("/auth/login", json={"username": credentials[0], "password": credentials[1]})
            token = response.cookies.get(accounts.COOKIE)
            self.client.tokens[credentials] = (token, accounts.csrf(token))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        st.DATA = self.old_data
        self.temp.cleanup()
        self.hash_patch.stop()

    def add(self, text="При создании заявки менеджер уточняет адрес."):
        return self.client.post("/kb/documents", auth=self.admin,
                                json={"title": "Инструкция", "text": text})

    def test_access_enforced(self):
        self.assertEqual(self.client.get("/kb/documents").status_code, 401)
        self.assertEqual(self.client.post("/kb/documents", auth=self.staff,
                         json={"title": "А", "text": "Б"}).status_code, 403)

    def test_invalid_input_audited(self):
        self.assertEqual(self.client.post("/kb/documents", auth=self.admin,
                         json={"title": " ", "text": "x"}).status_code, 422)
        logs = self.client.get("/audit/runs", auth=self.admin).json()
        self.assertTrue(any(r["status"] == "error" for r in logs))

    def test_audit_question_filter_precedes_limit(self):
        # Новые служебные проверки не должны скрывать старый вопрос из фильтра.
        response = self.client.post("/kb/ask", auth=self.admin,
                                    json={"question": "Есть ли инструкция?"})
        self.assertTrue(response.json()["needs_review"])
        question_run = st.rows("SELECT id FROM audit_runs WHERE action='POST /kb/ask'")[0]
        with st.db() as con:
            con.executemany("INSERT INTO audit_runs VALUES(?,?,?,?,?,?,?,?)", [
                (st.uid(), "2099-01-01T00:00:00+00:00", "GET /health",
                 "{}", "{}", "ok", None, 1) for _ in range(110)
            ])
        all_runs = self.client.get("/audit/runs", auth=self.admin).json()
        self.assertEqual(len(all_runs), 100)
        self.assertTrue(all(row["action"] == "GET /health" for row in all_runs))
        filtered = self.client.get("/audit/runs?questions_only=true", auth=self.admin).json()
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], question_run["id"])
        self.assertEqual(filtered[0]["status"], "needs_review")
        self.assertEqual(self.client.get("/audit/runs?questions_only=true",
                                        auth=self.staff).status_code, 403)

    def test_document_provenance_and_snippets(self):
        response = self.add()
        self.assertEqual(response.status_code, 200)
        doc = self.client.get("/kb/documents/"+response.json()["document_id"],
                              auth=self.staff).json()
        self.assertEqual(doc["created_by"], "admin")
        self.assertTrue(st.rows("SELECT * FROM snippets"))

    def test_grounded_answer_and_history(self):
        doc = self.add().json()["document_id"]
        parsed = ai.Answer(answer="Уточните адрес.",
                          sources=[ai.Quote(quote="менеджер уточняет адрес")],
                          confidence="high", needs_review=False)
        with patch("app.main.ai.answer", new=AsyncMock(return_value=(parsed, None, {}))):
            r = self.client.post("/kb/ask", auth=self.staff,
                                 json={"question": "Как уточнить адрес заявки?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sources"][0]["document_id"], doc)
        self.assertFalse(r.json()["needs_review"])
        history = self.client.get("/kb/history", auth=self.staff).json()
        self.assertEqual(len(history), 1)
        logs = self.client.get("/audit/runs", auth=self.admin).json()
        self.assertTrue(any('qa_id' in r["output"] for r in logs))

    def test_no_documents_review_and_reason(self):
        result = self.client.post("/kb/ask", auth=self.staff,
                                  json={"question": "Как оформить заявку?"}).json()
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["sources"], [])
        history = self.client.get("/kb/history?review=true", auth=self.staff).json()
        self.assertTrue(history[0]["error"])

    def test_csv_columns_and_formula_escaping(self):
        self.client.post("/kb/ask", auth=self.staff,
                         json={"question": "=HYPERLINK(опасная формула)"})
        r = self.client.get("/kb/export?format=csv", auth=self.staff)
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("created_at;question;answer;needs_review;sources_json;error", r.text)
        self.assertIn("'=HYPERLINK", r.text)
        rows = self.client.get("/kb/export?format=json", auth=self.staff).json()
        self.assertEqual(len(rows), 1)
        self.assertIn("sources", rows[0])

    def test_txt_upload_preview_not_indexed_until_save(self):
        r = self.client.post("/kb/documents/upload", auth=self.admin,
                             files={"file": ("guide.txt", "Важный текст".encode())})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/kb/documents", auth=self.staff).json(), [])
        p = r.json()
        saved = self.client.post("/kb/documents", auth=self.admin, json={
            "title": p["title"], "text": p["text"], "import_id": p["import_id"]})
        self.assertEqual(saved.status_code, 200)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.client.post("/kb/ask", auth=self.staff,
                         headers={"Origin": "https://other.example"},
                         json={"question": "Вопрос"}).status_code, 403)

    def test_settings_persist_and_apply(self):
        cfg = st.settings()
        cfg.update(company_name="Учебная компания", MAX_TITLE_CHARS=3)
        self.assertEqual(self.client.put("/settings", auth=self.admin,
                                        json=cfg).status_code, 200)
        self.assertEqual(st.settings()["MAX_TITLE_CHARS"], 3)
        self.assertEqual(self.add().status_code, 422)

    def test_seven_known_questions_have_retrieval_candidates(self):
        documents = [json.loads(s) for s in
                     (ROOT/"tests_data/kb_documents.jsonl").read_text(encoding="utf-8").splitlines()]
        snippets = [{"document_id": d["title"], "snippet_text": part}
                    for d in documents for part in search.split_text(d["text"])]
        questions = [json.loads(s) for s in
                     (ROOT/"tests_data/kb_questions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(documents), 5)
        self.assertEqual(len(questions), 10)
        for d in documents:
            self.assertTrue(20 <= len(d["text"].splitlines()) <= 50)
        for q in questions:
            if not q["expected_needs_review"]:
                found = search.retrieve(q["question"], snippets)
                self.assertTrue(found, q["question"])
                self.assertIn(q["why"], [s["document_id"] for s in found], q["question"])


if __name__ == "__main__":
    unittest.main()
