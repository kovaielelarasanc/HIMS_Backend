# FILE: app/services/pdf_discharge.py
from __future__ import annotations

import io
import html
from datetime import datetime, date
from typing import Optional

from sqlalchemy.orm import Session

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_dt(
    value: Optional[datetime | date],
    with_time: bool = True,
    default: str = "",
) -> str:
    """
    Safely format datetime/date. Handles None and weird values gracefully.
    """
    if not value:
        return default

    # If MySQL "zero date" somehow leaks through as string
    if isinstance(value, str) and value.startswith("0000-00-00"):
        return default

    try:
        if isinstance(value, datetime):
            fmt = "%d-%m-%Y %I:%M %p" if with_time else "%d-%m-%Y"
            return value.strftime(fmt)
        if isinstance(value, date):
            return value.strftime("%d-%m-%Y")
        # Fallback: try to parse string
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace(" ", "T"))
            fmt = "%d-%m-%Y %I:%M %p" if with_time else "%d-%m-%Y"
            return dt.strftime(fmt)
    except Exception:
        return default

    return default


def _esc(text: Optional[str]) -> str:
    """HTML-escape plain text (None-safe)."""
    if not text:
        return ""
    return html.escape(text)


def _nl2br(text: Optional[str]) -> str:
    """Convert multi-line text into HTML with <br> (with proper escaping)."""
    if not text:
        return ""
    return "<br>".join(html.escape(line) for line in text.splitlines())


def _age_sex(patient: Optional[Patient]) -> str:
    if not patient:
        return ""
    age_str = ""
    try:
        if getattr(patient, "age_years", None) is not None:
            age_str = f"{patient.age_years} Y"
        elif getattr(patient, "dob", None):
            today = date.today()
            years = today.year - patient.dob.year - (
                (today.month, today.day) < (patient.dob.month, patient.dob.day)
            )
            age_str = f"{years} Y"
    except Exception:
        pass

    sex = getattr(patient, "gender", "") or getattr(patient, "sex", "") or ""
    if sex:
        sex = sex.upper()[0]
    parts = [p for p in [age_str, sex] if p]
    return " / ".join(parts)


def _br_to_para(text: Optional[str]) -> str:
    """
    Convert our stored HTML-ish strings (with <br>) into something
    ReportLab Paragraph can understand.
    """
    if not text:
        return ""
    return text.replace("<br>", "<br/>")


# ---------------------------------------------------------------------------
# Core: Build context from DB
# ---------------------------------------------------------------------------

def build_discharge_context(
    db: Session,
    admission_id: int,
    org_name: Optional[str] = None,
    org_tagline: Optional[str] = None,
    org_address: Optional[str] = None,
    org_phone: Optional[str] = None,
    org_email: Optional[str] = None,
) -> dict:
    """
    Build a rich context dictionary for the discharge summary.
    org_* parameters allow overriding defaults from router/branding.
    """
    adm: IpdAdmission | None = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise ValueError("Admission not found")

    patient: Patient | None = (
        db.query(Patient).get(adm.patient_id)
        if getattr(adm, "patient_id", None)
        else None
    )
    dept: Department | None = (
        db.query(Department).get(adm.department_id)
        if getattr(adm, "department_id", None)
        else None
    )
    bed: IpdBed | None = (
        db.query(IpdBed).get(adm.current_bed_id)
        if getattr(adm, "current_bed_id", None)
        else None
    )
    doctor: User | None = (
        db.query(User).get(adm.practitioner_user_id)
        if getattr(adm, "practitioner_user_id", None)
        else None
    )

    summary: IpdDischargeSummary | None = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )

    checklist: IpdDischargeChecklist | None = (
        db.query(IpdDischargeChecklist)
        .filter(IpdDischargeChecklist.admission_id == admission_id)
        .first()
    )

    # Basic org details – overridable via args (for branding)
    org_name = org_name or "HOSPITAL NAME"
    org_tagline = org_tagline or "Quality Healthcare & Patient Safety"
    org_address = org_address or ""
    org_phone = org_phone or ""
    org_email = org_email or ""

    ctx = {
        "org_name": org_name,
        "org_tagline": org_tagline,
        "org_address": org_address,
        "org_phone": org_phone,
        "org_email": org_email,
        # Admission / patient identifiers
        "admission_code": getattr(adm, "admission_code", None)
        or f"IP-{adm.id:06d}",
        "patient_code": getattr(patient, "display_code", None)
        or getattr(patient, "patient_code", None)
        or (f"PT-{patient.id:06d}" if patient else ""),
        "patient_name": getattr(patient, "full_name", None)
        or getattr(patient, "name", None)
        or "",
        "age_sex": _age_sex(patient),
        "uhid": getattr(patient, "uhid", None)
        or getattr(patient, "mrn", None)
        or "",
        "mobile": getattr(patient, "mobile", None)
        or getattr(patient, "phone", None)
        or "",
        "address": getattr(patient, "address", "") or "",
        "department": getattr(dept, "name", "") or "",
        "doctor_name": getattr(doctor, "full_name", None)
        or getattr(doctor, "name", None)
        or "",
        "admitted_at": _safe_dt(getattr(adm, "admitted_at", None)),
        "expected_discharge_at": _safe_dt(
            getattr(adm, "expected_discharge_at", None)
        ),
        "bed_code": getattr(bed, "code", "") or "",
        "payor_type": getattr(adm, "payor_type", "") or "",
        "insurer_name": getattr(adm, "insurer_name", "") or "",
        "policy_number": getattr(adm, "policy_number", "") or "",
    }

    s = summary

    ctx.update(
        {
            # Demographics in free-text (if doctor overrode)
            "demographics": _nl2br(getattr(s, "demographics", "") if s else ""),
            "medical_history": _nl2br(
                getattr(s, "medical_history", "") if s else ""
            ),
            "treatment_summary": _nl2br(
                getattr(s, "treatment_summary", "") if s else ""
            ),
            "medications": _nl2br(getattr(s, "medications", "") if s else ""),
            "follow_up": _nl2br(getattr(s, "follow_up", "") if s else ""),
            "icd10_codes": _esc(getattr(s, "icd10_codes", "") if s else ""),
            # A. MUST-HAVE
            "final_diag_primary": _esc(
                getattr(s, "final_diagnosis_primary", "") if s else ""
            ),
            "final_diag_secondary": _esc(
                getattr(s, "final_diagnosis_secondary", "") if s else ""
            ),
            "hospital_course": _nl2br(
                getattr(s, "hospital_course", "") if s else ""
            ),
            "discharge_condition": _esc(
                getattr(s, "discharge_condition", "") if s else ""
            ),
            "discharge_type": _esc(
                getattr(s, "discharge_type", "") if s else ""
            ),
            "allergies": _nl2br(getattr(s, "allergies", "") if s else ""),
            # B. Strongly recommended
            "procedures": _nl2br(getattr(s, "procedures", "") if s else ""),
            "investigations": _nl2br(getattr(s, "investigations", "") if s else ""),
            "diet_instructions": _nl2br(
                getattr(s, "diet_instructions", "") if s else ""
            ),
            "activity_instructions": _nl2br(
                getattr(s, "activity_instructions", "") if s else ""
            ),
            "warning_signs": _nl2br(
                getattr(s, "warning_signs", "") if s else ""
            ),
            "referral_details": _nl2br(
                getattr(s, "referral_details", "") if s else ""
            ),
            # C. Operational / admin / billing
            "insurance_details": _nl2br(
                getattr(s, "insurance_details", "") if s else ""
            ),
            "stay_summary": _nl2br(
                getattr(s, "stay_summary", "") if s else ""
            ),
            "patient_ack_name": _esc(
                getattr(s, "patient_ack_name", "") if s else ""
            ),
            "patient_ack_datetime": _safe_dt(
                getattr(s, "patient_ack_datetime", None) if s else None
            ),
            # D. Doctor & validation
            "prepared_by_name": _esc(
                getattr(s, "prepared_by_name", "") if s else ""
            ),
            "reviewed_by_name": _esc(
                getattr(s, "reviewed_by_name", "") if s else ""
            ),
            "reviewed_by_regno": _esc(
                getattr(s, "reviewed_by_regno", "") if s else ""
            ),
            "discharge_datetime": _safe_dt(
                getattr(s, "discharge_datetime", None) if s else None
            ),
            # E. Safety & Quality
            "implants": _nl2br(getattr(s, "implants", "") if s else ""),
            "pending_reports": _nl2br(
                getattr(s, "pending_reports", "") if s else ""
            ),
            "patient_education": _nl2br(
                getattr(s, "patient_education", "") if s else ""
            ),
            "followup_appointment_ref": _esc(
                getattr(s, "followup_appointment_ref", "") if s else ""
            ),
            "finalized": bool(getattr(s, "finalized", False) if s else False),
            "finalized_at": _safe_dt(
                getattr(s, "finalized_at", None) if s else None
            ),
        }
    )

    # Checklist info (financial / clinical clearances)
    c = checklist
    ctx.update(
        {
            "financial_clearance": bool(
                getattr(c, "financial_clearance", False) if c else False
            ),
            "clinical_clearance": bool(
                getattr(c, "clinical_clearance", False) if c else False
            ),
            "delay_reason": _nl2br(
                getattr(c, "delay_reason", "") if c else ""
            ),
            "checklist_submitted": bool(
                getattr(c, "submitted", False) if c else False
            ),
            "checklist_submitted_at": _safe_dt(
                getattr(c, "submitted_at", None) if c else None
            ),
        }
    )

    return ctx


# ---------------------------------------------------------------------------
# PDF builder using ReportLab (no WeasyPrint)
# ---------------------------------------------------------------------------

def render_discharge_summary_pdf(
    db: Session,
    admission_id: int,
    org_name: Optional[str] = None,
    org_address: Optional[str] = None,
    org_phone: Optional[str] = None,
    org_tagline: Optional[str] = None,
    org_email: Optional[str] = None,
) -> bytes:
    """
    High-level API used by FastAPI route.
    Generates a structured A4 PDF using ReportLab.
    """
    ctx = build_discharge_context(
        db,
        admission_id,
        org_name=org_name,
        org_tagline=org_tagline,
        org_address=org_address,
        org_phone=org_phone,
        org_email=org_email,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=18 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
        textColor=colors.HexColor("#0f172a"),
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#6b7280"),
    )
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
    )
    label_style = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#6b7280"),
    )
    section_title_style = ParagraphStyle(
        "SectionTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=6,
        spaceAfter=2,
    )
    bold_label = ParagraphStyle(
        "BoldLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
    )

    story: list = []

    # -------------------------------------------------------------------
    # HEADER
    # -------------------------------------------------------------------
    story.append(Paragraph(_esc(ctx.get("org_name")), title_style))
    if ctx.get("org_tagline"):
        story.append(Paragraph(_esc(ctx.get("org_tagline")), subtitle_style))

    org_meta_parts = []
    if ctx.get("org_address"):
        org_meta_parts.append(_esc(ctx.get("org_address")))
    if ctx.get("org_phone"):
        org_meta_parts.append(f"Phone: {_esc(ctx.get('org_phone'))}")
    if ctx.get("org_email"):
        org_meta_parts.append(f"Email: {_esc(ctx.get('org_email'))}")
    org_meta = "<br/>".join(org_meta_parts)

    if org_meta:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(org_meta, small_style))

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("<b>Discharge Summary</b>", styles["Heading4"]))
    story.append(
        Paragraph(
            f"<font size='8' color='#6b7280'>Generated on {_safe_dt(datetime.utcnow())}</font>",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 4 * mm))

    # -------------------------------------------------------------------
    # PATIENT / ADMISSION BLOCK
    # -------------------------------------------------------------------
    data = [
        [
            Paragraph("<b>Patient Name</b>", label_style),
            Paragraph(_esc(ctx.get("patient_name")), small_style),
            Paragraph("<b>UHID / Patient ID</b>", label_style),
            Paragraph(_esc(ctx.get("patient_code") or ctx.get("uhid")), small_style),
        ],
        [
            Paragraph("<b>Age / Sex</b>", label_style),
            Paragraph(_esc(ctx.get("age_sex")), small_style),
            Paragraph("<b>Mobile</b>", label_style),
            Paragraph(_esc(ctx.get("mobile")), small_style),
        ],
        [
            Paragraph("<b>Admission No.</b>", label_style),
            Paragraph(_esc(ctx.get("admission_code")), small_style),
            Paragraph("<b>Ward / Bed</b>", label_style),
            Paragraph(_esc(ctx.get("bed_code")), small_style),
        ],
        [
            Paragraph("<b>Department</b>", label_style),
            Paragraph(_esc(ctx.get("department")), small_style),
            Paragraph("<b>Consultant</b>", label_style),
            Paragraph(_esc(ctx.get("doctor_name")), small_style),
        ],
        [
            Paragraph("<b>Admitted On</b>", label_style),
            Paragraph(_esc(ctx.get("admitted_at")), small_style),
            Paragraph("<b>Discharge Date &amp; Time</b>", label_style),
            Paragraph(_esc(ctx.get("discharge_datetime")), small_style),
        ],
        [
            Paragraph("<b>Payor Type</b>", label_style),
            Paragraph(_esc(ctx.get("payor_type")), small_style),
            Paragraph("<b>Insurer / Policy No.</b>", label_style),
            Paragraph(
                "<br/>".join(
                    [
                        _esc(ctx.get("insurer_name")),
                        _esc(ctx.get("policy_number")),
                    ]
                ),
                small_style,
            ),
        ],
    ]
    t = Table(data, colWidths=[30 * mm, 55 * mm, 30 * mm, 55 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 2 * mm))
    if ctx.get("address"):
        story.append(
            Paragraph(
                f"<font size='8' color='#6b7280'>Address: {_esc(ctx.get('address'))}</font>",
                styles["Normal"],
            )
        )

    # -------------------------------------------------------------------
    # SECTION A: Final Diagnosis & Hospital Course
    # -------------------------------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("A. Final Diagnosis & Hospital Course", section_title_style))

    story.append(Paragraph("Final primary diagnosis:", bold_label))
    story.append(Paragraph(_esc(ctx.get("final_diag_primary")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Secondary / comorbid diagnoses:", bold_label))
    story.append(Paragraph(_esc(ctx.get("final_diag_secondary")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Hospital course / clinical summary:", bold_label))
    story.append(
        Paragraph(_br_to_para(ctx.get("hospital_course", "")), small_style)
    )
    story.append(Spacer(1, 1 * mm))

    cond = _esc(ctx.get("discharge_condition"))
    dtype = _esc(ctx.get("discharge_type"))
    story.append(
        Paragraph(
            f"Discharge condition: {cond or '-'}<br/>Discharge type: {dtype or '-'}",
            small_style,
        )
    )
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Allergies:", bold_label))
    allergies = ctx.get("allergies") or "NKDA (if none specified)"
    story.append(Paragraph(_br_to_para(allergies), small_style))

    # -------------------------------------------------------------------
    # SECTION B: Procedures, Investigations, Instructions
    # -------------------------------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("B. Procedures, Investigations & Instructions", section_title_style))

    story.append(Paragraph("Procedures / surgeries performed:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("procedures", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Investigation highlights:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("investigations", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Diet instructions:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("diet_instructions", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Activity instructions:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("activity_instructions", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Warning / red-flag symptoms:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("warning_signs", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Referral / transfer details (if referred):", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("referral_details", "")), small_style))

    # -------------------------------------------------------------------
    # SECTION C: Medications & Follow-up
    # -------------------------------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("C. Discharge Medications & Follow-up", section_title_style))

    story.append(Paragraph("Discharge medications:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("medications", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    follow = _br_to_para(ctx.get("follow_up", ""))
    follow_ref = _esc(ctx.get("followup_appointment_ref") or "")
    follow_text = follow + (
        f"<br/><font size='8' color='#6b7280'>Follow-up appointment ID / token: {follow_ref}</font>"
        if follow_ref
        else ""
    )
    story.append(Paragraph("Follow-up advice:", bold_label))
    story.append(Paragraph(follow_text, small_style))

    # -------------------------------------------------------------------
    # SECTION D: Insurance, Stay, Safety
    # -------------------------------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("D. Insurance, Stay Summary & Safety", section_title_style))

    story.append(Paragraph("Insurance / TPA details:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("insurance_details", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Hospital stay summary:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("stay_summary", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Implants used (if any):", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("implants", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Pending reports:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("pending_reports", "")), small_style))
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Patient education provided:", bold_label))
    story.append(Paragraph(_br_to_para(ctx.get("patient_education", "")), small_style))

    # -------------------------------------------------------------------
    # SECTION E: Clearances & Signatures
    # -------------------------------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("E. Clearances, Acknowledgement & Signatures", section_title_style))

    fin_done = ctx.get("financial_clearance")
    clin_done = ctx.get("clinical_clearance")
    checklist_sub = ctx.get("checklist_submitted")

    story.append(Paragraph("Financial & clinical clearance:", bold_label))
    story.append(
        Paragraph(
            f"Financial clearance: {'Done' if fin_done else 'Pending'}<br/>"
            f"Clinical discharge clearance: {'Done' if clin_done else 'Pending'}<br/>"
            f"Delay reason: {_br_to_para(ctx.get('delay_reason', ''))}",
            small_style,
        )
    )
    story.append(Spacer(1, 1 * mm))

    story.append(Paragraph("Checklist submission:", bold_label))
    story.append(
        Paragraph(
            f"Status: {'Submitted' if checklist_sub else 'Not submitted'}<br/>"
            f"Submitted at: {_esc(ctx.get('checklist_submitted_at') or '')}",
            small_style,
        )
    )
    story.append(Spacer(1, 2 * mm))

    story.append(Paragraph("Patient / Attendant acknowledgement:", bold_label))
    story.append(
        Paragraph(
            f"Name: {_esc(ctx.get('patient_ack_name') or '')}<br/>"
            f"Date &amp; time: {_esc(ctx.get('patient_ack_datetime') or '')}<br/>"
            "Signature: __________________________",
            small_style,
        )
    )
    story.append(Spacer(1, 2 * mm))

    story.append(Paragraph("Doctor & system validation:", bold_label))
    story.append(
        Paragraph(
            f"Prepared by: {_esc(ctx.get('prepared_by_name') or '')}<br/>"
            f"Reviewed &amp; approved by: {_esc(ctx.get('reviewed_by_name') or '')}<br/>"
            f"Registration no.: {_esc(ctx.get('reviewed_by_regno') or '')}<br/>"
            "Digital signature: __________________",
            small_style,
        )
    )
    story.append(Spacer(1, 3 * mm))

    finalized_text = f"Finalized: {'Yes' if ctx.get('finalized') else 'No'}"
    finalized_at_text = f"Finalized at: {_esc(ctx.get('finalized_at') or '')}"
    story.append(
        Paragraph(
            f"<font size='8' color='#6b7280'>{finalized_text} &nbsp;&nbsp; {finalized_at_text}</font>",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 3 * mm))

    story.append(
        Paragraph(
            "<font size='8' color='#6b7280'>"
            "This is a computer-generated discharge summary. Kindly bring this document for all "
            "future consultations. In case of any worsening symptoms, please visit the hospital "
            "emergency / casualty immediately."
            "</font>",
            styles["Normal"],
        )
    )

    # Build PDF
    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Backward-compatible alias for existing router imports
# ---------------------------------------------------------------------------

def generate_discharge_summary_pdf(
    db: Session,
    admission_id: int,
    org_name: Optional[str] = None,
    org_address: Optional[str] = None,
    org_phone: Optional[str] = None,
    org_tagline: Optional[str] = None,
    org_email: Optional[str] = None,
) -> bytes:
    """
    Alias kept so existing imports like:
        from app.services.pdf_discharge import generate_discharge_summary_pdf
    continue to work without any change.
    """
    return render_discharge_summary_pdf(
        db,
        admission_id,
        org_name=org_name,
        org_address=org_address,
        org_phone=org_phone,
        org_tagline=org_tagline,
        org_email=org_email,
    )
