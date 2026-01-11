# FILE: app/services/pdf/ipd_case_sheet.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload

from reportlab.platypus import (
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
    Image, 
)
from reportlab.lib import colors
from reportlab.lib.units import mm

# ✅ Patient model import can vary by project; keep it safe
try:
    from app.models.patient import Patient  # common split model
except Exception:  # pragma: no cover
    from app.models.ipd import Patient  # fallback if Patient is in ipd.py

from app.models.ipd import (
    IpdAdmission,
    IpdBed,
    IpdRoom,
    IpdDischargeSummary,
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
    IpdDressingTransfusion,
    IpdDischargeSummary,
    IpdAdmissionFeedback,
    IpdAnaesthesiaRecord,
    IpdAssessment,
    IpdBedAssignment,
    IpdBedRate,
    IpdDischargeChecklist,
    IpdDischargeMedication,
    IpdDrugChartDoctorAuth,
    IpdDrugChartMeta,
    IpdDrugChartNurseRow,
    IpdFeedback,
    IpdIvFluidOrder,
    IpdMedication,
    IpdOrder,
    IpdOtCase,
    IpdPackage,
    IpdProgressNote,
    IpdShiftHandover,
    IpdRound,
    IpdWard
)

from app.models.ipd_referral import (
    IpdReferral,
    IpdReferralEvent,
)

from app.models.ipd_nursing import (
    IcuFlowSheet,
    IpdBloodTransfusion,
    IpdDressingRecord,
    IpdIsolationPrecaution,
    IpdNursingTimeline,
    IpdRestraintRecord,
)



from app.models.ipd_newborn import IpdNewbornResuscitation


from app.models.pdf_template import PdfTemplate
from app.models.ui_branding import UiBranding

from app.services.pdfs.engine import (
    build_pdf,
    PdfBuildContext,
    get_styles,
    fmt_ist,
    _safe_str,
)

STYLES = get_styles()

# ============================================================
# Helpers
# ============================================================
def _sty(name: str, fallback: str = "Normal"):
    return STYLES.get(name) or STYLES.get(fallback) or STYLES.get("Small") or STYLES["Normal"]


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


def _patient_full_name(p: Any) -> str:
    if not p:
        return ""
    parts = [
        getattr(p, "prefix", ""),
        getattr(p, "first_name", ""),
        getattr(p, "last_name", ""),
    ]
    return " ".join([str(x).strip() for x in parts if x and str(x).strip()]).strip()


def _patient_address(p: Any) -> str:
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
    return _safe_str(_get(p, "address", "full_address", "address_line", default=""))


def _yn_box(v: Optional[bool]) -> str:
    # Govt form style Yes/No tick boxes (image-free)
    if v is True:
        return "Yes (X)   No ( )"
    if v is False:
        return "Yes ( )   No (X)"
    return "Yes ( )   No ( )"


def _opt_box(options: List[str], selected: Optional[str]) -> str:
    # options => ["A", "B", ...], selected => "A"
    out = []
    for o in options:
        out.append(f"{o} ({'X' if selected == o else ' '})")
    return "   ".join(out)


def _para(txt: str, style: str = "Small") -> Paragraph:
    s = _sty(style, "Small")
    safe = _safe_str(txt).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
    return Paragraph(safe, s)


def _h1(txt: str) -> Paragraph:
    # Govt-like centered heading
    return Paragraph(f"<para align='center'><b>{_safe_str(txt)}</b></para>", _sty("Normal", "Normal"))


def _section_band(title: str) -> Table:
    # Dark band header like Govt forms (still monochrome)
    t = Table([[Paragraph(f"<b>{_safe_str(title)}</b>", _sty("Small", "Small"))]], colWidths=[None])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def _boxed_table(data: List[List[Any]], col_widths: Optional[List[float]] = None, repeat_rows: int = 0) -> Table:
    t = Table(data, colWidths=col_widths, repeatRows=repeat_rows, hAlign="LEFT", splitByRow=1)
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t

def _boxed_lines(
    title: Optional[str],
    lines: List[str],
    *,
    style: str = "Small",
) -> Table:
    """
    ✅ Splittable Govt-like boxed content:
    - Each line is its own table row => ReportLab can split across pages safely.
    - Avoids LayoutError caused by one huge cell.
    """
    data: List[List[Any]] = []
    if title:
        data.append([_para(f"<b>{title}</b>", style)])
    for ln in lines:
        data.append([_para(ln if ln.strip() else " ", style)])
    return _boxed_table(data, col_widths=[None])



def _field_row_2col(label: str, value: Any) -> List[Any]:
    sL = _sty("Small", "Small")
    sV = _sty("Small", "Small")
    return [
        Paragraph(f"<b>{_safe_str(label)}</b>", sL),
        Paragraph(_safe_str(value) if _safe_str(value).strip() else " ", sV),
    ]


def _kv_grid_2x2(pairs: List[List[str]]) -> Table:
    """
    pairs is list of [label, value], will render as 2 columns repeated across rows:
    [L1 V1 | L2 V2]
    """
    rows = []
    sL = _sty("Small", "Small")
    sV = _sty("Small", "Small")

    # pad to even count
    p = list(pairs)
    if len(p) % 2 == 1:
        p.append(["", ""])

    for i in range(0, len(p), 2):
        (l1, v1) = p[i]
        (l2, v2) = p[i + 1]
        rows.append(
            [
                Paragraph(f"<b>{_safe_str(l1)}</b>", sL),
                Paragraph(_safe_str(v1) if _safe_str(v1).strip() else " ", sV),
                Paragraph(f"<b>{_safe_str(l2)}</b>", sL),
                Paragraph(_safe_str(v2) if _safe_str(v2).strip() else " ", sV),
            ]
        )

    return _boxed_table(rows, col_widths=[38 * mm, None, 38 * mm, None])


def _blank_lines(title: str, lines: int = 8) -> Table:
    s = _sty("Small", "Small")
    data = [[Paragraph(f"<b>{_safe_str(title)}</b>", s)]]
    for _ in range(lines):
        data.append([Paragraph("__________________________________________________________________________________________", s)])
    t = Table(data, colWidths=[None], hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


# ============================================================
# Template resolve (Govt L3 style)
# ============================================================
def get_ipd_case_sheet_default_template() -> Dict[str, Any]:
    # Govt-style maternity case-sheet sections (image-free)
    return {
        "name": "Case Sheet for Maternity Services - L3 Facility (Govt Layout, No Images)",
        "sections": [
            {"code": "gov_header", "label": "Govt Header", "enabled": True, "order": 5, "required": True},
            {"code": "admission_form", "label": "Admission Form", "enabled": True, "order": 10, "required": True},
            {"code": "presenting_history", "label": "Presenting Complaints & History", "enabled": True, "order": 20, "required": True},
            {"code": "safe_checklist_1", "label": "SAFE Childbirth Checklist - Check 1", "enabled": True, "order": 30, "required": True},
            {"code": "obstetric_notes", "label": "Obstetric Notes", "enabled": True, "order": 40, "required": True},
            {"code": "partograph", "label": "Simplified Partograph (Blank Grid)", "enabled": True, "order": 50, "required": True},
            {"code": "consent", "label": "Consent for Procedures", "enabled": True, "order": 60, "required": True},
            {"code": "pre_anaesthetic", "label": "Pre-Anesthetic Check-up", "enabled": True, "order": 70, "required": True},
            {"code": "anaesthesia_notes", "label": "Anesthesia Notes", "enabled": True, "order": 80, "required": True},
            {"code": "safe_checklist_2", "label": "SAFE Childbirth Checklist - Check 2", "enabled": True, "order": 90, "required": True},
            {"code": "procedure_notes", "label": "Operation/Procedure Notes", "enabled": True, "order": 100, "required": True},
            {"code": "delivery_notes", "label": "Delivery Notes + Baby Notes", "enabled": True, "order": 110, "required": True},
            {"code": "post_delivery_continuation", "label": "Post Delivery Continuation Sheets", "enabled": True, "order": 120, "required": True},
            {"code": "transfusion_notes", "label": "Blood Transfusion / Procedure Notes", "enabled": True, "order": 130, "required": True},
            {"code": "safe_checklist_3", "label": "SAFE Childbirth Checklist - Check 3", "enabled": True, "order": 140, "required": True},
            {"code": "postpartum_assessment", "label": "Assessment of Postpartum Condition", "enabled": True, "order": 150, "required": True},
            {"code": "safe_checklist_4", "label": "SAFE Childbirth Checklist - Check 4", "enabled": True, "order": 160, "required": True},
            {"code": "discharge_notes", "label": "Discharge Notes", "enabled": True, "order": 170, "required": True},
            {"code": "discharge_form", "label": "Discharge/Referral/LAMA/Death Form", "enabled": True, "order": 180, "required": True},
            {"code": "signatures", "label": "Signatures", "enabled": True, "order": 190, "required": True},
            {"code": "newborn_resuscitation", "label": "Newborn Resuscitation & Examination", "enabled": True, "order": 115, "required": False},
        ],
        "settings": {
            "max_rows_per_section": 30,
            "show_empty_sections": True,
        },
    }


def _resolve_template(db, template_id: Optional[int]) -> Dict[str, Any]:
    if template_id:
        t = (
            db.query(PdfTemplate)
            .filter(PdfTemplate.id == template_id, PdfTemplate.is_active == True)  # noqa: E712
            .first()
        )
        if t:
            return {"name": t.name, "sections": t.sections or [], "settings": t.settings or {}}

    t2 = (
        db.query(PdfTemplate)
        .filter(
            PdfTemplate.module == "ipd",
            PdfTemplate.code == "case_sheet",
            PdfTemplate.is_active == True,  # noqa: E712
        )
        .first()
    )
    if t2:
        return {"name": t2.name, "sections": t2.sections or [], "settings": t2.settings or {}}

    return get_ipd_case_sheet_default_template()


# ============================================================
# Data container
# ============================================================
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
    newborn_resus: Optional[Any]   # ✅ NEW



def _load_ipd_case_sheet_data(db, admission_id: int) -> IpdCaseSheetData:
    adm = (
        db.query(IpdAdmission)
        .options(
            joinedload(IpdAdmission.current_bed)
            .joinedload(IpdBed.room)
            .joinedload(IpdRoom.ward),
        )
        .filter(IpdAdmission.id == admission_id)
        .first()
    )
    if not adm:
        raise ValueError("Admission not found")

    # patient (attach)
    patient = None
    try:
        patient = db.query(Patient).options(joinedload(getattr(Patient, "addresses"))).filter(Patient.id == adm.patient_id).first()
    except Exception:
        patient = db.query(Patient).filter(Patient.id == adm.patient_id).first()

    try:
        setattr(adm, "patient", patient)
    except Exception:
        pass

    discharge = db.query(IpdDischargeSummary).filter(IpdDischargeSummary.admission_id == admission_id).first()

    return IpdCaseSheetData(admission=adm, discharge=discharge)


# ============================================================
# Govt Header (NO images)
# ============================================================
def _gov_header(db, d: IpdCaseSheetData) -> List[Any]:
    branding = None
    try:
        branding = (
            db.query(UiBranding)
            .filter(getattr(UiBranding, "is_active", True) == True)  # noqa: E712
            .order_by(UiBranding.id.desc())
            .first()
        )
    except Exception:
        branding = None

    facility = _safe_str(_get(branding, "org_name", "facility_name", "name", default="Name of Facility"))
    district = _safe_str(_get(branding, "org_district", "district", default=""))
    block = _safe_str(_get(branding, "org_block", "block", default=""))
    phone = _safe_str(_get(branding, "org_phone", "phone", "contact_number", default=""))

    title = "CASE SHEET FOR MATERNITY SERVICES - L3 FACILITY"
    motto = "lR;eso t;rs"  # as in sample PDF (ASCII transliteration)

    head = Table(
        [
            [Paragraph(f"<b>{_safe_str(facility)}</b>", _sty("Normal", "Normal")), _h1(title)],
            [Paragraph(f"Block: {_safe_str(block)}", _sty("Small", "Small")), Paragraph(motto, _sty("Small", "Small"))],
            [Paragraph(f"District: {_safe_str(district)}", _sty("Small", "Small")), Paragraph(f"Contact No. (facility): {_safe_str(phone)}", _sty("Small", "Small"))],
        ],
        colWidths=[None, None],
        hAlign="LEFT",
    )
    head.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    return [head, Spacer(1, 6)]


# ============================================================
# Section: Admission Form (Govt layout)
# ============================================================
def _sec_admission_form(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    patient = getattr(adm, "patient", None)
    discharge = d.discharge

    # Patient basics
    name = _safe_str(_patient_full_name(patient))
    age = _calc_age_years(_get(patient, "dob", default=None))
    gender = _safe_str(_get(patient, "gender", default=""))
    phone = _safe_str(_get(patient, "phone", "mobile", default=""))
    address = _safe_str(_patient_address(patient))

    # Govt-specific IDs (if your DB has them; else blank)
    mcts = _safe_str(_get(adm, "mcts_no", "mcts_number", default=""))
    aadhar = _safe_str(_get(patient, "aadhar_no", "aadhaar_no", "aadhaar", default=_get(adm, "aadhar_no", default="")))
    ipd_reg = _safe_str(_get(adm, "display_code", "registration_no", default=""))
    booked = _get(adm, "booked", default=None)  # bool?
    bpl = _get(adm, "bpl_jsy_registered", "bpl_jsy", default=None)  # bool?
    referred_from = _safe_str(_get(adm, "referred_from", "referred_by", default=""))
    referral_reason = _safe_str(_get(adm, "referral_reason", "referred_reason", default=""))

    marital = _safe_str(_get(patient, "marital_status", default=""))
    relation = _safe_str(_get(patient, "relation_name", "spouse_name", "husband_name", "father_name", default=""))

    admitted_at = fmt_ist(_get(adm, "admitted_at", default=None))
    companion = _safe_str(_get(adm, "birth_companion_name", "companion_name", default=""))

    admission_cat = _safe_str(_get(adm, "admission_category", default=""))
    lmp = _safe_str(_get(adm, "lmp", default=""))
    edd = _safe_str(_get(adm, "edd", default=""))

    prov_dx = _safe_str(_get(adm, "preliminary_diagnosis", "provisional_diagnosis", default=""))
    final_dx = _safe_str(_get(discharge, "final_diagnosis_primary", default=_get(adm, "final_diagnosis", default="")))

    contraception = _safe_str(_get(adm, "contraception_history", default=""))

    # Delivery outcome (if you store)
    delivery_outcome = _safe_str(_get(adm, "delivery_outcome", default=""))
    baby_sex = _safe_str(_get(adm, "baby_sex", default=""))
    baby_weight = _safe_str(_get(adm, "baby_birth_weight_kg", "baby_weight_kg", default=""))
    delivery_dt = _safe_str(_get(adm, "delivery_date", default=""))
    delivery_time = _safe_str(_get(adm, "delivery_time", default=""))
    mode_delivery = _safe_str(_get(adm, "mode_of_delivery", default=""))
    final_outcome = _safe_str(_get(adm, "final_outcome", default=""))

    # Yes/No boxes
    booked_box = _yn_box(booked if isinstance(booked, bool) else None)
    bpl_box = _yn_box(bpl if isinstance(bpl, bool) else None)

    # Admission category options exactly like sample wording (stored or blank)
    cat_opts = [
        "presented with labor pain",
        "presented with complications of pregnancy",
        "referred in from other facility",
    ]
    cat_box = _opt_box(cat_opts, admission_cat if admission_cat in cat_opts else None)

    top = _kv_grid_2x2(
        [
            ["MCTS No.", mcts],
            ["Booked", booked_box],
            ["IPD/Registration No.", ipd_reg],
            ["BPL/JSY Registration", bpl_box],
            ["Aadhar Card No.", aadhar],
            ["Referred from & Reason", (referred_from + (" | " + referral_reason if referral_reason else "")).strip(" |")],
        ]
    )

    mid = _boxed_table(
        [
            [
                _para(f"<b>Name:</b> {name}"),
                _para(f"<b>Age:</b> {age}"),
                _para(f"<b>W/o OR D/o:</b> {relation}"),
            ],
            [
                _para(f"<b>Address:</b> {address}"),
                _para(f"<b>Contact No:</b> {phone}"),
                _para(f"<b>Marital status:</b> {marital}"),
            ],
            [
                _para(f"<b>Admission date & time:</b> {admitted_at}"),
                _para(f"<b>Name of birth companion:</b> {companion}"),
                _para(" "),
            ],
            [
                _para("<b>Admission category:</b>"),
                _para(cat_box),
                _para(" "),
            ],
            [
                _para(f"<b>LMP:</b> {lmp}"),
                _para(f"<b>EDD:</b> {edd}"),
                _para(" "),
            ],
            [
                _para(f"<b>Provisional Diagnosis:</b> {prov_dx}"),
                _para(f"<b>Final Diagnosis:</b> {final_dx}"),
                _para(" "),
            ],
            [
                _para(f"<b>Contraception History:</b> {contraception}"),
                _para(" "),
                _para(" "),
            ],
        ],
        col_widths=[None, None, None],
    )

    delivery = _boxed_table(
        [
            [
                _para("<b>Delivery outcome:</b> Live / Abortion / Still Birth (Fresh/Macerated) / Preterm (Yes/No)"),
                _para("<b>Sex of Baby:</b> Male / Female"),
                _para("<b>Birth weight (in kgs):</b>"),
            ],
            [
                _para(_safe_str(delivery_outcome) or " "),
                _para(_safe_str(baby_sex) or " "),
                _para(_safe_str(baby_weight) or " "),
            ],
            [
                _para("<b>Delivery date:</b>"),
                _para("<b>Time:</b>"),
                _para("<b>Mode of Delivery/Procedure:</b> Normal / Assisted / CS / Other"),
            ],
            [
                _para(_safe_str(delivery_dt) or " "),
                _para(_safe_str(delivery_time) or " "),
                _para(_safe_str(mode_delivery) or " "),
            ],
            [
                _para("<b>Indication for assisted / LSCS / Others:</b>"),
                _para(" "),
                _para(" "),
            ],
            [
                _para("__________________________________________________________________________________"),
                _para(" "),
                _para(" "),
            ],
            [
                _para("<b>Final outcome:</b> Discharge / Referral / Death / LAMA / Abortion"),
                _para(" "),
                _para(" "),
            ],
            [
                _para(_safe_str(final_outcome) or " "),
                _para(" "),
                _para(" "),
            ],
        ],
        col_widths=[None, 50 * mm, 60 * mm],
    )

    sign = _boxed_table(
        [
            [_para("<b>Name and signature of service provider:</b> ________________________________"),
             _para("<b>Designation:</b> ____________________"),
             _para("<b>Date & Time:</b> ____________________")],
        ],
        col_widths=[None, 60 * mm, 55 * mm],
    )

    return [
        _section_band("Admission Form"),
        top,
        Spacer(1, 6),
        mid,
        Spacer(1, 6),
        delivery,
        Spacer(1, 6),
        sign,
        PageBreak(),
    ]


# ============================================================
# Section: Presenting complaints & history
# ============================================================
def _sec_presenting_history(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission

    # pull from admission if exists
    presenting = _safe_str(_get(adm, "presenting_complaints", "chief_complaint", default=""))
    medical = _safe_str(_get(adm, "medical_history", default=""))
    surgical = _safe_str(_get(adm, "surgical_history", default=""))
    family = _safe_str(_get(adm, "family_history", default=""))
    gravida = _safe_str(_get(adm, "gravida", default=""))
    parity = _safe_str(_get(adm, "parity", default=""))
    abortion = _safe_str(_get(adm, "abortion", default=""))
    living = _safe_str(_get(adm, "living_children", default=""))
    onset = _safe_str(_get(adm, "labour_onset_datetime", "onset_of_labor", default=""))

    # PV + exam fields (if you have)
    cerv_dil = _safe_str(_get(adm, "cervical_dilatation_cm", default=""))
    cerv_eff = _safe_str(_get(adm, "cervical_effacement_pct", default=""))
    pv_count = _safe_str(_get(adm, "pv_exams_count", default=""))
    membranes = _safe_str(_get(adm, "membranes", default=""))  # Ruptured/Intact
    amniotic = _safe_str(_get(adm, "amniotic_fluid_color", default=""))  # Clear/Meconium/Blood
    pelvis = _safe_str(_get(adm, "pelvis_adequate", default=""))  # Yes/No

    # Vitals
    pulse = _safe_str(_get(adm, "pulse", default=""))
    rr = _safe_str(_get(adm, "resp_rate", "respiratory_rate", default=""))
    fhr = _safe_str(_get(adm, "fhr", default=""))
    bp = _safe_str(_get(adm, "bp", default=""))
    temp = _safe_str(_get(adm, "temperature", "temp", default=""))

    # Investigations
    bg = _safe_str(_get(adm, "blood_group", default=""))
    hb = _safe_str(_get(adm, "hb", default=""))
    sugar = _safe_str(_get(adm, "blood_sugar", default=""))
    urine_prot = _safe_str(_get(adm, "urine_protein", default=""))
    urine_sugar = _safe_str(_get(adm, "urine_sugar", default=""))
    hiv = _safe_str(_get(adm, "hiv", default=""))
    hbsag = _safe_str(_get(adm, "hbsag", default=""))
    syphilis = _safe_str(_get(adm, "syphilis", default=""))
    malaria = _safe_str(_get(adm, "malaria", default=""))

    # Antenatal / gestation
    lmp = _safe_str(_get(adm, "lmp", default=""))
    edd = _safe_str(_get(adm, "edd", default=""))
    fundal = _safe_str(_get(adm, "fundal_height_wks", default=""))
    usg_age = _safe_str(_get(adm, "usg_gestation_age", default=""))
    preterm = _get(adm, "preterm", default=None)
    steroid = _get(adm, "antenatal_corticosteroid_given", default=None)

    # build Govt-like blocks
    block1 = _boxed_table(
        [
            [_para("<b>Presenting complaints:</b>"), _para(presenting or " ")],
        ],
        col_widths=[60 * mm, None],
    )

    block2 = _boxed_table(
        [
            [_para("<b>Past Obstetrics History:</b>"),
             _para("APH: ________   PPH: ________   PE/E: ________   C-section: ________"),
             _para("Obstructed labor: ________   Still births: ________   Congenital anomaly: ________"),
             _para("Anemia: ________   Others (Specify): __________________________________________")],
        ],
        col_widths=[None],
    )

    block3 = _boxed_table(
        [
            [_para("<b>Medical / Surgical History (Please specify):</b>"),
             _para((medical + ("\n" + surgical if surgical else "")).strip() or " ")],
            [_para("<b>Family H/o chronic illness (Please specify):</b>"), _para(family or " ")],
        ],
        col_widths=[70 * mm, None],
    )

    block4 = _boxed_table(
        [
            [
                _para(f"<b>Date and time of onset of labor:</b> {onset}"),
                _para(f"<b>Gravida:</b> {gravida}   <b>Parity:</b> {parity}   <b>Abortion:</b> {abortion}   <b>Living:</b> {living}"),
            ],
            [
                _para("<b>PV Examination</b> Cervical dilatation: ______  Cervical effacement: ______  No. of PV Examinations: ______"),
                _para(f"Cervical dilatation: {cerv_dil or ' '}   Cervical effacement: {cerv_eff or ' '}   PV count: {pv_count or ' '}"),
            ],
            [
                _para("Membranes: Ruptured / Intact     Colour of amniotic fluid: Clear / Meconium / Blood     Pelvis adequate: Yes / No"),
                _para(f"Membranes: {membranes or ' '}   AF colour: {amniotic or ' '}   Pelvis: {pelvis or ' '}"),
            ],
            [
                _para(f"<b>Gestational Age</b>  LMP: {lmp}   EDD: {edd}   Fundal height (wks): {fundal}   Age from USG: {usg_age}"),
                _para(f"Pre-term: {_yn_box(preterm if isinstance(preterm, bool) else None)}    Antenatal corticosteroid given: {_yn_box(steroid if isinstance(steroid, bool) else None)}"),
            ],
        ],
        col_widths=[None, None],
    )

    vitals = _boxed_table(
        [
            [_para("<b>Vitals</b> Pulse: ___/min   Respiratory rate: ___/min   FHR: ___/min   BP: ___ mmHg   Temperature: ___ C/F"),
             _para(f"Pulse: {pulse}   RR: {rr}   FHR: {fhr}   BP: {bp}   Temp: {temp}")],
        ],
        col_widths=[None, None],
    )

    inv = _boxed_table(
        [
            [_para("<b>Investigations</b> Blood Group & Rh: ____   Hb: ____   Blood Sugar: ____   Urine Protein: ____   Urine Sugar: ____"),
             _para(f"BG/Rh: {bg}   Hb: {hb}   Sugar: {sugar}   Ur Prot: {urine_prot}   Ur Sugar: {urine_sugar}")],
            [_para("HIV: ____   HBsAg: ____   Syphilis: ____   Malaria: ____   Others: _________________________________"),
             _para(f"HIV: {hiv}   HBsAg: {hbsag}   Syphilis: {syphilis}   Malaria: {malaria}")],
        ],
        col_widths=[None, None],
    )

    exam = _boxed_table(
        [
            [_para("<b>PA Examination</b> Presentation: Cephalic / Others ___   Engagement: ___   Lie: ___"),
             _para("General: Height: ____ cms   Weight: ____ kgs   Pallor: ___   Jaundice: ___   Pedal Edema: ___")],
        ],
        col_widths=[None, None],
    )

    return [
        _section_band("Presenting Complaints & History"),
        block1,
        Spacer(1, 6),
        block2,
        Spacer(1, 6),
        block3,
        Spacer(1, 6),
        block4,
        Spacer(1, 6),
        vitals,
        Spacer(1, 6),
        inv,
        Spacer(1, 6),
        exam,
        PageBreak(),
    ]


# ============================================================
# SAFE Childbirth Checklist (structured, image-free)
# ============================================================
def _sec_safe_checklist_1(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_band("Before Birth – SAFE CHILDBIRTH CHECKLIST"))

    # Page 1 (Admission)
    story.append(_boxed_lines(
        "CHECK-1 On Admission",
        [
            "Does Mother need referral?   Yes ( )   No ( )",
            "Partograph started?   Yes ( )   No ( )   (Start when cervix ≥ 4 cm)",
            "Does Mother need antibiotics?   Yes, given ( )   No ( )",
            "Inj. Magnesium sulfate?   Yes, given ( )   No ( )",
            "Corticosteroid (24–34 weeks if indicated)?   Yes, given ( )   No ( )",
            "HIV status of the mother: Positive ( )  Negative ( )  Follow Universal Precautions ( )",
            "Encouraged a birth companion during labour/birth/till discharge: Yes ( )  No ( )",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Counsel Mother and Birth Companion to call for help if there is:",
        [
            "• Bleeding",
            "• Severe abdominal pain",
            "• Difficulty in breathing",
            "• Severe headache or blurring vision",
            "• Urge to push",
            "• Can’t empty bladder every 2 hours",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Confirm Supplies / Hygiene",
        [
            "Are soap, water and gloves available?   Yes ( )   No ( )",
            "I will wash hands and wear gloves for each vaginal exam ( )",
            "If not available, supplies arranged ( )",
            "Mother/companion will call for help during labour if needed ( )",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Refer to FRU/Higher centre if danger signs present (tick):",
        [
            "Vaginal bleeding ( )   High fever ( )   Severe headache/blurred vision ( )   Convulsions ( )",
            "Severe abdominal pain ( )   Difficulty in breathing ( )   Oligouria (<400 ml/24 hrs) ( )",
            "Foul-smelling discharge ( )   History of heart disease/major illness ( )",
        ],
    ))

    # ✅ split checklist into two pages to guarantee no LayoutError
    story.append(PageBreak())

    # Page 2 (Clinical criteria blocks)
    story.append(_section_band("SAFE Checklist – Clinical Actions (Continuation)"))

    story.append(_boxed_lines(
        "Give antibiotics to Mother if (tick):",
        [
            "Temperature ≥ 38°C (100.5°F) ( )",
            "Foul-smelling vaginal discharge ( )",
            "Rupture of membranes >12 hrs without labour OR >18 hrs with labour ( )",
            "Labour >24 hrs / obstructed labour ( )",
            "Rupture of membranes <37 wks gestation ( )",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Give first dose of inj. magnesium sulfate and refer OR full dose if at FRU if:",
        [
            "Systolic ≥160 or Diastolic ≥110 with ≥+3 proteinuria OR BP ≥140/90 with trace to +2 proteinuria along with:",
            "Severe headache ( )  Pain in upper abdomen ( )  Convulsions ( )  Blurring of vision ( )  Difficulty breathing ( )",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Give corticosteroids (24–34 weeks) if:",
        [
            "True pre-term labour ( )",
            "Imminent delivery (APH, PPROM, Severe PE/E) ( )",
            "Dose: Inj. Dexamethasone 6 mg IM 12 hourly – total 4 doses",
        ],
    ))
    story.append(Spacer(1, 6))

    story.append(_boxed_lines(
        "Provider Details",
        [
            "Name of Provider: ____________________________",
            "Date: ____/____/____     Signature: ____________________________",
            "NO OXYTOCIN/other uterotonics for unnecessary induction/augmentation of labour",
            "Counsel: No bath/oil for baby, no pre-lacteal feed, start breastfeeding within 30 minutes, wrap and keep warm",
        ],
    ))

    story.append(PageBreak())
    return story



def _sec_obstetric_notes(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    augmentation = _get(adm, "augmentation_performed", default=None)
    indication = _safe_str(_get(adm, "augmentation_indication", default=""))

    t = _boxed_table(
        [
            [
                _para("<b>OBSTETRIC NOTES (INTERVENTIONS BEFORE DELIVERY)</b>", "Small"),
            ],
            [
                _para(f"Augmentation performed: {_yn_box(augmentation if isinstance(augmentation, bool) else None)}", "Small"),
            ],
            [
                _para("If yes, specify indication for augmentation:", "Small"),
            ],
            [
                _para(indication or "__________________________________________________________________________________", "Small"),
            ],
            [
                _para("<b>AUGMENT ONLY IF INDICATED AND IN CENTERS WITH FACILITY FOR C-SECTION</b>", "Small"),
            ],
        ],
        col_widths=[None],
    )
    return [
        _section_band("Obstetric Notes"),
        t,
        PageBreak(),
    ]


# ============================================================
# Partograph (blank grid, image-free)
# ============================================================
def _sec_partograph(_: IpdCaseSheetData) -> List[Any]:
    s = _sty("Small", "Small")

    # Identification data lines
    ident = _boxed_table(
        [
            [
                _para("<b>THE SIMPLIFIED PARTOGRAPH</b><br/>Start plotting partograph when woman is in active labor i.e., Cx ≥ 4 cms", "Small")
            ],
            [
                _para("Identification Data:  Name: ____________________   W/o: ____________________   Age: _____   Reg. No.: ____________________", "Small")
            ],
            [
                _para("Date & Time of Admission: ____________________   Date & Time of ROM: ____________________", "Small")
            ],
        ],
        col_widths=[None],
    )

    # Build a blank graph grid (simple, but Govt-like)
    # 12 time columns + label column
    cols = 13
    header = [""] + [str(i) for i in range(1, 13)]
    grid = [header]
    # Foetal condition block
    grid.append(["Foetal heart rate"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)
    grid.append(["Amniotic fluid"] + [""] * 12)
    grid.append([""] + [""] * 12)

    # Labour block
    grid.append(["Cervix (cm) (Plot X)"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)

    grid.append(["Contractions per 10 min"] + [""] * 12)
    for _ in range(4):
        grid.append([""] + [""] * 12)

    grid.append(["Drugs and IV fluid given"] + [""] * 12)
    grid.append([""] + [""] * 12)

    # Maternal condition
    grid.append(["Pulse and BP"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)
    grid.append(["Temp (°C)"] + [""] * 12)

    t = Table(grid, colWidths=[40 * mm] + [ (None) ] * 12, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 7),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    footer = _boxed_table(
        [[Paragraph("Initiate plotting on alert line     |     Refer to FRU when ALERT LINE is crossed", s)]],
        col_widths=[None],
    )

    return [
        _section_band("Partograph (Blank Grid, No Images)"),
        ident,
        Spacer(1, 6),
        t,
        Spacer(1, 6),
        footer,
        PageBreak(),
    ]


# ============================================================
# Consent + Pre-anesthetic + Anesthesia Notes
# ============================================================
def _sec_consent(_: IpdCaseSheetData) -> List[Any]:
    consent1 = _boxed_table(
        [
            [
                _para(
                    "<b>Consent for procedures</b><br/>"
                    "I ____________________ son/daughter/wife of ____________________ age (yrs) _____ address ____________________<br/>"
                    "________________________________________ myself ( ) or other ( ) age _____ relation (Son/Daughter/Father/Mother/Wife/Other) __________ "
                    "provide my consent for Medical/Surgical __________ / Anesthesia __________ procedure (write the name of procedure).<br/>"
                    "I have been informed about probable consequences in detail in the language I understand. "
                    "I am signing this consent without undue pressure and in complete consciousness.<br/>"
                    "The doctor/provider shall not be held responsible in case of complication/mishap.<br/><br/>"
                    "Parent/Guardian's signature: ____________________",
                    "Small",
                )
            ]
        ],
        col_widths=[None],
    )

    consent2 = _boxed_table(
        [
            [
                _para(
                    "<b>Consent for PPIUCD</b><br/>"
                    "I ____________________ son/daughter/wife of ____________________ age (yrs) ____ address ____________________<br/>"
                    "_________________ myself ( ) or other ( ) age _____ relation (Mother/Father/Husband) __________ provide my consent for procedure "
                    "(write the name of procedure).<br/>"
                    "I have been informed about probable consequences in detail in the language I understand. "
                    "I am signing this consent without undue pressure and in complete consciousness.<br/>"
                    "The doctor/provider shall not be held responsible in case of complication/mishap.<br/><br/>"
                    "Name & signature of patient: ____________________      Name & signature of attendant: ____________________",
                    "Small",
                )
            ]
        ],
        col_widths=[None],
    )

    return [
        _section_band("Consent Forms"),
        consent1,
        Spacer(1, 6),
        consent2,
        PageBreak(),
    ]


def _sec_pre_anaesthetic(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_band("Notes on Pre-Anesthetic Check-up"),
        _boxed_table(
            [[
                _para(
                    "Date: ____/____/____   Time: __________<br/>"
                    "Planned procedure: ________________________________________________________________<br/><br/>"
                    "<b>History:</b><br/>"
                    "__________________________________________________________________________________<br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "<b>Physical Examination:</b><br/>"
                    "__________________________________________________________________________________<br/>"
                    "CVS: _____________________________________________________________________________<br/>"
                    "RS: ______________________________________________________________________________<br/>"
                    "CNS: _____________________________________________________________________________<br/>"
                    "Others: __________________________________________________________________________<br/><br/>"
                    "<b>Investigations:</b><br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "<b>Instructions:</b><br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "Signature of Anesthetist: ____________________",
                    "Small",
                )
            ]],
            col_widths=[None],
        ),
        PageBreak(),
    ]


def _sec_anaesthesia_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_band("Anesthesia Notes"),
        _boxed_table(
            [[
                _para(
                    "Date: ____/____/____   Start time: __________   End time: __________<br/>"
                    "Procedure: _______________________________________________________________________<br/>"
                    "Name of Anesthesiologist: ____________________   Name of Anesthesia Nurse: ____________________<br/><br/>"
                    "<b>Notes:</b><br/>"
                    "__________________________________________________________________________________<br/>"
                    "__________________________________________________________________________________<br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "<b>Reversal:</b><br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "<b>Post-operative Instructions:</b><br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "Signature of Anesthetist: ____________________",
                    "Small",
                )
            ]],
            col_widths=[None],
        ),
        PageBreak(),
    ]


# ============================================================
# SAFE Checklist 2/3/4 (structured)
# ============================================================
def _sec_safe_checklist_2(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_band("Just Before and During Birth – SAFE CHILDBIRTH CHECKLIST"))
    story.append(_boxed_lines(
        "CHECK-2 Just Before and During Birth (or C-Section)",
        [
            "Give antibiotics to Mother if any present (tick):",
            "• Temperature ≥38°C ( )  • Foul-smelling discharge ( )  • ROM >18 hrs with labour ( )",
            "• Labour >24 hrs/obstructed ( )  • Cesarean section ( )",
            "",
            "Does Mother need: Antibiotics? Yes, given ( ) No ( )     Magnesium sulfate? Yes, given ( ) No ( )",
            "",
            "Confirm essential supplies:",
            "For Mother: Gloves ( ) Soap & clean water ( ) Oxytocin 10 units syringe ( ) Pads ( )",
            "For Baby: Two clean warm towels ( ) Sterile scissors/blade ( ) Mucus extractor ( ) Cord ties ( ) Bag-and-mask ( )",
            "",
            "Care for mother after birth (AMTSL):",
            "• Oxytocin 10 units IM within 1 minute ( )  • Controlled cord traction ( )  • Uterine massage ( )",
            "",
            "Care for baby after birth:",
            "• Dry, wrap, keep warm ( )  • Vit K ( )  • Initiate breastfeeding ( )",
            "If not breathing: clear airway + stimulate ( ) then bag-and-mask ( ) call for help ( )",
            "",
            "Provider: Name ____________________   Date ____/____/____   Signature ____________________",
        ],
    ))
    story.append(PageBreak())
    return story


def _sec_safe_checklist_3(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_band("Soon After Birth – SAFE CHILDBIRTH CHECKLIST"))
    story.append(_boxed_lines(
        "CHECK-3 Soon After Birth (within 1 hour)",
        [
            "Is Mother bleeding abnormally?   Yes ( )   No ( )",
            "Does Mother need: Antibiotics? Yes, given ( ) No ( )    Magnesium sulfate? Yes, given ( ) No ( )",
            "Does Baby need: Antibiotics? Yes, given ( ) No ( )    Referral? Yes, organized ( ) No ( )",
            "Special care/monitoring?   Yes, organized ( )   No ( )",
            "Syrup Nevirapine (if HIV+ as per protocol): Yes ( ) No ( )",
            "",
            "Provider: Name ____________________   Date ____/____/____   Signature ____________________",
        ],
    ))
    story.append(PageBreak())
    return story


def _sec_safe_checklist_4(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_band("Before Discharge – SAFE CHILDBIRTH CHECKLIST"))
    story.append(_boxed_lines(
        "CHECK-4 Before Discharge",
        [
            "Is Mother’s bleeding controlled?   Yes ( )   No ( )",
            "Does mother need antibiotics?   Yes ( )   No ( )",
            "Does baby need antibiotics?   Yes ( )   No ( )",
            "Is baby feeding well?   Yes ( )   No ( )",
            "",
            "Counsel danger signs:",
            "Baby: fast/difficult breathing, fever, unusually cold, stops feeding, lethargy, whole body yellow",
            "Mother: excessive bleeding, severe abdominal pain, severe headache/blurred vision, breathing difficulty, fever/chills, foul discharge",
            "",
            "Offer family planning options ( )   Arrange follow-up/transport ( )",
            "",
            "Provider: Name ____________________   Date ____/____/____   Signature ____________________",
        ],
    ))
    story.append(PageBreak())
    return story



# ============================================================
# Procedure / Delivery / Continuation / Transfusion notes
# ============================================================
def _sec_procedure_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_band("Operation / Procedure Notes (If applicable)"),
        _boxed_table(
            [[
                _para(
                    "Procedure performed: ______________________________________________________________<br/>"
                    "Indication for the procedure: ______________________________________________________<br/><br/>"
                    "__________________________________________________________________________________<br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "Condition at transfer to ward: _____________________________________________________<br/><br/>"
                    "Treatment advised: _________________________________________________________________<br/>"
                    "__________________________________________________________________________________<br/><br/>"
                    "Whether Patient/Guardian explained about the procedure and probable complications: Yes ( ) No ( )<br/>"
                    "Consent of patient/guardian: Yes ( ) No ( )<br/>"
                    "Procedure start time: __________   Procedure end time: __________   Type of Anesthesia: __________<br/><br/>"
                    "Procedure notes: _________________________________________________________________<br/><br/>"
                    "Signature of Doctor: ____________________",
                    "Small",
                )
            ]],
            col_widths=[None],
        ),
        PageBreak(),
    ]


def _sec_delivery_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_band("Delivery Notes"),
        _boxed_table(
            [[
                _para(
                    "<b>DELIVERY NOTES</b><br/>"
                    "Delivery date: ____/____/____   Time: __________<br/>"
                    "Abortion ( )   Single ( )   Twin/Multiple ( )<br/>"
                    "Episiotomy: No ( ) Yes ( )   Delayed Cord Clamping ( )<br/>"
                    "PPIUCD Inserted: Yes ( ) No ( )<br/>"
                    "Type of delivery: Normal ( )  Assisted: Vacuum ( ) Forceps ( )  LSCS ( )  Others ( ) __________<br/>"
                    "Outcome: Live birth ( )  Fresh Still Birth ( )  Macerated Still Birth ( )<br/>"
                    "AMTSL performed: No ( ) Yes ( )<br/>"
                    "1. Uterotonic administered: Inj. Oxytocin ( ) OR Tab Misoprostol ( )<br/>"
                    "2. CCT: Yes ( ) No ( )<br/>"
                    "3. Uterine massage: Yes ( ) No ( )<br/>"
                    "Complications (tick): PPH ( ) Sepsis ( ) PE/E ( ) Prolonged labor ( ) Obstructed labor ( ) Fetal distress ( )<br/>"
                    "Maternal death: Cause and Time: ___________________________________________________<br/>"
                    "Others (specify): _________________________________________________________________<br/><br/>"
                    "<b>BABY NOTES</b><br/>"
                    "Sex of the baby: Male ( ) Female ( )   Skin-to-skin contact done: Yes ( ) No ( )<br/>"
                    "Any congenital anomaly (specify): _________________________________________________<br/>"
                    "Any other complication (specify): _________________________________________________<br/>"
                    "Injection Vitamin K1 administered: Yes ( ) No ( )  If yes, dose: __________<br/>"
                    "Vaccination done: BCG ( ) OPV ( ) Hep B ( )   Temperature of baby: __________<br/>"
                    "Birth weight (kg): __________   Did the baby cry immediately after birth: Yes ( ) No ( )<br/>"
                    "Did the baby require resuscitation: Yes ( ) No ( )  If yes, initiated in labor room: Yes ( ) No ( )<br/>"
                    "Breastfeeding initiated: Yes ( ) No ( )   Time of initiation: __________<br/>"
                    "Preterm: Yes ( ) No ( )<br/>",
                    "Small",
                )
            ]],
            col_widths=[None],
        ),
        PageBreak(),
    ]


def _sec_post_delivery_continuation(_: IpdCaseSheetData) -> List[Any]:
    # Govt sample shows multiple continuation sheets (we provide 2 pages)
    sheet = _boxed_table(
        [[_para("<b>Continuation Sheet for Post Delivery Notes</b>", "Small")],
         [_para("Notes:", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("__________________________________________________________________________________", "Small")]],
        col_widths=[None],
    )
    return [
        _section_band("Post Delivery Notes (Continuation)"),
        sheet,
        PageBreak(),
        _section_band("Post Delivery Notes (Continuation)"),
        sheet,
        PageBreak(),
    ]


def _sec_transfusion_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_band("Blood Transfusion or Other Procedure Notes"),
        _boxed_table(
            [[_para("Details / Notes:", "Small")],
             [_para("__________________________________________________________________________________", "Small")],
             [_para("__________________________________________________________________________________", "Small")],
             [_para("__________________________________________________________________________________", "Small")],
             [_para("__________________________________________________________________________________", "Small")],
             [_para("Signature: ____________________   Date: ____/____/____   Time: __________", "Small")]],
            col_widths=[None],
        ),
        PageBreak(),
    ]


# ============================================================
# Postpartum Assessment Grid (Mother + Baby) like sample
# ============================================================
def _sec_postpartum_assessment(_: IpdCaseSheetData) -> List[Any]:
    s = _sty("Small", "Small")

    # columns as in sample
    cols = ["", ""] + ["30 min", "30 min", "30 min", "30 min", "6 hrs", "6 hrs", "6 hrs", "Day 2\nMorning", "Day 2\nEvening"]

    def row(section: str, param: str) -> List[Any]:
        return [section, param] + [""] * (len(cols) - 2)

    data: List[List[Any]] = []
    data.append(cols)

    # Mother block
    mother_rows = [
        "BP (mmHg)",
        "Temp (°C/°F)",
        "Pulse (per min)",
        "Breast condition (soft/engorged)",
        "Bleeding PV (Normal-N / Excessive-E)",
        "Uterine Tone (Soft-S / Contracted-C / Tender-T)",
        "Episiotomy/Tear (healthy/infected)",
    ]
    for i, p in enumerate(mother_rows):
        data.append(row("Mother" if i == 0 else "", p))

    # Baby block
    baby_rows = [
        "Resp rate (per min)",
        "Temp (°C/°F)",
        "Breastfeeding/Suckling (yes/no)",
        "Activity (good/lethargy)",
        "Umbilical stump (dry/bleeding)",
        "Jaundice (yes/no)",
        "Passed urine? (yes/no)",
        "Passed stool? (yes/no)",
    ]
    for i, p in enumerate(baby_rows):
        data.append(row("Baby" if i == 0 else "", p))

    t = Table(data, colWidths=[22 * mm, 55 * mm] + [18 * mm] * (len(cols) - 2), hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 7),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("SPAN", (0, 1), (0, 7)),
                ("SPAN", (0, 8), (0, 15)),
                ("VALIGN", (0, 1), (0, 15), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    top_notes = _boxed_table(
        [[_para("Notes for Mother:", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("Notes for Baby:", "Small")],
         [_para("__________________________________________________________________________________", "Small")],
         [_para("Clinical diagnosis (tick): Normal ( ) Infection ( ) Jaundice ( ) Hypothermia ( ) Convulsions ( ) Death ( ) Others: __________", "Small")],
         [_para("Date & time of transfer to PNC ward: ____/____/____  Time: __________   Condition at transfer: ____________________________", "Small")],
         [_para("If referred, reason for referral of mother/baby: _________________________________________________", "Small")]],
        col_widths=[None],
    )

    return [
        _section_band("Assessment of Postpartum Condition"),
        top_notes,
        Spacer(1, 6),
        t,
        PageBreak(),
    ]


# ============================================================
# Discharge Notes + Discharge Form
# ============================================================
def _sec_discharge_notes(d: IpdCaseSheetData) -> List[Any]:
    ds = d.discharge
    # Use discharge summary if present; else blank lines
    cond = _safe_str(_get(ds, "discharge_condition", default=""))
    advise = _safe_str(_get(ds, "follow_up", "advice", default=""))
    other = _safe_str(_get(ds, "notes", default=""))

    t = _boxed_table(
        [
            [_para("<b>Discharge Notes</b>", "Small")],
            [_para("Condition at Discharge:", "Small")],
            [_para(cond or "__________________________________________________________________________________", "Small")],
            [_para("Advise at Discharge:", "Small")],
            [_para(advise or "__________________________________________________________________________________", "Small")],
            [_para("Other notes:", "Small")],
            [_para(other or "__________________________________________________________________________________", "Small")],
        ],
        col_widths=[None],
    )

    return [
        _section_band("Discharge Notes"),
        t,
        PageBreak(),
    ]


def _sec_discharge_form(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    patient = getattr(adm, "patient", None)

    name = _safe_str(_patient_full_name(patient))
    age = _calc_age_years(_get(patient, "dob", default=None))
    mcts = _safe_str(_get(adm, "mcts_no", "mcts_number", default=""))
    ipd_reg = _safe_str(_get(adm, "display_code", "registration_no", default=""))

    t = _boxed_table(
        [[
            _para(
                "<b>Discharge/ Referral/ LAMA/ Death Form</b> (Tick whichever applicable)<br/><br/>"
                f"Name: {name}   W/o or D/o: ____________________   Age (yrs): {age}   MCTS No.: {mcts}<br/>"
                f"IPD/Registration No.: {ipd_reg}<br/><br/>"
                "Date of admission: ____/____/____   Time of admission: __________<br/>"
                "Date of Discharge/Referral: ____/____/____   Time of Discharge/Referral: __________<br/>"
                "Date of delivery: ____/____/____   Time of delivery: __________<br/><br/>"
                "Delivery outcome: Live birth ( )  Abortion ( )  Single ( )  Still birth Fresh ( )  Macerated ( )  Twins/Multiple ( )<br/>"
                "Final outcome: Discharge ( )  Referred out ( )  LAMA ( )  Death ( )  Abortion ( )<br/><br/>"
                "<b>Discharge summary:</b><br/>"
                "Condition of mother: ____________________________________________<br/>"
                "FP option (if provided): _________________________________________<br/>"
                "Condition of baby: ______________________________________________<br/>"
                "Sex of baby: M ( ) F ( )   Birth weight (kgs): ________<br/>"
                "Pre-term: Yes ( ) No ( )   Inj. Vit K1: Yes ( ) No ( )<br/>"
                "Immunization: BCG ( ) OPV ( ) Hepatitis B ( )<br/>"
                "Advice on discharge: Counselling on danger signs for mother and baby ( )  Rest/nutrition/fluids ( )<br/>"
                "Tab iron: ________   Tab calcium: ________<br/><br/>"
                "<b>Treatment given:</b><br/>"
                "__________________________________________________________________________________<br/>"
                "__________________________________________________________________________________<br/><br/>"
                "Follow-up date: ____/____/____<br/><br/>"
                "<b>Referral summary (if referred):</b><br/>"
                "Reason for referral: _____________________________________________________________<br/>"
                "Facility name (referred to): _____________________________________________________<br/>"
                "Treatment given on referral: _____________________________________________________<br/>"
                "__________________________________________________________________________________<br/><br/>"
                "Name and Phone No. / Signature of service provider: ________________________________<br/>",
                "Small",
            )
        ]],
        col_widths=[None],
    )
    return [
        _section_band("Notes on Discharge / Referral / Death"),
        t,
        PageBreak(),
    ]


def _sec_signatures(d: IpdCaseSheetData) -> List[Any]:
    ds = d.discharge
    prepared = _safe_str(_get(ds, "prepared_by_name", default=""))
    reviewed = _safe_str(_get(ds, "reviewed_by_name", default=""))
    regno = _safe_str(_get(ds, "reviewed_by_regno", default=""))

    t = _boxed_table(
        [
            [_para("<b>Signatures</b>", "Small")],
            [_para(f"Prepared By: {prepared or '__________________________'}", "Small")],
            [_para(f"Reviewed By (Doctor): {reviewed or '__________________________'}   Reg No: {regno or '__________'}", "Small")],
            [_para("Patient/Attendant Acknowledgement: __________________________", "Small")],
            [_para("Date & Time: ____________________", "Small")],
        ],
        col_widths=[None],
    )
    return [
        _section_band("Signatures & Acknowledgement"),
        t,
    ]


# ============================================================
# Build PDF
# ============================================================
def build_ipd_case_sheet_pdf(
    *,
    db,
    admission_id: int,
    template_id: Optional[int],
    period_from: Optional[datetime],
    period_to: Optional[datetime],
    user: Any = None,          # ✅ accept route param
    **_kwargs: Any,            # ✅ ignore any future extra params
) -> tuple[bytes, str]:
    tpl = _resolve_template(db, template_id)
    sections = sorted(tpl["sections"], key=lambda x: int(x.get("order", 9999)))
    enabled_codes = {s["code"] for s in sections if s.get("enabled") or s.get("required")}

    data = _load_ipd_case_sheet_data(db, admission_id)
    adm = data.admission

    subtitle = ""
    if period_from or period_to:
        subtitle = f"Report Period: {_safe_str(period_from.date() if period_from else '')} to {_safe_str(period_to.date() if period_to else '')}"

    story: List[Any] = []

    # Govt header always first (no engine title duplication)
    if "gov_header" in enabled_codes:
        story.extend(_gov_header(db, data))

    # sections in order
    for s in sections:
        code = s.get("code")
        required = bool(s.get("required"))
        enabled = bool(s.get("enabled"))
        if not (enabled or required):
            continue

        if code == "gov_header":
            continue
        if code == "admission_form":
            story.extend(_sec_admission_form(data))
        elif code == "presenting_history":
            story.extend(_sec_presenting_history(data))
        elif code == "safe_checklist_1":
            story.extend(_sec_safe_checklist_1(data))
        elif code == "obstetric_notes":
            story.extend(_sec_obstetric_notes(data))
        elif code == "partograph":
            story.extend(_sec_partograph(data))
        elif code == "consent":
            story.extend(_sec_consent(data))
        elif code == "pre_anaesthetic":
            story.extend(_sec_pre_anaesthetic(data))
        elif code == "anaesthesia_notes":
            story.extend(_sec_anaesthesia_notes(data))
        elif code == "safe_checklist_2":
            story.extend(_sec_safe_checklist_2(data))
        elif code == "procedure_notes":
            story.extend(_sec_procedure_notes(data))
        elif code == "delivery_notes":
            story.extend(_sec_delivery_notes(data))
        elif code == "post_delivery_continuation":
            story.extend(_sec_post_delivery_continuation(data))
        elif code == "transfusion_notes":
            story.extend(_sec_transfusion_notes(data))
        elif code == "safe_checklist_3":
            story.extend(_sec_safe_checklist_3(data))
        elif code == "postpartum_assessment":
            story.extend(_sec_postpartum_assessment(data))
        elif code == "safe_checklist_4":
            story.extend(_sec_safe_checklist_4(data))
        elif code == "discharge_notes":
            story.extend(_sec_discharge_notes(data))
        elif code == "discharge_form":
            story.extend(_sec_discharge_form(data))
        elif code == "signatures":
            story.extend(_sec_signatures(data))
        else:
            continue

        story.append(Spacer(1, 6))

    pdf = build_pdf(
        db=db,
        ctx=PdfBuildContext(
            title="",  # keep blank to avoid duplicate engine headers
            subtitle=subtitle or "",
            meta={"admission_id": admission_id},
        ),
        story=story,
    )

    filename = f"Maternity_CaseSheet_L3_{_safe_str(getattr(adm, 'display_code', admission_id))}.pdf"
    return pdf, filename
