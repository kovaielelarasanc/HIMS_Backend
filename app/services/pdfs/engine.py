# FILE: app/services/pdf/engine.py
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Any, Dict

from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from app.models.ui_branding import UiBranding

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# -----------------------------
# Helpers
# -----------------------------
def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    try:
        s = str(v)
    except Exception:
        return ""
    return s.replace("\u2011", "-")  # avoid non-breaking hyphen rendering issues


def fmt_ist(dt) -> str:
    if not dt:
        return ""
    try:
        if getattr(dt, "tzinfo", None) is None:
            # naive -> assume UTC
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d-%b-%Y %I:%M %p")
    except Exception:
        return _safe_str(dt)


def mm_pt(x_mm: float) -> float:
    return x_mm * mm


def _img_reader(path: Optional[str]) -> Optional[ImageReader]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        logger.warning("PDF image not found: %s", path)
        return None
    try:
        return ImageReader(str(p))
    except Exception:
        logger.exception("Failed to load image: %s", path)
        return None


# -----------------------------
# Branding loader
# -----------------------------
def get_branding(db) -> UiBranding:
    b = db.query(UiBranding).order_by(UiBranding.id.asc()).first()
    if not b:
        # return a dummy object-like fallback
        b = UiBranding()
        b.org_name = "NABH HIMS"
        b.org_address = ""
        b.org_phone = ""
        b.org_email = ""
        b.pdf_show_page_number = True
        b.pdf_header_height_mm = 26
        b.pdf_footer_height_mm = 14
        b.letterhead_type = None
        b.letterhead_position = "background"
    # sensible defaults
    if not b.pdf_header_height_mm:
        b.pdf_header_height_mm = 26
    if not b.pdf_footer_height_mm:
        b.pdf_footer_height_mm = 14
    return b


@dataclass
class PdfBuildContext:
    title: str
    subtitle: str = ""
    meta: Dict[str, Any] = None


# -----------------------------
# Page-number canvas (Page X of Y)
# -----------------------------
class NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        super().showPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(num_pages)
            super().showPage()
        super().save()

    def _draw_page_number(self, page_count: int):
        # footer right: Page X of Y
        if not getattr(self, "_pdf_show_page_number", True):
            return
        self.saveState()
        self.setFont("Helvetica", 9)
        txt = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(self._page_width - mm_pt(12), mm_pt(8), txt)
        self.restoreState()


# -----------------------------
# Header/Footer drawing
# -----------------------------
def make_on_page(branding: UiBranding, ctx: PdfBuildContext):
    logo = _img_reader(getattr(branding, "logo_path", None))
    header_img = _img_reader(getattr(branding, "pdf_header_path", None))
    footer_img = _img_reader(getattr(branding, "pdf_footer_path", None))
    letterhead_img = _img_reader(getattr(branding, "letterhead_path", None)) if getattr(branding, "letterhead_type", None) == "image" else None

    header_h = mm_pt(float(branding.pdf_header_height_mm or 26))
    footer_h = mm_pt(float(branding.pdf_footer_height_mm or 14))

    def _on_page(canv: rl_canvas.Canvas, doc: BaseDocTemplate):
        page_w, page_h = doc.pagesize

        # expose to NumberedCanvas
        canv._page_width = page_w
        canv._page_height = page_h
        canv._pdf_show_page_number = bool(getattr(branding, "pdf_show_page_number", True))

        # letterhead background (image only here)
        if letterhead_img and getattr(branding, "letterhead_position", "background") == "background":
            canv.saveState()
            try:
                canv.drawImage(letterhead_img, 0, 0, width=page_w, height=page_h, mask="auto")
            except Exception:
                logger.exception("Failed to draw letterhead background")
            canv.restoreState()

        # header artwork
        if header_img:
            canv.saveState()
            try:
                canv.drawImage(header_img, 0, page_h - header_h, width=page_w, height=header_h, mask="auto")
            except Exception:
                logger.exception("Failed to draw header artwork")
            canv.restoreState()

        # footer artwork
        if footer_img:
            canv.saveState()
            try:
                canv.drawImage(footer_img, 0, 0, width=page_w, height=footer_h, mask="auto")
            except Exception:
                logger.exception("Failed to draw footer artwork")
            canv.restoreState()

        # header text block (professional)
        left = mm_pt(12)
        top = page_h - mm_pt(8)

        canv.saveState()
        canv.setFillColor(colors.black)

        # logo
        if logo:
            try:
                canv.drawImage(logo, left, page_h - mm_pt(22), width=mm_pt(16), height=mm_pt(16), mask="auto")
            except Exception:
                logger.exception("Failed to draw logo")

        text_x = left + (mm_pt(20) if logo else 0)

        canv.setFont("Helvetica-Bold", 12)
        canv.drawString(text_x, top, _safe_str(getattr(branding, "org_name", "") or ""))

        canv.setFont("Helvetica", 9)
        y2 = top - mm_pt(5)
        tagline = _safe_str(getattr(branding, "org_tagline", "") or "")
        if tagline:
            canv.drawString(text_x, y2, tagline)
            y2 -= mm_pt(4)

        addr = _safe_str(getattr(branding, "org_address", "") or "")
        contact = " | ".join([x for x in [
            _safe_str(getattr(branding, "org_phone", "") or ""),
            _safe_str(getattr(branding, "org_email", "") or ""),
            _safe_str(getattr(branding, "org_website", "") or ""),
        ] if x])

        if addr:
            canv.setFillColor(colors.grey)
            canv.drawString(text_x, y2, addr[:120])
            y2 -= mm_pt(4)

        if contact:
            canv.setFillColor(colors.grey)
            canv.drawString(text_x, y2, contact[:120])

        # doc title right
        canv.setFillColor(colors.black)
        canv.setFont("Helvetica-Bold", 11)
        canv.drawRightString(page_w - mm_pt(12), top, _safe_str(ctx.title))
        if ctx.subtitle:
            canv.setFont("Helvetica", 9)
            canv.setFillColor(colors.grey)
            canv.drawRightString(page_w - mm_pt(12), top - mm_pt(5), _safe_str(ctx.subtitle))

        # separator line
        canv.setStrokeColor(colors.lightgrey)
        canv.setLineWidth(0.6)
        canv.line(mm_pt(10), page_h - header_h, page_w - mm_pt(10), page_h - header_h)

        canv.restoreState()

    return _on_page


# -----------------------------
# Document Builder
# -----------------------------
def build_pdf(*, db, ctx, story):
    import io
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.lib.pagesizes import A4

    # ✅ MUST be new buffer per call (prevents duplicate PDFs being appended)
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=12 * mm,
        title=ctx.title or "",
        author="",
    )

    # ✅ build ONLY ONCE
    doc.build(story)

    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes



# -----------------------------
# Styles + Components
# -----------------------------
def get_styles():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(
        name="H2",
        parent=base["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        spaceBefore=8,
        spaceAfter=6,
        textColor=colors.black,
    ))
    base.add(ParagraphStyle(
        name="Small",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.black,
    ))
    base.add(ParagraphStyle(
        name="Muted",
        parent=base["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.grey,
    ))
    return base


def section_title(text: str):
    styles = get_styles()
    return Paragraph(_safe_str(text), styles["H2"])


def kv_table(rows: List[List[str]]):
    data = [[_safe_str(a), _safe_str(b)] for a, b in rows]
    t = Table(data, colWidths=[mm_pt(45), None])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.whitesmoke, colors.white]),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def simple_table(data: List[List[str]], col_widths=None):
    clean = [[_safe_str(x) for x in row] for row in data]
    t = Table(clean, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t
