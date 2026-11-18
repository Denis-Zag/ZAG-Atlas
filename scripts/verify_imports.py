"""Функциональная проверка файлов и OCR в изолированной базе контейнера."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")
from app.verification import VerificationClient as TestClient
from pypdf import PdfReader
from app import storage as st
from app.main import app

fixtures = Path(sys.argv[1])
scan_path = Path(sys.argv[2])
documents = [json.loads(line) for line in
             (fixtures.parent / "kb_documents.jsonl").read_text(encoding="utf-8").splitlines()]
manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
normalize = lambda text: " ".join(text.split())
report = {"checks": []}
with tempfile.TemporaryDirectory(prefix="atlas-import-") as temp:
    st.DATA = Path(temp)
    with TestClient(app) as client:
        auth = ("admin", os.environ["ADMIN_PASSWORD"])
        for source, item in zip(documents, manifest[:5]):
            file = fixtures / item["file"]
            r = client.post("/kb/documents/upload", auth=auth,
                            files={"file": (file.name, file.read_bytes())})
            r.raise_for_status()
            preview = r.json()
            preserved = all(normalize(p) in normalize(preview["text"])
                            for p in source["text"].split("\n\n"))
            assert preserved, "Потеря содержимого: " + file.name
            r = client.post("/kb/documents", auth=auth,
                            json={"title": source["title"], "text": preview["text"],
                                  "import_id": preview["import_id"]})
            r.raise_for_status()
            report["checks"].append({"file": file.name, "content_preserved": preserved,
                                     "method": preview["method"], "saved": True})
        assert not (PdfReader(scan_path).pages[0].extract_text() or "").strip()
        r = client.post("/kb/documents/upload", auth=auth,
                        files={"file": (scan_path.name, scan_path.read_bytes())})
        r.raise_for_status()
        preview = r.json()
        assert preview["method"] == "ocr"
        assert "менеджер" in preview["text"].lower()
        assert "заявк" in preview["text"].lower()
        assert len(client.get("/kb/documents", auth=auth).json()) == 5
        report["checks"].append({"file": scan_path.name, "method": preview["method"],
                                "text_length": len(preview["text"]),
                                "preview": preview["text"][:150],
                                "warnings": preview["warnings"],
                                "not_saved_automatically": True})
        for name, data, expected in [
            ("invalid.pdf", b"this is not a pdf", 422),
            ("unsupported.exe", b"not executable", 422),
            ("oversize.txt", b"x" * (10 * 1024 * 1024 + 1), 413),
        ]:
            r = client.post("/kb/documents/upload", auth=auth,
                            files={"file": (name, data)})
            assert r.status_code == expected, (name, r.status_code)
            report["checks"].append({"file": name, "expected_http": expected,
                                     "actual_http": r.status_code})
report["passed"] = True
print(json.dumps(report, ensure_ascii=False, indent=2))
