"""Векторный индекс Qdrant и гибридный поиск. SQLite — источник истины."""
import asyncio
import hashlib
import json
import logging
import math
import os

import httpx

from . import diagnostics as diag, search, storage as st


def enabled():
    return os.getenv("SEARCH_MODE", "lexical") == "hybrid"


class IndexFailure(Exception):
    """Поисковый индекс временно недоступен."""


class VectorIndex:
    def __init__(self, client):
        self.client = client
        self.lock = asyncio.Lock()
        self.model = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
        self.dim = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
        self.collection = "atlas_" + hashlib.sha256(
            (self.model + str(self.dim)).encode()).hexdigest()[:12]
        self.url = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
        self.digest = None
        with st.db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS embedding_cache("
                        "cache_key TEXT PRIMARY KEY, vector_json TEXT NOT NULL)")

    async def request(self, method, path, body=None):
        headers = {}
        if os.getenv("QDRANT_API_KEY"):
            headers["api-key"] = os.environ["QDRANT_API_KEY"]
        try:
            response = await self.client.request(method, self.url + path,
                                                 json=body, headers=headers, timeout=30)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            diag.log(logging.ERROR, "vector_request_failed", component="vectors",
                     service="qdrant", operation="request", exception=exc)
            raise IndexFailure("Qdrant: " + type(exc).__name__) from None
        return response.json()

    async def embed(self, texts):
        key = os.getenv("PROXYAPI_API_KEY", "")
        if not key:
            diag.log(logging.ERROR, "embeddings_unavailable", component="vectors",
                     service="proxyapi", reason_code="missing_api_key")
            raise IndexFailure("Не настроен ключ эмбеддингов")
        try:
            response = await self.client.post(
                os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/v1").rstrip("/")
                + "/embeddings", headers={"Authorization": "Bearer " + key},
                json={"model": self.model, "input": texts, "dimensions": self.dim},
                timeout=httpx.Timeout(90, connect=30))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            diag.log(logging.ERROR, "embeddings_failed", component="vectors",
                     service="proxyapi", operation="embeddings", exception=exc,
                     input_count=len(texts))
            raise IndexFailure("ProxyAPI (эмбеддинги): " + type(exc).__name__) from None
        data = response.json()
        items = sorted(data["data"], key=lambda item: item["index"])
        if [item["index"] for item in items] != list(range(len(texts))):
            raise IndexFailure("Неверный порядок эмбеддингов")
        vectors = [item["embedding"] for item in items]
        for vector in vectors:
            if len(vector) != self.dim or not all(
                    isinstance(v, (float, int)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in vector) or not any(vector):
                raise IndexFailure("Некорректный вектор")
        return vectors, data.get("usage", {})

    async def sync(self):
        async with self.lock:
            snippets = st.rows("SELECT * FROM snippets ORDER BY id")
            digest = hashlib.sha256(json.dumps(snippets, sort_keys=True).encode()).hexdigest()
            base = "/collections/" + self.collection
            try:
                response = await self.client.get(self.url + base, timeout=15,
                    headers={"api-key": os.getenv("QDRANT_API_KEY", "")} if os.getenv("QDRANT_API_KEY") else {})
            except httpx.HTTPError as exc:
                diag.log(logging.ERROR, "vector_request_failed", component="vectors",
                         service="qdrant", operation="sync", exception=exc)
                raise IndexFailure("Qdrant: " + type(exc).__name__) from None
            if response.status_code == 404:
                await self.request("PUT", base, {"vectors": {"size": self.dim, "distance": "Cosine"}})
                self.digest = None
            else:
                response.raise_for_status()
                if response.json()["result"]["points_count"] != len(snippets):
                    self.digest = None
            if self.digest == digest:
                return {"indexed": len(snippets), "embedding_tokens": 0}
            points, missing, total = [], [], 0
            for snippet in snippets:
                cache_key = hashlib.sha256((self.model + str(self.dim) + snippet["snippet_text"]).encode()).hexdigest()
                cached = st.rows("SELECT vector_json FROM embedding_cache WHERE cache_key=?", (cache_key,))
                if cached:
                    points.append({"id": snippet["id"], "vector": json.loads(cached[0]["vector_json"]),
                                   "payload": {"document_id": snippet["document_id"]}})
                else:
                    missing.append((snippet, cache_key))
            for start in range(0, len(missing), 32):
                batch = missing[start:start+32]
                vectors, usage = await self.embed([s["snippet_text"] for s, _ in batch])
                total += usage.get("total_tokens", 0)
                with st.db() as con:
                    for (snippet, cache_key), vector in zip(batch, vectors):
                        con.execute("INSERT OR REPLACE INTO embedding_cache VALUES(?,?)",
                                    (cache_key, json.dumps(vector)))
                        points.append({"id": snippet["id"], "vector": vector,
                                       "payload": {"document_id": snippet["document_id"]}})
            for start in range(0, len(points), 64):
                await self.request("PUT", base + "/points?wait=true", {"points": points[start:start+64]})
            # Убираем устаревшие точки; тексты для ответа берём только из SQLite.
            live = {s["id"] for s in snippets}
            offset = None
            while True:
                body = {"limit": 256, "with_payload": False, "with_vector": False}
                if offset is not None:
                    body["offset"] = offset
                page = (await self.request("POST", base + "/points/scroll", body))["result"]
                stale = [p["id"] for p in page["points"] if p["id"] not in live]
                if stale:
                    await self.request("POST", base + "/points/delete?wait=true", {"points": stale})
                offset = page.get("next_page_offset")
                if offset is None:
                    break
            self.digest = digest
            diag.log(logging.INFO, "index_synced", component="vectors",
                     snippet_count=len(snippets), search_mode="hybrid")
            return {"indexed": len(snippets), "embedding_tokens": total}

    async def retrieve(self, question, budget):
        indexed = await self.sync()
        snippets = st.rows("SELECT snippets.*,documents.title FROM snippets JOIN documents "
                           "ON documents.id=snippets.document_id")
        if not snippets:
            return [], {"search_mode": "hybrid", **indexed}
        vectors, usage = await self.embed([question])
        result = await self.request("POST", "/collections/" + self.collection + "/points/query",
            {"query": vectors[0], "limit": 16, "with_payload": False,
             "score_threshold": float(os.getenv("VECTOR_MIN_SCORE", "0.25"))})
        current = {s["id"]: s for s in st.rows(
            "SELECT snippets.*,documents.title FROM snippets JOIN documents ON documents.id=snippets.document_id")}
        semantic = [current[p["id"]] for p in result["result"]["points"] if p["id"] in current]
        lexical = search.retrieve(question, snippets, budget, top_k=16)
        found = fuse(semantic, lexical, current, budget)
        return found, {"search_mode": "hybrid", **indexed, "query_embedding_usage": usage,
                       "semantic_candidates": len(semantic), "lexical_candidates": len(lexical)}


def fuse(semantic, lexical, current, budget):
    scores = {}
    for ranking in (semantic, lexical):
        for rank, snippet in enumerate(ranking, 1):
            scores[snippet["id"]] = scores.get(snippet["id"], 0) + 1 / (60 + rank)
    found, used = [], 0
    for ident in sorted(scores, key=scores.get, reverse=True):
        if ident not in current:
            continue
        snippet = current[ident]
        # Два перевода строки между фрагментами также входят в бюджет.
        length = len(snippet["snippet_text"]) + (2 if found else 0)
        if used + length <= budget:
            found.append(snippet)
            used += length
        if len(found) == 8:
            break
    return found


def safe_error(exc):
    return "Векторный поиск недоступен: " + (str(exc) if isinstance(exc, IndexFailure) else type(exc).__name__)
