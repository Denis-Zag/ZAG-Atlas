"""Проверки отказа векторного поиска и объединения результатов."""
import os
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_app as legacy
from app import vectors

class HybridTests(unittest.TestCase):
    setUp = legacy.ApiTests.setUp
    tearDown = legacy.ApiTests.tearDown
    def test_hybrid_failure_is_saved_as_technical(self):
        with patch.dict(os.environ, {"SEARCH_MODE": "hybrid"}):
            index = vectors.VectorIndex(self.client.app.state.llm_client)
            self.client.app.state.vector_index = index
            with patch.object(index, "retrieve", new=AsyncMock(side_effect=RuntimeError())):
                response = self.client.post('/kb/ask', auth=self.staff, json={"question":"Вопрос"})
            self.assertTrue(response.json()['needs_review'])
            self.assertEqual(response.json()['sources'], [])
            row = self.client.get('/kb/history', auth=self.staff).json()[0]
            self.assertIn('Векторный поиск недоступен', row['error'])
            self.assertIn('временно недоступен', row['answer'])

    def test_reindex_admin_only(self):
        self.assertEqual(self.client.post('/kb/reindex', auth=self.staff).status_code,403)

class FusionTests(unittest.TestCase):
    def test_stale_source_excluded_and_budget_kept(self):
        a={'id':'a','snippet_text':'1234'}
        b={'id':'b','snippet_text':'5678'}
        self.assertEqual(vectors.fuse([a,b],[b],{'a':a},4),[a])
        self.assertEqual(vectors.fuse([a,b],[b],{'a':a,'b':b},9),[b])

class RequestRecoveryTests(unittest.TestCase):
    setUp = legacy.ApiTests.setUp
    tearDown = legacy.ApiTests.tearDown

    def test_response_can_be_recovered_without_second_model_call(self):
        import uuid
        from app import ai
        ident = str(uuid.uuid4())
        parsed, reason = ai.fallback("Тест недостатка данных")
        mock = AsyncMock(return_value=(parsed, reason, {}))
        with patch("app.main.ai.answer", new=mock):
            first = self.client.post("/kb/ask", auth=self.staff,
                headers={"X-Request-ID": ident}, json={"question": "Вопрос"})
            saved = self.client.get("/kb/history/"+ident, auth=self.staff)
            repeated = self.client.post("/kb/ask", auth=self.staff,
                headers={"X-Request-ID": ident}, json={"question": "Вопрос"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(first.json()["answer"], saved.json()["answer"])
        self.assertEqual(repeated.json()["answer"], first.json()["answer"])
        self.assertEqual(mock.await_count, 1)
        self.assertEqual(len(self.client.get("/kb/history", auth=self.staff).json()), 1)

    def test_reused_request_id_cannot_change_question(self):
        import uuid
        ident = str(uuid.uuid4())
        self.client.post("/kb/ask", auth=self.staff,
            headers={"X-Request-ID": ident}, json={"question": "Вопрос"})
        result = self.client.post("/kb/ask", auth=self.staff,
            headers={"X-Request-ID": ident}, json={"question": "Другой"})
        self.assertEqual(result.status_code, 409)

    def test_invalid_request_id_rejected(self):
        result = self.client.post("/kb/ask", auth=self.staff,
            headers={"X-Request-ID": "invalid"}, json={"question": "Вопрос"})
        self.assertEqual(result.status_code, 422)

    def test_external_embedding_failure_identifies_service(self):
        import asyncio
        import httpx
        async def run():
            def failed(request):
                raise httpx.ConnectError("test", request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
                index = vectors.VectorIndex(client)
                with patch.dict(os.environ, {"PROXYAPI_API_KEY": "test-key"}):
                    with self.assertRaises(vectors.IndexFailure) as caught:
                        await index.embed(["Текст"])
                reason = vectors.safe_error(caught.exception)
                self.assertIn("ProxyAPI (эмбеддинги)", reason)
                self.assertIn("ConnectError", reason)
                self.assertNotIn("test-key", reason)
        asyncio.run(run())
