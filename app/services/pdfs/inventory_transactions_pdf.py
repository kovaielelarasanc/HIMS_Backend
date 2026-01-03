# FILE: app/services/pdf/inventory_transactions_pdf.py
from __future__ import annotations

from io import BytesIO
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics

from app.models.ui_branding import UiBranding

IST = ZoneInfo("Asia/Kolkata")

TEXT = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#475569")
BORDER = colors.HexColor("#E2E8F0")
BG_HEAD = colors.HexColor("#F8FAFC")
ROW_ALT = colors.Color(0, 0, 0, alpha=0.03)


# -------------------------
# Safe image helpers
# -------------------------
def _img_reader(path: Optional[str]):
    if not path:
        return None
    try:
        return ImageReader(path)
    except Exception:
        return None


def _safe_set_alpha(c: canvas.Canvas, a: float):
    if hasattr(c, "setFillAlpha"):
        try:
            c.setFillAlpha(a)
        except Exception:
            pass
    if hasattr(c, "setStrokeAlpha"):
        try:
            c.setStrokeAlpha(a)
        except Exception:
            pass


def _safe_draw_image(c: canvas.Canvas, img, x, y, w, h):
    if not img:
        return
    try:
        c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    except Exception:
        pass


# -------------------------
# Formatting
# -------------------------
def _fmt_dt_ist(dt: Any) -> str:
    """
    ✅ Always render as IST: dd-mm-YYYY hh:mm AM/PM
    Handles naive UTC datetimes (common when stored from utcnow()).
    """
    if not isinstance(dt, datetime):
        return "" if dt is None else str(dt)

    d = dt
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(IST)
    return d.strftime("%d-%m-%Y %I:%M %p")


def _d(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _fmt_compact(v: Any, places: int = 4) -> str:
    """
    ✅ 15.0000 -> 15
       14.3000 -> 14.3
    """
    dv = _d(v)
    if dv is None:
        return "" if v is None else str(v)

    q = Decimal("1." + ("0" * places))
    try:
        dv = dv.quantize(q)
    except Exception:
        pass

    s = f"{dv:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


# -------------------------
# Wrap (NO ellipsis, NO dots)
# -------------------------
def _wrap_text_hard(text: str, font: str, size: float, max_w: float) -> List[str]:
    t = "" if text is None else str(text)
    t = t.replace("\r", "").strip()
    if not t:
        return [""]

    out: List[str] = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue

        lines = simpleSplit(para, font, size, max_w) or [para]
        for line in lines:
            if pdfmetrics.stringWidth(line, font, size) <= max_w:
                out.append(line)
                continue

            seg = ""
            for ch in line:
                if pdfmetrics.stringWidth(seg + ch, font, size) <= max_w:
                    seg += ch
                else:
                    if seg:
                        out.append(seg)
                    seg = ch
            if seg:
                out.append(seg)

    return out if out else [""]


def _wrap_limit_no_dots(text: str, font: str, size: float, max_w: float, max_lines: int) -> List[str]:
    """
    ✅ Wrap but if exceeds max_lines, cut (NO ... / NO …)
    """
    lines = _wrap_text_hard(text, font, size, max_w)
    if len(lines) <= max_lines:
        return lines
    return lines[:max_lines]


# -------------------------
# Page numbering canvas
# -------------------------
class NumberedCanvas(canvas.Canvas):
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
            cb = getattr(self, "_page_number_cb", None)
            if cb:
                try:
                    cb(self._pageNumber, num_pages)
                except Exception:
                    pass
            super().showPage()
        super().save()


def build_stock_transactions_pdf(
    *,
    rows: List[Dict[str, Any]],
    branding: Optional[UiBranding],
    filters_text: str = "",
) -> bytes:
    buf = BytesIO()
    c = NumberedCanvas(buf, pagesize=A4)
    W, H = A4

    left = 12 * mm
    right = 12 * mm
    top = 12 * mm
    bottom = 12 * mm

    header_h = 28 * mm
    footer_h = 10 * mm

    usable_w = W - left - right

    header_img = _img_reader(getattr(branding, "pdf_header_path", None) if branding else None)
    footer_img = _img_reader(getattr(branding, "pdf_footer_path", None) if branding else None)
    logo_img = _img_reader(getattr(branding, "logo_path", None) if branding else None)

    letterhead_path = getattr(branding, "letterhead_path", None) if branding else None
    letterhead_type = (getattr(branding, "letterhead_type", "") or "").lower() if branding else ""
    letterhead_pos = (getattr(branding, "letterhead_position", "background") or "background").lower() if branding else "background"
    letterhead_img = _img_reader(letterhead_path) if letterhead_type in ("", "image", "img", "png", "jpg", "jpeg") else None

    show_pages = bool(getattr(branding, "pdf_show_page_number", True) if branding else True)

    # ✅ Fixed-width columns (sum = 186mm exactly)
    col_names = ["Date/Time", "Ref", "Item", "Batch", "Loc", "Qty", "MRP", "User", "Doctor"]
    col_w_mm = [24, 34, 40, 16, 18, 12, 12, 15, 15]  # 186mm
    cols = [(n, w * mm) for n, w in zip(col_names, col_w_mm)]
    table_w = sum(w for _, w in cols)

    start_x = left
    pad_x = 1.8 * mm
    pad_y = 1.2 * mm

    head_font = "Helvetica-Bold"
    head_size = 8.0
    head_line_h = 3.6 * mm

    body_font = "Helvetica"
    body_size = 8.0
    body_line_h = 3.8 * mm

    max_lines_by_col = {
        "Date/Time": 2,
        "Ref": 3,
        "Item": 3,
        "Batch": 2,
        "Loc": 2,
        "Qty": 1,
        "MRP": 1,
        "User": 2,
        "Doctor": 2,
    }

    def draw_header() -> float:
        if letterhead_img and letterhead_pos == "background":
            c.saveState()
            _safe_set_alpha(c, 0.12)
            _safe_draw_image(c, letterhead_img, 0, 0, W, H)
            c.restoreState()

        if header_img:
            _safe_draw_image(c, header_img, 0, H - header_h, W, header_h)

        y_top = H - top

        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(left, y_top - 7 * mm, "Stock Transactions")

        org_name = (getattr(branding, "org_name", "") or "") if branding else ""
        org_addr = (getattr(branding, "org_address", "") or "") if branding else ""
        org_phone = (getattr(branding, "org_phone", "") or "") if branding else ""
        org_web = (getattr(branding, "org_website", "") or "") if branding else ""

        logo_w = 24 * mm
        logo_h = 24 * mm
        text_max_w = usable_w - (logo_w + 4 * mm if logo_img else 0)

        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)

        # ✅ no dots
        line2 = " | ".join([x for x in [org_name, org_phone, org_web] if x])
        y = y_top - 13.5 * mm
        if line2:
            c.drawString(left, y, line2[:200])
            y -= 5 * mm

        if org_addr:
            for ln in simpleSplit(org_addr, "Helvetica", 9, text_max_w)[:2]:
                c.drawString(left, y, ln)
                y -= 4.6 * mm

        if logo_img:
            _safe_draw_image(c, logo_img, W - right - logo_w, y_top - logo_h, logo_w, logo_h)

        if filters_text:
            c.setFont("Helvetica", 8.6)
            c.setFillColor(MUTED)
            c.drawString(left, y_top - 26.5 * mm, (filters_text or "").replace("•", "|"))

        c.setStrokeColor(BORDER)
        c.setLineWidth(1)
        c.line(left, y_top - 29 * mm, W - right, y_top - 29 * mm)

        return y_top - 32 * mm

    def draw_footer(page_no: int, total_pages: int):
        if footer_img:
            _safe_draw_image(c, footer_img, 0, 0, W, footer_h)

        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(left, 7.5 * mm, f"Generated: {datetime.now(IST).strftime('%d-%m-%Y %I:%M %p')}")
        if show_pages:
            c.drawRightString(W - right, 7.5 * mm, f"Page {page_no} of {total_pages}")

    c._page_number_cb = draw_footer

    def draw_table_header(y_pos: float) -> float:
        wrapped = []
        max_lines = 1
        for name, w in cols:
            max_w = w - 2 * pad_x
            lines = _wrap_text_hard(name, head_font, head_size, max_w)
            wrapped.append(lines)
            max_lines = max(max_lines, len(lines))

        head_h = max(9.2 * mm, (max_lines * head_line_h) + (2 * pad_y) + 1.6 * mm)

        x = start_x
        for (name, w), lines in zip(cols, wrapped):
            c.setFillColor(BG_HEAD)
            c.setStrokeColor(BORDER)
            c.setLineWidth(0.9)
            c.rect(x, y_pos - head_h, w, head_h, stroke=1, fill=1)

            c.setFont(head_font, head_size)
            c.setFillColor(MUTED)

            total_text_h = len(lines) * head_line_h
            baseline = y_pos - ((head_h - total_text_h) / 2) - (0.78 * head_line_h)

            for li, ln in enumerate(lines):
                c.drawString(x + pad_x, baseline - (li * head_line_h), ln)

            x += w

        return y_pos - head_h

    def draw_row(y_pos: float, r: Dict[str, Any], alt: bool) -> float:
        values = {
            "Date/Time": _fmt_dt_ist(r.get("txn_time")),
            "Ref": str(r.get("ref_display") or (f'{r.get("ref_type","")} #{r.get("ref_id")}' if r.get("ref_type") and r.get("ref_id") else "") or ""),
            "Item": str(r.get("item_name") or ""),
            "Batch": str(r.get("batch_no") or ""),
            "Loc": str(r.get("location_name") or ""),
            "Qty": _fmt_compact(r.get("quantity_change"), 4),
            "MRP": _fmt_compact(r.get("mrp"), 4),
            "User": str(r.get("user_name") or ""),
            "Doctor": str(r.get("doctor_name") or ""),
        }

        wrapped_cells: List[List[str]] = []
        max_lines = 1
        for name, w in cols:
            max_w = w - 2 * pad_x
            ml = max_lines_by_col.get(name, 2)
            lines = _wrap_limit_no_dots(values.get(name, ""), body_font, body_size, max_w, ml)
            wrapped_cells.append(lines)
            max_lines = max(max_lines, len(lines))

        row_h = max(8.2 * mm, (max_lines * body_line_h) + (2 * pad_y) + 1.6 * mm)

        if alt:
            c.setFillColor(ROW_ALT)
            c.rect(start_x, y_pos - row_h, table_w, row_h, stroke=0, fill=1)

        x = start_x
        for (name, w), lines in zip(cols, wrapped_cells):
            c.setStrokeColor(BORDER)
            c.setLineWidth(0.6)
            c.rect(x, y_pos - row_h, w, row_h, stroke=1, fill=0)

            c.setFont(body_font, body_size)
            c.setFillColor(TEXT)

            total_text_h = len(lines) * body_line_h
            baseline = y_pos - ((row_h - total_text_h) / 2) - (0.78 * body_line_h)

            for li, ln in enumerate(lines):
                yy = baseline - (li * body_line_h)
                if name in ("Qty", "MRP"):
                    c.drawRightString(x + w - pad_x, yy, ln)
                else:
                    c.drawString(x + pad_x, yy, ln)

            x += w

        return y_pos - row_h

    # -------------------------
    # Render
    # -------------------------
    y = draw_header()
    y = draw_table_header(y)

    usable_bottom = bottom + footer_h + 6 * mm
    alt = False

    for r in rows:
        # safe page break estimate
        if y - (14 * mm) < usable_bottom:
            c.showPage()
            y = draw_header()
            y = draw_table_header(y)

        y = draw_row(y, r, alt)
        alt = not alt

    c.save()
    return buf.getvalue()
