"""Проверки диагностики: причины ошибок, корреляция и отсутствие утечек."""
import asyncio
import io
import json
import logging
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx

from app import ai, diagnostics as diag, storage as st, vectors
from tests import test_app as legacy


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SEARCH_MODE": "lexical"})
        self.env.start()
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            legacy.ApiTests.setUp(self)
        self.output.seek(0)
        self.output.truncate(0)
        with st.db() as con:
            con.execute("DELETE FROM audit_runs")

    def tearDown(self):
        legacy.ApiTests.tearDown(self)
        self.env.stop()

    def records(self, event=None):
        rows = [json.loads(s) for s in self.output.getvalue().splitlines()
                if s.startswith("{")]
        return [r for r in rows if event is None or r["event"] == event]

    def test_unexpected_error_keeps_stack_and_request_id_without_payload(self):
        ident = str(uuid4())
        secret = "private-document-and-password"
        with patch("app.main.st.rows", side_effect=RuntimeError(secret)):
            response = self.client.get("/kb/documents?private="+secret,
                                       auth=self.admin, headers={"X-Request-ID": ident})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["X-Request-ID"], ident)
        record = self.records("unexpected_error")[0]
        self.assertEqual(record["request_id"], ident)
        self.assertEqual(record["exceptions"][0]["type"], "RuntimeError")
        self.assertTrue(record["exceptions"][0]["frames"])
        self.assertNotIn(secret, self.output.getvalue())
        self.assertNotIn("test-admin", self.output.getvalue())
        audits = st.rows("SELECT * FROM audit_runs")
        self.assertEqual(json.loads(audits[0]["input"])["request_id"], ident)

    def test_audit_failure_does_not_hide_completed_response(self):
        with patch("app.main.st.audit", side_effect=sqlite3.OperationalError("private sql")):
            response = self.client.get("/kb/documents", auth=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(response.headers["X-Audit-Status"], "failed")
        record = self.records("audit_write_failed")[0]
        self.assertEqual(record["level"], "ERROR")
        self.assertEqual(record["exceptions"][0]["type"], "OperationalError")
        self.assertEqual(self.records("request_completed")[0]["audit_status"], "failed")
        self.assertNotIn("private sql", self.output.getvalue())

    def test_validation_and_auth_are_logged_without_input(self):
        secret = "private-title"
        response = self.client.post("/kb/documents", auth=self.admin,
                                    json={"title": secret, "text": ""})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.records("request_completed")[-1]["level"], "WARNING")
        self.client.get("/kb/documents", auth=("admin", "private-password"))
        self.assertEqual(self.records("request_completed")[-1]["http_status"], 401)
        self.assertNotIn(secret, self.output.getvalue())
        self.assertNotIn("private-password", self.output.getvalue())

    def test_unknown_path_and_query_are_not_written_to_technical_log(self):
        response = self.client.get("/private-secret?token=private-secret", auth=self.admin)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.records("request_completed")[-1]["route"], "<unmatched>")
        self.assertNotIn("private-secret", self.output.getvalue())

    def test_generated_id_links_answer_audit_and_diagnostics(self):
        response = self.client.post("/kb/ask", auth=self.staff,
                                    json={"question": "private-question"})
        ident = response.headers["X-Request-ID"]
        self.assertEqual(response.json()["qa_id"], ident)
        self.assertEqual(self.records("answer_completed")[0]["request_id"], ident)
        self.assertEqual(self.records("answer_completed")[0]["status"], "needs_review")
        self.assertNotIn("private-question", self.output.getvalue())
        self.assertIsNone(diag.request_id.get())

    def test_concurrent_requests_have_separate_ids(self):
        identifiers = [str(uuid4()), str(uuid4())]

        async def delayed(*args, **kwargs):
            before = diag.request_id.get()
            await asyncio.sleep(0.02)
            self.assertEqual(diag.request_id.get(), before)
            diag.log(logging.INFO, "concurrency_probe", component="ai")
            value, reason = ai.fallback("Тест")
            return value, reason, {}

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app),
                                         base_url="http://testserver") as client:
                return await asyncio.gather(*[
                    client.post("/ai/answer_with_sources",
                                headers={**self.client.auth_headers(self.staff), "X-Request-ID": ident},
                                json={"question": "private", "context": "private"})
                    for ident in identifiers
                ])
        with patch("app.main.ai.answer", new=delayed):
            responses = asyncio.run(run())
        self.assertEqual([r.headers["X-Request-ID"] for r in responses], identifiers)
        self.assertEqual({r["request_id"] for r in self.records("concurrency_probe")},
                         set(identifiers))
        self.assertEqual({r["request_id"] for r in self.records("request_completed")},
                         set(identifiers))
        self.assertIsNone(diag.request_id.get())

    def test_model_connection_chain_does_not_expose_error_message(self):
        secret = "private-key-body-and-context"

        def failed(request):
            try:
                raise OSError(5, secret)
            except OSError as exc:
                raise httpx.ConnectError(secret, request=request) from exc

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
                return await ai.answer(secret, secret, client=client)
        with patch.dict(os.environ, {"PROXYAPI_API_KEY": secret}):
            result, error, meta = asyncio.run(run())
        self.assertTrue(result.needs_review)
        self.assertTrue(meta["technical_error"])
        record = self.records("model_failed")[0]
        self.assertEqual([r["type"] for r in record["exceptions"]], ["ConnectError", "OSError"])
        self.assertEqual(record["exceptions"][1]["errno"], 5)
        self.assertNotIn(secret, self.output.getvalue())

    def test_embedding_http_error_keeps_status_without_credentials(self):
        secret = "private-embedding-key"

        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(
                401, json={"detail": secret}))
            async with httpx.AsyncClient(transport=transport) as client:
                index = vectors.VectorIndex(client)
                with self.assertRaises(vectors.IndexFailure):
                    await index.embed([secret])
        with patch.dict(os.environ, {"PROXYAPI_API_KEY": secret}):
            asyncio.run(run())
        record = self.records("embeddings_failed")[0]
        self.assertEqual(record["service"], "proxyapi")
        self.assertEqual(record["exceptions"][0]["http_status"], 401)
        self.assertNotIn(secret, self.output.getvalue())

    def test_malformed_model_response_has_diagnostics_without_body(self):
        secret = "private-model-output"

        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": secret}}]}))
            async with httpx.AsyncClient(transport=transport) as client:
                return await ai.answer("private-question", "private-context", client=client)
        with patch.dict(os.environ, {"PROXYAPI_API_KEY": "test-key"}):
            result, error, meta = asyncio.run(run())
        self.assertTrue(meta["technical_error"])
        record = self.records("model_failed")[0]
        self.assertEqual(record["reason_code"], "invalid_response")
        self.assertEqual(record["exceptions"][0]["type"], "JSONDecodeError")
        for value in (secret, "test-key", "private-question", "private-context"):
            self.assertNotIn(value, self.output.getvalue())

    def test_import_timeout_does_not_write_file_or_command(self):
        import subprocess
        with patch("app.main.importer.extract", side_effect=subprocess.TimeoutExpired(
                ["tesseract", "private-file.pdf"], 30, output=b"private-text")):
            response = self.client.post("/kb/documents/upload", auth=self.admin,
                                        files={"file": ("private-file.pdf", b"private-file")})
        self.assertEqual(response.status_code, 422)
        record = self.records("document_import_failed")[0]
        self.assertEqual(record["exceptions"][0]["type"], "TimeoutExpired")
        for value in ("private-file", "private-text"):
            self.assertNotIn(value, self.output.getvalue())

    def test_startup_failure_is_logged_before_propagating(self):
        from fastapi.testclient import TestClient
        from app.main import app
        output = io.StringIO()
        with redirect_stdout(output), patch("app.main.st.initialize", side_effect=OSError(5, "private-path")):
            with self.assertRaises(OSError):
                with TestClient(app):
                    pass
        records = [json.loads(s) for s in output.getvalue().splitlines()]
        failed = next(r for r in records if r["event"] == "application_failed")
        self.assertEqual(failed["level"], "ERROR")
        self.assertEqual(failed["exceptions"][0]["errno"], 5)
        self.assertNotIn("private-path", output.getvalue())

    def test_third_party_messages_and_unapproved_fields_are_discarded(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}), redirect_stdout(self.output):
            diag.configure()
        self.assertEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)
        logging.getLogger("library").warning("Bearer private-secret, body=private-document")
        diag.log(logging.WARNING, "safe_event", component="ai", password="private-secret",
                 question="private-question", text="private-document")
        self.assertNotIn("private-secret", self.output.getvalue())
        self.assertNotIn("private-document", self.output.getvalue())
        self.assertNotIn("private-question", self.output.getvalue())
        self.assertTrue(self.records("library_event"))
        with patch.dict(os.environ, {"LOG_LEVEL": "ERROR"}), redirect_stdout(self.output):
            diag.configure()
        diag.log(logging.INFO, "level_filtered")
        self.assertFalse(self.records("level_filtered"))


if __name__ == "__main__":
    unittest.main()
