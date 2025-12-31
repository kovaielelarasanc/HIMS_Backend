# FILE: app/services/pdf/ipd_case_sheet.py
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload

from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib import colors
from reportlab.lib.units import mm

# ✅ Patient model import can vary by project; keep it safe
try:
    from app.models.patient import Patient  # common split model
except Exception:  # pragma: no cover
    from app.models.ipd import Patient  # fallback if Patient is in ipd.py

from app.models.ui_branding import UiBranding
from app.models.ipd import (
    IpdAdmission,
    IpdBed,
    IpdRoom,
    IpdVital,
    IpdNursingNote,
    IpdIntakeOutput,
    IpdTransfer,
    IpdMedicationOrder,
    IpdMedicationAdministration,
    IpdPainAssessment,
    IpdFallRiskAssessment,
    IpdPressureUlcerAssessment,
    IpdNutritionAssessment,
    IpdDischargeSummary,
)

# ✅ optional discharge medications table support (won’t crash if model absent)
try:
    from app.models.ipd import IpdDischargeMedication
except Exception:  # pragma: no cover
    IpdDischargeMedication = None  # type: ignore

from app.models.ipd_referral import IpdReferral
from app.models.ipd_nursing import (
    IpdDressingRecord,
    IpdBloodTransfusion,
    IpdRestraintRecord,
    IpdIsolationPrecaution,
    IcuFlowSheet,
)
from app.models.pdf_template import PdfTemplate

from app.services.pdfs.engine import (
    build_pdf,
    PdfBuildContext,
    get_styles,
    section_title,
    kv_table as _kv_table,
    simple_table as _simple_table,
    fmt_ist,
    _safe_str,
)

STYLES = get_styles()


# -------------------------------
# Small helpers
# -------------------------------
def _sty(name: str, fallback: str = "Normal"):
    return STYLES.get(name) or STYLES.get(fallback) or STYLES.get("Small")


def _get(obj: Any, *names: str, default: Any = "") -> Any:
    if not obj:
        return default
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


def kv_table_fullwidth(rows: List[List[str]], label_w: float = 52 * mm):
    """2-col key/value table that wraps + splits across pages safely."""
    try:
        s = STYLES["Small"].clone("SmallWrap")
        s.wordWrap = "CJK"
        s.leading = max(getattr(s, "leading", 10), 10)
    except Exception:
        s = STYLES["Small"]

    data = []
    for k, v in rows:
        key = Paragraph(f"<b>{_safe_str(k)}</b>", s)
        vv = _safe_str(v or "—").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
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


def _patient_full_name(p: Any) -> str:
    if not p:
        return ""
    parts = [getattr(p, "prefix", ""), getattr(p, "first_name", ""), getattr(p, "last_name", "")]
    return " ".join([str(x).strip() for x in parts if x and str(x).strip()]).strip()


def _patient_address(p: Any) -> str:
    """Safe address resolution: Patient.addresses[0] if present, else fallback fields."""
    if not p:
        return ""
    addrs = getattr(p, "addresses", None) or []
    if addrs:
        a = addrs[0]
        parts = [
            _get(a, "line1", "address_line1", "address1", default=""),
            _get(a, "line2", "address_line2", "address2", default=""),
            _get(a, "area", "locality", default=""),
            _get(a, "city", default=""),
            _get(a, "state", default=""),
            _get(a, "pincode", "pin_code", "zip", default=""),
        ]
        return ", ".join([x for x in parts if x and str(x).strip()]).strip(", ")

    # fallback if your Patient stores address directly
    return _safe_str(_get(p, "address", "full_address", "address_line", default=""))


# -------------------------------
# Template resolve
# -------------------------------
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


# -------------------------------
# Data container
# -------------------------------
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
    discharge_meds: List[Any]


def _load_ipd_case_sheet_data(
    db,
    admission_id: int,
    enabled_section_codes: set[str],
    start: Optional[datetime],
    end: Optional[datetime],
    max_rows: int,
) -> IpdCaseSheetData:
    # ✅ DO NOT joinedload(patient_id). It's a column.
    # ✅ Load admission + bed/ward first
    adm = db.query(IpdAdmission).options(
        joinedload(IpdAdmission.current_bed)
            .joinedload(IpdBed.room)
            .joinedload(IpdRoom.ward),
    ).filter(IpdAdmission.id == admission_id).first()
    if not adm:
        raise ValueError("Admission not found")

    # ✅ Load patient separately (works even if you didn't define relationship in model)
    patient = None
    try:
        patient = db.query(Patient).options(
            joinedload(getattr(Patient, "addresses"))
        ).filter(Patient.id == adm.patient_id).first()
    except Exception:
        patient = db.query(Patient).filter(Patient.id == adm.patient_id).first()

    # attach for PDF usage
    try:
        setattr(adm, "patient", patient)
    except Exception:
        pass

    vitals: List[IpdVital] = []
    nursing_notes: List[IpdNursingNote] = []
    io_rows: List[IpdIntakeOutput] = []
    transfers: List[IpdTransfer] = []
    referrals: List[IpdReferral] = []
    med_orders: List[IpdMedicationOrder] = []
    mar_rows: List[IpdMedicationAdministration] = []
    pain: List[IpdPainAssessment] = []
    fall: List[IpdFallRiskAssessment] = []
    pressure: List[IpdPressureUlcerAssessment] = []
    nutrition: List[IpdNutritionAssessment] = []
    dressings: List[IpdDressingRecord] = []
    transfusions: List[IpdBloodTransfusion] = []
    restraints: List[IpdRestraintRecord] = []
    isolations: List[IpdIsolationPrecaution] = []
    icu_flows: List[IcuFlowSheet] = []
    discharge: Optional[IpdDischargeSummary] = None
    discharge_meds: List[Any] = []

    if "vitals" in enabled_section_codes:
        rows = db.query(IpdVital).filter(IpdVital.admission_id == admission_id).order_by(IpdVital.recorded_at.desc()).limit(max_rows * 3).all()
        vitals = [r for r in rows if _in_range(getattr(r, "recorded_at", None), start, end)][:max_rows]

    if "nursing_notes" in enabled_section_codes:
        rows = db.query(IpdNursingNote).filter(IpdNursingNote.admission_id == admission_id).order_by(IpdNursingNote.entry_time.desc()).limit(max_rows * 3).all()
        nursing_notes = [r for r in rows if _in_range(getattr(r, "entry_time", None), start, end)][:max_rows]

    if "intake_output" in enabled_section_codes:
        rows = db.query(IpdIntakeOutput).filter(IpdIntakeOutput.admission_id == admission_id).order_by(IpdIntakeOutput.recorded_at.desc()).limit(max_rows * 3).all()
        io_rows = [r for r in rows if _in_range(getattr(r, "recorded_at", None), start, end)][:max_rows]

    if "bed_transfers" in enabled_section_codes:
        rows = db.query(IpdTransfer).filter(IpdTransfer.admission_id == admission_id).order_by(IpdTransfer.requested_at.desc()).limit(max_rows * 2).all()
        transfers = [r for r in rows if _in_range(getattr(r, "requested_at", None), start, end)][:max_rows]

    if "referrals" in enabled_section_codes:
        rows = db.query(IpdReferral).filter(IpdReferral.admission_id == admission_id).order_by(IpdReferral.requested_at.desc()).limit(max_rows * 2).all()
        referrals = [r for r in rows if _in_range(getattr(r, "requested_at", None), start, end)][:max_rows]

    if "med_orders" in enabled_section_codes:
        rows = db.query(IpdMedicationOrder).filter(IpdMedicationOrder.admission_id == admission_id).order_by(IpdMedicationOrder.start_datetime.desc()).limit(max_rows * 3).all()
        med_orders = [r for r in rows if _in_range(getattr(r, "start_datetime", None), start, end)][:max_rows]

    if "mar" in enabled_section_codes:
        rows = db.query(IpdMedicationAdministration).filter(IpdMedicationAdministration.admission_id == admission_id).order_by(IpdMedicationAdministration.scheduled_datetime.desc()).limit(max_rows * 4).all()
        mar_rows = [r for r in rows if _in_range(getattr(r, "scheduled_datetime", None), start, end)][:max_rows]

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
        # discharge meds (optional)
        if IpdDischargeMedication is not None:
            discharge_meds = db.query(IpdDischargeMedication).filter(
                IpdDischargeMedication.admission_id == admission_id
            ).order_by(IpdDischargeMedication.id.asc()).all()

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
        discharge_meds=discharge_meds,
    )


# -------------------------------
# Header (Logo + Org details + Doc title)
# -------------------------------
def _sec_header(db, doc_title: str = "IPD Case Sheet") -> List[Any]:
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

    left_lines = []
    if org_name:
        left_lines.append(f"<b>{org_name}</b>")
    if tagline:
        left_lines.append(f"<font size='9'>{tagline}</font>")
    if address:
        left_lines.append(f"<font size='8'>{address}</font>")

    contact_bits = []
    if phone:
        contact_bits.append(phone)
    if website:
        contact_bits.append(website)
    if contact_bits:
        left_lines.append(f"<font size='8'>{' | '.join(contact_bits)}</font>")

    left_html = "<br/>".join(left_lines) if left_lines else "<b> </b>"
    left = Paragraph(left_html, _sty("Normal"))

    right = Paragraph(f"<para align='right'><b>{_safe_str(doc_title)}</b></para>", _sty("Normal"))

    hdr = Table(
        [[logo_flow, left, right]],
        colWidths=[26 * mm, None, 40 * mm],
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


# -------------------------------
# Build PDF
# -------------------------------
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

    data = _load_ipd_case_sheet_data(db, admission_id, enabled_codes, period_from, period_to, max_rows)

    adm = data.admission
    subtitle = ""
    if period_from or period_to:
        subtitle = f"Report Period: {_safe_str(period_from.date() if period_from else '')} to {_safe_str(period_to.date() if period_to else '')}"

    story: List[Any] = []
    story.extend(_sec_header(db, doc_title="IPD Case Sheet"))
    story.append(Spacer(1, 4))

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

    # ✅ Important: keep ctx.title blank to avoid duplicate engine header blocks (if engine prints title)
    pdf = build_pdf(
        db=db,
        ctx=PdfBuildContext(
            title="",
            subtitle=subtitle or "",
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
    ✅ Layout:
    - 2 rows × 2 columns
    - 5 fields in each column
    """
    adm = d.admission
    bed = getattr(adm, "current_bed", None)
    room = getattr(bed, "room", None) if bed else None
    ward = getattr(room, "ward", None) if room else None

    patient = getattr(adm, "patient", None)

    patient_name = _safe_str(_patient_full_name(patient))
    patient_code = _safe_str(_get(patient, "uhid", default=""))
    abha = _safe_str(_get(patient, "abha_number", default=""))
    gender = _safe_str(_get(patient, "gender", default=""))
    dob = _get(patient, "dob", default=None)
    age = _calc_age_years(dob)
    phone = _safe_str(_get(patient, "phone", default=""))
    address = _safe_str(_patient_address(patient))

    primary_doc = _safe_str(_get(adm, "practitioner_user_id", "primary_doctor_user_id", default=""))
    primary_nurse = _safe_str(_get(adm, "primary_nurse_user_id", "nurse_user_id", default=""))

    top_left = _kv_box([
        ["UHID", patient_code],
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
        ["ABHA No", abha],
        ["Remarks", _safe_str(_get(adm, "remarks", "notes", default=""))],
    ])

    grid = Table([[top_left, top_right], [bottom_left, bottom_right]], colWidths=[None, None], hAlign="LEFT")
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    return [section_title("Patient & Admission Snapshot"), grid]


def _sec_diagnosis_plan(d: IpdCaseSheetData) -> List[Any]:
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

    t = Table([[diag, hist], [plan, ""]], colWidths=[None, None], hAlign="LEFT")
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
            fmt_ist(getattr(x, "requested_at", None)),
            _safe_str(getattr(getattr(x, "from_bed", None), "code", "")),
            _safe_str(getattr(getattr(x, "to_bed", None), "code", "")),
            _safe_str(getattr(x, "status", "")),
            _safe_str(getattr(x, "reason", "")),
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
        if getattr(v, "bp_systolic", None) is not None or getattr(v, "bp_diastolic", None) is not None:
            bp = f"{_safe_str(getattr(v, 'bp_systolic', ''))}/{_safe_str(getattr(v, 'bp_diastolic', ''))}"
        data.append([
            fmt_ist(getattr(v, "recorded_at", None)),
            bp,
            _safe_str(getattr(v, "pulse", "")),
            _safe_str(getattr(v, "rr", "")),
            _safe_str(getattr(v, "spo2", "")),
            _safe_str(getattr(v, "temp_c", "")),
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
            _safe_str(getattr(n, "nursing_interventions", "")),
            _safe_str(getattr(n, "response_progress", "")),
            _safe_str(getattr(n, "significant_events", "")),
        ] if x])
        data.append([
            fmt_ist(getattr(n, "entry_time", None)),
            _safe_str(getattr(n, "shift", "")),
            (_safe_str(getattr(n, "patient_condition", ""))[:120] or "—"),
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
        urine = (getattr(x, "urine_foley_ml", 0) or 0) + (getattr(x, "urine_voided_ml", 0) or 0)
        data.append([
            fmt_ist(getattr(x, "recorded_at", None)),
            _safe_str(getattr(x, "intake_oral_ml", "")),
            _safe_str(getattr(x, "intake_iv_ml", "")),
            _safe_str(getattr(x, "intake_blood_ml", "")),
            _safe_str(urine),
            _safe_str(getattr(x, "drains_ml", "")),
            _safe_str(getattr(x, "remarks", ""))[:90],
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
            rows.append([fmt_ist(getattr(x, "recorded_at", None)), _safe_str(getattr(x, "score", "")), _safe_str(getattr(x, "intervention", ""))[:120]])
        add_block("Pain Assessment", rows)

    if d.fall:
        rows = [["Recorded At", "Risk Level", "Precautions"]]
        for x in d.fall[:10]:
            rows.append([fmt_ist(getattr(x, "recorded_at", None)), _safe_str(getattr(x, "risk_level", "")), _safe_str(getattr(x, "precautions", ""))[:120]])
        add_block("Fall Risk", rows)

    if d.pressure:
        rows = [["Recorded At", "Risk Level", "Plan"]]
        for x in d.pressure[:10]:
            rows.append([fmt_ist(getattr(x, "recorded_at", None)), _safe_str(getattr(x, "risk_level", "")), _safe_str(getattr(x, "management_plan", ""))[:120]])
        add_block("Pressure Ulcer Risk", rows)

    if d.nutrition:
        rows = [["Recorded At", "Risk Level", "Dietician Referral"]]
        for x in d.nutrition[:10]:
            rows.append([fmt_ist(getattr(x, "recorded_at", None)), _safe_str(getattr(x, "risk_level", "")), "Yes" if getattr(x, "dietician_referral", False) else "No"])
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
        if getattr(x, "dose", None) is not None:
            dose = f"{_safe_str(getattr(x, 'dose', ''))} {_safe_str(getattr(x, 'dose_unit', ''))}".strip()
        data.append([
            fmt_ist(getattr(x, "start_datetime", None)),
            _safe_str(getattr(x, "drug_name", ""))[:45],
            dose,
            _safe_str(getattr(x, "route", "")),
            _safe_str(getattr(x, "frequency", "")),
            _safe_str(getattr(x, "order_status", "")),
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
            fmt_ist(getattr(x, "scheduled_datetime", None)),
            _safe_str(getattr(x, "given_status", "")),
            fmt_ist(getattr(x, "given_datetime", None)),
            _safe_str(getattr(x, "remarks", ""))[:90],
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
        to_txt = (
            getattr(r, "to_department", None)
            or getattr(r, "to_service", None)
            or _safe_str(getattr(getattr(r, "to_user", None), "name", ""))
            or getattr(r, "external_org", None)
            or ""
        )
        data.append([
            fmt_ist(getattr(r, "requested_at", None)),
            _safe_str(getattr(r, "ref_type", "")),
            _safe_str(getattr(r, "category", "")),
            _safe_str(getattr(r, "priority", "")),
            _safe_str(to_txt)[:35],
            _safe_str(getattr(r, "status", "")),
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
            data.append([
                fmt_ist(getattr(x, "performed_at", None)),
                _safe_str(getattr(x, "wound_site", "")),
                _safe_str(getattr(x, "dressing_type", "")),
                _safe_str(getattr(x, "pain_score", "")),
                fmt_ist(getattr(x, "next_dressing_due", None)),
            ])
        story.append(Paragraph("<b>Dressing</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, None, mm * 30, mm * 20, mm * 30]))
        story.append(Spacer(1, 4))

    if d.transfusions:
        data = [["Created At", "Status", "Indication", "Consent", "Notes"]]
        for x in d.transfusions[:10]:
            data.append([
                fmt_ist(getattr(x, "created_at", None)),
                _safe_str(getattr(x, "status", "")),
                _safe_str(getattr(x, "indication", ""))[:30],
                "Yes" if getattr(x, "consent_taken", False) else "No",
                _safe_str(getattr(x, "edit_reason", ""))[:40],
            ])
        story.append(Paragraph("<b>Blood Transfusion</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 25, None, mm * 18, mm * 40]))
        story.append(Spacer(1, 4))

    if d.restraints:
        data = [["Started At", "Status", "Type", "Device/Site", "Reason"]]
        for x in d.restraints[:10]:
            data.append([
                fmt_ist(getattr(x, "started_at", None)),
                _safe_str(getattr(x, "status", "")),
                _safe_str(getattr(x, "restraint_type", "")),
                f"{_safe_str(getattr(x, 'device', ''))}/{_safe_str(getattr(x, 'site', ''))}",
                _safe_str(getattr(x, "reason", ""))[:50],
            ])
        story.append(Paragraph("<b>Restraints</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 22, mm * 22, mm * 35, None]))
        story.append(Spacer(1, 4))

    if d.isolations:
        data = [["Started At", "Status", "Type", "Indication", "Review Due"]]
        for x in d.isolations[:10]:
            data.append([
                fmt_ist(getattr(x, "started_at", None)),
                _safe_str(getattr(x, "status", "")),
                _safe_str(getattr(x, "precaution_type", "")),
                _safe_str(getattr(x, "indication", ""))[:40],
                fmt_ist(getattr(x, "review_due_at", None)),
            ])
        story.append(Paragraph("<b>Isolation Precautions</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 22, mm * 30, None, mm * 30]))
        story.append(Spacer(1, 4))

    if d.icu_flows:
        data = [["Recorded At", "Shift", "GCS", "Urine (ml)", "Notes"]]
        for x in d.icu_flows[:10]:
            data.append([
                fmt_ist(getattr(x, "recorded_at", None)),
                _safe_str(getattr(x, "shift", "")),
                _safe_str(getattr(x, "gcs_score", "")),
                _safe_str(getattr(x, "urine_output_ml", "")),
                _safe_str(getattr(x, "notes", ""))[:50],
            ])
        story.append(Paragraph("<b>ICU Flow Sheet</b>", STYLES["Small"]))
        story.append(simple_table(data, col_widths=[mm * 35, mm * 20, mm * 15, mm * 25, None]))

    return story


def _discharge_meds_table(meds: List[Any]) -> Optional[Table]:
    if not meds:
        return None
    try:
        s = STYLES["Small"].clone("MedWrap")
        s.wordWrap = "CJK"
    except Exception:
        s = STYLES["Small"]

    header = ["Drug", "Dose", "Route", "Frequency", "Days", "Advice"]
    data = [[Paragraph(f"<b>{h}</b>", s) for h in header]]

    for m in meds:
        dose = f"{_safe_str(getattr(m, 'dose', ''))} {_safe_str(getattr(m, 'dose_unit', ''))}".strip()
        advice = _safe_str(getattr(m, "advice_text", "")).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")

        data.append([
            Paragraph(_safe_str(getattr(m, "drug_name", "")) or "—", s),
            Paragraph(dose or "—", s),
            Paragraph(_safe_str(getattr(m, "route", "")) or "—", s),
            Paragraph(_safe_str(getattr(m, "frequency", "")) or "—", s),
            Paragraph(_safe_str(getattr(m, "duration_days", "")) or "—", s),
            Paragraph(advice or "—", s),
        ])

    t = Table(
        data,
        colWidths=[None, 26 * mm, 18 * mm, 24 * mm, 14 * mm, None],
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


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

    add("Final Dx (Primary)", ds.final_diagnosis_primary, always=True)
    add("Final Dx (Secondary)", ds.final_diagnosis_secondary)
    add("ICD-10 Codes", ds.icd10_codes)
    add("Hospital Course", ds.hospital_course, always=True)
    add("Discharge Condition", ds.discharge_condition, always=True)
    add("Discharge Type", ds.discharge_type)
    add("Allergies", ds.allergies)

    add("Demographics", ds.demographics)
    add("Medical History", ds.medical_history)
    add("Treatment Summary", ds.treatment_summary)

    add("Procedures", ds.procedures)
    add("Investigations", ds.investigations)
    add("Diet Instructions", ds.diet_instructions)
    add("Activity Instructions", ds.activity_instructions)
    add("Warning Signs", ds.warning_signs)
    add("Referral Details", ds.referral_details)
    add("Patient Education", ds.patient_education)

    add("Insurance Details", ds.insurance_details)
    add("Stay Summary", ds.stay_summary)
    add("Follow-up", ds.follow_up)
    add("Follow-up Appointment Ref", ds.followup_appointment_ref)

    add("Implants", ds.implants)
    add("Pending Reports", ds.pending_reports)

    add("Discharge Date & Time", fmt_ist(ds.discharge_datetime))
    add("Prepared By", ds.prepared_by_name)
    reviewed = (ds.reviewed_by_name or "").strip()
    regno = (ds.reviewed_by_regno or "").strip()
    add("Reviewed By (Doctor)", f"{reviewed}{('  Reg No: ' + regno) if regno else ''}".strip())
    add("Finalized", "Yes" if ds.finalized else "No")
    add("Finalized At", fmt_ist(ds.finalized_at))

    story.append(kv_table_fullwidth(rows, label_w=52 * mm))

    # ✅ Discharge medications table (20+ rows will auto expand & split)
    meds_tbl = _discharge_meds_table(d.discharge_meds or [])
    if meds_tbl is not None:
        story.append(Spacer(1, 4))
        story.append(Paragraph("<b>Discharge Medications</b>", STYLES["Small"]))
        story.append(meds_tbl)

    return story


def _sec_signatures(d: IpdCaseSheetData) -> List[Any]:
    story = [section_title("Signatures & Acknowledgement")]
    ds = d.discharge

    prepared_by = _safe_str(getattr(ds, "prepared_by_name", "")) if ds else ""
    reviewed_by = _safe_str(getattr(ds, "reviewed_by_name", "")) if ds else ""
    regno = _safe_str(getattr(ds, "reviewed_by_regno", "")) if ds else ""
    ack = _safe_str(getattr(ds, "patient_ack_name", "")) if ds else ""
    ack_dt = fmt_ist(getattr(ds, "patient_ack_datetime", None)) if ds else ""

    rows = [
        ["Prepared By", prepared_by or "_________________________"],
        ["Reviewed By (Doctor)", (reviewed_by or "_________________________") + (f"\nReg No: {regno}" if regno else "")],
        ["Patient / Attendant\nAcknowledgement", ack or "_________________________"],
        ["Date & Time", ack_dt or "_________________________"],
    ]

    # ✅ Increase label column width to avoid overflow
    # Try 72mm. If still tight, increase to 78mm/80mm.
    story.append(kv_table_fullwidth(rows, label_w=72 * mm))
    return story
