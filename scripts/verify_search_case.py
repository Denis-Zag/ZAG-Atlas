"""Сравнение поиска по словам и гибридного RAG на одной рабочей базе.
Запуск внутри контейнера Atlas. Ключи/пароли не выводятся.
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import json
import os
import re
import time
import uuid

import httpx

from app import ai, search, storage as st
from app.verification import login_client

CASES = [
    ("Можно передвинуть приезд мастера?", "04_changes_warranty.pdf"),
    ("Сколько стоит поставить сплит-систему?", "03_prices.docx"),
]
snippets = st.rows("SELECT snippets.*,documents.title,documents.filename FROM snippets "
                   "JOIN documents ON documents.id=snippets.document_id")
budget = st.settings()["MAX_CONTEXT_CHARS"]
report = {
    "created_at": st.now(),
    "comparison": "Одинаковые вопросы, документы и лимит контекста; текущая реализация поиска по словам против hybrid",
    "documents": len(st.rows("SELECT id FROM documents")),
    "snippets": len(snippets),
    "context_budget": budget,
    "cases": [],
}
with httpx.Client(base_url="http://127.0.0.1:8000",
                  timeout=150) as client:
    login_client(client)
    for question, expected_file in CASES:
        ident = str(uuid.uuid4())
        lexical = search.retrieve(question, snippets, budget)
        lexical_context = "\n\n".join(s["snippet_text"] for s in lexical)
        baseline = None
        if not lexical_context:
            answer, reason, _ = asyncio.run(ai.answer(question, lexical_context))
            baseline = {"answer": answer.answer, "needs_review": answer.needs_review,
                        "error": reason, "external_model_called": False}
        started = time.perf_counter()
        response = client.post("/kb/ask", json={"question": question},
                               headers={"X-Request-ID": ident})
        response.raise_for_status()
        result = response.json()
        sources = []
        for source in result["sources"]:
            doc = st.rows("SELECT title,filename,text FROM documents WHERE id=?",
                          (source["document_id"],))[0]
            sources.append({**source, "title": doc["title"], "filename": doc["filename"],
                            "exact_quote_verified": source["quote"] in doc["text"]})
        audit = [json.loads(r["output"]) for r in st.rows(
            "SELECT output FROM audit_runs WHERE action='POST /kb/ask' ORDER BY created_at DESC LIMIT 20")]
        meta = next((a.get("meta", {}) for a in audit if a.get("qa_id") == ident), {})
        passed = (not lexical and not result["needs_review"]
                  and not result.get("technical_error")
                  and any(s["filename"] == expected_file for s in sources)
                  and all(s["exact_quote_verified"] for s in sources))
        if expected_file == "03_prices.docx":
            passed = passed and "12000" in re.sub(r"\s", "", result["answer"])
        case = {"question": question, "expected_file": expected_file,
                "lexical_candidates": len(lexical), "lexical_result": baseline,
                "hybrid_seconds": round(time.perf_counter()-started, 3),
                "hybrid_result": result, "sources": sources, "audit_meta": meta,
                "passed": bool(passed)}
        report["cases"].append(case)
        print("PROGRESS=" + json.dumps({"question": question, "passed": bool(passed)}, ensure_ascii=False), flush=True)
        if result.get("technical_error"):
            report["stopped_on_technical_error"] = True
            break
report["passed"] = len(report["cases"]) == len(CASES) and all(c["passed"] for c in report["cases"])
print("REPORT_JSON=" + json.dumps(report, ensure_ascii=False), flush=True)
