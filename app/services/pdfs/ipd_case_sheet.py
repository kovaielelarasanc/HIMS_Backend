# FILE: app/services/pdf/ipd_case_sheet.py
from __future__ import annotations

import os
import logging
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Callable, Tuple

from sqlalchemy.orm import joinedload
from sqlalchemy import and_

from reportlab.platypus import (
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    Image,
)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

logger = logging.getLogger(__name__)

# ✅ Patient model import can vary by project; keep it safe
try:
    from app.models.patient import Patient  # common split model
except Exception:  # pragma: no cover
    from app.models.ipd import Patient  # fallback if Patient is in ipd.py

# ---- IPD models ----
from app.models.ipd import (
    IpdAdmission,
    IpdBed,
    IpdRoom,
    IpdWard,
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
    IpdDischargeChecklist,
    IpdDischargeMedication,
)

from app.models.ipd_referral import IpdReferral
from app.models.ipd_nursing import (
    IcuFlowSheet,
    IpdBloodTransfusion,
    IpdDressingRecord,
    IpdIsolationPrecaution,
    IpdRestraintRecord,
    IpdNursingTimeline,
)

# optional newborn model
try:  # pragma: no cover
    from app.models.ipd_newborn import IpdNewbornResuscitation
except Exception:  # pragma: no cover
    IpdNewbornResuscitation = None  # type: ignore

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

# =============================================================================
# GOVT STYLE PALETTE (print-friendly)
# =============================================================================
GOV_BLUE = colors.HexColor("#1E5DA8")
GOV_BLUE_DARK = colors.HexColor("#174B86")
GOV_LT_BLUE = colors.HexColor("#DCEBFA")
GOV_LT_BLUE2 = colors.HexColor("#EEF6FF")

SAFE_PINK = colors.HexColor("#FCE4EC")
SAFE_PINK2 = colors.HexColor("#FFF1F4")
SAFE_GREEN = colors.HexColor("#E8F5E9")
SAFE_YELLOW = colors.HexColor("#FFF8E1")

BORDER = colors.black
GRID = colors.lightgrey


# ---------------------------------------------------------------------------
# SAFE FILE HANDLING + LOGO (NO CRASH)
# ---------------------------------------------------------------------------
def _safe_relpath(rel: str | None) -> str:
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    # prevent traversal
    rel = rel.replace("..", "")
    return rel


def _resolve_storage_file(storage_dir: str, rel_path: str | None) -> Optional[Path]:
    rel = _safe_relpath(rel_path)
    if not rel:
        return None
    p = Path(storage_dir).joinpath(rel)
    if p.exists() and p.is_file():
        return p
    return None


def _get_storage_dir() -> str:
    """
    Where uploaded files live on disk.
    Priority:
      1) env STORAGE_DIR / UPLOAD_DIR / MEDIA_ROOT
      2) ./storage (project root/cwd)
    """
    for k in ("STORAGE_DIR", "UPLOAD_DIR", "MEDIA_ROOT"):
        v = (os.getenv(k) or "").strip()
        if v:
            return os.path.abspath(v)
    return os.path.abspath(os.path.join(os.getcwd(), "storage"))


def _safe_image_abs_path(storage_dir: str, raw_path: str | None) -> Optional[str]:
    """
    Returns an absolute path string for ReportLab Image() if the file exists.
    Accepts:
      - absolute file path
      - relative path stored in DB like: branding/logo_x.png
    """
    raw = (raw_path or "").strip()
    if not raw:
        return None

    # Skip remote URLs (ReportLab won't fetch remote reliably in your setup)
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("data:"):
        return None

    # Absolute path support
    try:
        p = Path(raw)
        if p.is_absolute() and p.exists() and p.is_file():
            return str(p)
    except Exception:
        pass

    # Relative under storage_dir
    p2 = _resolve_storage_file(storage_dir, raw)
    if p2:
        return str(p2)

    # Extra fallback search (useful in dev envs)
    try:
        here = Path(__file__).resolve()
        backend_root = here.parents[4] if len(here.parents) > 4 else Path.cwd()
    except Exception:
        backend_root = Path.cwd()

    candidates = [
        Path.cwd() / raw,
        backend_root / raw,
        backend_root / "storage" / _safe_relpath(raw),
        backend_root / "app" / raw,
        backend_root / "app" / "static" / raw,
        backend_root / "static" / raw,
    ]

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except Exception:
            continue

    # last chance: file name inside storage/branding
    try:
        fname = Path(raw).name
        if fname:
            c2 = Path(storage_dir) / "branding" / fname
            if c2.exists() and c2.is_file():
                return str(c2)
    except Exception:
        pass

    return None


def _rl_image_fit(abs_path: str | None, max_w, max_h, hAlign: str = "LEFT"):
    """
    Creates a ReportLab platypus Image scaled to fit within max_w x max_h.
    If missing/invalid -> returns Spacer to avoid PDF crash.
    """
    if not abs_path:
        return Spacer(max_w, max_h)

    try:
        ir = ImageReader(abs_path)
        iw, ih = ir.getSize()
        if not iw or not ih:
            return Spacer(max_w, max_h)

        scale = min(float(max_w) / float(iw), float(max_h) / float(ih))
        w = iw * scale
        h = ih * scale

        img = Image(abs_path, width=w, height=h)
        img.hAlign = hAlign
        return img
    except Exception:
        logger.exception("Image load failed: %s", abs_path)
        return Spacer(max_w, max_h)


def _try_image(path_or_rel: str | None, width_mm: float, height_mm: float, *, hAlign: str = "LEFT"):
    """
    ✅ Safe logo/image flowable:
    - resolves DB relative paths under storage dir (storage/branding/..)
    - validates eagerly (ImageReader)
    - NEVER crashes build: returns Spacer if missing/invalid
    """
    storage_dir = _get_storage_dir()
    abs_path = _safe_image_abs_path(storage_dir, (path_or_rel or "").strip())
    return _rl_image_fit(abs_path, width_mm * mm, height_mm * mm, hAlign=hAlign)


# =============================================================================
# Helpers
# =============================================================================
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


def _para(txt: str, style: str = "Small") -> Paragraph:
    s = _sty(style, "Small")
    safe = _safe_str(txt).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
    return Paragraph(safe if safe.strip() else "&nbsp;", s)


def _h_center(txt: str, style: str = "Normal") -> Paragraph:
    return Paragraph(f"<para align='center'><b>{_safe_str(txt)}</b></para>", _sty(style, style))


def _yn(v: Optional[bool]) -> str:
    # Govt-style (image-free) check boxes
    if v is True:
        return "Yes (X)   No ( )"
    if v is False:
        return "Yes ( )   No (X)"
    return "Yes ( )   No ( )"


def _opt_box(options: List[str], selected: Optional[str]) -> str:
    out = []
    for o in options:
        out.append(f"{o} ({'X' if (selected or '').strip().lower() == o.strip().lower() else ' '})")
    return "   ".join(out)


def _table(
    data: List[List[Any]],
    col_widths: Optional[List[Any]] = None,
    *,
    repeat_rows: int = 0,
    outer_box: float = 0.8,
    inner_grid: float = 0.25,
    bg: Optional[Any] = None,
    paddings: Tuple[int, int, int, int] = (4, 4, 3, 3),
    valign: str = "TOP",
) -> Table:
    t = Table(data, colWidths=col_widths, repeatRows=repeat_rows, hAlign="LEFT", splitByRow=1)
    lp, rp, tp, bp = paddings
    ts = [
        ("VALIGN", (0, 0), (-1, -1), valign),
        ("BOX", (0, 0), (-1, -1), outer_box, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), inner_grid, GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), lp),
        ("RIGHTPADDING", (0, 0), (-1, -1), rp),
        ("TOPPADDING", (0, 0), (-1, -1), tp),
        ("BOTTOMPADDING", (0, 0), (-1, -1), bp),
    ]
    if bg is not None:
        ts.append(("BACKGROUND", (0, 0), (-1, -1), bg))
    t.setStyle(TableStyle(ts))
    return t


def _section_bar(title: str, *, color_bg=GOV_BLUE, color_fg=colors.white) -> Table:
    t = Table([[Paragraph(f"<b>{_safe_str(title)}</b>", _sty("Small", "Small"))]], colWidths=[None], hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), color_bg),
                ("TEXTCOLOR", (0, 0), (-1, -1), color_fg),
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _boxed_lines(
    title: Optional[str],
    lines: List[str],
    *,
    bg: Optional[Any] = None,
    style: str = "Small",
) -> Table:
    """
    ✅ Splittable Govt-like boxed content:
    - Each line becomes its own row => safe page splitting.
    """
    data: List[List[Any]] = []
    if title:
        data.append([_para(f"<b>{title}</b>", style)])
    for ln in lines:
        data.append([_para(ln if (ln or "").strip() else " ", style)])
    return _table(data, col_widths=[None], bg=bg)


def _kv_2x2(pairs: List[List[str]]) -> Table:
    """
    pairs: [[label,value], ...]
    renders rows as: L1 V1 | L2 V2
    """
    p = list(pairs)
    if len(p) % 2 == 1:
        p.append(["", ""])

    rows: List[List[Any]] = []
    sL = _sty("Small", "Small")
    sV = _sty("Small", "Small")

    for i in range(0, len(p), 2):
        l1, v1 = p[i]
        l2, v2 = p[i + 1]
        rows.append(
            [
                Paragraph(f"<b>{_safe_str(l1)}</b>", sL),
                Paragraph(_safe_str(v1) if _safe_str(v1).strip() else " ", sV),
                Paragraph(f"<b>{_safe_str(l2)}</b>", sL),
                Paragraph(_safe_str(v2) if _safe_str(v2).strip() else " ", sV),
            ]
        )

    return _table(rows, col_widths=[40 * mm, None, 40 * mm, None], bg=GOV_LT_BLUE2)


def _in_period(period_from: Optional[datetime], period_to: Optional[datetime]) -> Callable[[Any], Any]:
    def _f(col):
        conds = []
        if period_from:
            conds.append(col >= period_from)
        if period_to:
            conds.append(col <= period_to)
        return and_(*conds) if conds else True

    return _f


# =============================================================================
# Template resolve (Govt L3 layout)
# =============================================================================
def get_ipd_case_sheet_default_template() -> Dict[str, Any]:
    return {
        "name": "Case Sheet for Maternity Services - L3 Facility (Govt Layout)",
        "sections": [
            {"code": "gov_header", "label": "Govt Header", "enabled": True, "order": 5, "required": True},
            {"code": "admission_form", "label": "Admission Form", "enabled": True, "order": 10, "required": True},
            {"code": "presenting_history", "label": "Presenting Complaints & History", "enabled": True, "order": 20, "required": True},
            {"code": "safe_checklist_1", "label": "SAFE Checklist - Check 1", "enabled": True, "order": 30, "required": True},
            {"code": "obstetric_notes", "label": "Obstetric Notes", "enabled": True, "order": 40, "required": True},
            {"code": "partograph", "label": "Simplified Partograph (Blank Grid)", "enabled": True, "order": 50, "required": True},
            {"code": "consent", "label": "Consent for Procedures", "enabled": True, "order": 60, "required": True},
            {"code": "pre_anaesthetic", "label": "Pre-Anesthetic Check-up", "enabled": True, "order": 70, "required": True},
            {"code": "anaesthesia_notes", "label": "Anesthesia Notes", "enabled": True, "order": 80, "required": True},
            {"code": "safe_checklist_2", "label": "SAFE Checklist - Check 2", "enabled": True, "order": 90, "required": True},
            {"code": "procedure_notes", "label": "Operation/Procedure Notes", "enabled": True, "order": 100, "required": True},
            {"code": "delivery_notes", "label": "Delivery Notes + Baby Notes", "enabled": True, "order": 110, "required": True},
            {"code": "post_delivery_continuation", "label": "Post Delivery Continuation Sheets", "enabled": True, "order": 120, "required": True},
            {"code": "transfusion_notes", "label": "Blood Transfusion / Procedure Notes", "enabled": True, "order": 130, "required": True},
            {"code": "safe_checklist_3", "label": "SAFE Checklist - Check 3", "enabled": True, "order": 140, "required": True},
            {"code": "postpartum_assessment", "label": "Assessment of Postpartum Condition", "enabled": True, "order": 150, "required": True},
            {"code": "safe_checklist_4", "label": "SAFE Checklist - Check 4", "enabled": True, "order": 160, "required": True},
            {"code": "discharge_notes", "label": "Discharge Notes", "enabled": True, "order": 170, "required": True},
            {"code": "discharge_form", "label": "Discharge/Referral/LAMA/Death Form", "enabled": True, "order": 180, "required": True},
            {"code": "signatures", "label": "Signatures", "enabled": True, "order": 190, "required": True},
            {"code": "newborn_resuscitation", "label": "Newborn Resuscitation & Examination", "enabled": False, "order": 115, "required": False},
        ],
        "settings": {
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


# =============================================================================
# Data container
# =============================================================================
@dataclass
class IpdCaseSheetData:
    admission: IpdAdmission
    patient: Optional[Any]

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
    nursing_timeline: List[IpdNursingTimeline]

    discharge: Optional[IpdDischargeSummary]
    discharge_checklist: Optional[IpdDischargeChecklist]
    discharge_meds: List[IpdDischargeMedication]

    newborn_resus: Optional[Any]


def _load_ipd_case_sheet_data(
    db,
    admission_id: int,
    *,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
) -> IpdCaseSheetData:
    adm = (
        db.query(IpdAdmission)
        .options(
            joinedload(IpdAdmission.current_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
        )
        .filter(IpdAdmission.id == admission_id)
        .first()
    )
    if not adm:
        raise ValueError("Admission not found")

    # patient attach
    patient = None
    try:
        if hasattr(Patient, "addresses"):
            patient = (
                db.query(Patient)
                .options(joinedload(getattr(Patient, "addresses")))
                .filter(Patient.id == adm.patient_id)
                .first()
            )
        else:
            patient = db.query(Patient).filter(Patient.id == adm.patient_id).first()
    except Exception:
        patient = db.query(Patient).filter(Patient.id == adm.patient_id).first()

    try:
        setattr(adm, "patient", patient)
    except Exception:
        pass

    P = _in_period(period_from, period_to)

    vitals = (
        db.query(IpdVital)
        .filter(IpdVital.admission_id == admission_id, P(IpdVital.recorded_at))
        .order_by(IpdVital.recorded_at.asc())
        .all()
    )

    nursing_notes = (
        db.query(IpdNursingNote)
        .options(joinedload(IpdNursingNote.vitals))
        .filter(IpdNursingNote.admission_id == admission_id, P(IpdNursingNote.entry_time))
        .order_by(IpdNursingNote.entry_time.asc())
        .all()
    )

    io_rows = (
        db.query(IpdIntakeOutput)
        .filter(IpdIntakeOutput.admission_id == admission_id, P(IpdIntakeOutput.recorded_at))
        .order_by(IpdIntakeOutput.recorded_at.asc())
        .all()
    )

    transfers = (
        db.query(IpdTransfer)
        .filter(IpdTransfer.admission_id == admission_id, P(IpdTransfer.requested_at))
        .order_by(IpdTransfer.requested_at.asc())
        .all()
    )

    referrals = (
        db.query(IpdReferral)
        .filter(IpdReferral.admission_id == admission_id, P(IpdReferral.requested_at))
        .order_by(IpdReferral.requested_at.asc())
        .all()
    )

    med_orders = (
        db.query(IpdMedicationOrder)
        .filter(IpdMedicationOrder.admission_id == admission_id, P(IpdMedicationOrder.start_datetime))
        .order_by(IpdMedicationOrder.start_datetime.asc())
        .all()
    )

    mar_rows = (
        db.query(IpdMedicationAdministration)
        .filter(IpdMedicationAdministration.admission_id == admission_id, P(IpdMedicationAdministration.scheduled_datetime))
        .order_by(IpdMedicationAdministration.scheduled_datetime.asc())
        .all()
    )

    pain = (
        db.query(IpdPainAssessment)
        .filter(IpdPainAssessment.admission_id == admission_id, P(IpdPainAssessment.recorded_at))
        .order_by(IpdPainAssessment.recorded_at.asc())
        .all()
    )

    fall = (
        db.query(IpdFallRiskAssessment)
        .filter(IpdFallRiskAssessment.admission_id == admission_id, P(IpdFallRiskAssessment.recorded_at))
        .order_by(IpdFallRiskAssessment.recorded_at.asc())
        .all()
    )

    pressure = (
        db.query(IpdPressureUlcerAssessment)
        .filter(IpdPressureUlcerAssessment.admission_id == admission_id, P(IpdPressureUlcerAssessment.recorded_at))
        .order_by(IpdPressureUlcerAssessment.recorded_at.asc())
        .all()
    )

    nutrition = (
        db.query(IpdNutritionAssessment)
        .filter(IpdNutritionAssessment.admission_id == admission_id, P(IpdNutritionAssessment.recorded_at))
        .order_by(IpdNutritionAssessment.recorded_at.asc())
        .all()
    )

    dressings = (
        db.query(IpdDressingRecord)
        .filter(IpdDressingRecord.admission_id == admission_id, P(IpdDressingRecord.performed_at))
        .order_by(IpdDressingRecord.performed_at.asc())
        .all()
    )

    transfusions = (
        db.query(IpdBloodTransfusion)
        .filter(IpdBloodTransfusion.admission_id == admission_id, P(IpdBloodTransfusion.created_at))
        .order_by(IpdBloodTransfusion.created_at.asc())
        .all()
    )

    restraints = (
        db.query(IpdRestraintRecord)
        .filter(IpdRestraintRecord.admission_id == admission_id, P(IpdRestraintRecord.ordered_at))
        .order_by(IpdRestraintRecord.ordered_at.asc())
        .all()
    )

    isolations = (
        db.query(IpdIsolationPrecaution)
        .filter(IpdIsolationPrecaution.admission_id == admission_id, P(IpdIsolationPrecaution.ordered_at))
        .order_by(IpdIsolationPrecaution.ordered_at.asc())
        .all()
    )

    icu_flows = (
        db.query(IcuFlowSheet)
        .filter(IcuFlowSheet.admission_id == admission_id, P(IcuFlowSheet.recorded_at))
        .order_by(IcuFlowSheet.recorded_at.asc())
        .all()
    )

    nursing_timeline = (
        db.query(IpdNursingTimeline)
        .filter(IpdNursingTimeline.admission_id == admission_id, P(IpdNursingTimeline.event_at))
        .order_by(IpdNursingTimeline.event_at.asc())
        .all()
    )

    discharge = db.query(IpdDischargeSummary).filter(IpdDischargeSummary.admission_id == admission_id).first()
    discharge_checklist = db.query(IpdDischargeChecklist).filter(IpdDischargeChecklist.admission_id == admission_id).first()
    discharge_meds = (
        db.query(IpdDischargeMedication)
        .filter(IpdDischargeMedication.admission_id == admission_id)
        .order_by(IpdDischargeMedication.created_at.asc())
        .all()
    )

    newborn_resus = None
    if IpdNewbornResuscitation is not None:
        try:
            newborn_resus = db.query(IpdNewbornResuscitation).filter(
                IpdNewbornResuscitation.admission_id == admission_id
            ).first()
        except Exception:
            newborn_resus = None

    return IpdCaseSheetData(
        admission=adm,
        patient=patient,
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
        nursing_timeline=nursing_timeline,
        discharge=discharge,
        discharge_checklist=discharge_checklist,
        discharge_meds=discharge_meds,
        newborn_resus=newborn_resus,
    )


# =============================================================================
# GOVT HEADER (L3) – SAFE logo support (NO CRASH)
# =============================================================================
def _gov_header(db, d: IpdCaseSheetData) -> List[Any]:
    branding = None
    try:
        q = db.query(UiBranding).order_by(UiBranding.id.desc())
        if hasattr(UiBranding, "is_active"):
            q = q.filter(UiBranding.is_active == True)  # noqa: E712
        branding = q.first()
    except Exception:
        branding = None

    facility = _safe_str(_get(branding, "org_name", "facility_name", "name", default="Name of Facility"))
    district = _safe_str(_get(branding, "org_district", "district", default=""))
    block = _safe_str(_get(branding, "org_block", "block", default=""))
    phone = _safe_str(_get(branding, "org_phone", "phone", "contact_number", default=""))

    # ✅ SAFE logos (DB can store "branding/logo.png" => resolved under storage_dir)
    logo_left = _try_image(
        _safe_str(_get(branding, "logo_path", "org_logo_path", "header_logo_path", "logo_left_path", default="")),
        18,
        18,
        hAlign="LEFT",
    )
    logo_right = _try_image(
        _safe_str(_get(branding, "logo_right_path", "header_logo_right_path", default="")),
        18,
        18,
        hAlign="RIGHT",
    )

    title = "CASE SHEET FOR MATERNITY SERVICES - L3 FACILITY"
    motto = "lR;eso t;rs"  # (as in sample PDF)

    head1 = Table(
        [
            [
                logo_left,
                _h_center(title, "Normal"),
                logo_right,
            ]
        ],
        colWidths=[22 * mm, None, 22 * mm],
        hAlign="LEFT",
    )
    head1.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("BACKGROUND", (0, 0), (-1, -1), GOV_LT_BLUE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    head2 = Table(
        [
            [
                _para(f"<b>{facility}</b>", "Small"),
                _para(f"Block: {block}", "Small"),
                _para(motto, "Small"),
            ],
            [
                _para(f"District: {district}", "Small"),
                _para(f"Contact number (facility): {phone}", "Small"),
                _para(" ", "Small"),
            ],
        ],
        colWidths=[None, None, 35 * mm],
        hAlign="LEFT",
    )
    head2.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, GRID),
                ("BACKGROUND", (0, 0), (-1, -1), GOV_LT_BLUE2),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    return [head1, Spacer(1, 4), head2, Spacer(1, 6)]


# =============================================================================
# Section: Admission Form (Govt format)
# =============================================================================
def _sec_admission_form(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    patient = d.patient
    discharge = d.discharge

    name = _safe_str(_patient_full_name(patient))
    age = _calc_age_years(_get(patient, "dob", default=None))
    phone = _safe_str(_get(patient, "phone", "mobile", default=""))
    address = _safe_str(_patient_address(patient))
    marital = _safe_str(_get(patient, "marital_status", default=""))
    relation = _safe_str(_get(patient, "relation_name", "spouse_name", "husband_name", "father_name", default=""))

    mcts = _safe_str(_get(adm, "mcts_no", "mcts_number", default=""))
    aadhar = _safe_str(_get(patient, "aadhar_no", "aadhaar_no", "aadhaar", default=_get(adm, "aadhar_no", default="")))
    ipd_reg = _safe_str(_get(adm, "display_code", "registration_no", default=f"IP-{adm.id:06d}"))

    booked = _get(adm, "booked", default=None)
    bpl = _get(adm, "bpl_jsy_registered", "bpl_jsy", default=None)

    referred_from = _safe_str(_get(adm, "referred_from", "referred_by", default=""))
    referral_reason = _safe_str(_get(adm, "referral_reason", "referred_reason", default=""))

    admitted_at = _get(adm, "admitted_at", default=None)
    admitted_dt = fmt_ist(admitted_at) if admitted_at else ""
    asha_name = _safe_str(_get(adm, "asha_name", default=""))

    companion = _safe_str(_get(adm, "birth_companion_name", "companion_name", default=""))

    admission_cat = _safe_str(_get(adm, "admission_category", default=""))
    cat_opts = [
        "presented with labor pain",
        "presented with complications of pregnancy",
        "referred in from other facility",
    ]
    cat_box = _opt_box(cat_opts, admission_cat if admission_cat in cat_opts else None)

    lmp = _safe_str(_get(adm, "lmp", default=""))
    edd = _safe_str(_get(adm, "edd", default=""))

    prov_dx = _safe_str(_get(adm, "preliminary_diagnosis", "provisional_diagnosis", default=""))
    final_dx = _safe_str(_get(discharge, "final_diagnosis_primary", default=_get(adm, "final_diagnosis", default="")))
    contraception = _safe_str(_get(adm, "contraception_history", default=""))

    top = _kv_2x2(
        [
            ["MCTS No.", mcts],
            ["Booked", _yn(booked if isinstance(booked, bool) else None)],
            ["IPD/Registration No.", ipd_reg],
            ["BPL/JSY Registration", _yn(bpl if isinstance(bpl, bool) else None)],
            ["Aadhar Card No.", aadhar],
            ["Referred from & Reason", (referred_from + (" | " + referral_reason if referral_reason else "")).strip(" |")],
        ]
    )

    mid = _table(
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
                _para(f"<b>Admission date & time:</b> {admitted_dt}"),
                _para(f"<b>Name of birth companion:</b> {companion}"),
                _para(f"<b>Name of ASHA:</b> {asha_name}"),
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
        bg=GOV_LT_BLUE2,
    )

    delivery = _table(
        [
            [
                _para("<b>Delivery outcome:</b> Live / Abortion / Still Birth (Fresh/Macerated) / Preterm (Yes/No)"),
                _para("<b>Sex of Baby:</b> Male / Female"),
                _para("<b>Birth weight (in kgs):</b>"),
            ],
            [
                _para(_safe_str(_get(adm, "delivery_outcome", default="")) or " "),
                _para(_safe_str(_get(adm, "baby_sex", default="")) or " "),
                _para(_safe_str(_get(adm, "baby_birth_weight_kg", "baby_weight_kg", default="")) or " "),
            ],
            [
                _para("<b>Delivery date:</b>"),
                _para("<b>Time:</b>"),
                _para("<b>Mode of Delivery/Procedure:</b> Normal / Assisted / CS / Other"),
            ],
            [
                _para(_safe_str(_get(adm, "delivery_date", default="")) or " "),
                _para(_safe_str(_get(adm, "delivery_time", default="")) or " "),
                _para(_safe_str(_get(adm, "mode_of_delivery", default="")) or " "),
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
                _para(_safe_str(_get(adm, "final_outcome", default="")) or " "),
                _para(" "),
                _para(" "),
            ],
        ],
        col_widths=[None, 52 * mm, 60 * mm],
        bg=GOV_LT_BLUE2,
    )

    sign = _table(
        [
            [
                _para("<b>Name and signature of service provider:</b> ________________________________"),
                _para("<b>Designation:</b> ____________________"),
                _para("<b>Date & Time:</b> ____________________"),
            ],
        ],
        col_widths=[None, 60 * mm, 55 * mm],
        bg=GOV_LT_BLUE2,
    )

    return [
        _section_bar("Admission Form", color_bg=GOV_BLUE),
        Spacer(1, 4),
        top,
        Spacer(1, 6),
        mid,
        Spacer(1, 6),
        delivery,
        Spacer(1, 6),
        sign,
        PageBreak(),
    ]


# =============================================================================
# Section: Presenting complaints & history (Govt format)
# =============================================================================
def _sec_presenting_history(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission

    presenting = _safe_str(_get(adm, "presenting_complaints", "chief_complaint", default=""))
    medical = _safe_str(_get(adm, "medical_history", default=""))
    surgical = _safe_str(_get(adm, "surgical_history", default=""))
    family = _safe_str(_get(adm, "family_history", default=""))

    gravida = _safe_str(_get(adm, "gravida", default=""))
    parity = _safe_str(_get(adm, "parity", default=""))
    abortion = _safe_str(_get(adm, "abortion", default=""))
    living = _safe_str(_get(adm, "living_children", default=""))
    onset = _safe_str(_get(adm, "labour_onset_datetime", "onset_of_labor", default=""))

    lmp = _safe_str(_get(adm, "lmp", default=""))
    edd = _safe_str(_get(adm, "edd", default=""))
    fundal = _safe_str(_get(adm, "fundal_height_wks", default=""))
    usg_age = _safe_str(_get(adm, "usg_gestation_age", default=""))

    preterm = _get(adm, "preterm", default=None)
    steroid = _get(adm, "antenatal_corticosteroid_given", default=None)

    cerv_dil = _safe_str(_get(adm, "cervical_dilatation_cm", default=""))
    cerv_eff = _safe_str(_get(adm, "cervical_effacement_pct", default=""))
    pv_count = _safe_str(_get(adm, "pv_exams_count", default=""))
    membranes = _safe_str(_get(adm, "membranes", default=""))
    amniotic = _safe_str(_get(adm, "amniotic_fluid_color", default=""))
    pelvis = _safe_str(_get(adm, "pelvis_adequate", default=""))

    pulse = _safe_str(_get(adm, "pulse", default=""))
    rr = _safe_str(_get(adm, "resp_rate", "respiratory_rate", default=""))
    fhr = _safe_str(_get(adm, "fhr", default=""))
    bp = _safe_str(_get(adm, "bp", default=""))
    temp = _safe_str(_get(adm, "temperature", "temp", default=""))

    bg = _safe_str(_get(adm, "blood_group", default=""))
    hb = _safe_str(_get(adm, "hb", default=""))
    sugar = _safe_str(_get(adm, "blood_sugar", default=""))
    urine_prot = _safe_str(_get(adm, "urine_protein", default=""))
    urine_sugar = _safe_str(_get(adm, "urine_sugar", default=""))
    hiv = _safe_str(_get(adm, "hiv", default=""))
    hbsag = _safe_str(_get(adm, "hbsag", default=""))
    syphilis = _safe_str(_get(adm, "syphilis", default=""))
    malaria = _safe_str(_get(adm, "malaria", default=""))

    story: List[Any] = []
    story.append(_section_bar("Presenting Complaints & History", color_bg=GOV_BLUE))
    story.append(Spacer(1, 4))

    story.append(
        _table(
            [[_para("<b>Presenting complaints:</b>", "Small"), _para(presenting or " ", "Small")]],
            col_widths=[60 * mm, None],
            bg=GOV_LT_BLUE2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Past Obstetrics History:",
            [
                "APH: ________   PPH: ________   PE/E: ________   C-section: ________",
                "Obstructed labor: ________   Still births: ________   Congenital anomaly: ________",
                "Anemia: ________   Others (Specify): ________________________________________________",
                "____________________________________________________________________________________",
            ],
            bg=GOV_LT_BLUE2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _table(
            [
                [_para("<b>Medical/ Surgical History (Please specify):</b>"), _para((medical + ("\n" + surgical if surgical else "")).strip() or " ")],
                [_para("<b>Family H/o chronic illness (Please specify):</b>"), _para(family or " ")],
            ],
            col_widths=[70 * mm, None],
            bg=GOV_LT_BLUE2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _table(
            [
                [
                    _para(f"<b>Date and time of onset of labor:</b> {onset}"),
                    _para(f"<b>Gravida:</b> {gravida}   <b>Parity:</b> {parity}   <b>Abortion:</b> {abortion}   <b>Living:</b> {living}"),
                ],
                [
                    _para("<b>PV Examination</b> Cervical dilatation: ______  Cervical effacement: ______  No. of PV Examinations: ______"),
                    _para(f"Cervical dilatation: {cerv_dil or ' '}   Cervical effacement: {cerv_eff or ' '}   No. PV Exams: {pv_count or ' '}"),
                ],
                [
                    _para("Membranes: Ruptured / Intact     Colour of amniotic fluid: Clear / Meconium / Blood     Pelvis adequate: Yes / No"),
                    _para(f"Membranes: {membranes or ' '}   AF colour: {amniotic or ' '}   Pelvis: {pelvis or ' '}"),
                ],
                [
                    _para(f"<b>Gestational Age</b>  LMP: {lmp}   EDD: {edd}   Fundal height (wks): {fundal}   Age from USG: {usg_age}"),
                    _para(f"Pre-term: {_yn(preterm if isinstance(preterm, bool) else None)}    Antenatal corticosteroid given: {_yn(steroid if isinstance(steroid, bool) else None)}"),
                ],
            ],
            col_widths=[None, None],
            bg=GOV_LT_BLUE2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _table(
            [
                [
                    _para("<b>Vitals</b> Pulse: ___/min   Respiratory rate: ___/min   FHR: ___/min   BP: ___ mmHg   Temperature: ___ C/F"),
                    _para(f"Pulse: {pulse}   RR: {rr}   FHR: {fhr}   BP: {bp}   Temp: {temp}"),
                ]
            ],
            col_widths=[None, None],
            bg=GOV_LT_BLUE2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _table(
            [
                [
                    _para("<b>Investigations</b> Blood Group & Rh: ____   Hb: ____   Blood Sugar: ____   Urine Protein: ____   Urine Sugar: ____"),
                    _para(f"BG/Rh: {bg}   Hb: {hb}   Sugar: {sugar}   Ur Prot: {urine_prot}   Ur Sugar: {urine_sugar}"),
                ],
                [
                    _para("HIV: ____   HBsAg: ____   Syphilis: ____   Malaria: ____   Others: _________________________________"),
                    _para(f"HIV: {hiv}   HBsAg: {hbsag}   Syphilis: {syphilis}   Malaria: {malaria}"),
                ],
            ],
            col_widths=[None, None],
            bg=GOV_LT_BLUE2,
        )
    )

    story.append(PageBreak())
    return story


# =============================================================================
# SAFE Childbirth Checklist – CHECK 1 (Govt format, image-free)
# =============================================================================
def _sec_safe_checklist_1(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_bar("Before Birth – SAFE CHILDBIRTH CHECKLIST (CHECK-1)", color_bg=GOV_BLUE_DARK))
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
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
            bg=SAFE_PINK2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Counsel Mother and Birth Companion to call for help if there is:",
            [
                "• Bleeding",
                "• Severe abdominal pain",
                "• Difficulty in breathing",
                "• Severe headache or blurring vision",
                "• Urge to push",
                "• Can’t empty bladder every 2 hours",
            ],
            bg=SAFE_PINK2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Confirm Supplies / Hygiene",
            [
                "Are soap, water and gloves available?   Yes ( )   No ( )",
                "I will wash hands and wear gloves for each vaginal exam ( )",
                "If not available, supplies arranged ( )",
                "Mother/companion will call for help during labour if needed ( )",
            ],
            bg=SAFE_PINK2,
        )
    )
    story.append(PageBreak())

    story.append(_section_bar("SAFE Checklist – Clinical Actions (Continuation)", color_bg=GOV_BLUE_DARK))
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Give antibiotics to Mother if (tick):",
            [
                "Temperature ≥ 38°C (100.5°F) ( )",
                "Foul-smelling vaginal discharge ( )",
                "Rupture of membranes >12 hrs without labour OR >18 hrs with labour ( )",
                "Labour >24 hrs / obstructed labour ( )",
                "Rupture of membranes <37 wks gestation ( )",
            ],
            bg=SAFE_PINK2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Give first dose of inj. magnesium sulfate and refer OR full dose if at FRU if:",
            [
                "Systolic ≥160 or Diastolic ≥110 with ≥+3 proteinuria OR BP ≥140/90 with trace to +2 proteinuria along with:",
                "Severe headache ( )  Pain in upper abdomen ( )  Convulsions ( )  Blurring of vision ( )  Difficulty breathing ( )",
            ],
            bg=SAFE_PINK2,
        )
    )
    story.append(Spacer(1, 6))

    story.append(
        _boxed_lines(
            "Give corticosteroids (24–34 weeks) if:",
            [
                "True pre-term labour ( )",
                "Imminent delivery (APH, PPROM, Severe PE/E) ( )",
                "Dose: Inj. Dexamethasone 6 mg IM 12 hourly – total 4 doses",
            ],
            bg=SAFE_PINK2,
        )
    )
    story.append(Spacer(1, 8))

    story.append(
        _boxed_lines(
            "Provider Details",
            [
                "Name of Provider: ____________________________",
                "Date: ____/____/____     Signature: ____________________________",
                "NO OXYTOCIN/other uterotonics for unnecessary induction/augmentation of labour",
            ],
            bg=SAFE_PINK2,
        )
    )

    story.append(PageBreak())
    return story


# =============================================================================
# Obstetric notes
# =============================================================================
def _sec_obstetric_notes(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    augmentation = _get(adm, "augmentation_performed", default=None)
    indication = _safe_str(_get(adm, "augmentation_indication", default=""))

    body = _table(
        [
            [_para("<b>OBSTETRIC NOTES (INTERVENTIONS BEFORE DELIVERY)</b>", "Small")],
            [_para(f"Augmentation performed: {_yn(augmentation if isinstance(augmentation, bool) else None)}", "Small")],
            [_para("If yes, specify indication for augmentation:", "Small")],
            [_para(indication or "__________________________________________________________________________________", "Small")],
            [_para("<b>AUGMENT ONLY IF INDICATED AND IN CENTERS WITH FACILITY FOR C-SECTION</b>", "Small")],
        ],
        col_widths=[None],
        bg=GOV_LT_BLUE2,
    )

    return [
        _section_bar("Obstetric Notes", color_bg=GOV_BLUE),
        Spacer(1, 4),
        body,
        PageBreak(),
    ]


# =============================================================================
# Partograph (Govt: blank grid – safe, printable)
# =============================================================================
def _sec_partograph(_: IpdCaseSheetData) -> List[Any]:
    s = _sty("Small", "Small")

    ident = _table(
        [
            [
                _para(
                    "<b>THE SIMPLIFIED PARTOGRAPH</b><br/>"
                    "Start plotting partograph when woman is in active labor i.e., Cx ≥ 4 cms",
                    "Small",
                )
            ],
            [
                _para(
                    "Identification Data:  Name: ____________________   W/o: ____________________   Age: _____   Reg. No.: ____________________",
                    "Small",
                )
            ],
            [
                _para(
                    "Date & Time of Admission: ____________________   Date & Time of ROM: ____________________",
                    "Small",
                )
            ],
        ],
        col_widths=[None],
        bg=SAFE_YELLOW,
    )

    # label col + 12 time cols (fits A4 with margins)
    label_w = 35 * mm
    time_w = 12 * mm

    header = [""] + [str(i) for i in range(1, 13)]
    grid: List[List[Any]] = [header]

    grid.append(["Foetal heart rate"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)
    grid.append(["Amniotic fluid"] + [""] * 12)
    grid.append([""] + [""] * 12)

    grid.append(["Cervix (cm) (Plot X)"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)

    grid.append(["Contractions per 10 min"] + [""] * 12)
    for _ in range(4):
        grid.append([""] + [""] * 12)

    grid.append(["Drugs and IV fluid given"] + [""] * 12)
    grid.append([""] + [""] * 12)

    grid.append(["Pulse and BP"] + [""] * 12)
    for _ in range(6):
        grid.append([""] + [""] * 12)
    grid.append(["Temp (°C)"] + [""] * 12)

    t = Table(grid, colWidths=[label_w] + [time_w] * 12, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 7),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, GRID),
                ("BACKGROUND", (0, 0), (-1, 0), GOV_LT_BLUE),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    footer = _table(
        [[Paragraph("Initiate plotting on alert line     |     Refer to FRU when ALERT LINE is crossed", s)]],
        col_widths=[None],
        bg=SAFE_YELLOW,
    )

    return [
        _section_bar("Partograph (Blank Grid)", color_bg=GOV_BLUE),
        Spacer(1, 4),
        ident,
        Spacer(1, 6),
        t,
        Spacer(1, 6),
        footer,
        PageBreak(),
    ]


# =============================================================================
# Consent + Pre-anesthetic + Anesthesia Notes (Govt wording)
# =============================================================================
def _sec_consent(_: IpdCaseSheetData) -> List[Any]:
    consent1 = _table(
        [[
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
        ]],
        col_widths=[None],
        bg=GOV_LT_BLUE2,
    )

    consent2 = _table(
        [[
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
        ]],
        col_widths=[None],
        bg=GOV_LT_BLUE2,
    )

    return [
        _section_bar("Consent Forms", color_bg=GOV_BLUE),
        Spacer(1, 4),
        consent1,
        Spacer(1, 6),
        consent2,
        PageBreak(),
    ]


def _sec_pre_anaesthetic(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_bar("Notes on Pre-Anesthetic Check-up", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _table(
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
            bg=GOV_LT_BLUE2,
        ),
        PageBreak(),
    ]


def _sec_anaesthesia_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_bar("Anesthesia Notes", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _table(
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
            bg=GOV_LT_BLUE2,
        ),
        PageBreak(),
    ]


# =============================================================================
# SAFE Checklist 2/3/4
# =============================================================================
def _sec_safe_checklist_2(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_bar("Just Before and During Birth – SAFE CHILDBIRTH CHECKLIST (CHECK-2)", color_bg=GOV_BLUE_DARK))
    story.append(Spacer(1, 6))
    story.append(
        _boxed_lines(
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
            bg=SAFE_PINK2,
        )
    )
    story.append(PageBreak())
    return story


def _sec_safe_checklist_3(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_bar("Soon After Birth – SAFE CHILDBIRTH CHECKLIST (CHECK-3)", color_bg=GOV_BLUE_DARK))
    story.append(Spacer(1, 6))
    story.append(
        _boxed_lines(
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
            bg=SAFE_GREEN,
        )
    )
    story.append(PageBreak())
    return story


def _sec_safe_checklist_4(_: IpdCaseSheetData) -> List[Any]:
    story: List[Any] = []
    story.append(_section_bar("Before Discharge – SAFE CHILDBIRTH CHECKLIST (CHECK-4)", color_bg=GOV_BLUE_DARK))
    story.append(Spacer(1, 6))
    story.append(
        _boxed_lines(
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
            bg=SAFE_GREEN,
        )
    )
    story.append(PageBreak())
    return story


# =============================================================================
# Procedure / Delivery / Continuation / Transfusion notes
# =============================================================================
def _sec_procedure_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_bar("Operation / Procedure Notes (If applicable)", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _table(
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
            bg=SAFE_PINK2,
        ),
        PageBreak(),
    ]


def _sec_delivery_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_bar("Delivery Notes", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _table(
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
            bg=SAFE_PINK2,
        ),
        PageBreak(),
    ]


def _sec_post_delivery_continuation(_: IpdCaseSheetData) -> List[Any]:
    sheet = _table(
        [
            [_para("<b>Continuation Sheet for Post Delivery Notes</b>", "Small")],
            [_para("Notes:", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
        ],
        col_widths=[None],
        bg=SAFE_PINK2,
    )
    return [
        _section_bar("Post Delivery Notes (Continuation)", color_bg=GOV_BLUE),
        Spacer(1, 4),
        sheet,
        PageBreak(),
        _section_bar("Post Delivery Notes (Continuation)", color_bg=GOV_BLUE),
        Spacer(1, 4),
        sheet,
        PageBreak(),
    ]


def _sec_transfusion_notes(_: IpdCaseSheetData) -> List[Any]:
    return [
        _section_bar("Blood Transfusion or Other Procedure Notes", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _table(
            [
                [_para("Details / Notes:", "Small")],
                [_para("__________________________________________________________________________________", "Small")],
                [_para("__________________________________________________________________________________", "Small")],
                [_para("__________________________________________________________________________________", "Small")],
                [_para("__________________________________________________________________________________", "Small")],
                [_para("Signature: ____________________   Date: ____/____/____   Time: __________", "Small")],
            ],
            col_widths=[None],
            bg=SAFE_PINK2,
        ),
        PageBreak(),
    ]


# =============================================================================
# Postpartum Assessment (Mother + Baby grid)
# =============================================================================
def _sec_postpartum_assessment(_: IpdCaseSheetData) -> List[Any]:
    cols = ["", ""] + ["30 min"] * 4 + ["6 hrs"] * 3 + ["Day 2\nMorning", "Day 2\nEvening"]

    def row(section: str, param: str) -> List[Any]:
        return [section, param] + [""] * (len(cols) - 2)

    data: List[List[Any]] = []
    data.append(cols)

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

    # widths tuned to fit A4/Letter margins reliably
    w_section = 17 * mm
    w_param = 52 * mm
    w_time = 12 * mm

    t = Table(data, colWidths=[w_section, w_param] + [w_time] * (len(cols) - 2), hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 7),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, GRID),
                ("BACKGROUND", (0, 0), (-1, 0), GOV_LT_BLUE),
                ("SPAN", (0, 1), (0, 7)),
                ("SPAN", (0, 8), (0, 15)),
                ("VALIGN", (0, 1), (0, 15), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("BACKGROUND", (0, 1), (-1, -1), SAFE_GREEN),
            ]
        )
    )

    top_notes = _table(
        [
            [_para("Notes for Mother:", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("Notes for Baby:", "Small")],
            [_para("__________________________________________________________________________________", "Small")],
            [_para("Clinical diagnosis (tick): Normal ( ) Infection ( ) Jaundice ( ) Hypothermia ( ) Convulsions ( ) Death ( ) Others: __________", "Small")],
            [_para("Date & time of transfer to PNC ward: ____/____/____  Time: __________   Condition at transfer: ____________________________", "Small")],
            [_para("If referred, reason for referral of mother/baby: _________________________________________________", "Small")],
        ],
        col_widths=[None],
        bg=SAFE_GREEN,
    )

    return [
        _section_bar("Assessment of Postpartum Condition", color_bg=GOV_BLUE),
        Spacer(1, 4),
        top_notes,
        Spacer(1, 6),
        t,
        PageBreak(),
    ]


# =============================================================================
# Discharge Notes + Discharge Form + Signatures
# =============================================================================
def _sec_discharge_notes(d: IpdCaseSheetData) -> List[Any]:
    ds = d.discharge
    cond = _safe_str(_get(ds, "discharge_condition", default=""))
    advise = _safe_str(_get(ds, "follow_up", "advice", default=""))
    other = _safe_str(_get(ds, "notes", default=""))

    t = _table(
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
        bg=SAFE_GREEN,
    )

    return [
        _section_bar("Discharge Notes", color_bg=GOV_BLUE),
        Spacer(1, 4),
        t,
        PageBreak(),
    ]


def _sec_discharge_form(d: IpdCaseSheetData) -> List[Any]:
    adm = d.admission
    patient = d.patient

    name = _safe_str(_patient_full_name(patient))
    age = _calc_age_years(_get(patient, "dob", default=None))
    mcts = _safe_str(_get(adm, "mcts_no", "mcts_number", default=""))
    ipd_reg = _safe_str(_get(adm, "display_code", "registration_no", default=f"IP-{adm.id:06d}"))

    t = _table(
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
        bg=SAFE_GREEN,
    )

    return [
        _section_bar("Notes on Discharge / Referral / Death", color_bg=GOV_BLUE),
        Spacer(1, 4),
        t,
        PageBreak(),
    ]


def _sec_signatures(d: IpdCaseSheetData) -> List[Any]:
    ds = d.discharge
    prepared = _safe_str(_get(ds, "prepared_by_name", default=""))
    reviewed = _safe_str(_get(ds, "reviewed_by_name", default=""))
    regno = _safe_str(_get(ds, "reviewed_by_regno", default=""))

    t = _table(
        [
            [_para("<b>Signatures</b>", "Small")],
            [_para(f"Prepared By: {prepared or '__________________________'}", "Small")],
            [_para(f"Reviewed By (Doctor): {reviewed or '__________________________'}   Reg No: {regno or '__________'}", "Small")],
            [_para("Patient/Attendant Acknowledgement: __________________________", "Small")],
            [_para("Date & Time: ____________________", "Small")],
        ],
        col_widths=[None],
        bg=GOV_LT_BLUE2,
    )

    return [
        _section_bar("Signatures & Acknowledgement", color_bg=GOV_BLUE),
        Spacer(1, 4),
        t,
    ]


# =============================================================================
# Optional: Newborn resuscitation & examination (text-only Govt-friendly)
# =============================================================================
def _sec_newborn_resuscitation(d: IpdCaseSheetData) -> List[Any]:
    nr = d.newborn_resus
    lines = [
        "Breathing/cry at birth: Yes ( ) No ( )",
        "Resuscitation required: Yes ( ) No ( )   If yes: Suction ( )  Bag&Mask ( )  O2 ( )  Other ( )",
        "APGAR 1 min: ____   5 min: ____   10 min: ____",
        "Birth defects screening (tick if present): Cleft lip/palate ( )  Club foot ( )  Spina bifida ( )  Imperforate anus ( )  Others ( )",
        "Examination notes: ________________________________________________________________",
        "____________________________________________________________________________________",
        "Provider name/signature: ____________________   Date: ____/____/____   Time: ________",
    ]
    if nr:
        pass

    return [
        _section_bar("Newborn Resuscitation & Examination", color_bg=GOV_BLUE),
        Spacer(1, 4),
        _boxed_lines(None, lines, bg=SAFE_YELLOW),
        PageBreak(),
    ]


# =============================================================================
# Build PDF (entry)
# =============================================================================
def build_ipd_case_sheet_pdf(
    *,
    db,
    admission_id: int,
    template_id: Optional[int] = None,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    user: Any = None,  # route param safe
    **_kwargs: Any,
) -> tuple[bytes, str]:
    tpl = _resolve_template(db, template_id)
    sections = sorted(tpl.get("sections") or [], key=lambda x: int(x.get("order", 9999)))
    enabled_codes = {s.get("code") for s in sections if s.get("enabled") or s.get("required")}

    data = _load_ipd_case_sheet_data(db, admission_id, period_from=period_from, period_to=period_to)

    subtitle = ""
    if period_from or period_to:
        subtitle = f"Report Period: {_safe_str(period_from.date() if period_from else '')} to {_safe_str(period_to.date() if period_to else '')}"

    story: List[Any] = []

    # Govt header
    if "gov_header" in enabled_codes:
        story.extend(_gov_header(db, data))

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
        elif code == "newborn_resuscitation":
            story.extend(_sec_newborn_resuscitation(data))
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

    adm = data.admission
    filename = f"CaseSheet_L3_{_safe_str(getattr(adm, 'display_code', admission_id))}.pdf"
    return pdf, filename
