"""Word (.docx) I/O for 降重 — extract paragraphs and rebuild a rewritten docx."""
import io

from docx import Document
from docx.shared import Pt
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

CJK_FONT = "宋体"
LATIN_FONT = "Times New Roman"


def extract_paragraphs(file_bytes: bytes) -> list:
    doc = Document(io.BytesIO(file_bytes))
    return [p.text.strip() for p in doc.paragraphs if p.text.strip()]


def _set_font(run):
    run.font.name = LATIN_FONT
    run.font.size = Pt(12)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), CJK_FONT)


def build_docx(paragraphs: list) -> bytes:
    doc = Document()
    for text in paragraphs:
        run = doc.add_paragraph().add_run(text)
        _set_font(run)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
