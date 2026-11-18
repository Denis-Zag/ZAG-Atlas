"""Живая проверка пояснений на временной базе; рабочие документы не меняются."""
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values


def run():
    config = dotenv_values(ROOT / ".env")
    if not config.get("PROXYAPI_API_KEY"):
        raise RuntimeError("PROXYAPI_API_KEY is missing")
    for key, value in config.items():
        if value is not None and (key.startswith("LLM_") or key.startswith("PROXYAPI_")):
            os.environ[key] = value
    os.environ["SEARCH_MODE"] = "lexical"
    os.environ["ADMIN_PASSWORD"] = secrets.token_urlsafe(28)
    os.environ["SESSION_COOKIE_SECURE"] = "false"
    from app import storage as st
    from app.main import app
    from app.verification import VerificationClient

    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "cases": [],
              "environment": "Изолированная временная SQLite, локальный TestClient, реальная модель ProxyAPI",
              "search_mode": "lexical", "working_data_changed": False, "passed": False}
    documents = [json.loads(line) for line in
                 (ROOT / "tests_data/kb_documents.jsonl").read_text(encoding="utf-8").splitlines()]
    cases = [
        {"name": "partial_price_and_warranty", "route": "/kb/ask",
         "question": "Сколько стоит стандартный монтаж и есть ли гарантия?", "review": True},
        {"name": "known_price", "route": "/kb/ask",
         "question": "Сколько стоит стандартный монтаж?", "review": False},
        {"name": "unknown_personal_discount", "route": "/kb/ask",
         "question": "Какую персональную скидку директор согласовал клиенту Иванову?", "review": True},
    ]
    for line in (ROOT / "tests_data/kb_adversarial.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        cases.append({"name": "conflict" if "12000" in case["context"] else "prompt_injection",
                      "route": "/ai/answer_with_sources", "question": case["question"],
                      "context": case["context"], "review": True})
    selected = sys.argv[1] if len(sys.argv) > 1 else ""
    if selected:
        cases = [case for case in cases if case["name"] == selected]
        if not cases:
            raise ValueError("Unknown verification case")

    original_data = st.DATA
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-review-check-") as folder:
            st.DATA = Path(folder)
            with VerificationClient(app) as client:
                document_texts = {}
                for document in documents:
                    response = client.post("/kb/documents", auth=("admin", ""), json=document)
                    response.raise_for_status()
                    document_texts[response.json()["document_id"]] = document["text"]
                for case in cases:
                    start = time.perf_counter()
                    body = {key: case[key] for key in ("question", "context") if key in case}
                    response = client.post(case["route"], auth=("admin", ""), json=body)
                    result = response.json()
                    audits = st.rows("SELECT * FROM audit_runs WHERE action=? ORDER BY created_at DESC LIMIT 1",
                                     ("POST " + case["route"],))
                    audit = audits[0]
                    audit_output = json.loads(audit["output"])
                    technical = audit["status"] == "error"
                    passed = (response.status_code == 200 and not technical
                              and result["needs_review"] is case["review"])
                    if case["name"] == "partial_price_and_warranty":
                        passed = passed and bool(result["sources"]) and "12000" in result["answer"].replace(" ", "")
                        passed = passed and "гарант" in result.get("error", "").lower()
                        passed = passed and "Следующий шаг:" in result["answer"]
                        history = client.get("/kb/history/"+result["qa_id"], auth=("admin", "")).json()
                        passed = passed and history["answer"] == result["answer"] and history["error"] == result["error"]
                        passed = passed and audit["error"] == result["error"] and audit_output["answer"] == result["answer"]
                    elif case["review"]:
                        passed = passed and not result["sources"]
                        passed = passed and "Следующий шаг:" in result["answer"]
                        if case["name"] == "conflict":
                            passed = passed and "противореч" in (audit["error"] or "").lower()
                    else:
                        passed = passed and bool(result["sources"])
                    for source in result["sources"]:
                        context = case.get("context", document_texts.get(source.get("document_id"), ""))
                        passed = passed and bool(source["quote"]) and source["quote"] in context
                    if case["route"].startswith("/ai/"):
                        passed = passed and set(result) == {"answer", "sources", "confidence", "needs_review"}
                    report["cases"].append({"name": case["name"], "question": case["question"],
                        "http_status": response.status_code, "result": result, "audit_status": audit["status"],
                        "audit_reason": audit["error"], "meta": audit_output.get("meta", {}),
                        "technical_error": technical, "seconds": round(time.perf_counter()-start, 3), "passed": bool(passed)})
                    print(case["name"] + ": " + ("passed" if passed else "failed"), flush=True)
                    if technical:
                        report["stopped_on_technical_error"] = True
                        break
                report["passed"] = len(report["cases"]) == len(cases) and all(c["passed"] for c in report["cases"])
    finally:
        st.DATA = original_data
    target = ROOT / ("verification/REVIEW_DETAILS_LIVE_20261117" +
                     ("_"+selected if selected else "") + ".json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report: " + str(target), flush=True)
    return report["passed"]


if __name__ == "__main__":
    try:
        sys.exit(0 if run() else 1)
    except Exception as error:
        print("Verification stopped: " + type(error).__name__, flush=True)
        sys.exit(1)
