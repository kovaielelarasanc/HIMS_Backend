from __future__ import annotations

import io
from datetime import datetime, date
from typing import Optional, Dict, Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    # store UTC; display as-is (front-end can show IST) - keep PDF stable
    return dt.strftime("%d-%b-%Y %I:%M %p")


def _fmt_d(d: Optional[date]) -> str:
    if not d:
        return "—"
    return d.strftime("%d-%b-%Y")


def _draw_kv(c: canvas.Canvas, x: float, y: float, k: str, v: str, w: float = 90 * mm):
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.black)
    c.drawString(x, y, k)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x + 45 * mm, y, v[:80])
    c.setFont("Helvetica", 9)


def _header(c: canvas.Canvas, title: str, hospital: Dict[str, Any]):
    c.setFont("Helvetica-Bold", 13)
    c.drawString(18 * mm, 285 * mm, hospital.get("name", "Hospital / Facility"))
    c.setFont("Helvetica", 9)
    addr = hospital.get("address", "")
    if addr:
        c.drawString(18 * mm, 280 * mm, str(addr)[:120])
    phone = hospital.get("phone", "")
    web = hospital.get("website", "")
    line = " | ".join([x for x in [phone, web] if x])
    if line:
        c.drawString(18 * mm, 276 * mm, line[:120])

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(18 * mm, 273 * mm, 192 * mm, 273 * mm)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, 266 * mm, title)

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawRightString(192 * mm, 266 * mm, f"Generated: {datetime.utcnow().strftime('%d-%b-%Y %H:%M')} UTC")
    c.setFillColor(colors.black)


def build_birth_form_pdf(birth: Any, hospital: Optional[Dict[str, Any]] = None) -> bytes:
    """
    Hospital Birth Intimation / CRS Form 1 (HMIS-generated)
    birth: BirthRegister ORM
    hospital: {"name","address","phone","website"}
    """
    hospital = hospital or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _header(c, "Birth Report (CRS Form 1) - Hospital Intimation", hospital)

    y = 255 * mm
    _draw_kv(c, 18 * mm, y, "Internal No:", birth.internal_no); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Birth Date/Time:", _fmt_dt(birth.birth_datetime)); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Place of Birth:", birth.place_of_birth or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Child Sex:", birth.child_sex or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Child Name:", birth.child_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Birth Weight:", birth.birth_weight_kg or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Gestation:", f"{birth.gestation_weeks} weeks" if birth.gestation_weeks else "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Mother Details"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    _draw_kv(c, 18 * mm, y, "Name:", birth.mother_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Age:", str(birth.mother_age_years) if birth.mother_age_years else "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "DOB:", _fmt_d(birth.mother_dob)); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Mobile:", birth.mother_mobile or "—"); y -= 6 * mm
    addr = birth.mother_address or {}
    _draw_kv(c, 18 * mm, y, "Address:", ", ".join([str(addr.get(k)) for k in ["line1","line2","city","district","state","pincode"] if addr.get(k)]) or "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Father Details"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    _draw_kv(c, 18 * mm, y, "Name:", birth.father_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Mobile:", birth.father_mobile or "—"); y -= 6 * mm
    faddr = birth.father_address or {}
    _draw_kv(c, 18 * mm, y, "Address:", ", ".join([str(faddr.get(k)) for k in ["line1","line2","city","district","state","pincode"] if faddr.get(k)]) or "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Informant / Facility Staff"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    _draw_kv(c, 18 * mm, y, "Name:", birth.informant_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Designation:", birth.informant_designation or "—"); y -= 10 * mm

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(18 * mm, 20 * mm, "Note: This is a hospital-generated intimation/report for CRS registration. Official certificate is issued by CRS authority.")
    c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buf.getvalue()


def build_death_form_pdf(death: Any, hospital: Optional[Dict[str, Any]] = None) -> bytes:
    """
    Hospital Death Intimation / CRS Form 2 (HMIS-generated)
    """
    hospital = hospital or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _header(c, "Death Report (CRS Form 2) - Hospital Intimation", hospital)

    y = 255 * mm
    _draw_kv(c, 18 * mm, y, "Internal No:", death.internal_no); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Death Date/Time:", _fmt_dt(death.death_datetime)); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Place of Death:", death.place_of_death or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Ward/Unit:", death.ward_or_unit or "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Deceased Details"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    _draw_kv(c, 18 * mm, y, "Name:", death.deceased_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Sex:", death.sex or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Age:", str(death.age_years) if death.age_years is not None else "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "DOB:", _fmt_d(death.dob)); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Manner:", death.manner_of_death or "—"); y -= 6 * mm

    addr = death.address or {}
    _draw_kv(c, 18 * mm, y, "Address:", ", ".join([str(addr.get(k)) for k in ["line1","line2","city","district","state","pincode"] if addr.get(k)]) or "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "MCCD (Form 4) - Summary"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    mccd = getattr(death, "mccd", None)
    if mccd:
        _draw_kv(c, 18 * mm, y, "Immediate Cause:", mccd.immediate_cause or "—"); y -= 6 * mm
        _draw_kv(c, 18 * mm, y, "Antecedent:", mccd.antecedent_cause or "—"); y -= 6 * mm
        _draw_kv(c, 18 * mm, y, "Underlying:", mccd.underlying_cause or "—"); y -= 6 * mm
    else:
        c.setFillColor(colors.red)
        c.drawString(18 * mm, y, "MCCD not recorded yet.")
        c.setFillColor(colors.black)
        y -= 8 * mm

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(18 * mm, 20 * mm, "Note: Hospital-generated intimation. MCCD (Form 4) must be provided to next of kin as per rules.")
    c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buf.getvalue()


def build_mccd_pdf(death: Any, hospital: Optional[Dict[str, Any]] = None) -> bytes:
    """
    MCCD Form 4 style certificate (simplified, HMIS-generated).
    """
    hospital = hospital or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _header(c, "Medical Certificate of Cause of Death (MCCD - Form 4)", hospital)

    y = 255 * mm
    _draw_kv(c, 18 * mm, y, "Death Internal No:", death.internal_no); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Deceased Name:", death.deceased_name or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Death Date/Time:", _fmt_dt(death.death_datetime)); y -= 10 * mm

    mccd = getattr(death, "mccd", None)
    if not mccd:
        c.setFillColor(colors.red)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(18 * mm, y, "MCCD not recorded.")
        c.setFillColor(colors.black)
        c.showPage()
        c.save()
        return buf.getvalue()

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Causes of Death"); y -= 6 * mm
    c.setFont("Helvetica", 9)

    _draw_kv(c, 18 * mm, y, "Immediate:", mccd.immediate_cause or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Antecedent:", mccd.antecedent_cause or "—"); y -= 6 * mm
    _draw_kv(c, 18 * mm, y, "Underlying:", mccd.underlying_cause or "—"); y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(18 * mm, y, "Other significant conditions"); y -= 6 * mm
    c.setFont("Helvetica", 9)
    txt = c.beginText(18 * mm, y)
    txt.setFont("Helvetica", 9)
    other = (mccd.other_significant_conditions or "—").replace("\n", " ")
    for chunk in [other[i:i+95] for i in range(0, len(other), 95)]:
        txt.textLine(chunk)
    c.drawText(txt)

    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, 55 * mm, f"Certifying Doctor User ID: {mccd.certifying_doctor_user_id or '—'}")
    c.drawString(18 * mm, 49 * mm, f"Certified At: {_fmt_dt(mccd.certified_at)}")
    c.drawString(18 * mm, 43 * mm, f"Signed: {'Yes' if mccd.signed else 'No'}  | Signed At: {_fmt_dt(mccd.signed_at)}")

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(18 * mm, 20 * mm, "This HMIS-generated MCCD is for facility workflow; ensure compliance with local statutory format and signatures.")
    c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buf.getvalue()
