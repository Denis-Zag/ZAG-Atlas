"""Причины ручной проверки, проверенные частичные ответы и их сохранность."""
import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import ai, auth, storage as st
from app.main import app
from tests.session_client import SessionClient


PRICE = "Базовая стоимость стандартного монтажа составляет 12000 рублей."
GAP = "Наличие, срок и условия гарантии на монтаж"
QUESTION = "Сколько стоит стандартный монтаж и есть ли гарантия?"


def payload(**changes):
    return {"answer": "Модель могла добавить сюда неподтверждённый текст.",
            "sources": [{"quote": PRICE}], "confidence": "low", "needs_review": True,
            "review_details": {"kind": "partial", "missing_information": [GAP]}, **changes}


class ReviewValidationTests(unittest.TestCase):
    def test_partial_answer_contains_only_exact_confirmed_quotes(self):
        result, reason = ai.validate_answer(payload(answer="Гарантия пять лет."), PRICE)
        self.assertTrue(result.needs_review)
        self.assertIn(PRICE, result.answer)
        self.assertIn(GAP, result.answer)
        self.assertIn("Следующий шаг:", result.answer)
        self.assertNotIn("Гарантия пять лет", result.answer)
        self.assertIn("В найденных материалах не подтверждено", reason)
        self.assertEqual([source.quote for source in result.sources], [PRICE])

    def test_forged_partial_quote_discards_all_claims(self):
        result, reason = ai.validate_answer(payload(sources=[{"quote": "Гарантия пять лет."}]), PRICE)
        self.assertEqual(result.sources, [])
        self.assertTrue(result.answer.startswith(ai.SAFE))
        self.assertNotIn("Гарантия пять лет", result.answer)
        self.assertIn("Цитата", reason)

    def test_conflicting_quotes_are_not_presented_as_confirmed_price(self):
        result, reason = ai.validate_answer(payload(review_details={
            "kind": "conflict", "missing_information": ["Актуальная цена и приоритет документов"]}), PRICE)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.sources, [])
        self.assertNotIn(PRICE, result.answer)
        self.assertIn("противоречие", reason)
        self.assertIn("приоритет", result.answer)

    def test_conflict_without_gap_names_still_explains_source_priority(self):
        result, reason = ai.validate_answer(payload(review_details={
            "kind": "conflict", "missing_information": []}), PRICE)
        self.assertEqual(result.sources, [])
        self.assertIn("противоречие", reason)
        self.assertIn("приоритет", result.answer)
        self.assertIn("Следующий шаг:", result.answer)

    def test_review_details_override_incorrect_success_flag(self):
        result, _ = ai.validate_answer(payload(confidence="high", needs_review=False), PRICE)
        self.assertTrue(result.needs_review)

    def test_unknown_answer_has_specific_gap_without_confirmed_facts(self):
        result, reason = ai.validate_answer(payload(sources=[]), PRICE)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.sources, [])
        self.assertNotIn("Подтверждено источниками:", result.answer)
        self.assertIn(GAP, reason)
        self.assertNotIn("гарантии нет", result.answer.lower())

    def test_absence_quote_is_not_a_confirmed_answer_to_unknown_question(self):
        absence = "Персональные скидки в материалах не указаны."
        result, _ = ai.validate_answer(payload(sources=[{"quote": absence}], review_details={
            "kind": "missing_data", "missing_information": ["Размер скидки Иванову"]}), absence)
        self.assertEqual(result.sources, [])
        self.assertNotIn("Подтверждено источниками:", result.answer)
        self.assertIn("Размер скидки Иванову", result.answer)

    def test_partial_without_sources_is_treated_as_missing_data(self):
        result, _ = ai.validate_answer(payload(sources=[]), PRICE)
        self.assertEqual(result._review_details["kind"], "missing_data")

    def test_inconsistent_none_kind_keeps_specific_gap_but_drops_facts(self):
        result, error = ai.validate_answer(payload(needs_review=False, confidence="high",
            review_details={"kind": "none", "missing_information": [GAP]}), PRICE)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.sources, [])
        self.assertIn(GAP, error)

    def test_empty_details_do_not_allow_partial_claims(self):
        result, _ = ai.validate_answer(payload(review_details={
            "kind": "missing_data", "missing_information": [" "]}), PRICE)
        self.assertEqual(result.sources, [])
        self.assertTrue(result.answer.startswith(ai.SAFE))
        self.assertNotIn(PRICE, result.answer)
        self.assertIn("Следующий шаг:", result.answer)

    def test_fully_grounded_result_keeps_public_four_field_contract(self):
        result, error = ai.validate_answer(payload(
            answer="Монтаж стоит 12000 рублей.", confidence="high", needs_review=False,
            review_details={"kind": "none", "missing_information": []}), PRICE)
        self.assertFalse(result.needs_review)
        self.assertIsNone(error)
        self.assertEqual(set(result.model_dump()), {"answer", "sources", "confidence", "needs_review"})

    def test_model_schema_requires_review_details_but_public_schema_does_not(self):
        schema = ai.ModelAnswer.model_json_schema()
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("review_details", ai.Answer.model_json_schema()["properties"])

    def test_model_metadata_preserves_specific_reason(self):
        captured = {}

        def reply(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(payload())}}]})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                return await ai.answer(QUESTION, PRICE, client=client)
        with patch.dict(os.environ, {"PROXYAPI_API_KEY": "test-key"}):
            result, error, meta = asyncio.run(run())
        self.assertEqual(meta["review_details"]["reason"], error)
        self.assertEqual(meta["review_details"]["missing_information"], [GAP])
        self.assertTrue(captured["response_format"]["json_schema"]["strict"])
        self.assertIn("не означает", captured["messages"][0]["content"].lower())
        self.assertTrue(result.needs_review)

    def test_technical_failure_has_no_partial_facts_or_knowledge_gap(self):
        def failed(request):
            raise httpx.ConnectError("private-error", request=request)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
                return await ai.answer(QUESTION, PRICE, client=client)
        with patch.dict(os.environ, {"PROXYAPI_API_KEY": "test-key"}):
            result, error, meta = asyncio.run(run())
        self.assertTrue(meta["technical_error"])
        self.assertNotIn("review_details", meta)
        self.assertEqual(result.sources, [])
        self.assertNotIn(PRICE, result.answer)
        self.assertIn("ProxyAPI", error)


class ReviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.hash_patch = patch("app.auth.ITERATIONS", 1000)
        self.hash_patch.start()
        self.env_patch = patch.dict(os.environ, {"ADMIN_PASSWORD": "test-admin", "SEARCH_MODE": "lexical"})
        self.env_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = st.DATA
        st.DATA = Path(self.temp.name)
        self.client = SessionClient(app)
        self.client.__enter__()
        with st.db() as con:
            con.execute("UPDATE users SET must_change_password=0 WHERE username='admin'")
        response = self.client.post("/auth/login", json={"username": "admin", "password": "test-admin"})
        token = response.cookies[auth.COOKIE]
        self.credentials = ("admin", "test-admin")
        self.client.tokens[self.credentials] = (token, auth.csrf(token))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        st.DATA = self.old_data
        self.temp.cleanup()
        self.env_patch.stop()
        self.hash_patch.stop()

    def test_partial_sources_and_details_survive_history_audit_and_recovery(self):
        document = self.client.post("/kb/documents", auth=self.credentials,
                                    json={"title": "Стоимость", "text": PRICE}).json()
        answer, reason = ai.validate_answer(payload(), PRICE)
        request_id = str(uuid.uuid4())
        mocked = AsyncMock(return_value=(answer, reason, {"review_details": answer._review_details}))
        with patch("app.main.ai.answer", new=mocked):
            fresh = self.client.post("/kb/ask", auth=self.credentials,
                headers={"X-Request-ID": request_id}, json={"question": QUESTION}).json()
            recovered = self.client.post("/kb/ask", auth=self.credentials,
                headers={"X-Request-ID": request_id}, json={"question": QUESTION}).json()
        self.assertEqual(mocked.await_count, 1)
        self.assertEqual(recovered, fresh)
        self.assertEqual(fresh["sources"][0]["document_id"], document["document_id"])
        self.assertTrue(fresh["needs_review"])
        self.assertIn(GAP, fresh["error"])
        history = self.client.get("/kb/history/"+request_id, auth=self.credentials).json()
        self.assertEqual(history["answer"], fresh["answer"])
        self.assertEqual(json.loads(history["sources_json"]), fresh["sources"])
        self.assertEqual(history["error"], fresh["error"])
        audits = self.client.get("/audit/runs?questions_only=true", auth=self.credentials).json()
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(row["status"] == "needs_review" and row["error"] == reason for row in audits))
        self.assertTrue(all(json.loads(row["output"])["answer"] == fresh["answer"] for row in audits))
        exported = self.client.get("/kb/export?format=json", auth=self.credentials).json()[0]
        self.assertEqual(exported["answer"], fresh["answer"])

    def test_direct_ai_endpoint_returns_only_required_public_fields(self):
        answer, reason = ai.validate_answer(payload(), PRICE)
        with patch("app.main.ai.answer", new=AsyncMock(return_value=(answer, reason, {}))):
            response = self.client.post("/ai/answer_with_sources", auth=self.credentials,
                json={"question": QUESTION, "context": PRICE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"answer", "sources", "confidence", "needs_review"})
        self.assertIn(GAP, response.json()["answer"])

    def test_quote_about_unavailability_is_not_a_technical_failure_on_recovery(self):
        quote = "Поиск временно недоступен при обновлении индекса."
        self.client.post("/kb/documents", auth=self.credentials,
                         json={"title": "Поиск", "text": quote})
        answer, reason = ai.validate_answer(payload(sources=[{"quote": quote}]), quote)
        request_id = str(uuid.uuid4())
        with patch("app.main.ai.answer", new=AsyncMock(return_value=(answer, reason, {}))):
            self.client.post("/kb/ask", auth=self.credentials,
                headers={"X-Request-ID": request_id}, json={"question": "Как работает поиск?"})
            recovered = self.client.post("/kb/ask", auth=self.credentials,
                headers={"X-Request-ID": request_id}, json={"question": "Как работает поиск?"}).json()
        self.assertFalse(recovered["technical_error"])
        self.assertTrue(recovered["needs_review"])
