# FILE: app/services/pdf_discharge.py
from __future__ import annotations

import io
import os
import html
import logging
from pathlib import Path
from datetime import datetime, date, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.models.ipd import (
    IpdAdmission,
    IpdDischargeSummary,
    IpdDischargeChecklist,
    IpdBed,
)
from app.models.patient import Patient
from app.models.user import User
from app.models.department import Department
from app.models.ui_branding import UiBranding

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# SAFE FILE HANDLING (your original logic)
# ---------------------------------------------------------------------------
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


def _get_storage_dir() -> str:
    """
    Where uploaded files live on disk.
    Priority:
      1) env STORAGE_DIR / UPLOAD_DIR / MEDIA_ROOT
      2) ./storage (project root)
    """
    for k in ("STORAGE_DIR", "UPLOAD_DIR", "MEDIA_ROOT"):
        v = (os.getenv(k) or "").strip()
        if v:
            return os.path.abspath(v)
    return os.path.abspath(os.path.join(os.getcwd(), "storage"))


def _resolve_branding_logo_reader(
    branding: Optional[UiBranding],
    storage_dir: str,
) -> Optional[ImageReader]:
    """
    Logo priority:
      1) If logo_path is absolute + exists => use
      2) Else treat logo_path as relative under storage_dir (safe)
      3) Else None
    """
    if not branding:
        return None

    raw = (getattr(branding, "logo_path", None) or "").strip()
    if not raw:
        return None

    # Skip URLs (ReportLab won't fetch remote)
    if raw.startswith("http://") or raw.startswith("https://"):
        return None

    # Absolute path support
    try:
        p = Path(raw)
        if p.is_absolute() and p.exists() and p.is_file():
            img = _try_image_reader(p)
            if img:
                return img
    except Exception:
        pass

    # Relative under storage_dir
    safe_p = _resolve_storage_file(storage_dir, raw)
    return _try_image_reader(safe_p)


# ---------------------------------------------------------------------------
# Helpers (IST formatting)
# ---------------------------------------------------------------------------
def _esc(text: Optional[str]) -> str:
    return html.escape(text) if text else ""


def _nl(text: Optional[str]) -> str:
    if not text:
        return ""
    return "<br/>".join(html.escape(x) for x in str(text).splitlines() if x is not None)


def _to_ist(dt: datetime) -> datetime:
    """
    Convert datetime to IST.
    - If tz-aware: convert to IST
    - If tz-naive: assume UTC (common in backend: datetime.utcnow()) and convert to IST
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _safe_date(dt: Optional[datetime | date]) -> str:
    if not dt:
        return ""
    try:
        if isinstance(dt, datetime):
            return _to_ist(dt).strftime("%d-%m-%Y")
        if isinstance(dt, date):
            return dt.strftime("%d-%m-%Y")
    except Exception:
        return ""
    return ""


def _safe_time(dt: Optional[datetime | date]) -> str:
    if not dt:
        return ""
    try:
        if isinstance(dt, datetime):
            return _to_ist(dt).strftime("%I:%M %p")  # IST
    except Exception:
        return ""
    return ""


def _safe_dt(dt: Optional[datetime | date], with_time: bool = True) -> str:
    if not dt:
        return ""
    try:
        if isinstance(dt, datetime):
            d = _to_ist(dt)
            return d.strftime("%d-%m-%Y %I:%M %p") if with_time else d.strftime("%d-%m-%Y")
        if isinstance(dt, date):
            return dt.strftime("%d-%m-%Y")
    except Exception:
        return ""
    return ""


def _patient_name(patient: Optional[Patient]) -> str:
    if not patient:
        return ""
    for attr in ("full_name", "name", "patient_name", "display_name"):
        val = getattr(patient, attr, None)
        if val:
            return str(val)
    first = getattr(patient, "first_name", None) or ""
    last = getattr(patient, "last_name", None) or ""
    mid = getattr(patient, "middle_name", None) or ""
    return " ".join([x for x in [first, mid, last] if x]).strip()


def _load_branding(db: Session) -> Optional[UiBranding]:
    try:
        return db.query(UiBranding).order_by(UiBranding.id.asc()).first()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Page X of Y canvas
# ---------------------------------------------------------------------------
class NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        # ✅ Save current page state, then start a new page WITHOUT finalizing twice
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()  # ✅ IMPORTANT (prevents duplicate pages)

    def save(self):
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            if getattr(self, "_draw_page_count_cb", None):
                self._draw_page_count_cb(self, page_count)
            super().showPage()
        super().save()



# ---------------------------------------------------------------------------
# Header / Footer (FIXED ALIGNMENT)
# ---------------------------------------------------------------------------
def _draw_letterhead_header_footer(
    branding: Optional[UiBranding],
    ctx: dict,
    *,
    storage_dir: str,
    header_h_mm: int,
    footer_h_mm: int,
    show_page_number: bool,
):
    styles = getSampleStyleSheet()

    org_name_style = ParagraphStyle(
        "OrgName",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=13.5,
        textColor=colors.black,
        alignment=TA_RIGHT,   # ✅ RIGHT ALIGN
    )
    org_tag_style = ParagraphStyle(
        "OrgTag",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=10.5,
        textColor=colors.black,
        alignment=TA_RIGHT,   # ✅ RIGHT ALIGN
    )
    org_meta_style = ParagraphStyle(
        "OrgMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=9.6,
        textColor=colors.black,
        alignment=TA_RIGHT,   # ✅ RIGHT ALIGN
    )
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9,
        textColor=colors.black,
    )

    LOGO_W = 70 * mm  # requirement

    def on_page(c: rl_canvas.Canvas, doc: SimpleDocTemplate):
        page_w, page_h = A4
        left = doc.leftMargin
        right = page_w - doc.rightMargin

        header_h = header_h_mm * mm
        footer_h = footer_h_mm * mm

        top = page_h
        header_bottom = top - header_h

        # Divider under header
        c.saveState()
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(left, header_bottom, right, header_bottom)
        c.restoreState()

        pad_top = 2 * mm
        pad_bottom = 1 * mm


        # ✅ LOGO TOP-LEFT aligned
        logo_box_h = max(1, header_h - (pad_top + pad_bottom))
        logo_x = left
        logo_y = top - pad_top - logo_box_h  # top aligned

        img = _resolve_branding_logo_reader(branding, storage_dir)
        if img:
            try:
                c.drawImage(
                    img,
                    logo_x,
                    logo_y,
                    width=LOGO_W,
                    height=logo_box_h,
                    preserveAspectRatio=True,
                    anchor="nw",  # top-left anchor feel
                    mask="auto",
                )
            except Exception:
                logger.exception("drawImage failed for branding logo")

        # ✅ ORG BLOCK TOP-RIGHT aligned
        gap = 6 * mm
        avail_w = (right - left) - LOGO_W - gap
        if avail_w < 40 * mm:
            # very small space fallback
            text_x = left + LOGO_W + gap
            text_w = max(0, right - text_x)
        else:
            text_w = min(avail_w, 120 * mm)  # keep it right-side column
            text_x = right - text_w

        org_name = ctx.get("org_name", "") or ""
        org_tagline = ctx.get("org_tagline", "") or ""
        org_phone = ctx.get("org_phone", "") or ""
        org_website = ctx.get("org_website", "") or ""
        org_address = ctx.get("org_address", "") or ""

        meta_line = " | ".join(
            [
                x
                for x in [
                    (f"Ph: {_esc(org_phone)}" if org_phone else ""),
                    (f"W: {_esc(org_website)}" if org_website else ""),
                ]
                if x
            ]
        )
        # ✅ Use FULL remaining width after logo, but text ends at RIGHT margin
        gap = 6 * mm
        text_x = left + LOGO_W + gap
        text_w = max(0, right - text_x)

        # top start
        y = top - pad_top

        p1 = Paragraph(_esc(org_name), org_name_style)
        _, h1 = p1.wrap(text_w, header_h)
        p1.drawOn(c, text_x, y - h1)
        y -= (h1 + 1.2 * mm)

        if org_tagline:
            p2 = Paragraph(_esc(org_tagline), org_tag_style)
            _, h2 = p2.wrap(text_w, header_h)
            p2.drawOn(c, text_x, y - h2)
            y -= (h2 + 1.0 * mm)

        meta_block = "<br/>".join([x for x in [meta_line, _esc(org_address)] if x]).strip()
        if meta_block:
            max_h = max(10 * mm, y - (header_bottom + pad_bottom))
            p3 = Paragraph(meta_block, org_meta_style)
            _, h3 = p3.wrap(text_w, max_h)
            p3.drawOn(c, text_x, y - h3)

        # Footer line
        c.saveState()
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(left, footer_h, right, footer_h)
        c.restoreState()

        ftxt = "Confidential Medical Record • Computer-generated document • For emergencies, visit Emergency/Casualty immediately."
        pf = Paragraph(ftxt, footer_style)
        pf.wrap((right - left) * 0.78, footer_h - 2 * mm)
        pf.drawOn(c, left, 2 * mm)

    def draw_page_count(c: rl_canvas.Canvas, page_count: int):
        if not show_page_number:
            return
        page_w, _ = A4
        right_margin = getattr(c, "_doc_right_margin", 15 * mm)
        right = page_w - right_margin
        c.saveState()
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.black)
        c.drawRightString(right, 2.2 * mm, f"Page {c.getPageNumber()} of {page_count}")
        c.restoreState()

    return on_page, draw_page_count


# ---------------------------------------------------------------------------
# Build context
# ---------------------------------------------------------------------------
def build_discharge_context(db: Session, admission_id: int) -> dict:
    adm: IpdAdmission | None = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise ValueError("Admission not found")

    patient: Patient | None = db.query(Patient).get(adm.patient_id) if getattr(adm, "patient_id", None) else None
    dept: Department | None = db.query(Department).get(adm.department_id) if getattr(adm, "department_id", None) else None
    doctor: User | None = db.query(User).get(adm.practitioner_user_id) if getattr(adm, "practitioner_user_id", None) else None

    s: IpdDischargeSummary | None = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )

    _ = (
        db.query(IpdDischargeChecklist)
        .filter(IpdDischargeChecklist.admission_id == admission_id)
        .first()
    )

    b = _load_branding(db)

    org_name = (getattr(b, "org_name", None) if b else None) or "HOSPITAL NAME"
    org_tagline = (getattr(b, "org_tagline", None) if b else None) or ""
    org_address = (getattr(b, "org_address", None) if b else None) or ""
    org_phone = (getattr(b, "org_phone", None) if b else None) or ""
    org_website = (getattr(b, "org_website", None) if b else None) or ""

    tel = getattr(patient, "phone", None) or getattr(patient, "tel_no", None) or ""
    mobile = getattr(patient, "mobile", None) or getattr(patient, "mobile_no", None) or getattr(patient, "phone", None) or ""

    doc_phone = getattr(doctor, "mobile", None) or getattr(doctor, "phone", None) or ""

    admitted_at = getattr(adm, "admitted_at", None)
    discharge_dt = (getattr(s, "discharge_datetime", None) if s else None) or getattr(adm, "discharged_at", None) or getattr(adm, "discharge_datetime", None)

    ipd_no = (
        getattr(adm, "ipd_no", None)
        or getattr(adm, "ipd_number", None)
        or getattr(adm, "admission_code", None)
        or f"IP-{adm.id:06d}"
    )
    admission_no = (
        getattr(adm, "admission_no", None)
        or getattr(adm, "admission_number", None)
        or str(adm.id)
    )

    consultant_block = " / ".join([x for x in [
        (_esc(getattr(doctor, "full_name", None) or getattr(doctor, "name", None) or "") if doctor else ""),
        (_esc(doc_phone) if doc_phone else ""),
        (_esc(getattr(dept, "name", None) or "") if dept else ""),
    ] if x]).strip()

    provisional_dx = getattr(s, "provisional_diagnosis", None) if s else ""
    final_dx = " / ".join([x for x in [
        getattr(s, "final_diagnosis_primary", "") if s else "",
        getattr(s, "final_diagnosis_secondary", "") if s else "",
    ] if x]).strip()

    icd10 = getattr(s, "icd10_codes", "") if s else ""
    presenting_complaints = getattr(s, "presenting_complaints", "") if s else ""
    summary_illness = getattr(s, "summary_presenting_illness", "") if s else ""
    if not summary_illness:
        summary_illness = getattr(s, "hospital_course", "") if s else ""

    key_findings = getattr(s, "key_findings", "") if s else ""
    substance = getattr(s, "substance_abuse_history", "") if s else ""
    past_history = getattr(s, "medical_history", "") if s else ""
    family_history = getattr(s, "family_history", "") if s else ""
    investigations = getattr(s, "investigations", "") if s else ""
    course = getattr(s, "hospital_course", "") if s else ""

    meds = getattr(s, "medications", "") if s else ""
    follow = getattr(s, "follow_up", "") if s else ""
    diet = getattr(s, "diet_instructions", "") if s else ""
    activity = getattr(s, "activity_instructions", "") if s else ""
    warning = getattr(s, "warning_signs", "") if s else ""

    advice_parts = []
    if meds:
        advice_parts.append("Medications:\n" + str(meds))
    if diet:
        advice_parts.append("Diet:\n" + str(diet))
    if activity:
        advice_parts.append("Activity:\n" + str(activity))
    if follow:
        advice_parts.append("Follow-up:\n" + str(follow))
    if warning:
        advice_parts.append("Warning signs:\n" + str(warning))
    advice = "\n\n".join(advice_parts)

    patient_ack_name = getattr(s, "patient_ack_name", "") if s else ""
    if not patient_ack_name:
        patient_ack_name = _patient_name(patient)

    reviewed_by = getattr(s, "reviewed_by_name", "") if s else ""
    prepared_by = getattr(s, "prepared_by_name", "") if s else ""
    reviewed_reg = getattr(s, "reviewed_by_regno", "") if s else ""
    mlc = getattr(adm, "mlc_no", None) or (getattr(s, "mlc_no", None) if s else "") or ""

    return {
        "org_name": org_name,
        "org_tagline": org_tagline,
        "org_address": org_address,
        "org_phone": org_phone,
        "org_website": org_website,

        "patient_name": _patient_name(patient),
        "tel": str(tel or ""),
        "mobile": str(mobile or ""),
        "ipd_no": str(ipd_no or ""),
        "admission_no": str(admission_no or ""),
        "consultant_block": consultant_block,

        # ✅ IST output
        "date_adm": _safe_date(admitted_at),
        "time_adm": _safe_time(admitted_at),
        "date_dis": _safe_date(discharge_dt),
        "time_dis": _safe_time(discharge_dt),

        "mlc": str(mlc or ""),
        "provisional_dx": str(provisional_dx or ""),
        "final_dx": str(final_dx or ""),
        "icd10": str(icd10 or ""),
        "presenting_complaints": str(presenting_complaints or ""),
        "summary_illness": str(summary_illness or ""),
        "key_findings": str(key_findings or ""),
        "substance": str(substance or ""),
        "past_history": str(past_history or ""),
        "family_history": str(family_history or ""),
        "investigations": str(investigations or ""),
        "course": str(course or ""),
        "advice": str(advice or ""),

        "doctor_name": (getattr(doctor, "full_name", None) or getattr(doctor, "name", None) or ""),
        "prepared_by": str(prepared_by or ""),
        "reviewed_by": str(reviewed_by or ""),
        "reviewed_regno": str(reviewed_reg or ""),
        "patient_ack_name": str(patient_ack_name or ""),
    }


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------
def render_discharge_summary_pdf(db: Session, admission_id: int) -> bytes:
    ctx = build_discharge_context(db, admission_id)
    branding = _load_branding(db)
    storage_dir = _get_storage_dir()

    header_h_mm = int(getattr(branding, "pdf_header_height_mm", None) or 28) if branding else 40
    header_h_mm = max(22, min(header_h_mm, 32))
    footer_h_mm = int(getattr(branding, "pdf_footer_height_mm", None) or 14) if branding else 14
    show_page_number = bool(getattr(branding, "pdf_show_page_number", True) if branding else True)

    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=(header_h_mm * mm) + (0.5 * mm),     # ✅ reduced gap under header
        bottomMargin=(footer_h_mm * mm) + (2 * mm),  # ✅ slightly tighter
    )

    on_page, draw_page_count = _draw_letterhead_header_footer(
        branding,
        ctx,
        storage_dir=storage_dir,
        header_h_mm=header_h_mm,
        footer_h_mm=footer_h_mm,
        show_page_number=show_page_number,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        alignment=1,
        textColor=colors.black,
        spaceAfter=4,
    )
    cell_label = ParagraphStyle(
        "CellLabel",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.black,
    )
    cell_value = ParagraphStyle(
        "CellValue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.black,
    )

    def L(text: str) -> Paragraph:
        return Paragraph(_esc(text), cell_label)

    def V(text: str) -> Paragraph:
        return Paragraph(_nl(text) if text else "", cell_value)

    story: list = []
    story.append(Paragraph("DISCHARGE SUMMARY", title_style))
    story.append(Spacer(1, 0.8 * mm))  # ✅ tighter


    colw = [35 * mm, 55 * mm, 35 * mm, 55 * mm]
    top_rows = [
        [L("Name of Patient:"), V(ctx["patient_name"]), "", ""],
        [L("Tel No."), V(ctx["tel"]), L("Mobile No."), V(ctx["mobile"])],
        [L("IPD No."), V(ctx["ipd_no"]), L("Admission No."), V(ctx["admission_no"])],
        [L("Treating Consultant/s Name, contact numbers\nand Department/Specialty"), V(ctx["consultant_block"]), "", ""],
        [L("Date of Admission"), V(ctx["date_adm"]), L("Time of Admission"), V(ctx["time_adm"])],
        [L("Date of Discharge"), V(ctx["date_dis"]), L("Time of Discharge"), V(ctx["time_dis"])],
    ]

    top = Table(top_rows, colWidths=colw)
    top.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 1, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("SPAN", (1, 0), (3, 0)),
        ("SPAN", (1, 3), (3, 3)),
    ]))
    story.append(top)
    story.append(Spacer(1, 3 * mm))

    mlc_tbl = Table([[L("MLC No. / FIR No."), V(ctx["mlc"])]], colWidths=[60 * mm, sum(colw) - 60 * mm])
    mlc_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 1, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(mlc_tbl)

    main_rows = [
        ["Provisional Diagnosis at the time of Admission", ctx["provisional_dx"]],
        ["Final Diagnosis at the time of Discharge", ctx["final_dx"]],
        ["ICD-10 code(s) or any other codes, as recommended\nby the Authority, for Final diagnosis", ctx["icd10"]],
        ["Presenting Complaints with Duration and Reason\nfor Admission", ctx["presenting_complaints"]],
        ["Summary of Presenting Illness", ctx["summary_illness"]],
        ["Key findings, on physical examination at the time of\nadmission", ctx["key_findings"]],
        ["History of alcoholism, tobacco or substance abuse,\nif any", ctx["substance"]],
        ["Significant Past Medical and Surgical History, if any", ctx["past_history"]],
        ["Family History if significant/relevant to diagnosis or\ntreatment", ctx["family_history"]],
        ["Summary of key investigations during\nHospitalization", ctx["investigations"]],
        ["Course in the Hospital including complications, if\nany", ctx["course"]],
        ["Advice on Discharge", ctx["advice"]],
    ]

    main = Table([[L(a), V(b)] for a, b in main_rows], colWidths=[75 * mm, sum(colw) - 75 * mm])
    main.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 1, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(main)

    story.append(Spacer(1, 3 * mm))

    sign_rows = [
        [L("Name of treating\nConsultant/ Authorized\nTeam Doctor"), L("Signature of treating\nConsultant/ Authorized\nTeam Doctor")],
        [V(ctx["doctor_name"] or ctx["prepared_by"] or ctx["reviewed_by"]), V("____________________________")],
        [L("Name of Patient /\nAttendant"), L("Signature of Patient /\nAttendant")],
        [V(ctx["patient_ack_name"]), V("____________________________")],
    ]
    sign = Table(sign_rows, colWidths=[(sum(colw) / 2), (sum(colw) / 2)])
    sign.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 1, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(sign)

    def _on_page(c, d):
        c._doc_right_margin = d.rightMargin
        on_page(c, d)

    def canvas_maker(*args, **kwargs):
        c = NumberedCanvas(*args, **kwargs)
        c._draw_page_count_cb = draw_page_count
        return c

    doc.build(
        story,
        onFirstPage=_on_page,
        onLaterPages=_on_page,
        canvasmaker=canvas_maker,
    )

    buf.seek(0)
    return buf.getvalue()


def generate_discharge_summary_pdf(
    db: Session,
    admission_id: int,
    org_name: Optional[str] = None,
    org_address: Optional[str] = None,
    org_phone: Optional[str] = None,
    org_tagline: Optional[str] = None,
    org_email: Optional[str] = None,
) -> bytes:
    return render_discharge_summary_pdf(db, admission_id)
