"""Живой прогон десяти вопросов в отдельной временной базе."""
import json
import os
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app.verification import VerificationClient as TestClient
from app import storage as st
from app.main import app

output = ROOT / "verification"
output.mkdir(exist_ok=True)
report = {"checked_at": datetime.now(timezone.utc).isoformat(),
          "model": os.getenv("LLM_MODEL"), "results": []}
documents = [json.loads(line) for line in
             (ROOT/"tests_data/kb_documents.jsonl").read_text(encoding="utf-8").splitlines()]
questions = [json.loads(line) for line in
             (ROOT/"tests_data/kb_questions.jsonl").read_text(encoding="utf-8").splitlines()]
with tempfile.TemporaryDirectory(prefix="atlas-live-") as temp:
    st.DATA = Path(temp)
    with TestClient(app) as client:
        for doc in documents:
            r = client.post("/kb/documents", json=doc,
                            auth=("admin", os.environ["ADMIN_PASSWORD"]))
            r.raise_for_status()
        consecutive_errors = 0
        for index, case in enumerate(questions, 1):
            start = perf_counter()
            r = client.post("/kb/ask", json={"question": case["question"]},
                            auth=("staff", os.environ["STAFF_PASSWORD"]))
            answer = r.json()
            passed = (r.status_code == 200 and
                      answer.get("needs_review") == case["expected_needs_review"] and
                      (case["expected_needs_review"] or bool(answer.get("sources"))))
            audit = st.rows("SELECT output,error FROM audit_runs "
                            "WHERE action='POST /kb/ask' ORDER BY created_at DESC LIMIT 1")[0]
            meta = json.loads(audit["output"]).get("meta", {})
            passed = passed and not meta.get("technical_error", False)
            consecutive_errors = consecutive_errors + 1 if meta.get("technical_error") else 0
            report["results"].append({"number": index, **case, "http_status": r.status_code,
                                      "result": answer, "passed": passed,
                                      "duration_ms": round((perf_counter()-start)*1000),
                                      "error": audit["error"], "meta": meta})
            (output/"LIVE_TEST_RESULTS.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Test {index}: {'PASS' if passed else 'FAIL'}", flush=True)
            if consecutive_errors >= 2:
                report["blocked"] = "Два последовательных сбоя внешнего сервиса"
                break
report["completed"] = len(report["results"])
report["passed"] = sum(item["passed"] for item in report["results"])
report["total"] = len(questions)
(output/"LIVE_TEST_RESULTS.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Result: {report['passed']}/{report['total']}", flush=True)
raise SystemExit(0 if report["passed"] == report["total"] else 1)
