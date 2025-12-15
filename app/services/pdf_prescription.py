# FILE: app/services/pdf_prescription.py
from __future__ import annotations

from io import BytesIO
from typing import Optional, List, Tuple
from datetime import datetime, date

import re
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors

from sqlalchemy.orm import Session
from app.models.patient import Patient
from app.models.user import User
from app.models.pharmacy_prescription import (
    PharmacyPrescription,
    PharmacyPrescriptionLine,
)
from app.models.ui_branding import UiBranding


# ----------------------------
# Doctor-style dosage helpers
# ----------------------------
TIMING_MAP = {
    "BF": "Before food",
    "AF": "After food",
    "AC": "Before food",
    "PC": "After food",
    "HS": "Bedtime",
}

def parse_man_from_frequency(freq: str | None, times_per_day: int | None = None) -> Tuple[int, int, int, str]:
    """
    Returns (m, a, n, extra_note)
    Supports:
      - "1-0-1"
      - BD/TDS/OD/HS/SOS/PRN/STAT
      - fallback times_per_day
    """
    f = (freq or "").strip().upper()

    if re.match(r"^\d+\-\d+\-\d+$", f):
        m, a, n = [int(x) for x in f.split("-")]
        return m, a, n, ""

    if f in ("OD", "QD", "DAILY"):
        return 1, 0, 0, ""
    if f in ("BD", "BID", "TWICE"):
        return 1, 0, 1, ""
    if f in ("TDS", "TID", "THRICE"):
        return 1, 1, 1, ""
    if f in ("HS",):
        return 0, 0, 1, ""
    if f in ("SOS", "PRN"):
        return 0, 0, 0, "PRN / SOS"
    if f in ("STAT",):
        return 1, 0, 0, "STAT"

    if times_per_day == 1:
        return 1, 0, 0, ""
    if times_per_day == 2:
        return 1, 0, 1, ""
    if times_per_day == 3:
        return 1, 1, 1, ""
    if times_per_day and times_per_day > 3:
        return 1, 1, 1, f"{times_per_day} times/day"

    return 0, 0, 0, ""


def parse_timing_tokens(timing: str | None) -> List[str]:
    """
    Example:
      "BF" -> ["BF"]
      "BF/AF" -> ["BF","AF"]
      "BF,AF" -> ["BF","AF"]
    """
    t = (timing or "").strip().upper()
    if not t:
        return []
    parts = re.split(r"[\/,\|\s\-]+", t)
    return [p for p in parts if p]


def dose_cell(count: int, timing_code: str | None) -> str:
    if not count or count <= 0:
        return "0"
    if timing_code:
        # compact for narrow cells
        return f"{count}({timing_code})"
    return str(count)


# ----------------------------
# Branding / text helpers
# ----------------------------
def _org(db: Session) -> dict:
    o = db.get(UiBranding, 1)
    if o:
        return {
            "name": o.org_name or "Hospital",
            "addr": o.org_address or "",
            "phone": o.org_phone or "",
            "email": o.org_email or "",
            "gst": o.org_gstin or "",
        }
    return {"name": "Hospital", "addr": "", "phone": "", "email": "", "gst": ""}


def _fmt_dt(x) -> str:
    if not x:
        return "—"
    try:
        if isinstance(x, (datetime,)):
            return x.strftime("%d-%m-%Y %H:%M")
        if isinstance(x, (date,)):
            return x.strftime("%d-%m-%Y")
        s = str(x)
        if "T" in s:
            return s.replace("T", " ")[:16]
        return s[:16]
    except Exception:
        return "—"


def _wrap(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    font_name: str = "Helvetica",
    font_size: float = 9,
    line_height: float = 11,
) -> float:
    """Simple left wrap; returns new y (after drawing)."""
    if not text:
        return y
    c.setFont(font_name, font_size)
    words = str(text).split()
    line = ""
    for w0 in words:
        test = (line + " " + w0).strip()
        if c.stringWidth(test, font_name, font_size) > max_width:
            if line:
                c.drawString(x, y, line)
                y -= line_height
            line = w0
        else:
            line = test
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y


def _patient_display(patient: Optional[Patient]) -> Tuple[str, str]:
    if not patient:
        return "—", "—"
    name = (
        f"{getattr(patient,'first_name','') or ''} {getattr(patient,'last_name','') or ''}".strip()
        or getattr(patient, "name", None)
        or "—"
    )
    uhid = (
        getattr(patient, "uhid", None)
        or getattr(patient, "patient_uid", None)
        or getattr(patient, "mrn", None)
        or "—"
    )
    return name, uhid


def _doctor_display(rx: PharmacyPrescription) -> str:
    doc: Optional[User] = getattr(rx, "doctor", None)
    return getattr(doc, "full_name", None) or getattr(doc, "name", None) or "—"


def _duration_text(l: PharmacyPrescriptionLine) -> str:
    if getattr(l, "duration_days", None):
        return f"{int(l.duration_days)} d"
    # fallback from dates
    sd = getattr(l, "start_date", None)
    ed = getattr(l, "end_date", None)
    if sd and ed:
        try:
            days = (ed - sd).days + 1
            if days > 0:
                return f"{days} d"
        except Exception:
            pass
    return "—"


# ----------------------------
# Page drawing
# ----------------------------
def _draw_header(
    c: canvas.Canvas,
    *,
    org: dict,
    rx: PharmacyPrescription,
    patient: Optional[Patient],
    left: float,
    right: float,
    top_y: float,
    page_no: int,
) -> float:
    y = top_y

    # Org name
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 13)
    c.drawString(left, y, org["name"])
    y -= 6 * mm

    # Org meta line
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#334155"))
    meta = " • ".join([x for x in [org.get("addr", ""), org.get("phone", ""), org.get("email", "")] if x])
    if meta:
        y = _wrap(c, meta, left, y, right - left, font_name="Helvetica", font_size=9, line_height=11)

    if org.get("gst"):
        c.setFont("Helvetica", 8.5)
        c.setFillColor(colors.HexColor("#64748b"))
        c.drawString(left, y, f"GSTIN: {org['gst']}")
        y -= 5 * mm

    # Divider
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.setLineWidth(1)
    c.line(left, y, right, y)
    y -= 8 * mm

    # Title + Rx info
    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "PRESCRIPTION")

    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#0f172a"))
    rx_no = getattr(rx, "prescription_number", None) or f"RX-{rx.id}"
    dt = _fmt_dt(getattr(rx, "created_at", None))
    c.drawRightString(right, y, f"Rx No: {rx_no}   Date: {dt}")
    y -= 8 * mm

    # Patient / Doctor row
    p_name, p_uhid = _patient_display(patient)
    doc_name = _doctor_display(rx)

    c.setFillColor(colors.HexColor("#475569"))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "Patient")
    c.drawString(left + 85 * mm, y, "Doctor")
    y -= 5 * mm

    c.setFillColor(colors.HexColor("#0f172a"))
    c.setFont("Helvetica", 9)
    c.drawString(left, y, f"{p_name}  (UHID: {p_uhid})")
    c.drawString(left + 85 * mm, y, doc_name)
    y -= 7 * mm

    # Small rx type
    rx_type = (getattr(rx, "type", None) or "").upper()
    if rx_type:
        c.setFillColor(colors.HexColor("#64748b"))
        c.setFont("Helvetica", 8)
        c.drawString(left, y, f"Type: {rx_type}")
        y -= 6 * mm

    # Divider
    c.setStrokeColor(colors.HexColor("#e2e8f0"))
    c.setLineWidth(1)
    c.line(left, y, right, y)
    y -= 7 * mm

    # Page number
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.setFont("Helvetica", 8)
    c.drawRightString(right, top_y + 2 * mm, f"Page {page_no}")

    return y


def _draw_table_header(c: canvas.Canvas, left: float, right: float, y: float, radius: float = 7) -> float:
    header_h = 9 * mm
    c.setFillColor(colors.HexColor("#f1f5f9"))
    c.roundRect(left, y - header_h, right - left, header_h, radius, fill=1, stroke=0)

    c.setFillColor(colors.HexColor("#475569"))
    c.setFont("Helvetica-Bold", 8)

    # column layout inside content width
    # total width = (right-left)
    # # 8mm, med 78mm, M 12mm, A 12mm, N 12mm, Days 18mm, Notes rest
    x = {
        "sl": left + 2 * mm,
        "med": left + 10 * mm,
        "m": left + 88 * mm,
        "a": left + 102 * mm,
        "n": left + 116 * mm,
        "dur": left + 130 * mm,
        "note": left + 150 * mm,
    }

    c.drawString(x["sl"], y - 6 * mm, "#")
    c.drawString(x["med"], y - 6 * mm, "Medicine")
    c.drawString(x["m"], y - 6 * mm, "M")
    c.drawString(x["a"], y - 6 * mm, "A")
    c.drawString(x["n"], y - 6 * mm, "N")
    c.drawString(x["dur"], y - 6 * mm, "Days")
    c.drawString(x["note"], y - 6 * mm, "Notes")

    return y - header_h - 2 * mm


# ----------------------------
# Main PDF builder
# ----------------------------
def build_prescription_pdf(db: Session, rx: PharmacyPrescription, patient: Optional[Patient]) -> bytes:
    org = _org(db)
    lines: List[PharmacyPrescriptionLine] = list(getattr(rx, "lines", None) or [])

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    margin = 16 * mm
    left = margin
    right = W - margin
    top = H - margin
    bottom = 18 * mm

    page_no = 1
    y = _draw_header(c, org=org, rx=rx, patient=patient, left=left, right=right, top_y=top, page_no=page_no)
    y = _draw_table_header(c, left, right, y)

    # column x positions and widths (must match header)
    x = {
        "sl": left + 2 * mm,
        "med": left + 10 * mm,
        "m": left + 88 * mm,
        "a": left + 102 * mm,
        "n": left + 116 * mm,
        "dur": left + 130 * mm,
        "note": left + 150 * mm,
    }
    note_w = right - x["note"] - 2 * mm
    med_w = x["m"] - x["med"] - 2 * mm

    # Row rendering
    if not lines:
        c.setFillColor(colors.HexColor("#64748b"))
        c.setFont("Helvetica", 9)
        c.drawString(left, y, "No medicines in this prescription.")
        y -= 10 * mm
    else:
        for i, l in enumerate(lines, start=1):
            # page break
            if y < bottom + 28 * mm:
                # footer on previous page
                c.setStrokeColor(colors.HexColor("#e2e8f0"))
                c.line(left, bottom + 12 * mm, right, bottom + 12 * mm)
                c.setFillColor(colors.HexColor("#64748b"))
                c.setFont("Helvetica", 8)
                c.drawString(left, bottom + 7 * mm, "Legend: BF=Before food, AF=After food, HS=Bedtime. (Computer-generated)")
                c.showPage()

                page_no += 1
                y = _draw_header(c, org=org, rx=rx, patient=patient, left=left, right=right, top_y=top, page_no=page_no)
                y = _draw_table_header(c, left, right, y)

            # zebra background
            if i % 2 == 0:
                c.setFillColor(colors.HexColor("#fcfcfd"))
                c.roundRect(left, y - 12 * mm, right - left, 12 * mm, 6, fill=1, stroke=0)

            # medicine text
            med = (getattr(l, "item_name", None) or "—").strip() or "—"
            strength = (getattr(l, "item_strength", None) or "").strip()
            if strength:
                med = f"{med} ({strength})"

            # M/A/N
            m, a, n, extra = parse_man_from_frequency(
                getattr(l, "frequency_code", None),
                getattr(l, "times_per_day", None),
            )

            # timing mapping (supports BF/AF split)
            tks = parse_timing_tokens(getattr(l, "timing", None))
            timing_for_slots = {"M": None, "A": None, "N": None}

            if len(tks) <= 1:
                one = tks[0] if tks else None
                timing_for_slots = {"M": one, "A": one, "N": one}
            else:
                nonzero = []
                if m > 0: nonzero.append("M")
                if a > 0: nonzero.append("A")
                if n > 0: nonzero.append("N")
                for idx, slot in enumerate(nonzero):
                    timing_for_slots[slot] = tks[idx] if idx < len(tks) else tks[-1]

            dur_txt = _duration_text(l)

            # notes: dose + route + instructions + extras
            dose = (getattr(l, "dose_text", None) or "").strip()
            route = (getattr(l, "route", None) or "").strip()
            instr = (getattr(l, "instructions", None) or "").strip()

            notes_parts = []
            if dose:
                notes_parts.append(dose)
            if route:
                notes_parts.append(route)
            if instr:
                notes_parts.append(instr)
            if extra:
                notes_parts.append(extra)

            notes = " • ".join([p for p in notes_parts if p]) or "—"

            # draw main row line (top baseline)
            c.setFillColor(colors.HexColor("#0f172a"))
            c.setFont("Helvetica", 9)
            c.drawString(x["sl"], y - 7 * mm, str(i))

            # medicine wrapped in its cell (1 line, clipped)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x["med"], y - 7 * mm, med[:55])

            # M/A/N cells
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(x["m"], y - 7 * mm, dose_cell(m, timing_for_slots["M"]))
            c.drawString(x["a"], y - 7 * mm, dose_cell(a, timing_for_slots["A"]))
            c.drawString(x["n"], y - 7 * mm, dose_cell(n, timing_for_slots["N"]))

            # duration
            c.setFont("Helvetica", 8.5)
            c.setFillColor(colors.HexColor("#334155"))
            c.drawString(x["dur"], y - 7 * mm, dur_txt)

            # notes wrapped (may take 1–2 lines inside the notes cell)
            c.setFillColor(colors.HexColor("#334155"))
            c.setFont("Helvetica", 8.5)
            note_y = y - 6.8 * mm
            note_y2 = _wrap(
                c,
                notes,
                x["note"],
                note_y,
                note_w,
                font_name="Helvetica",
                font_size=8.3,
                line_height=9.5,
            )

            # reduce y based on notes wrap
            used_h = max(12 * mm, (note_y - note_y2) + 6 * mm)
            y -= used_h

            # small gap
            y -= 1.5 * mm

    # Footer (final page)
    y_footer = max(y, bottom + 24 * mm)

    # legend + disclaimer
    c.setStrokeColor(colors.HexColor("#e2e8f0"))
    c.line(left, y_footer, right, y_footer)

    c.setFillColor(colors.HexColor("#64748b"))
    c.setFont("Helvetica", 8)
    c.drawString(left, y_footer - 6 * mm, "Legend: BF=Before food, AF=After food, HS=Bedtime.")
    c.drawString(left, y_footer - 11 * mm, "This prescription is computer-generated. Follow doctor instructions. Not for misuse.")

    # signature boxes (simple)
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.setLineWidth(1)
    sig_y = bottom + 10 * mm
    c.line(left, sig_y, left + 70 * mm, sig_y)
    c.line(right - 70 * mm, sig_y, right, sig_y)

    c.setFillColor(colors.HexColor("#64748b"))
    c.setFont("Helvetica", 8)
    c.drawString(left, sig_y - 5 * mm, "Doctor Signature")
    c.drawRightString(right, sig_y - 5 * mm, "Patient / Attendant")

    c.showPage()
    c.save()
    return buf.getvalue()
