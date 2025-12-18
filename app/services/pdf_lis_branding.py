from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, List, Dict

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# APPLE-STYLE COLOR SYSTEM
# ---------------------------------------------------------
PRIMARY = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#64748B")
BORDER = colors.HexColor("#E5E7EB")
ROW_DIVIDER = colors.HexColor("#F1F5F9")

GOOD = colors.HexColor("#16A34A")
WARN = colors.HexColor("#F59E0B")
BAD = colors.HexColor("#DC2626")


# ---------------------------------------------------------
# SAFE FILE HANDLING
# ---------------------------------------------------------
def _safe_relpath(rel: str | None) -> str:
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    return rel.replace("..", "")


def _resolve_storage_file(storage_dir: str, rel_path: str | None) -> Optional[Path]:
    rel = _safe_relpath(rel_path)
    if not rel:
        return None
    p = Path(storage_dir).joinpath(rel)
    if p.exists() and p.is_file():
        return p
    return None


def _try_image_reader(path: Path | None) -> Optional[ImageReader]:
    if not path:
        return None
    try:
        return ImageReader(str(path))
    except Exception:
        logger.exception("Image load failed: %s", path)
        return None


# ---------------------------------------------------------
# LETTERHEAD BACKGROUND (OPTIONAL)
# ---------------------------------------------------------
def draw_letterhead_background(
    c: canvas.Canvas,
    *,
    branding: Any,
    storage_dir: str,
    page_num: int = 1,
) -> None:
    if not branding:
        return

    letterhead_path = getattr(branding, "letterhead_path", None)
    if not letterhead_path:
        return

    position = (getattr(branding, "letterhead_position", "background") or "").lower()
    if position == "none":
        return
    if position == "first_page_only" and page_num != 1:
        return

    p = _resolve_storage_file(storage_dir, letterhead_path)
    img = _try_image_reader(p)
    if not img:
        return

    try:
        w, h = A4
        c.drawImage(img, 0, 0, width=w, height=h, preserveAspectRatio=True, mask="auto")
    except Exception:
        logger.exception("Letterhead draw failed")


# ---------------------------------------------------------
# HEADER (USES YOUR EXISTING BRAND DATA)
# ---------------------------------------------------------
def draw_pdf_header(
    c: canvas.Canvas,
    *,
    branding: Any,
    storage_dir: str,
    page_num: int,
    width: float,
    height: float,
    left_margin: float,
    right_margin: float,
    header_h: float,
    primary_hex: str | None = None,
    small_title: str = "LABORATORY REPORT",
    right_small_text: str | None = None,
) -> None:
    if header_h <= 0:
        return

    # Resolve colors
    primary = colors.HexColor(primary_hex or "#0F172A")
    border = colors.HexColor("#E5E7EB")
    muted = colors.HexColor("#475569")

    y_top = height
    y_bottom = height - header_h

    # Header background
    c.setFillColor(colors.white)
    c.rect(0, y_bottom, width, header_h, stroke=0, fill=1)

    # Divider
    c.setStrokeColor(border)
    c.setLineWidth(0.8)
    c.line(left_margin, y_bottom, width - right_margin, y_bottom)

    # Logo
    logo_img = _try_image_reader(
        _resolve_storage_file(storage_dir, getattr(branding, "logo_path", None))
    )

    org_name = (
        getattr(branding, "org_name", None)
        or getattr(branding, "hospital_name", None)
        or "Medical Laboratory"
    )

    address = getattr(branding, "org_address", "") or ""
    phone = getattr(branding, "org_phone", "") or ""
    email = getattr(branding, "org_email", "") or ""
    website = getattr(branding, "org_website", "") or ""

    x = left_margin
    y = y_top - 8 * mm

    if logo_img:
        c.drawImage(logo_img, x, y - 18, width=18 * mm, height=18 * mm, mask="auto")
        x += 22 * mm

    # Org name
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(primary)
    c.drawString(x, y, str(org_name)[:80])

    # Address + contact
    c.setFont("Helvetica", 8)
    c.setFillColor(muted)

    y2 = y - 5 * mm
    for line in str(address).split("\n")[:2]:
        if line.strip():
            c.drawString(x, y2, line[:110])
            y2 -= 3.5 * mm

    contact = " | ".join(filter(None, [phone, email, website]))
    if contact:
        c.drawString(x, y2, contact[:120])

    # Right-side small title / lab no
    rx = width - right_margin
    if page_num > 1 or right_small_text:
        c.setFont("Helvetica-Bold", 9)



# ---------------------------------------------------------
# FOOTER
# ---------------------------------------------------------
def draw_pdf_footer(
    c: canvas.Canvas,
    *,
    branding: Any,
    page_num: int,
    width: float,
    left_margin: float,
    right_margin: float,
    bottom_margin: float,
    generated_on: str,
) -> None:
    footer_y = bottom_margin - 8 * mm

    c.setStrokeColor(BORDER)
    c.line(left_margin, bottom_margin - 4 * mm, width - right_margin, bottom_margin - 4 * mm)

    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(left_margin, footer_y, f"Generated on: {generated_on}")
    c.drawRightString(width - right_margin, footer_y, "Computer generated report")

    if getattr(branding, "pdf_show_page_number", True):
        c.drawRightString(width - right_margin, footer_y - 3 * mm, f"Page {page_num}")


# ---------------------------------------------------------
# PATIENT INFO CARD (APPLE STYLE)
# ---------------------------------------------------------
def draw_patient_card(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    w: float,
    patient: Dict[str, Any],
) -> float:
    h = 32 * mm

    c.setFillColor(colors.white)
    c.setStrokeColor(BORDER)
    c.roundRect(x, y - h, w, h, 6, stroke=1, fill=1)

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(PRIMARY)
    c.drawString(x + 8, y - 10, "Patient Information")

    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)

    fields = [
        ("Name", patient.get("name")),
        ("Age / Sex", patient.get("age_sex")),
        ("Patient ID", patient.get("patient_id")),
        ("Sample ID", patient.get("sample_id")),
        ("Collected On", patient.get("collected_on")),
        ("Reported On", patient.get("reported_on")),
    ]

    lx = x + 8
    ly = y - 16
    col_w = w / 2

    for i, (label, val) in enumerate(fields):
        if val:
            c.drawString(
                lx + (i % 2) * col_w,
                ly - (i // 2) * 5 * mm,
                f"{label}: {val}",
            )

    return y - h - 6 * mm


# ---------------------------------------------------------
# SUMMARY STRIP
# ---------------------------------------------------------
def draw_summary_strip(c, *, x: float, y: float, w: float, text: str) -> float:
    h = 14 * mm
    c.setFillColor(colors.HexColor("#F8FAFC"))
    c.roundRect(x, y - h, w, h, 6, stroke=0, fill=1)

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(PRIMARY)
    c.drawString(x + 10, y - 9, text)

    return y - h - 6 * mm


# ---------------------------------------------------------
# RESULTS TABLE (CORE SECTION)
# ---------------------------------------------------------
def draw_results_table(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    w: float,
    results: List[Dict[str, Any]],
) -> float:
    row_h = 9 * mm
    cols = [0.36, 0.14, 0.12, 0.22, 0.16]

    headers = ["Test", "Result", "Unit", "Reference Range", "Status"]

    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(PRIMARY)

    cx = x
    for h, p in zip(headers, cols):
        c.drawString(cx + 3, y, h)
        cx += w * p

    c.setStrokeColor(BORDER)
    c.line(x, y - 2, x + w, y - 2)

    y -= row_h
    c.setFont("Helvetica", 8)

    for r in results:
        cx = x
        flag = (r.get("flag") or "").upper()

        if flag == "H":
            color, status = BAD, "High"
        elif flag == "L":
            color, status = WARN, "Low"
        else:
            color, status = GOOD, "Normal"

        c.setFillColor(PRIMARY)
        c.drawString(cx + 3, y, str(r.get("name", "")))
        cx += w * cols[0]

        c.drawString(cx + 3, y, str(r.get("value", "")))
        cx += w * cols[1]

        c.drawString(cx + 3, y, str(r.get("unit", "")))
        cx += w * cols[2]

        c.drawString(cx + 3, y, str(r.get("range", "")))
        cx += w * cols[3]

        c.setFillColor(color)
        c.drawString(cx + 3, y, status)

        c.setStrokeColor(ROW_DIVIDER)
        c.line(x, y - 2, x + w, y - 2)

        y -= row_h

    return y - 6 * mm


# ---------------------------------------------------------
# NOTES & SIGNATURES
# ---------------------------------------------------------
def draw_notes(c, *, x: float, y: float, w: float, notes: str) -> float:
    if not notes:
        return y

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(PRIMARY)
    c.drawString(x, y, "Clinical Notes")

    y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)

    t = c.beginText(x, y)
    t.setLeading(12)
    for line in notes.split("\n"):
        t.textLine(line)
    c.drawText(t)

    return t.getY() - 8 * mm


def draw_signatures(c, *, x: float, y: float) -> None:
    c.setFont("Helvetica", 8)
    c.setFillColor(PRIMARY)
    c.drawString(x, y, "Authorized Signatory")
    c.drawString(x + 90 * mm, y, "Consultant Pathologist")
