"""Извлечение текста и ограниченное локальное OCR."""
import io
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from . import diagnostics as diag


def extract(data, filename):
    suffix = Path(filename).suffix.lower()
    warnings = ["Содержимое фотографий и схем не анализировалось"]
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8-sig"), "text", []
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("Слишком большой распакованный DOCX")
        doc = Document(io.BytesIO(data))
        text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        tables = ["\n".join(" | ".join(c.text for c in row.cells)
                            for row in table.rows) for table in doc.tables]
        return "\n\n".join([text, *tables]).strip(), "docx", warnings
    if suffix != ".pdf":
        raise ValueError("Поддерживаются PDF, DOCX, TXT и MD")
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("PDF защищён паролем. Загрузите незашифрованную копию")
    if len(reader.pages) > int(os.getenv("PDF_MAX_PAGES", "100")):
        raise ValueError("Превышен PDF_MAX_PAGES; проверьте настройки сервера")
    texts, ocr_count = [], 0
    with tempfile.TemporaryDirectory() as temp:
        original = Path(temp) / "input.pdf"
        original.write_bytes(data)
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if not text.strip():
                if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
                    raise ValueError("Для этой страницы нужен OCR. Запустите приложение через Docker Compose")
                ocr_count += 1
                if ocr_count > int(os.getenv("OCR_MAX_PAGES", "10")):
                    raise ValueError("Превышен OCR_MAX_PAGES; требуется обработка меньшего файла")
                diag.log(logging.INFO, "ocr_page_started", component="importer",
                         operation="ocr", pages=index+1)
                prefix = str(Path(temp) / ("page-" + str(index)))
                subprocess.run(["pdftoppm", "-f", str(index+1), "-l", str(index+1),
                                "-scale-to", "2200", "-png", "-singlefile",
                                str(original), prefix], check=True, timeout=30,
                               capture_output=True)
                result = subprocess.run(
                    ["tesseract", prefix+".png", "stdout", "-l", "rus+eng"],
                    capture_output=True, check=True,
                    timeout=int(os.getenv("OCR_PAGE_TIMEOUT", "30")))
                text = result.stdout.decode("utf-8")
                if not text.strip():
                    raise ValueError(f"Страница {index+1} не распознана. Документ не добавлен")
            texts.append(text.strip())
    if ocr_count:
        warnings.append("Текст распознан OCR: проверьте его перед сохранением")
    diag.log(logging.INFO, "pdf_extracted", component="importer",
             pages=len(texts), ocr_pages=ocr_count)
    return "\n\n".join(texts), "ocr" if ocr_count else "pdf", warnings
