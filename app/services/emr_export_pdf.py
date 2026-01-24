# FILE: app/services/emr_export_pdf.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime
from typing import Any, List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit

from sqlalchemy.orm import Session

from app.models.emr_all import EmrTemplateVersion, EmrRecord


def _s(x) -> str:
    return "" if x is None else str(x)

def _dt(x) -> str:
    try:
        if not x:
            return ""
        if isinstance(x, str):
            return x
        return x.strftime("%d-%b-%Y %H:%M")
    except Exception:
        return _s(x)

def _patient_line(patient) -> str:
    # Safe field reads (works with different Patient schemas)
    uhid = getattr(patient, "uhid", "") or getattr(patient, "patient_id", "") or ""
    first = getattr(patient, "first_name", "") or ""
    last = getattr(patient, "last_name", "") or ""
    name = (f"{first} {last}").strip() or getattr(patient, "name", "") or "Patient"
    gender = getattr(patient, "gender", "") or ""
    phone = getattr(patient, "phone", "") or ""
    return f"{name} | UHID: {uhid} | {gender} | {phone}".strip(" |")


def _draw_watermark(c: canvas.Canvas, text: str):
    if not text:
        return
    c.saveState()
    c.setFillColor(colors.lightgrey)
    c.setFont("Helvetica-Bold", 44)
    c.translate(105 * mm, 150 * mm)
    c.rotate(30)
    c.drawCentredString(0, 0, text)
    c.restoreState()


def build_export_pdf_bytes(*, patient: Any, bundle_title: str, records: List[EmrRecord], watermark: Optional[str], db: Session) -> bytes:
    buff = BytesIO()
    c = canvas.Canvas(buff, pagesize=A4)
    W, H = A4

    def header():
        _draw_watermark(c, watermark or "")
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(colors.black)
        c.drawString(18 * mm, H - 18 * mm, _s(bundle_title or "EMR Export Bundle"))
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.grey)
        c.drawString(18 * mm, H - 24 * mm, f"Generated: {_dt(datetime.utcnow())}")
        if patient:
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 10)
            c.drawString(18 * mm, H - 30 * mm, _patient_line(patient))

        c.setStrokeColor(colors.lightgrey)
        c.line(18 * mm, H - 34 * mm, W - 18 * mm, H - 34 * mm)

    def new_page():
        c.showPage()
        header()

    header()

    y = H - 42 * mm
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.black)

    if not records:
        c.drawString(18 * mm, y, "No records selected.")
        c.showPage()
        c.save()
        return buff.getvalue()

    for idx, r in enumerate(records, start=1):
        if y < 35 * mm:
            new_page()
            y = H - 42 * mm

        # Record header block
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(18 * mm, y, f"{idx}. {_s(r.title)}")
        y -= 5 * mm

        c.setFont("Helvetica", 9)
        c.setFillColor(colors.grey)
        line = f"{_s(r.dept_code)} | {_s(r.record_type_code)} | {_s(getattr(r.status, 'value', r.status))} | Created: {_dt(r.created_at)}"
        if r.signed_at:
            line += f" | Signed: {_dt(r.signed_at)}"
        c.drawString(18 * mm, y, line[:140])
        y -= 5 * mm

        # Sections from template
        sections = []
        if r.template_version_id:
            v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(r.template_version_id)).one_or_none()
            if v:
                try:
                    import json
                    sections = json.loads(v.sections_json or "[]")
                except Exception:
                    sections = []

        if sections:
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(18 * mm, y, "Sections:")
            y -= 4 * mm

            c.setFont("Helvetica", 9)
            c.setFillColor(colors.black)
            sline = " · ".join([_s(s) for s in sections][:30])
            parts = simpleSplit(sline, "Helvetica", 9, W - 36 * mm)
            for p in parts[:5]:
                if y < 30 * mm:
                    new_page()
                    y = H - 42 * mm
                c.drawString(22 * mm, y, p)
                y -= 4 * mm

        # Note (optional)
        if r.note:
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(18 * mm, y, "Note:")
            y -= 4 * mm
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.black)
            parts = simpleSplit(_s(r.note), "Helvetica", 9, W - 36 * mm)
            for p in parts[:6]:
                if y < 30 * mm:
                    new_page()
                    y = H - 42 * mm
                c.drawString(22 * mm, y, p)
                y -= 4 * mm

        c.setStrokeColor(colors.lightgrey)
        c.line(18 * mm, y, W - 18 * mm, y)
        y -= 6 * mm

    c.showPage()
    c.save()
    return buff.getvalue()
