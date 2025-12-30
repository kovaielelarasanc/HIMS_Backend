# FILE: app/services/pdf/ipd_case_sheet.py
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload

from reportlab.platypus import (
    Paragraph,
    Spacer,
    KeepTogether,
    Table,
    TableStyle,
    Image,
)
from reportlab.lib import colors
from reportlab.lib.units import mm

from app.models.ui_branding import UiBranding
from app.models.ipd import (
    IpdAdmission, IpdBed, IpdRoom,
    IpdVital, IpdNursingNote, IpdIntakeOutput,
    IpdTransfer, IpdMedicationOrder, IpdMedicationAdministration,
    IpdPainAssessment, IpdFallRiskAssessment, IpdPressureUlcerAssessment, IpdNutritionAssessment,
    IpdDischargeSummary
)
from app.models.ipd_referral import IpdReferral
from app.models.ipd_nursing import (
    IpdDressingRecord, IpdBloodTransfusion, IpdRestraintRecord, IpdIsolationPrecaution, IcuFlowSheet
)
from app.models.pdf_template import PdfTemplate

from app.services.pdfs.engine import (
    build_pdf, PdfBuildContext, get_styles, section_title,
    kv_table as _kv_table, simple_table as _simple_table, fmt_ist, _safe_str
)

STYLES = get_styles()

def kv_table_fullwidth(rows: List[List[str]], label_w: float = 48 * mm):
    # clone a wrap-friendly style
    try:
        s = STYLES["Small"].clone("SmallWrap")
        s.wordWrap = "CJK"  # helps break long words/codes
        s.leading = max(getattr(s, "leading", 10), 10)
    except Exception:
        s = STYLES["Small"]

    data = []
    for k, v in rows:
        key = Paragraph(f"<b>{_safe_str(k)}</b>", s)

        # ✅ preserve newlines (medications/day-wise notes etc.)
        vv = _safe_str(v or "—")
        vv = vv.replace("\r\n", "\n").replace("\r", "\n")
        vv = vv.replace("\n", "<br/>")

        val = Paragraph(vv, s)
        data.append([key, val])

    t = Table(data, colWidths=[label_w, None], hAlign="LEFT", splitByRow=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t

def kv_table(*args, **kwargs):
    t = _kv_table(*args, **kwargs)
    try:
        t.hAlign = "LEFT"
    except Exception:
        pass
    return t

def simple_table(*args, **kwargs):
    t = _simple_table(*args, **kwargs)
    try:
        t.hAlign = "LEFT"
    except Exception:
        pass
    return t


def _sty(name: str, fallback: str = "Normal"):
    return STYLES.get(name) or STYLES.get(fallback) or STYLES.get("Small")


def _get(obj: Any, *names: str, default: Any = "") -> Any:
    for n in names:
        try:
            v = getattr(obj, n, None)
        except Exception:
            v = None
        if v not in (None, ""):
            return v
    return default


def _calc_age_years(dob: Any) -> str:
    try:
        if not dob:
            return ""
        d = dob.date() if hasattr(dob, "date") else dob
        today = datetime.utcnow().date()
        years = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        return str(max(years, 0))
    except Exception:
        return ""


def _in_range(dt: Optional[datetime], start: Optional[datetime], end: Optional[datetime]) -> bool:
    if not dt:
        return False
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def get_ipd_case_sheet_default_template() -> Dict[str, Any]:
    return {
        "name": "IPD Case Sheet (NABH)",
        "sections": [
            {"code": "patient_admission", "label": "Patient & Admission Snapshot", "enabled": True, "order": 10, "required": True},
            {"code": "diagnosis_plan", "label": "Diagnosis, History & Care Plan", "enabled": True, "order": 20, "required": True},
            {"code": "bed_transfers", "label": "Bed & Transfer History", "enabled": True, "order": 30, "required": False},
            {"code": "vitals", "label": "Vitals Chart", "enabled": True, "order": 40, "required": False},
            {"code": "nursing_notes", "label": "Nursing Notes", "enabled": True, "order": 50, "required": False},
            {"code": "intake_output", "label": "Intake / Output", "enabled": True, "order": 60, "required": False},
            {"code": "assessments", "label": "Risk & Clinical Assessments", "enabled": True, "order": 70, "required": False},
            {"code": "med_orders", "label": "Medication Orders", "enabled": True, "order": 80, "required": False},
            {"code": "mar", "label": "Medication Administration (MAR)", "enabled": True, "order": 90, "required": False},
            {"code": "referrals", "label": "Referrals", "enabled": True, "order": 100, "required": False},
            {"code": "procedures", "label": "Dressing / Transfusion / Restraint / Isolation / ICU", "enabled": True, "order": 110, "required": False},
            {"code": "discharge", "label": "Discharge Summary & Counseling", "enabled": True, "order": 120, "required": False},
            {"code": "signatures", "label": "Signatures & Acknowledgement", "enabled": True, "order": 130, "required": True},
        ],
        "settings": {
            "show_empty_sections": False,
            "max_rows_per_section": 30,
        }
    }


def _resolve_template(db, template_id: Optional[int]) -> Dict[str, Any]:
    if template_id:
        t = db.query(PdfTemplate).filter(PdfTemplate.id == template_id, PdfTemplate.is_active == True).first()
        if t:
            return {"name": t.name, "sections": t.sections or [], "settings": t.settings or {}}

    t2 = db.query(PdfTemplate).filter(
        PdfTemplate.module == "ipd",
        PdfTemplate.code == "case_sheet",
        PdfTemplate.is_active == True,
    ).first()
    if t2:
        return {"name": t2.name, "sections": t2.sections or [], "settings": t2.settings or {}}

    return get_ipd_case_sheet_default_template()


@dataclass
class IpdCaseSheetData:
    admission: IpdAdmission
    vitals: List[IpdVital]
    nursing_notes: List[IpdNursingNote]
    io_rows: List[IpdIntakeOutput]
    transfers: List[IpdTransfer]
    referrals: List[IpdReferral]
    med_orders: List[IpdMedicationOrder]
    mar_rows: List[IpdMedicationAdministration]
    pain: List[IpdPainAssessment]
    fall: List[IpdFallRiskAssessment]
    pressure: List[IpdPressureUlcerAssessment]
    nutrition: List[IpdNutritionAssessment]
    dressings: List[IpdDressingRecord]
    transfusions: List[IpdBloodTransfusion]
    restraints: List[IpdRestraintRecord]
    isolations: List[IpdIsolationPrecaution]
    icu_flows: List[IcuFlowSheet]
    discharge: Optional[IpdDischargeSummary]


def _load_ipd_case_sheet_data(
    db,
    admission_id: int,
    enabled_section_codes: set[str],
    start: Optional[datetime],
    end: Optional[datetime],
    max_rows: int,
) -> IpdCaseSheetData:
    adm = db.query(IpdAdmission).options(
        joinedload(IpdAdmission.current_bed)
        .joinedload(IpdBed.room)
        .joinedload(IpdRoom.ward),
        # patient relation may exist; safe if it doesn't
    ).filter(IpdAdmission.id == admission_id).first()
    if not adm:
        raise ValueError("Admission not found")

    vitals = []
    nursing_notes = []
    io_rows = []
    transfers = []
    referrals = []
    med_orders = []
    mar_rows = []
    pain = []
    fall = []
    pressure = []
    nutrition = []
    dressings = []
    transfusions = []
    restraints = []
    isolations = []
    icu_flows = []
    discharge = None

    if "vitals" in enabled_section_codes:
        q = db.query(IpdVital).filter(IpdVital.admission_id == admission_id).order_by(IpdVital.recorded_at.desc())
        rows = q.limit(max_rows * 3).all()
        vitals = [r for r in rows if _in_range(r.recorded_at, start, end)][:max_rows]

    if "nursing_notes" in enabled_section_codes:
        q = db.query(IpdNursingNote).filter(IpdNursingNote.admission_id == admission_id).order_by(IpdNursingNote.entry_time.desc())
        rows = q.limit(max_rows * 3).all()
        nursing_notes = [r for r in rows if _in_range(r.entry_time, start, end)][:max_rows]

    if "intake_output" in enabled_section_codes:
        q = db.query(IpdIntakeOutput).filter(IpdIntakeOutput.admission_id == admission_id).order_by(IpdIntakeOutput.recorded_at.desc())
        rows = q.limit(max_rows * 3).all()
        io_rows = [r for r in rows if _in_range(r.recorded_at, start, end)][:max_rows]

    if "bed_transfers" in enabled_section_codes:
        q = db.query(IpdTransfer).filter(IpdTransfer.admission_id == admission_id).order_by(IpdTransfer.requested_at.desc())
        rows = q.limit(max_rows * 2).all()
        transfers = [r for r in rows if _in_range(r.requested_at, start, end)][:max_rows]

    if "referrals" in enabled_section_codes:
        q = db.query(IpdReferral).filter(IpdReferral.admission_id == admission_id).order_by(IpdReferral.requested_at.desc())
        rows = q.limit(max_rows * 2).all()
        referrals = [r for r in rows if _in_range(r.requested_at, start, end)][:max_rows]

    if "med_orders" in enabled_section_codes:
        q = db.query(IpdMedicationOrder).filter(IpdMedicationOrder.admission_id == admission_id).order_by(IpdMedicationOrder.start_datetime.desc())
        rows = q.limit(max_rows * 3).all()
        med_orders = [r for r in rows if _in_range(r.start_datetime, start, end)][:max_rows]

    if "mar" in enabled_section_codes:
        q = db.query(IpdMedicationAdministration).filter(
            IpdMedicationAdministration.admission_id == admission_id
        ).order_by(IpdMedicationAdministration.scheduled_datetime.desc())
        rows = q.limit(max_rows * 4).all()
        mar_rows = [r for r in rows if _in_range(r.scheduled_datetime, start, end)][:max_rows]

    if "assessments" in enabled_section_codes:
        pain = db.query(IpdPainAssessment).filter(IpdPainAssessment.admission_id == admission_id).order_by(IpdPainAssessment.recorded_at.desc()).limit(max_rows).all()
        fall = db.query(IpdFallRiskAssessment).filter(IpdFallRiskAssessment.admission_id == admission_id).order_by(IpdFallRiskAssessment.recorded_at.desc()).limit(max_rows).all()
        pressure = db.query(IpdPressureUlcerAssessment).filter(IpdPressureUlcerAssessment.admission_id == admission_id).order_by(IpdPressureUlcerAssessment.recorded_at.desc()).limit(max_rows).all()
        nutrition = db.query(IpdNutritionAssessment).filter(IpdNutritionAssessment.admission_id == admission_id).order_by(IpdNutritionAssessment.recorded_at.desc()).limit(max_rows).all()

    if "procedures" in enabled_section_codes:
        dressings = db.query(IpdDressingRecord).filter(IpdDressingRecord.admission_id == admission_id).order_by(IpdDressingRecord.performed_at.desc()).limit(max_rows).all()
        transfusions = db.query(IpdBloodTransfusion).filter(IpdBloodTransfusion.admission_id == admission_id).order_by(IpdBloodTransfusion.created_at.desc()).limit(max_rows).all()
        restraints = db.query(IpdRestraintRecord).filter(IpdRestraintRecord.admission_id == admission_id).order_by(IpdRestraintRecord.created_at.desc()).limit(max_rows).all()
        isolations = db.query(IpdIsolationPrecaution).filter(IpdIsolationPrecaution.admission_id == admission_id).order_by(IpdIsolationPrecaution.created_at.desc()).limit(max_rows).all()
        icu_flows = db.query(IcuFlowSheet).filter(IcuFlowSheet.admission_id == admission_id).order_by(IcuFlowSheet.recorded_at.desc()).limit(max_rows).all()

    if "discharge" in enabled_section_codes:
        discharge = db.query(IpdDischargeSummary).filter(IpdDischargeSummary.admission_id == admission_id).first()

    return IpdCaseSheetData(
        admission=adm,
        vitals=vitals,
        nursing_notes=nursing_notes,
        io_rows=io_rows,
        transfers=transfers,
        referrals=referrals,
        med_orders=med_orders,
        mar_rows=mar_rows,
        pain=pain,
        fall=fall,
        pressure=pressure,
        nutrition=nutrition,
        dressings=dressings,
        transfusions=transfusions,
        restraints=restraints,
        isolations=isolations,
        icu_flows=icu_flows,
        discharge=discharge,
    )


# -------------------------------
# Header (Logo + Org details)
# -------------------------------
def _sec_header(db) -> List[Any]:
    branding = None
    try:
        branding = db.query(UiBranding).filter(
            getattr(UiBranding, "is_active", True) == True
        ).order_by(UiBranding.id.desc()).first()
    except Exception:
        branding = None

    org_name = _safe_str(_get(branding, "org_name", "organization_name", "name", default=""))
    tagline = _safe_str(_get(branding, "tagline", "slogan", default=""))
    address = _safe_str(_get(branding, "address", "org_address", "full_address", default=""))
    phone = _safe_str(_get(branding, "phone", "phone_number", "mobile", "contact_number", default=""))
    website = _safe_str(_get(branding, "website", "web", "url", default=""))

    # logo sources (support common patterns)
    logo_flow = None
    logo_bytes = _get(branding, "logo_bytes", "logo_blob", "logo_data", default=None)
    logo_path = _get(branding, "logo_path", "logo_file_path", "logo_local_path", default=None)

    try:
        if logo_bytes and isinstance(logo_bytes, (bytes, bytearray)):
            logo_flow = Image(io.BytesIO(logo_bytes), width=22 * mm, height=22 * mm)
        elif logo_path and isinstance(logo_path, str) and os.path.exists(logo_path):
            logo_flow = Image(logo_path, width=22 * mm, height=22 * mm)
    except Exception:
        logo_flow = None

    if not logo_flow:
        logo_flow = Spacer(22 * mm, 22 * mm)

    right_lines = []
    if org_name:
        right_lines.append(f"<b>{org_name}</b>")
    if tagline:
        right_lines.append(f"<font size='9'>{tagline}</font>")
    if address:
        right_lines.append(f"<font size='8'>{address}</font>")
    contact_bits = []
    if phone:
        contact_bits.append(f"Phone: {phone}")
    if website:
        contact_bits.append(f"Web: {website}")
    if contact_bits:
        right_lines.append(f"<font size='8'>{'  |  '.join(contact_bits)}</font>")

    right_html = "<br/>".join(right_lines) if right_lines else "<b> </b>"
    right = Paragraph(right_html, _sty("Normal"))

    hdr = Table(
        [[logo_flow, right]],
        colWidths=[26 * mm, None],
        hAlign="LEFT",
    )
    hdr.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    sep = Table([[""]], colWidths=[None])
    sep.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    return [hdr, sep, Spacer(1, 2)]


def build_ipd_case_sheet_pdf(
    *,
    db,
    admission_id: int,
    template_id: Optional[int],
    period_from: Optional[datetime],
    period_to: Optional[datetime],
) -> tuple[bytes, str]:
    tpl = _resolve_template(db, template_id)
    sections = sorted(tpl["sections"], key=lambda x: int(x.get("order", 9999)))
    enabled_codes = {s["code"] for s in sections if s.get("enabled") or s.get("required")}

    settings = tpl.get("settings") or {}
    max_rows = int(settings.get("max_rows_per_section") or 30)
    show_empty = bool(settings.get("show_empty_sections") or False)

    data = _load_ipd_case_sheet_data(
        db, admission_id, enabled_codes, period_from, period_to, max_rows
    )

    adm = data.admission
    subtitle = ""
    if period_from or period_to:
        subtitle = f"Report Period: {_safe_str(period_from.date() if period_from else '')} to {_safe_str(period_to.date() if period_to else '')}"

    story: List[Any] = []
    # ✅ NEW: Header block (logo + org info)
    # story.extend(_sec_header(db))
    # story.append(Spacer(1, 4))

    for s in sections:
        code = s.get("code")
        required = bool(s.get("required"))
        enabled = bool(s.get("enabled"))

        if not (enabled or required):
            continue

        if code == "patient_admission":
            story.extend(_sec_patient_admission(data))
        elif code == "diagnosis_plan":
            story.extend(_sec_diagnosis_plan(data))
        elif code == "bed_transfers":
            story.extend(_sec_bed_transfers(data, show_empty))
        elif code == "vitals":
            story.extend(_sec_vitals(data, show_empty))
        elif code == "nursing_notes":
            story.extend(_sec_nursing_notes(data, show_empty))
        elif code == "intake_output":
            story.extend(_sec_intake_output(data, show_empty))
        elif code == "assessments":
            story.extend(_sec_assessments(data, show_empty))
        elif code == "med_orders":
            story.extend(_sec_med_orders(data, show_empty))
        elif code == "mar":
            story.extend(_sec_mar(data, show_empty))
        elif code == "referrals":
            story.extend(_sec_referrals(data, show_empty))
        elif code == "procedures":
            story.extend(_sec_procedures(data, show_empty))
        elif code == "discharge":
            story.extend(_sec_discharge_and_counseling(data, show_empty))
        elif code == "signatures":
            story.extend(_sec_signatures(data))
        else:
            continue

        story.append(Spacer(1, 6))

    pdf = build_pdf(
        db=db,
        ctx=PdfBuildContext(
            title="IPD Case Sheet",
            subtitle=subtitle,
            meta={"admission_id": admission_id},
        ),
        story=story,
    )

    filename = f"IPD_CaseSheet_{adm.display_code}.pdf"
    return pdf, filename


# -------------------------------
# Layout helpers
# -------------------------------
def _kv_box(rows: List[List[str]]) -> Table:
    # rows: [[label, value], ...]
    data = []
    for k, v in rows:
        data.append([
            Paragraph(f"<b>{_safe_str(k)}</b>", _sty("Small", "Small")),
            Paragraph((_safe_str(v) or "—"), _sty("Small", "Small")),
        ])

    t = Table(data, colWidths=[32 * mm, None], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    return t


# -------------------------------
# Section Renderers
# -------------------------------
def _sec_patient_admission(d: IpdCaseSheetData) -> List[Any]:
    """
    ✅ Required layout:
    - 2 rows × 2 columns
    - 5 fields in each column (total 20 fields)
    """
    adm = d.admission
    bed = getattr(adm, "current_bed", None)
    room = getattr(bed, "room", None) if bed else None
    ward = getattr(room, "ward", None) if room else None

    # patient relation (safe)
    patient = getattr(adm, "patient", None)
    patient_name = _safe_str(_get(patient, "name", "full_name", default=""))
    patient_code = _safe_str(_get(patient, "display_code", "uhid", "patient_code", default=""))
    gender = _safe_str(_get(patient, "gender", "sex", default=""))
    dob = _get(patient, "dob", "date_of_birth", default=None)
    age = _calc_age_years(dob)
    phone = _safe_str(_get(patient, "phone", "mobile", "contact", default=""))
    address = _safe_str(_get(patient, "address", "full_address", "address_line", default=""))

    # useful admission fields (safe)
    primary_doc = _safe_str(_get(adm, "practitioner_user_id", "primary_doctor_user_id", default=""))
    primary_nurse = _safe_str(_get(adm, "primary_nurse_user_id", "nurse_user_id", default=""))

    top_left = _kv_box([
        ["UHID / Patient ID", patient_code],
        ["Patient Name", patient_name],
        ["Age / Gender", f"{age} / {gender}".strip(" /")],
        ["Mobile", phone],
        ["Address", address],
    ])

    top_right = _kv_box([
        ["Admission Code", _safe_str(getattr(adm, "display_code", ""))],
        ["Admission Type", _safe_str(getattr(adm, "admission_type", ""))],
        ["Status", _safe_str(getattr(adm, "status", ""))],
        ["Admitted At", fmt_ist(getattr(adm, "admitted_at", None))],
        ["Expected Discharge", fmt_ist(getattr(adm, "expected_discharge_at", None))],
    ])

    bottom_left = _kv_box([
        ["Ward", _safe_str(getattr(ward, "name", ""))],
        ["Room", _safe_str(_get(room, "number", "name", default=""))],
        ["Bed", _safe_str(_get(bed, "code", "name", default=""))],
        ["Primary Doctor (user_id)", primary_doc],
        ["Primary Nurse (user_id)", primary_nurse],
    ])

    bottom_right = _kv_box([
        ["Payor Type", _safe_str(getattr(adm, "payor_type", ""))],
        ["Insurer", _safe_str(getattr(adm, "insurer_name", ""))],
        ["Policy No", _safe_str(getattr(adm, "policy_number", ""))],
        ["TPA / Corporate", _safe_str(_get(adm, "tpa_name", "corporate_name", default=""))],
        ["Remarks", _safe_str(_get(adm, "remarks", "notes", default=""))],
    ])

    grid = Table(
        [[top_left, top_right], [bottom_left, bottom_right]],
        colWidths=[None, None],
        hAlign="LEFT",
    )
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story = [section_title("Patient & Admission Snapshot")]
    story.append(grid)
    return story


def _sec_diagnosis_plan(d: IpdCaseSheetData) -> List[Any]:
    """
    ✅ Required layout:
    - 2 rows
      Row-1: 2 columns
      Row-2: 1 full-width column
    """
    adm = d.admission
    story = [section_title("Diagnosis, History & Care Plan")]

    diag = Paragraph(
        f"<b>Preliminary Diagnosis</b><br/>{_safe_str(getattr(adm, 'preliminary_diagnosis', '')) or '—'}",
        _sty("Small", "Small"),
    )
    hist = Paragraph(
        f"<b>History</b><br/>{_safe_str(getattr(adm, 'history', '')) or '—'}",
        _sty("Small", "Small"),
    )
    plan = Paragraph(
        f"<b>Care Plan</b><br/>{_safe_str(getattr(adm, 'care_plan', '')) or '—'}",
        _sty("Small", "Small"),
    )

    t = Table(
        [[diag, hist], [plan, ""]],
        colWidths=[None, None],
        hAlign="LEFT",
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("SPAN", (0, 1), (-1, 1)),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story.append(t)
    return story


def _sec_bed_transfers(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Bed & Transfer History")]
    if not d.transfers:
        if show_empty:
            story.append(Paragraph("No transfer records for selected period.", STYLES["Muted"]))
        return story

    data = [["Requested At", "From Bed", "To Bed", "Status", "Reason"]]
    for x in d.transfers:
        data.append([
            fmt_ist(x.requested_at),
            _safe_str(getattr(x.from_bed, "code", "")),
            _safe_str(getattr(x.to_bed, "code", "")),
            _safe_str(x.status),
            _safe_str(x.reason),
        ])
    story.append(simple_table(data, col_widths=[mm * 35, mm * 25, mm * 25, mm * 25, None]))
    return story


def _sec_vitals(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Vitals Chart")]
    if not d.vitals:
        if show_empty:
            story.append(Paragraph("No vitals recorded for selected period.", STYLES["Muted"]))
        return story

    data = [["Recorded At", "BP", "Pulse", "RR", "SpO2", "Temp (C)"]]
    for v in d.vitals:
        bp = ""
        if v.bp_systolic is not None or v.bp_diastolic is not None:
            bp = f"{_safe_str(v.bp_systolic)}/{_safe_str(v.bp_diastolic)}"
        data.append([
            fmt_ist(v.recorded_at),
            bp,
            _safe_str(v.pulse),
            _safe_str(v.rr),
            _safe_str(v.spo2),
            _safe_str(v.temp_c),
        ])
    story.append(simple_table(data, col_widths=[mm * 38, mm * 24, mm * 18, mm * 16, mm * 18, mm * 20]))
    return story


def _sec_nursing_notes(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Nursing Notes (NABH)")]
    if not d.nursing_notes:
        if show_empty:
            story.append(Paragraph("No nursing notes for selected period.", STYLES["Muted"]))
        return story

    data = [["Entry Time", "Shift", "Condition", "Interventions / Response"]]
    for n in d.nursing_notes:
        summary = " | ".join([x for x in [
            _safe_str(n.nursing_interventions),
            _safe_str(n.response_progress),
            _safe_str(n.significant_events),
        ] if x])
        data.append([
            fmt_ist(n.entry_time),
            _safe_str(n.shift),
            (_safe_str(n.patient_condition)[:120] or "—"),
            (summary[:180] or "—"),
        ])
    story.append(simple_table(data, col_widths=[mm * 35, mm * 20, mm * 55, None]))
    return story


def _sec_intake_output(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Intake / Output")]
    if not d.io_rows:
        if show_empty:
            story.append(Paragraph("No intake/output records for selected period.", STYLES["Muted"]))
        return story

    data = [["Recorded At", "Oral", "IV", "Blood", "Urine", "Drains", "Remarks"]]
    for x in d.io_rows:
        urine = (x.urine_foley_ml or 0) + (x.urine_voided_ml or 0)
        data.append([
            fmt_ist(x.recorded_at),
            _safe_str(x.intake_oral_ml),
            _safe_str(x.intake_iv_ml),
            _safe_str(x.intake_blood_ml),
            _safe_str(urine),
            _safe_str(x.drains_ml),
            _safe_str(x.remarks)[:90],
        ])
    story.append(simple_table(data, col_widths=[mm * 35, mm * 18, mm * 18, mm * 18, mm * 18, mm * 18, None]))
    return story


def _sec_assessments(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Risk & Clinical Assessments")]
    empty_all = not (d.pain or d.fall or d.pressure or d.nutrition)
    if empty_all:
        if show_empty:
            story.append(Paragraph("No assessments available.", STYLES["Muted"]))
        return story

    def add_block(title: str, rows: List[List[str]]):
        if not rows:
            return
        story.append(Paragraph(f"<b>{_safe_str(title)}</b>", STYLES["Small"]))
        story.append(simple_table([rows[0]] + rows[1:], col_widths=[mm * 35, mm * 25, None]))
        story.append(Spacer(1, 4))

    if d.pain:
        rows = [["Recorded At", "Score", "Intervention / Notes"]]
        for x in d.pain[:10]:
            rows.append([fmt_ist(x.recorded_at), _safe_str(x.score), _safe_str(x.intervention)[:120]])
        add_block("Pain Assessment", rows)

    if d.fall:
        rows = [["Recorded At", "Risk Level", "Precautions"]]
        for x in d.fall[:10]:
            rows.append([fmt_ist(x.recorded_at), _safe_str(x.risk_level), _safe_str(x.precautions)[:120]])
        add_block("Fall Risk", rows)

    if d.pressure:
        rows = [["Recorded At", "Risk Level", "Plan"]]
        for x in d.pressure[:10]:
            rows.append([fmt_ist(x.recorded_at), _safe_str(x.risk_level), _safe_str(x.management_plan)[:120]])
        add_block("Pressure Ulcer Risk", rows)

    if d.nutrition:
        rows = [["Recorded At", "Risk Level", "Dietician Referral"]]
        for x in d.nutrition[:10]:
            rows.append([fmt_ist(x.recorded_at), _safe_str(x.risk_level), "Yes" if x.dietician_referral else "No"])
        add_block("Nutrition", rows)

    return story


def _sec_med_orders(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Medication Orders")]
    if not d.med_orders:
        if show_empty:
            story.append(Paragraph("No medication orders for selected period.", STYLES["Muted"]))
        return story

    data = [["Start", "Drug", "Dose", "Route", "Freq", "Status"]]
    for x in d.med_orders:
        dose = ""
        if x.dose is not None:
            dose = f"{_safe_str(x.dose)} {_safe_str(x.dose_unit)}".strip()
        data.append([
            fmt_ist(x.start_datetime),
            _safe_str(x.drug_name)[:45],
            dose,
            _safe_str(x.route),
            _safe_str(x.frequency),
            _safe_str(x.order_status),
        ])
    story.append(simple_table(data, col_widths=[mm * 35, None, mm * 25, mm * 22, mm * 22, mm * 20]))
    return story


def _sec_mar(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Medication Administration Record (MAR)")]
    if not d.mar_rows:
        if show_empty:
            story.append(Paragraph("No MAR entries for selected period.", STYLES["Muted"]))
        return story

    data = [["Scheduled", "Status", "Given At", "Remarks"]]
    for x in d.mar_rows:
        data.append([
            fmt_ist(x.scheduled_datetime),
            _safe_str(x.given_status),
            fmt_ist(x.given_datetime),
            _safe_str(x.remarks)[:90],
        ])
    story.append(simple_table(data, col_widths=[mm * 40, mm * 25, mm * 35, None]))
    return story


def _sec_referrals(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Referrals")]
    if not d.referrals:
        if show_empty:
            story.append(Paragraph("No referrals for selected period.", STYLES["Muted"]))
        return story

    data = [["Requested At", "Type", "Category", "Priority", "To", "Status"]]
    for r in d.referrals:
        to_txt = r.to_department or r.to_service or _safe_str(getattr(r.to_user, "name", "")) or r.external_org
        data.append([
            fmt_ist(r.requested_at),
            _safe_str(r.ref_type),
            _safe_str(r.category),
            _safe_str(r.priority),
            _safe_str(to_txt)[:35],
            _safe_str(r.status),
        ])
    story.append(simple_table(data, col_widths=[mm * 35, mm * 18, mm * 25, mm * 20, None, mm * 22]))
    return story


def _sec_procedures(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Procedures & Critical Care Logs")]
    any_data = any([d.dressings, d.transfusions, d.restraints, d.isolations, d.icu_flows])
    if not any_data:
        if show_empty:
            story.append(Paragraph("No procedure/critical care entries.", STYLES["Muted"]))
        return story

    if d.dressings:
        data = [["Performed At", "Wound Site", "Type", "Pain Score", "Next Due"]]
        for x in d.dressings[:10]:
            data.append([fmt_ist(x.performed_at), _safe_str(x.wound_site), _safe_str(x.dressing_type), _safe_str(x.pain_score), fmt_ist(x.next_dressing_due)])
        story.append(Paragraph("<b>Dressing</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, None, mm * 30, mm * 20, mm * 30]))
        story.append(Spacer(1, 4))

    if d.transfusions:
        data = [["Created At", "Status", "Indication", "Consent", "Notes"]]
        for x in d.transfusions[:10]:
            data.append([fmt_ist(x.created_at), _safe_str(x.status), _safe_str(x.indication)[:30], "Yes" if x.consent_taken else "No", _safe_str(x.edit_reason)[:40]])
        story.append(Paragraph("<b>Blood Transfusion</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 25, None, mm * 18, mm * 40]))
        story.append(Spacer(1, 4))

    if d.restraints:
        data = [["Started At", "Status", "Type", "Device/Site", "Reason"]]
        for x in d.restraints[:10]:
            data.append([fmt_ist(x.started_at), _safe_str(x.status), _safe_str(x.restraint_type), f"{_safe_str(x.device)}/{_safe_str(x.site)}", _safe_str(x.reason)[:50]])
        story.append(Paragraph("<b>Restraints</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 22, mm * 22, mm * 35, None]))
        story.append(Spacer(1, 4))

    if d.isolations:
        data = [["Started At", "Status", "Type", "Indication", "Review Due"]]
        for x in d.isolations[:10]:
            data.append([fmt_ist(x.started_at), _safe_str(x.status), _safe_str(x.precaution_type), _safe_str(x.indication)[:40], fmt_ist(x.review_due_at)])
        story.append(Paragraph("<b>Isolation Precautions</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 22, mm * 30, None, mm * 30]))
        story.append(Spacer(1, 4))

    if d.icu_flows:
        data = [["Recorded At", "Shift", "GCS", "Urine (ml)", "Notes"]]
        for x in d.icu_flows[:10]:
            data.append([fmt_ist(x.recorded_at), _safe_str(x.shift), _safe_str(x.gcs_score), _safe_str(x.urine_output_ml), _safe_str(x.notes)[:50]])
        story.append(Paragraph("<b>ICU Flow Sheet</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 20, mm * 15, mm * 25, None]))

    return story


def _sec_discharge_and_counseling(d: IpdCaseSheetData, show_empty: bool) -> List[Any]:
    story = [section_title("Discharge Summary & Counseling")]
    ds = d.discharge
    if not ds:
        if show_empty:
            story.append(Paragraph("Discharge summary not available.", STYLES["Muted"]))
        return story

    rows: List[List[str]] = []

    def add(label: str, value: Any, always: bool = False):
        v = _safe_str(value)
        if always or v.strip() or show_empty:
            rows.append([label, v or "—"])

    # MUST-HAVE / core
    add("Final Dx (Primary)", ds.final_diagnosis_primary, always=True)
    add("Final Dx (Secondary)", ds.final_diagnosis_secondary)
    add("ICD-10 Codes", ds.icd10_codes)
    add("Hospital Course", ds.hospital_course, always=True)
    add("Discharge Condition", ds.discharge_condition, always=True)
    add("Discharge Type", ds.discharge_type)
    add("Allergies", ds.allergies)

    # Clinical narrative
    add("Demographics", ds.demographics)
    add("Medical History", ds.medical_history)
    add("Treatment Summary", ds.treatment_summary)
    add("Medications", ds.medications)

    # Recommended / counseling
    add("Procedures", ds.procedures)
    add("Investigations", ds.investigations)
    add("Diet Instructions", ds.diet_instructions)
    add("Activity Instructions", ds.activity_instructions)
    add("Warning Signs", ds.warning_signs)
    add("Referral Details", ds.referral_details)
    add("Patient Education", ds.patient_education)

    # Operational / billing
    add("Insurance Details", ds.insurance_details)
    add("Stay Summary", ds.stay_summary)
    add("Follow-up", ds.follow_up)
    add("Follow-up Appointment Ref", ds.followup_appointment_ref)

    # Safety & quality
    add("Implants", ds.implants)
    add("Pending Reports", ds.pending_reports)

    # Doctor/system validation
    add("Discharge Date & Time", fmt_ist(ds.discharge_datetime))
    add("Prepared By", ds.prepared_by_name)
    reviewed = (ds.reviewed_by_name or "").strip()
    regno = (ds.reviewed_by_regno or "").strip()
    add("Reviewed By (Doctor)", f"{reviewed}{('  Reg No: ' + regno) if regno else ''}".strip())
    add("Finalized", "Yes" if ds.finalized else "No")
    add("Finalized At", fmt_ist(ds.finalized_at))

    story.append(kv_table_fullwidth(rows, label_w=52 * mm))
    return story


def _sec_signatures(d: IpdCaseSheetData) -> List[Any]:
    story = [section_title("Signatures & Acknowledgement")]
    ds = d.discharge

    prepared_by = _safe_str(ds.prepared_by_name) if ds else ""
    reviewed_by = _safe_str(ds.reviewed_by_name) if ds else ""
    regno = _safe_str(ds.reviewed_by_regno) if ds else ""
    ack = _safe_str(ds.patient_ack_name) if ds else ""

    data = [
        ["Prepared By", prepared_by or "_________________________"],
        ["Reviewed By (Doctor)", (reviewed_by or "_________________________") + (f"  Reg No: {regno}" if regno else "")],
        ["Patient / Attendant Acknowledgement", ack or "_________________________"],
        ["Date & Time", fmt_ist(ds.patient_ack_datetime) if ds else ""],
    ]
    story.append(kv_table(data))
    return story
