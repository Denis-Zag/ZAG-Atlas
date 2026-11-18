"""Создание функциональных файлов для проверки импорта ZAG Atlas."""
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests_data" / "documents"
QA = ROOT / "verification" / "document_previews"
ASSETS = ROOT / "verification" / "assets"
for folder in (OUT, QA, ASSETS):
    folder.mkdir(parents=True, exist_ok=True)
FONT = Path(os.getenv("ATLAS_FIXTURE_FONT", r"C:\Windows\Fonts\arial.ttf"))
if not FONT.exists():
    raise SystemExit("Укажите ATLAS_FIXTURE_FONT: путь к TTF с кириллицей")
pdfmetrics.registerFont(TTFont("AtlasFixture", str(FONT)))
body = ParagraphStyle("Body", fontName="AtlasFixture", fontSize=11,
                      leading=15, spaceAfter=8, textColor=colors.black)
title_style = ParagraphStyle("Title", parent=body, fontSize=17, leading=21, spaceAfter=16)


def pdf_file(target, title, paragraphs):
    story = [Paragraph(escape(title), title_style)]
    for paragraph in paragraphs:
        story.append(Paragraph(escape(paragraph).replace("\n", "<br/>"), body))
    SimpleDocTemplate(str(target), pagesize=letter,
                      leftMargin=48, rightMargin=48, topMargin=45,
                      bottomMargin=45, title=title, author="ZAG Atlas").build(story)


def docx_file(target, title, paragraphs):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.65)
    section.left_margin = section.right_margin = Inches(.7)
    for name in ("Normal", "Title"):
        style = doc.styles[name]
        style.font.name = "Arial"
        style.font.color.rgb = RGBColor(0, 0, 0)
    doc.styles["Normal"].font.size = Pt(11)
    doc.styles["Normal"].paragraph_format.space_after = Pt(7)
    doc.styles["Title"].font.size = Pt(17)
    doc.core_properties.author = "ZAG Atlas"
    doc.core_properties.title = title
    doc.add_paragraph(title, "Title")
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    doc.save(target)


documents = [json.loads(line) for line in
             (ROOT/"tests_data/kb_documents.jsonl").read_text(encoding="utf-8").splitlines()]
filenames = ["01_services.docx", "02_intake.pdf", "03_prices.docx",
             "04_changes_warranty.pdf", "05_faq.txt"]
manifest = []
for document, filename in zip(documents, filenames):
    target = OUT / filename
    paragraphs = document["text"].split("\n\n")
    if target.suffix == ".docx":
        docx_file(target, document["title"], paragraphs)
    elif target.suffix == ".pdf":
        pdf_file(target, document["title"], paragraphs)
    else:
        target.write_text(document["title"]+"\n\n"+document["text"], encoding="utf-8")
    manifest.append({"file": filename, "title": document["title"], "kind": "baseline"})
schedule = [
    "Учебное расписание бригады номер один на 18 ноября 2026 года.",
    "Состояние расписания зафиксировано 15 ноября 2026 года.",
    "Свободное окно 18 ноября 2026 года: с 10:00 до 12:00.",
    "Свободное окно 18 ноября 2026 года: с 15:00 до 17:00.",
    "Остальные интервалы в этом документе не обозначены как свободные.",
    "Перед обещанием времени клиенту менеджер подтверждает актуальность у бригады.",
    "Документ является учебным примером и не подключён к действующему календарю."
]
pdf_file(OUT/"06_schedule_optional.pdf", "Расписание бригады", schedule)
manifest.append({"file": "06_schedule_optional.pdf", "title": "Расписание бригады",
                 "kind": "optional_after_unknown_question"})
for filename in ["02_intake.pdf", "04_changes_warranty.pdf", "06_schedule_optional.pdf"]:
    pdf = pdfium.PdfDocument(str(OUT/filename))
    for number in range(len(pdf)):
        image = pdf[number].render(scale=1.5).to_pil()
        image.save(QA/(Path(filename).stem+f"-{number+1}.png"))
    pdf.close()
# Отдельный контрольный скан: он не входит в комплект пяти документов.
pdf = pdfium.PdfDocument(str(OUT/"02_intake.pdf"))
image = pdf[0].render(scale=2).to_pil()
width, height = pdf[0].get_size()
scan = canvas.Canvas(str(ASSETS/"intake_scan.pdf"), pagesize=(width, height))
scan.drawImage(ImageReader(image), 0, 0, width=width, height=height)
scan.save()
image.save(QA/"intake_scan.png")
pdf.close()
(OUT/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"files": filenames+["06_schedule_optional.pdf"],
                  "pdf_previews": len(list(QA.glob("*.png"))),
                  "ocr_sample": "verification/assets/intake_scan.pdf"}, ensure_ascii=False))
