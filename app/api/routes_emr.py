# FILE: app/api/routes_emr.py
from __future__ import annotations

import base64
import importlib
import inspect as pyinspect
import mimetypes
from datetime import datetime, date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from sqlalchemy import or_
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import current_user, get_db
from app.core.config import settings
from app.models.billing import Invoice, InvoiceItem, Payment
from app.models.common import FileAttachment
from app.models.department import Department
from app.models.ipd import (
    IcuFlowSheet,
    IpdAdmission,
    IpdAdmissionFeedback,
    IpdAssessment,
    IpdBed,
    IpdBedAssignment,
    IpdBloodTransfusion,
    IpdDischargeChecklist,
    IpdDischargeMedication,
    IpdDischargeSummary,
    IpdDressingRecord,
    IpdDressingTransfusion,
    IpdDrugChartDoctorAuth,
    IpdDrugChartMeta,
    IpdDrugChartNurseRow,
    IpdFallRiskAssessment,
    IpdFeedback,
    IpdIntakeOutput,
    IpdIsolationPrecaution,
    IpdIvFluidOrder,
    IpdMedication,
    IpdMedicationAdministration,
    IpdMedicationOrder,
    IpdNursingNote,
    IpdOrder,
    IpdOtCase,
    IpdPainAssessment,
    IpdPressureUlcerAssessment,
    IpdProgressNote,
    IpdReferral,
    IpdRestraintRecord,
    IpdRound,
    IpdShiftHandover,
    IpdTransfer,
    IpdVital,
    IpdNutritionAssessment,
)
from app.models.lis import LisAttachment, LisOrder, LisOrderItem, LisResultLine
from app.models.opd import (
    Appointment,
    FollowUp,
    LabOrder,
    Prescription as OpdRx,
    PrescriptionItem as OpdRxItem,
    RadiologyOrder,
    Visit,
    Vitals as OpdVitals,
)
from app.models.ot import (
    AnaesthesiaDrugLog,
    AnaesthesiaRecord,
    AnaesthesiaVitalLog,
    OperationNote,
    OtBloodTransfusionRecord,
    OtCase,
    OtCleaningLog,
    OtEnvironmentLog,
    OtEquipmentDailyChecklist,
    OtImplantRecord,
    OtNursingRecord,
    OtProcedure,
    OtSchedule,
    OtScheduleProcedure,
    OtSpongeInstrumentCount,
    PacuRecord,
    PreAnaesthesiaEvaluation,
    PreOpChecklist,
    SurgicalSafetyChecklist,
)
from app.models.patient import Patient, PatientAddress, PatientConsent, PatientDocument
from app.models.ris import RisAttachment, RisOrder
from app.models.ui_branding import UiBranding
from app.models.user import User
from app.schemas.emr import (
    AttachmentOut,
    EmrExportRequest,
    FhirBundleOut,
    PatientLookupOut,
    PatientMiniOut,
    TimelineFilterIn,
    TimelineItemOut,
    TimelineType,
)
from app.services.ui_branding import get_ui_branding  # must return UiBranding or dict-like
# NOTE: we call PDF generator via a safe wrapper (signature-adaptive)
# from app.services.pdf_emr import generate_emr_pdf

HAS_PHARMACY = False
try:
    from app.models.pharmacy_prescription import (  # type: ignore
        PharmacyPrescription, PharmacyPrescriptionLine, PharmacySale,
        PharmacySaleItem,
    )

    HAS_PHARMACY = True
except Exception:
    HAS_PHARMACY = False

HAS_OT_ORDERS = False
try:
    # optional module (your OtOrder)
    from app.models.ot import OtAttachment, OtOrder  # type: ignore

    HAS_OT_ORDERS = True
except Exception:
    HAS_OT_ORDERS = False

router = APIRouter()


# ---------------- RBAC ----------------
def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


# ---------------- small utils ----------------
def _is_truthy(x: Optional[str]) -> bool:
    return str(x or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return datetime.utcnow()


def _as_float(x):
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    try:
        return float(x)
    except Exception:
        return None


def _bmi(height_cm, weight_kg):
    h = _as_float(height_cm)
    w = _as_float(weight_kg)
    if not h or not w:
        return None
    try:
        m = h / 100.0
        if m <= 0:
            return None
        return round(w / (m * m), 1)
    except Exception:
        return None


def _date_window(
        df: Optional[str],
        dt: Optional[str]) -> tuple[Optional[datetime], Optional[datetime]]:
    dfrom = dto = None
    if df:
        dfrom = datetime.fromisoformat(df + "T00:00:00")
    if dt:
        dto = datetime.fromisoformat(dt + "T23:59:59")
    return dfrom, dto


def _in_window(ts: datetime, dfrom: Optional[datetime],
               dto: Optional[datetime]) -> bool:
    if dfrom and ts < dfrom:
        return False
    if dto and ts > dto:
        return False
    return True


def _patient_by_uhid(db: Session, uhid: str) -> Optional[Patient]:
    if not uhid:
        return None
    return db.query(Patient).filter(Patient.uhid == uhid).first()


def _patient_brief(p: Patient) -> dict:
    name = " ".join([x for x in [p.first_name, p.last_name] if x
                     ]).strip() or p.first_name or ""
    return {
        "id": p.id,
        "uhid": p.uhid,
        "abha_number": p.abha_number,
        "name": name,
        "gender": p.gender,
        "dob": p.dob,
        "phone": p.phone,
        "email": p.email,
    }


# --- "DON'T MISS ANY FIELD" serializer (all table columns) ---
def _row(obj) -> Optional[dict]:
    if obj is None:
        return None
    insp = sa_inspect(obj)
    data: Dict[str, Any] = {}
    for attr in insp.mapper.column_attrs:
        data[attr.key] = getattr(obj, attr.key)
    return jsonable_encoder(data)


def _rows(objs) -> List[dict]:
    return [(_row(x) or {}) for x in (objs or [])]


# ---------------- timeline titles/status ----------------
_UI_STATUS = {
    "draft": "new",
    "pending": "new",
    "booked": "new",
    "signed": "in_progress",
    "sent": "in_progress",
    "ordered": "in_progress",
    "in_progress": "in_progress",
    "partially_dispensed": "in_progress",
    "fully_dispensed": "dispensed",
    "dispensed": "dispensed",
    "approved": "dispensed",
    "completed": "completed",
    "finalized": "completed",
    "discharged": "completed",
    "cancelled": "cancelled",
}


def _map_ui_status(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _UI_STATUS.get(raw, raw)


def _title_for(t: str) -> str:
    return {
        "opd_appointment": "OPD Appointment",
        "opd_visit": "OPD Visit",
        "opd_vitals": "OPD Vitals",
        "rx": "OPD Prescription",
        "opd_lab_order": "OPD Lab Order",
        "opd_radiology_order": "OPD Radiology Order",
        "followup": "Follow-up",
        "lab": "LIS Lab Result",
        "radiology": "RIS Radiology",
        "pharmacy_rx": "Pharmacy Prescription",
        "pharmacy": "Pharmacy Dispense",
        "ipd_admission": "IPD Admission",
        "ipd_transfer": "IPD Transfer",
        "ipd_discharge": "IPD Discharge Summary",
        "ipd_discharge_checklist": "IPD Discharge Checklist",
        "ipd_discharge_med": "IPD Discharge Medication",
        "ipd_vitals": "IPD Vitals",
        "ipd_nursing_note": "IPD Nursing Note",
        "ipd_shift_handover": "IPD Shift Handover",
        "ipd_intake_output": "IPD Intake / Output",
        "ipd_round": "IPD Round",
        "ipd_progress": "IPD Progress Note",
        "ipd_risk": "IPD Risk / Assessment",
        "ipd_orders": "IPD Orders",
        "ipd_med_order": "IPD Medication Order",
        "ipd_mar": "IPD Medication Administration (MAR)",
        "ipd_drug_chart_meta": "IPD Drug Chart Meta",
        "ipd_iv_fluid": "IPD IV Fluid",
        "ipd_dressing": "IPD Dressing",
        "ipd_blood": "IPD Blood Transfusion",
        "ipd_restraint": "IPD Restraint",
        "ipd_isolation": "IPD Isolation",
        "ipd_icu_flow": "ICU Flow Sheet",
        "ipd_feedback": "IPD Feedback",
        "ipd_referral": "IPD Referral",
        "ipd_ot_case": "IPD OT Case",
        "ot": "OT Case (Theatre)",
        "billing": "Invoice",
        "attachment": "Attachment",
        "consent": "Consent",
    }.get(t, "Event")


def _want(typ: str, allow: Optional[Set[str]]) -> bool:
    return (allow is None) or (typ in allow)


# ---------------- OPD enrich helpers ----------------
def _user_display_name(u) -> Optional[str]:
    if not u:
        return None
    return getattr(u, "full_name", None) or getattr(u, "name", None) or None


def _dept_display_name(d) -> Optional[str]:
    if not d:
        return None
    return getattr(d, "name", None) or None


def _enrich_row(base: Optional[dict], **extra) -> dict:
    d = base or {}
    for k, v in extra.items():
        if v is not None:
            d[k] = v
    return d


def _hhmm(t) -> Optional[str]:
    try:
        return t.strftime("%H:%M") if t else None
    except Exception:
        return None


def _visit_case_sheet(v: Visit) -> Dict[str, Any]:
    return {
        "chief_complaint": getattr(v, "chief_complaint", None),
        "symptoms": getattr(v, "symptoms", None),
        "subjective": getattr(v, "soap_subjective", None),
        "objective": getattr(v, "soap_objective", None),
        "assessment": getattr(v, "soap_assessment", None),
        "plan": getattr(v, "plan", None),
    }


def _pick_visit_vitals(
    v: Visit,
    vitals_by_appt: Dict[int, List[OpdVitals]],
    vitals_by_patient: List[OpdVitals],
) -> Optional[OpdVitals]:
    appt_id = getattr(v, "appointment_id", None)
    if appt_id and appt_id in vitals_by_appt and vitals_by_appt[appt_id]:
        return vitals_by_appt[appt_id][0]

    v_at = getattr(v, "visit_at", None)
    if v_at and vitals_by_patient:
        for vt in vitals_by_patient:
            try:
                dt = _safe_dt(getattr(vt, "created_at", None))
                if abs((dt - _safe_dt(v_at)).total_seconds()) <= 36 * 3600:
                    return vt
            except Exception:
                continue

    return vitals_by_patient[0] if vitals_by_patient else None


# =========================
# OT bundle helpers (PDF-aligned)
# =========================
def _resolve_ot_cases_for_schedules(
        db: Session,
        schedules: list[OtSchedule]) -> Dict[int, Optional[OtCase]]:
    if not schedules:
        return {}

    schedule_ids = [s.id for s in schedules if getattr(s, "id", None)]
    by_schedule: Dict[int, OtCase] = {}

    # 1) relationship
    for s in schedules:
        c = getattr(s, "case", None)
        if c is not None:
            by_schedule[s.id] = c

    # 2) schedule.case_id -> OtCase.id
    case_ids = [getattr(s, "case_id", None) for s in schedules]
    case_ids = [x for x in case_ids if x]
    if case_ids:
        rows = db.query(OtCase).filter(OtCase.id.in_(case_ids)).all()
        by_id = {c.id: c for c in rows}
        for s in schedules:
            if s.id in by_schedule:
                continue
            cid = getattr(s, "case_id", None)
            if cid and cid in by_id:
                by_schedule[s.id] = by_id[cid]

    # 3) OtCase.schedule_id -> schedule.id (critical)
    missing = [sid for sid in schedule_ids if sid not in by_schedule]
    if missing and hasattr(OtCase, "schedule_id"):
        rows = db.query(OtCase).filter(OtCase.schedule_id.in_(missing)).all()
        for c in rows:
            sid = getattr(c, "schedule_id", None)
            if sid:
                by_schedule[sid] = c

    return {s.id: by_schedule.get(s.id) for s in schedules}


def _prefetch_ot_children(db: Session, case_ids: list[int]) -> Dict[str, Any]:
    case_ids = [x for x in case_ids if x]
    if not case_ids:
        return {
            "pae_by": {},
            "preop_by": {},
            "safety_by": {},
            "an_hdr_by": {},
            "nursing_by": {},
            "counts_by": {},
            "opnote_by": {},
            "pacu_by": {},
            "implants_by": {},
            "blood_by": {},
            "cleaning_by": {},
            "vitals_by_rec": {},
            "drugs_by_rec": {},
        }

    def one_by_case(model):
        rows = db.query(model).filter(getattr(model,
                                              "case_id").in_(case_ids)).all()
        return {getattr(r, "case_id"): r for r in rows}

    def many_by_case(model, order_field: str = "id"):
        q = db.query(model).filter(getattr(model, "case_id").in_(case_ids))
        if hasattr(model, order_field):
            q = q.order_by(getattr(model, order_field).asc())
        rows = q.all()
        mp: Dict[int, list] = {}
        for r in rows:
            mp.setdefault(getattr(r, "case_id"), []).append(r)
        return mp

    pae_by = one_by_case(PreAnaesthesiaEvaluation)
    preop_by = one_by_case(PreOpChecklist)
    safety_by = one_by_case(SurgicalSafetyChecklist)
    an_hdr_by = one_by_case(AnaesthesiaRecord)
    nursing_by = one_by_case(OtNursingRecord)
    counts_by = one_by_case(OtSpongeInstrumentCount)
    opnote_by = one_by_case(OperationNote)
    pacu_by = one_by_case(PacuRecord)

    implants_by = many_by_case(OtImplantRecord, "id")
    blood_by = many_by_case(OtBloodTransfusionRecord, "id")
    cleaning_by = many_by_case(OtCleaningLog, "id")

    an_record_ids = [getattr(r, "id", None) for r in an_hdr_by.values()]
    an_record_ids = [x for x in an_record_ids if x]

    vitals_by_rec: Dict[int, list[AnaesthesiaVitalLog]] = {}
    drugs_by_rec: Dict[int, list[AnaesthesiaDrugLog]] = {}
    if an_record_ids:
        vrows = (db.query(AnaesthesiaVitalLog).filter(
            AnaesthesiaVitalLog.record_id.in_(an_record_ids)).order_by(
                AnaesthesiaVitalLog.time.asc()).all())
        drows = (db.query(AnaesthesiaDrugLog).filter(
            AnaesthesiaDrugLog.record_id.in_(an_record_ids)).order_by(
                AnaesthesiaDrugLog.time.asc()).all())
        for r in vrows:
            vitals_by_rec.setdefault(getattr(r, "record_id"), []).append(r)
        for r in drows:
            drugs_by_rec.setdefault(getattr(r, "record_id"), []).append(r)

    return {
        "pae_by": pae_by,
        "preop_by": preop_by,
        "safety_by": safety_by,
        "an_hdr_by": an_hdr_by,
        "nursing_by": nursing_by,
        "counts_by": counts_by,
        "opnote_by": opnote_by,
        "pacu_by": pacu_by,
        "implants_by": implants_by,
        "blood_by": blood_by,
        "cleaning_by": cleaning_by,
        "vitals_by_rec": vitals_by_rec,
        "drugs_by_rec": drugs_by_rec,
    }


def _planned_dt_from_schedule(sc: OtSchedule) -> Optional[datetime]:
    if sc.date and sc.planned_start_time:
        return datetime(
            sc.date.year,
            sc.date.month,
            sc.date.day,
            sc.planned_start_time.hour,
            sc.planned_start_time.minute,
        )
    return None


def _ot_case_bundle_row(sc: OtSchedule, case: Optional[OtCase],
                        children: Dict[str, Any]) -> Dict[str, Any]:
    schedule_procs = [{
        "link":
        _row(x),
        "procedure":
        _row(x.procedure) if getattr(x, "procedure", None) else None,
    } for x in (getattr(sc, "procedures", None) or [])]

    base: Dict[str, Any] = {
        "schedule":
        _row(sc),
        "ot_bed":
        _row(getattr(sc, "ot_bed", None))
        if getattr(sc, "ot_bed", None) else None,
        "surgeon":
        _row(getattr(sc, "surgeon", None))
        if getattr(sc, "surgeon", None) else None,
        "anaesthetist":
        _row(getattr(sc, "anaesthetist", None)) if getattr(
            sc, "anaesthetist", None) else None,
        "schedule_procedures":
        schedule_procs,
        "case":
        _row(case) if case else None,
        "speciality":
        _row(getattr(case, "speciality", None))
        if case and getattr(case, "speciality", None) else None,
    }

    if not case:
        return base

    cid = getattr(case, "id", None)

    pae = children["pae_by"].get(cid)
    preop = children["preop_by"].get(cid)
    safety = children["safety_by"].get(cid)
    an_hdr = children["an_hdr_by"].get(cid)
    nursing = children["nursing_by"].get(cid)
    counts = children["counts_by"].get(cid)
    opnote = children["opnote_by"].get(cid)
    pacu = children["pacu_by"].get(cid)
    implants = children["implants_by"].get(cid, [])
    blood = children["blood_by"].get(cid, [])
    cleaning = children["cleaning_by"].get(cid, [])

    ana_payload = None
    if an_hdr:
        rid = getattr(an_hdr, "id", None)
        ana_payload = {
            "header": _row(an_hdr),
            "vitals":
            _rows(children["vitals_by_rec"].get(rid, [])) if rid else [],
            "drugs":
            _rows(children["drugs_by_rec"].get(rid, [])) if rid else [],
        }

    base.update({
        "preanaesthesia": _row(pae) if pae else None,
        "pre_anaesthesia_evaluation": _row(pae) if pae else None,  # alias
        "preop_checklist": _row(preop) if preop else None,
        "safety_checklist": _row(safety) if safety else None,
        "surgical_safety_checklist": _row(safety) if safety else None,  # alias
        "anaesthesia_record": ana_payload,
        "nursing_record": _row(nursing) if nursing else None,
        "counts_record": _row(counts) if counts else None,
        "implant_records": _rows(implants),
        "implants": _rows(implants),  # alias
        "blood_records": _rows(blood),
        "blood_transfusions": _rows(blood),  # alias
        "operation_note": _row(opnote) if opnote else None,
        "pacu_record": _row(pacu) if pacu else None,
        "cleaning_logs": _rows(cleaning),
    })
    return base


# =========================
# PDF Branding Header (HTML/CSS) helpers
# =========================
def _bg(branding: Any, name: str, default: Any = "") -> Any:
    if branding is None:
        return default
    if isinstance(branding, dict):
        return branding.get(name, default)
    return getattr(branding, name, default)


def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _logo_data_uri(branding: Any, *, max_px: int = 320) -> Optional[str]:
    rel = str(_bg(branding, "logo_path", "") or "").strip()
    if not rel:
        return None

    abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
    if not abs_path.exists() or not abs_path.is_file():
        return None

    mime, _ = mimetypes.guess_type(str(abs_path))
    if not mime:
        mime = "image/png"

    try:
        raw = abs_path.read_bytes()
    except Exception:
        return None

    # Optional downscale (Pillow)
    try:
        from PIL import Image  # type: ignore

        im = Image.open(BytesIO(raw))
        im.load()
        im.thumbnail((max_px, max_px))  # no upscaling
        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
        mime = "image/png"
    except Exception:
        pass

    enc = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{enc}"


def brand_header_css() -> str:
    return """
    .brand-header{
      --logo-col: 270px;
      --logo-w: 240px;
      --logo-h: 72px;

      width:100%;
      padding-bottom: 6px;
      margin-bottom: 10px;
      border-bottom: 1px solid #e5e7eb;
    }
    .brand-row{ display: table; width: 100%; table-layout: fixed; }
    .brand-left{ display: table-cell; width: var(--logo-col); vertical-align: middle; }
    .brand-right{ display: table-cell; vertical-align: top; text-align: right; padding-left: 12px; }

    .brand-logo-wrap{
      width: var(--logo-w);
      height: calc(var(--logo-h) + 8px);
      display: flex;
      align-items: center;
      justify-content: flex-start;
      overflow: hidden;
    }
    .brand-logo{
      height: var(--logo-h);
      width: auto;
      max-width: var(--logo-w);
      object-fit: contain;
      display: block;
    }
    .brand-logo-placeholder{
      font-size: 10px;
      color: #94a3b8;
      letter-spacing: 0.6px;
      border: 1px dashed #cbd5e1;
      padding: 6px 10px;
      border-radius: 999px;
      display: inline-block;
    }

    .brand-box{ display: inline-block; text-align: left; max-width: 420px; }
    .brand-name{
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.2px;
      margin: 0;
      color: #0f172a;
      line-height: 1.1;
    }
    .brand-tagline{ margin-top: 3px; font-size: 11px; color: #64748b; line-height: 1.25; }
    .brand-meta{ margin-top: 8px; font-size: 10.5px; color: #0f172a; line-height: 1.35; }
    .brand-muted{ color: #64748b; }
    .brand-meta-line{ margin-top: 2px; }
    """.strip()


def render_brand_header_html(branding: Any) -> str:
    logo_src = _logo_data_uri(branding, max_px=320)

    org_name = _h(_bg(branding, "org_name", "") or "")
    org_tagline = _h(_bg(branding, "org_tagline", "") or "")

    addr = _h(_bg(branding, "org_address", "") or "")
    phone = _h(_bg(branding, "org_phone", "") or "")
    email = _h(_bg(branding, "org_email", "") or "")
    website = _h(_bg(branding, "org_website", "") or "")
    gstin = _h(_bg(branding, "org_gstin", "") or "")

    meta_lines: list[str] = []
    if addr:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>Address:</span> {addr}</div>"
        )

    contact_bits: list[str] = []
    if phone:
        contact_bits.append(
            f"<span><span class='brand-muted'>Phone:</span> {phone}</span>")
    if email:
        contact_bits.append(
            f"<span><span class='brand-muted'>Email:</span> {email}</span>")
    if contact_bits:
        meta_lines.append("<div class='brand-meta-line'>" +
                          " &nbsp; | &nbsp; ".join(contact_bits) + "</div>")

    if website:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>Website:</span> {website}</div>"
        )

    if gstin:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>GSTIN:</span> {gstin}</div>"
        )

    meta_html = f"<div class='brand-meta'>{''.join(meta_lines)}</div>" if meta_lines else ""
    logo_html = f"<img class='brand-logo' src='{logo_src}' alt='Logo' />" if logo_src else "<div class='brand-logo-placeholder'>LOGO</div>"

    return f"""
    <div class="brand-header">
      <div class="brand-row">
        <div class="brand-left">
          <div class="brand-logo-wrap">{logo_html}</div>
        </div>
        <div class="brand-right">
          <div class="brand-box">
            <p class="brand-name">{org_name}</p>
            {f"<div class='brand-tagline'>{org_tagline}</div>" if org_tagline else ""}
            {meta_html}
          </div>
        </div>
      </div>
    </div>
    """.strip()


def _call_emr_pdf_generator(
    *,
    patient: dict,
    items: list[dict],
    sections_selected: Optional[Set[str]],
    letterhead_bytes: Optional[bytes],
    branding: Any,
) -> bytes:
    """
    Calls app.services.pdf_emr.generate_emr_pdf safely (signature-adaptive).
    ✅ If generator supports header_css/header_html, we pass our branding header too.
    """
    mod = importlib.import_module("app.services.pdf_emr")
    if not hasattr(mod, "generate_emr_pdf"):
        raise HTTPException(
            500,
            "EMR PDF generator not found (app.services.pdf_emr.generate_emr_pdf)"
        )

    fn = getattr(mod, "generate_emr_pdf")
    sig = pyinspect.signature(fn)
    params = sig.parameters

    header_css = brand_header_css()
    header_html = render_brand_header_html(branding) if branding else None

    kwargs: Dict[str, Any] = {}
    # common names
    if "patient" in params:
        kwargs["patient"] = patient
    if "items" in params:
        kwargs["items"] = items
    if "sections_selected" in params:
        kwargs["sections_selected"] = sections_selected
    if "letterhead_bytes" in params:
        kwargs["letterhead_bytes"] = letterhead_bytes
    if "branding" in params:
        kwargs["branding"] = branding

    # optional header params (support multiple possible names)
    for k in ("branding_header_css", "header_css", "brand_css",
              "pdf_header_css"):
        if k in params:
            kwargs[k] = header_css
            break
    for k in ("branding_header_html", "header_html", "brand_header_html",
              "pdf_header_html"):
        if k in params:
            kwargs[k] = header_html
            break

    try:
        out = fn(**kwargs)
    except TypeError:
        # fallback: old signature without our optional args
        safe_kwargs = {
            k: v
            for k, v in kwargs.items() if k in {
                "patient", "items", "sections_selected", "letterhead_bytes",
                "branding"
            }
        }
        out = fn(**safe_kwargs)

    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if hasattr(out, "getvalue"):
        return out.getvalue()
    raise HTTPException(500, "EMR PDF generator returned unsupported type")


# =========================
# CORE TIMELINE
# =========================
def _build_timeline(
    db: Session,
    patient_id: int,
    dfrom: Optional[datetime],
    dto: Optional[datetime],
    allow: Optional[Set[str]],
) -> list[TimelineItemOut]:
    out: list[TimelineItemOut] = []

    # --- OPD Appointments ---
    if _want("opd_appointment", allow):
        appts = (db.query(Appointment).options(
            joinedload(Appointment.doctor),
            joinedload(Appointment.department)).filter(
                Appointment.patient_id == patient_id).order_by(
                    Appointment.date.desc(),
                    Appointment.slot_start.desc()).limit(500).all())
        for a in appts:
            ts = _safe_dt(a.created_at or a.date)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="opd_appointment",
                    ts=ts,
                    title=_title_for("opd_appointment"),
                    subtitle=f"{a.purpose or 'Consultation'} • {a.status}",
                    doctor_name=getattr(a.doctor, "name", None),
                    department_name=getattr(a.department, "name", None),
                    status=_map_ui_status(a.status),
                    data={"appointment": _row(a)},
                ))

    # --- OPD Visits (SOAP etc) ---
    if _want("opd_visit", allow):
        visits = (db.query(Visit).options(
            joinedload(Visit.doctor), joinedload(Visit.department),
            joinedload(Visit.appointment)).filter(
                Visit.patient_id == patient_id).order_by(
                    Visit.visit_at.desc()).limit(500).all())

        visit_ids = [v.id for v in visits]

        vitals_all = (db.query(OpdVitals).options(
            joinedload(OpdVitals.appointment)).filter(
                OpdVitals.patient_id == patient_id).order_by(
                    OpdVitals.created_at.desc()).limit(2000).all())
        vitals_by_patient = sorted(
            vitals_all,
            key=lambda z: _safe_dt(getattr(z, "created_at", None)),
            reverse=True)

        vitals_by_appt: Dict[int, List[OpdVitals]] = {}
        if hasattr(OpdVitals, "appointment_id"):
            for vt in vitals_all:
                aid = getattr(vt, "appointment_id", None)
                if aid:
                    vitals_by_appt.setdefault(aid, []).append(vt)
            for aid in list(vitals_by_appt.keys()):
                vitals_by_appt[aid].sort(
                    key=lambda z: _safe_dt(getattr(z, "created_at", None)),
                    reverse=True)

        rx_by_visit: Dict[int, Any] = {}
        if visit_ids:
            rxs = (db.query(OpdRx).options(selectinload(
                OpdRx.items), joinedload(OpdRx.signer)).filter(
                    OpdRx.visit_id.in_(visit_ids)).order_by(
                        OpdRx.id.desc()).all())
            for rx in rxs:
                if rx.visit_id not in rx_by_visit:
                    rx_by_visit[rx.visit_id] = rx

        lab_by_visit: Dict[int, List[Any]] = {}
        rad_by_visit: Dict[int, List[Any]] = {}
        if visit_ids:
            lab_orders = (db.query(LabOrder).options(
                joinedload(LabOrder.test)).filter(
                    LabOrder.visit_id.in_(visit_ids)).order_by(
                        LabOrder.id.desc()).all())
            for lo in lab_orders:
                lab_by_visit.setdefault(lo.visit_id, []).append(lo)

            rad_orders = (db.query(RadiologyOrder).options(
                joinedload(RadiologyOrder.test)).filter(
                    RadiologyOrder.visit_id.in_(visit_ids)).order_by(
                        RadiologyOrder.id.desc()).all())
            for ro in rad_orders:
                rad_by_visit.setdefault(ro.visit_id, []).append(ro)

        fu_by_visit: Dict[int, List[Any]] = {}
        if visit_ids:
            fus = (db.query(FollowUp).options(joinedload(
                FollowUp.appointment)).filter(
                    FollowUp.patient_id == patient_id,
                    FollowUp.source_visit_id.in_(visit_ids)).order_by(
                        FollowUp.due_date.desc(), FollowUp.id.desc()).all())
            for f in fus:
                fu_by_visit.setdefault(f.source_visit_id, []).append(f)

        for v in visits:
           # inside: for v in visits:
            ts = _safe_dt(v.visit_at)
            if not _in_window(ts, dfrom, dto):
                continue

            chosen_vitals = _pick_visit_vitals(v, vitals_by_appt, vitals_by_patient)
            rx = rx_by_visit.get(v.id)

            # ✅ IMPORTANT: enrich visit dict with SOAP keys used by PDF ("subjective/objective/assessment/plan")
            cs = _visit_case_sheet(v)  # returns chief_complaint, symptoms, subjective, objective, assessment, plan
            visit_row = _enrich_row(_row(v), **{k: val for k, val in cs.items() if val is not None})

            out.append(
                TimelineItemOut(
                    type="opd_visit",
                    ts=ts,
                    title=_title_for("opd_visit"),
                    subtitle=v.chief_complaint or v.symptoms or "Consultation",
                    doctor_name=_user_display_name(getattr(v, "doctor", None)),
                    department_name=_dept_display_name(getattr(v, "department", None)),
                    status=None,
                    data={
                        "visit": visit_row,              # ✅ now contains subjective/objective/assessment/plan
                        "appointment": _row(v.appointment) if v.appointment else None,
                        "case_sheet": cs,                # keep original structured block too

                        "vitals": _row(chosen_vitals) if chosen_vitals else None,
                        "bmi": _bmi(getattr(chosen_vitals, "height_cm", None),
                                    getattr(chosen_vitals, "weight_kg", None)) if chosen_vitals else None,

                        "prescription": _row(rx) if rx else None,
                        "prescription_items": _rows(getattr(rx, "items", None)) if rx else [],

                        "lab_orders": [{
                            "order": _row(x),
                            "test": _row(getattr(x, "test", None)) if getattr(x, "test", None) else None
                        } for x in (lab_by_visit.get(v.id, []) or [])],

                        "radiology_orders": [{
                            "order": _row(x),
                            "test": _row(getattr(x, "test", None)) if getattr(x, "test", None) else None
                        } for x in (rad_by_visit.get(v.id, []) or [])],

                        "followups": _rows(fu_by_visit.get(v.id, [])),
                    },
                )
            )


    # --- OPD Vitals ---
    if _want("opd_vitals", allow):
        vitals = (db.query(OpdVitals).options(joinedload(
            OpdVitals.appointment)).filter(
                OpdVitals.patient_id == patient_id).order_by(
                    OpdVitals.created_at.desc()).limit(500).all())
        for vt in vitals:
            ts = _safe_dt(vt.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            chips = []
            if vt.bp_systolic and vt.bp_diastolic:
                chips.append(f"BP {vt.bp_systolic}/{vt.bp_diastolic} mmHg")
            if vt.temp_c is not None:
                chips.append(f"T {_as_float(vt.temp_c)}°C")
            if vt.pulse:
                chips.append(f"Pulse {vt.pulse}/min")
            if vt.spo2:
                chips.append(f"SpO₂ {vt.spo2}%")
            out.append(
                TimelineItemOut(
                    type="opd_vitals",
                    ts=ts,
                    title=_title_for("opd_vitals"),
                    subtitle="  ·  ".join(chips)
                    if chips else "Vitals recorded",
                    status=None,
                    data={
                        "vitals":
                        _row(vt),
                        "bmi":
                        _bmi(vt.height_cm, vt.weight_kg),
                        "appointment":
                        _row(vt.appointment) if vt.appointment else None,
                    },
                ))

    # --- OPD Prescriptions ---
    if _want("rx", allow):
        rxs = (db.query(OpdRx).options(
            joinedload(OpdRx.visit).joinedload(Visit.doctor),
            joinedload(OpdRx.items),
            joinedload(OpdRx.signer),
        ).join(Visit, OpdRx.visit_id == Visit.id).filter(
            Visit.patient_id == patient_id).order_by(
                OpdRx.id.desc()).limit(500).all())
        for rx in rxs:
            ts_raw = rx.signed_at or (rx.visit.visit_at if rx.visit else
                                      None) or getattr(rx, "created_at", None)
            ts = _safe_dt(ts_raw)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="rx",
                    ts=ts,
                    title=_title_for("rx"),
                    subtitle=(rx.notes or "Prescription"),
                    doctor_name=rx.visit.doctor.name
                    if rx.visit and rx.visit.doctor else None,
                    status=_map_ui_status(
                        "signed" if rx.signed_at else "draft"),
                    data={
                        "prescription": _row(rx),
                        "items": _rows(rx.items),
                        "signed_by_name":
                        rx.signer.name if rx.signer else None,
                    },
                ))

    # --- OPD Lab Orders ---
    if _want("opd_lab_order", allow):
        lab_orders = (db.query(LabOrder).options(joinedload(
            LabOrder.visit), joinedload(LabOrder.test)).join(
                Visit, LabOrder.visit_id == Visit.id).filter(
                    Visit.patient_id == patient_id).order_by(
                        LabOrder.ordered_at.desc()).limit(500).all())
        for lo in lab_orders:
            ts = _safe_dt(lo.ordered_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="opd_lab_order",
                    ts=ts,
                    title=_title_for("opd_lab_order"),
                    subtitle=
                    f"{getattr(lo.test,'name',None) or 'Lab'} • {lo.status}",
                    status=_map_ui_status(lo.status),
                    data={
                        "opd_lab_order": _row(lo),
                        "test": _row(lo.test) if lo.test else None,
                        "visit": _row(lo.visit) if lo.visit else None
                    },
                ))

    # --- OPD Radiology Orders ---
    if _want("opd_radiology_order", allow):
        ris_orders = (db.query(RadiologyOrder).options(
            joinedload(RadiologyOrder.visit),
            joinedload(RadiologyOrder.test)).join(
                Visit, RadiologyOrder.visit_id == Visit.id).filter(
                    Visit.patient_id == patient_id).order_by(
                        RadiologyOrder.ordered_at.desc()).limit(500).all())
        for ro in ris_orders:
            ts = _safe_dt(ro.ordered_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="opd_radiology_order",
                    ts=ts,
                    title=_title_for("opd_radiology_order"),
                    subtitle=
                    f"{getattr(ro.test,'name',None) or 'Radiology'} • {ro.status}",
                    status=_map_ui_status(ro.status),
                    data={
                        "opd_radiology_order": _row(ro),
                        "test": _row(ro.test) if ro.test else None,
                        "visit": _row(ro.visit) if ro.visit else None
                    },
                ))

    # --- Follow-ups ---
    if _want("followup", allow):
        fus = (db.query(FollowUp).options(joinedload(
            FollowUp.source_visit), joinedload(FollowUp.appointment)).filter(
                FollowUp.patient_id == patient_id).order_by(
                    FollowUp.due_date.desc(),
                    FollowUp.id.desc()).limit(500).all())
        for f in fus:
            ts = _safe_dt(
                getattr(f, "updated_at", None)
                or getattr(f, "created_at", None) or f.due_date)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="followup",
                    ts=ts,
                    title=_title_for("followup"),
                    subtitle=f"Due {f.due_date} • {f.status}",
                    status=_map_ui_status(f.status),
                    data={
                        "followup":
                        _row(f),
                        "source_visit":
                        _row(f.source_visit) if f.source_visit else None,
                        "appointment":
                        _row(f.appointment) if f.appointment else None,
                    },
                ))

    # --- LIS (results + attachments + result_lines) ---
    if _want("lab", allow):
        lis_orders = (db.query(LisOrder).options(
            selectinload(LisOrder.items).selectinload(
                LisOrderItem.attachments)).filter(
                    LisOrder.patient_id == patient_id).order_by(
                        LisOrder.id.desc()).limit(250).all())
        order_ids = [x.id for x in lis_orders]
        res_lines_by_order: Dict[int, List[LisResultLine]] = {}
        if order_ids:
            res_lines = (db.query(LisResultLine).filter(
                LisResultLine.order_id.in_(order_ids)).order_by(
                    LisResultLine.id.asc()).all())
            for rl in res_lines:
                res_lines_by_order.setdefault(rl.order_id, []).append(rl)

        for lo in lis_orders:
            for it in (lo.items or []):
                ts = _safe_dt(it.result_at or lo.reported_at or lo.created_at)
                if not _in_window(ts, dfrom, dto):
                    continue
                atts = [
                    AttachmentOut(
                        label=(a.note or "Report"),
                        url=a.file_url,
                        content_type=None,
                        note=a.note or None,
                        size_bytes=None,
                    ) for a in (it.attachments or [])
                ]
                out.append(
                    TimelineItemOut(
                        type="lab",
                        ts=ts,
                        title=_title_for("lab"),
                        subtitle=
                        f"{it.test_name} ({it.test_code}) • Result: {it.result_value or '—'}",
                        status=_map_ui_status(it.status),
                        attachments=atts,
                        data={
                            "lis_order":
                            _row(lo),
                            "lis_item":
                            _row(it),
                            "attachments":
                            _rows(it.attachments),
                            "result_lines":
                            _rows(res_lines_by_order.get(lo.id, [])),
                        },
                    ))

    # --- RIS (report + attachments) ---
    if _want("radiology", allow):
        ris = (db.query(RisOrder).options(selectinload(
            RisOrder.attachments)).filter(
                RisOrder.patient_id == patient_id).order_by(
                    RisOrder.id.desc()).limit(250).all())
        for ro in ris:
            ts = _safe_dt(ro.reported_at or ro.scanned_at or ro.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            atts = [
                AttachmentOut(
                    label=(a.note or "Image/Report"),
                    url=a.file_url,
                    content_type=None,
                    note=a.note or None,
                    size_bytes=None,
                ) for a in (ro.attachments or [])
            ]
            out.append(
                TimelineItemOut(
                    type="radiology",
                    ts=ts,
                    title=_title_for("radiology"),
                    subtitle=f"{ro.test_name} ({ro.test_code})",
                    status=_map_ui_status(ro.status),
                    attachments=atts,
                    data={
                        "ris_order": _row(ro),
                        "attachments": _rows(ro.attachments)
                    },
                ))

    # --- Pharmacy Prescription (lines) ---
    if HAS_PHARMACY and _want("pharmacy_rx", allow):
        prxs = (db.query(PharmacyPrescription).options(
            selectinload(PharmacyPrescription.lines)).filter(
                PharmacyPrescription.patient_id == patient_id).order_by(
                    PharmacyPrescription.id.desc()).limit(250).all())
        for pr in prxs:
            ts = _safe_dt(pr.signed_at or pr.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="pharmacy_rx",
                    ts=ts,
                    title=_title_for("pharmacy_rx"),
                    subtitle=f"{pr.type} • {pr.status}",
                    status=_map_ui_status(pr.status),
                    data={
                        "pharmacy_prescription": _row(pr),
                        "lines": _rows(pr.lines)
                    },
                ))

    # --- Pharmacy Sale (dispense) ---
    if HAS_PHARMACY and _want("pharmacy", allow):
        sales = (db.query(PharmacySale).filter(
            PharmacySale.patient_id == patient_id,
            PharmacySale.invoice_status != "CANCELLED" if hasattr(
                PharmacySale, "invoice_status") else True,
        ).order_by(PharmacySale.id.desc()).limit(250).all())
        sale_ids = [s.id for s in sales]
        items_by_sale: Dict[int, List[PharmacySaleItem]] = {}
        if sale_ids:
            items = db.query(PharmacySaleItem).filter(
                PharmacySaleItem.sale_id.in_(sale_ids)).all()
            for it in items:
                items_by_sale.setdefault(it.sale_id, []).append(it)

        for s in sales:
            ts = _safe_dt(
                getattr(s, "created_at", None)
                or getattr(s, "bill_datetime", None))
            if not _in_window(ts, dfrom, dto):
                continue
            total_amount = _as_float(
                getattr(s, "net_amount", None)
                or getattr(s, "total_amount", None) or 0) or 0.0
            status_raw = (getattr(s, "status", "") or "").lower()
            out.append(
                TimelineItemOut(
                    type="pharmacy",
                    ts=ts,
                    title=_title_for("pharmacy"),
                    subtitle=f"Dispense • Net ₹{float(total_amount):.2f}",
                    status=_map_ui_status(status_raw) or "completed",
                    data={
                        "pharmacy_sale": _row(s),
                        "items": _rows(items_by_sale.get(s.id, []))
                    },
                ))

    # --- IPD Admission (header) ---
    if _want("ipd_admission", allow):
        adms = (db.query(IpdAdmission).filter(
            IpdAdmission.patient_id == patient_id).order_by(
                IpdAdmission.id.desc()).limit(200).all())
        bed_ids = [a.current_bed_id for a in adms if a.current_bed_id]
        beds: Dict[int, IpdBed] = {}
        if bed_ids:
            rows = db.query(IpdBed).filter(IpdBed.id.in_(bed_ids)).all()
            beds = {b.id: b for b in rows}

        for a in adms:
            ts = _safe_dt(a.admitted_at)
            if not _in_window(ts, dfrom, dto):
                continue
            bed = beds.get(a.current_bed_id)
            out.append(
                TimelineItemOut(
                    type="ipd_admission",
                    ts=ts,
                    title=_title_for("ipd_admission"),
                    subtitle=f"Admission {a.display_code}",
                    status=_map_ui_status(a.status),
                    data={
                        "admission": _row(a),
                        "current_bed": _row(bed) if bed else None
                    },
                ))

    # --- IPD Transfer ---
    if _want("ipd_transfer", allow):
        trs = (db.query(IpdTransfer).join(
            IpdAdmission, IpdTransfer.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdTransfer.id.desc()).limit(500).all())
        for t in trs:
            ts = _safe_dt(t.transferred_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_transfer",
                    ts=ts,
                    title=_title_for("ipd_transfer"),
                    subtitle="Bed transfer",
                    status="completed",
                    data={"transfer": _row(t)},
                ))

    # --- IPD Discharge Summary ---
    if _want("ipd_discharge", allow):
        ds = (db.query(IpdDischargeSummary).join(
            IpdAdmission,
            IpdDischargeSummary.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdDischargeSummary.id.desc()).limit(200).all())
        for d in ds:
            ts = _safe_dt(d.finalized_at
                          or getattr(d, "discharge_datetime", None)
                          or getattr(d, "created_at", None))
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_discharge",
                    ts=ts,
                    title=_title_for("ipd_discharge"),
                    subtitle="Discharge Summary",
                    status="completed" if d.finalized else "new",
                    data={"discharge_summary": _row(d)},
                ))

    # --- IPD Vitals ---
    if _want("ipd_vitals", allow):
        vts = (db.query(IpdVital).join(
            IpdAdmission, IpdVital.admission_id == IpdAdmission.id).options(
                joinedload(IpdVital.recorder)).filter(
                    IpdAdmission.patient_id == patient_id).order_by(
                        IpdVital.recorded_at.desc()).limit(1000).all())
        for v in vts:
            ts = _safe_dt(v.recorded_at)
            if not _in_window(ts, dfrom, dto):
                continue
            subtitle = (" • ".join([
                x for x in [
                    f"BP {v.bp_systolic}/{v.bp_diastolic}"
                    if v.bp_systolic and v.bp_diastolic else None,
                    f"T {_as_float(v.temp_c)}°C" if v.
                    temp_c is not None else None,
                    f"Pulse {v.pulse}/min" if v.pulse else None,
                    f"SpO₂ {v.spo2}%" if v.spo2 else None,
                    f"RR {v.rr}/min" if v.rr else None,
                ] if x
            ]) or "Vitals")
            out.append(
                TimelineItemOut(
                    type="ipd_vitals",
                    ts=ts,
                    title=_title_for("ipd_vitals"),
                    subtitle=subtitle,
                    status=None,
                    data={
                        "ipd_vitals":
                        _row(v),
                        "recorded_by_name":
                        getattr(v.recorder, "name", None)
                        if v.recorder else None
                    },
                ))

    # --- IPD Nursing Notes ---
    if _want("ipd_nursing_note", allow):
        notes = (db.query(IpdNursingNote).join(
            IpdAdmission,
            IpdNursingNote.admission_id == IpdAdmission.id).options(
                joinedload(IpdNursingNote.nurse),
                joinedload(IpdNursingNote.vitals)).filter(
                    IpdAdmission.patient_id == patient_id).order_by(
                        IpdNursingNote.entry_time.desc()).limit(1000).all())
        for n in notes:
            ts = _safe_dt(n.entry_time)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_nursing_note",
                    ts=ts,
                    title=_title_for("ipd_nursing_note"),
                    subtitle=(n.note_type or "routine"),
                    status="locked"
                    if getattr(n, "is_locked", False) else None,
                    data={
                        "nursing_note":
                        _row(n),
                        "nurse_name":
                        getattr(n.nurse, "name", None) if n.nurse else None,
                        "linked_vitals":
                        _row(n.vitals) if n.vitals else None,
                    },
                ))

    # --- IPD Intake/Output ---
    if _want("ipd_intake_output", allow):
        ios = (
            db.query(IpdIntakeOutput)
            .join(IpdAdmission, IpdIntakeOutput.admission_id == IpdAdmission.id)
            .filter(IpdAdmission.patient_id == patient_id)
            .order_by(IpdIntakeOutput.recorded_at.desc())
            .limit(2000)
            .all()
        )

    for io in ios:
        ts = _safe_dt(io.recorded_at)
        if not _in_window(ts, dfrom, dto):
            continue

        # ✅ split totals with fallback to legacy
        in_split = (int(io.intake_oral_ml or 0) + int(io.intake_iv_ml or 0) + int(io.intake_blood_ml or 0))
        out_ur_split = (int(io.urine_foley_ml or 0) + int(io.urine_voided_ml or 0))

        intake_total = in_split if in_split > 0 else int(io.intake_ml or 0)
        urine_total = out_ur_split if out_ur_split > 0 else int(io.urine_ml or 0)
        drains = int(io.drains_ml or 0)
        output_total = urine_total + drains
        net = intake_total - output_total

        subtitle = (
            f"In {intake_total} ml (Oral {int(io.intake_oral_ml or 0)}, IV {int(io.intake_iv_ml or 0)}, Blood {int(io.intake_blood_ml or 0)}) "
            f"• Out {output_total} ml (Foley {int(io.urine_foley_ml or 0)}, Voided {int(io.urine_voided_ml or 0)}, Drains {drains}) "
            f"• Net {('+' if net > 0 else '')}{net} ml"
        )

        out.append(
            TimelineItemOut(
                type="ipd_intake_output",
                ts=ts,
                title=_title_for("ipd_intake_output"),
                subtitle=subtitle,
                status=None,
                data={"intake_output": _row(io)},
            )
        )

    # --- IPD Rounds ---
    if _want("ipd_round", allow):
        rds = (db.query(IpdRound).join(
            IpdAdmission, IpdRound.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdRound.created_at.desc()).limit(2000).all())
        for r in rds:
            ts = _safe_dt(r.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(type="ipd_round",
                                ts=ts,
                                title=_title_for("ipd_round"),
                                subtitle="Doctor round",
                                data={"round": _row(r)}))

    # --- IPD Progress Notes ---
    if _want("ipd_progress", allow):
        pns = (db.query(IpdProgressNote).join(
            IpdAdmission,
            IpdProgressNote.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdProgressNote.created_at.desc()).limit(2000).all())
        for pn in pns:
            ts = _safe_dt(pn.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(type="ipd_progress",
                                ts=ts,
                                title=_title_for("ipd_progress"),
                                subtitle="Progress note",
                                data={"progress_note": _row(pn)}))

    # --- IPD Risk + Assessments ---
    if _want("ipd_risk", allow):

        def _emit_many(rows, typ: str, ts_field: str):
            for x in rows:
                ts = _safe_dt(
                    getattr(x, ts_field, None)
                    or getattr(x, "created_at", None))
                if not _in_window(ts, dfrom, dto):
                    continue
                out.append(
                    TimelineItemOut(type="ipd_risk",
                                    ts=ts,
                                    title=_title_for("ipd_risk"),
                                    subtitle=typ,
                                    data={
                                        "assessment_type": typ,
                                        "row": _row(x)
                                    }))

        pain = (db.query(IpdPainAssessment).join(
            IpdAdmission,
            IpdPainAssessment.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdPainAssessment.recorded_at.desc()).limit(2000).all())
        fall = (db.query(IpdFallRiskAssessment).join(
            IpdAdmission, IpdFallRiskAssessment.admission_id == IpdAdmission.id
        ).filter(IpdAdmission.patient_id == patient_id).order_by(
            IpdFallRiskAssessment.recorded_at.desc()).limit(2000).all())
        pres = (db.query(IpdPressureUlcerAssessment).join(
            IpdAdmission,
            IpdPressureUlcerAssessment.admission_id == IpdAdmission.id
        ).filter(IpdAdmission.patient_id == patient_id).order_by(
            IpdPressureUlcerAssessment.recorded_at.desc()).limit(2000).all())
        nutr = (db.query(IpdNutritionAssessment).join(
            IpdAdmission, IpdNutritionAssessment.admission_id == IpdAdmission.
            id).filter(IpdAdmission.patient_id == patient_id).order_by(
                IpdNutritionAssessment.recorded_at.desc()).limit(2000).all())
        gen = (db.query(IpdAssessment).join(
            IpdAdmission,
            IpdAssessment.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdAssessment.assessed_at.desc()).limit(2000).all())

        _emit_many(pain, "Pain Assessment", "recorded_at")
        _emit_many(fall, "Fall Risk", "recorded_at")
        _emit_many(pres, "Pressure Ulcer", "recorded_at")
        _emit_many(nutr, "Nutrition", "recorded_at")
        _emit_many(gen, "Generic Assessment", "assessed_at")

    # --- IPD Medication Orders + MAR ---
    if _want("ipd_med_order", allow) or _want("ipd_mar", allow):
        mos = (db.query(IpdMedicationOrder).join(
            IpdAdmission,
            IpdMedicationOrder.admission_id == IpdAdmission.id).options(
                selectinload(IpdMedicationOrder.administrations)).filter(
                    IpdAdmission.patient_id == patient_id).order_by(
                        IpdMedicationOrder.id.desc()).limit(2000).all())
        if _want("ipd_med_order", allow):
            for mo in mos:
                ts = _safe_dt(
                    getattr(mo, "start_datetime", None)
                    or getattr(mo, "created_at", None))
                if not _in_window(ts, dfrom, dto):
                    continue
                out.append(
                    TimelineItemOut(
                        type="ipd_med_order",
                        ts=ts,
                        title=_title_for("ipd_med_order"),
                        subtitle=f"{mo.drug_name} • {mo.order_status}",
                        status=_map_ui_status(mo.order_status),
                        data={
                            "med_order": _row(mo),
                            "administrations": _rows(mo.administrations)
                        },
                    ))
        if _want("ipd_mar", allow):
            for mo in mos:
                for ad in (mo.administrations or []):
                    ts = _safe_dt(ad.given_datetime or ad.scheduled_datetime)
                    if not _in_window(ts, dfrom, dto):
                        continue
                    out.append(
                        TimelineItemOut(
                            type="ipd_mar",
                            ts=ts,
                            title=_title_for("ipd_mar"),
                            subtitle=f"{mo.drug_name} • {ad.given_status}",
                            status=_map_ui_status(ad.given_status),
                            data={
                                "med_order": _row(mo),
                                "administration": _row(ad)
                            },
                        ))

    # --- IPD IV Fluids ---
    if _want("ipd_iv_fluid", allow):
        ivs = (db.query(IpdIvFluidOrder).join(
            IpdAdmission,
            IpdIvFluidOrder.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdIvFluidOrder.ordered_datetime.desc()).limit(2000).all())
        for iv in ivs:
            ts = _safe_dt(iv.ordered_datetime)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(type="ipd_iv_fluid",
                                ts=ts,
                                title=_title_for("ipd_iv_fluid"),
                                subtitle=iv.fluid,
                                data={"iv_fluid": _row(iv)}))

    # --- OT (Schedule+Case) + OT Orders ---
    if _want("ot", allow):
        ot_schedules = (db.query(OtSchedule).options(
            joinedload(OtSchedule.case),
            joinedload(OtSchedule.surgeon),
            joinedload(OtSchedule.anaesthetist),
            joinedload(OtSchedule.ot_bed),
            joinedload(OtSchedule.admission),
            selectinload(OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure),
        ).filter(OtSchedule.patient_id == patient_id).order_by(
            OtSchedule.date.desc(),
            OtSchedule.planned_start_time.desc()).limit(200).all())

        case_by_schedule = _resolve_ot_cases_for_schedules(db, ot_schedules)
        case_ids = [
            getattr(c, "id", None) for c in case_by_schedule.values() if c
        ]
        case_ids = [x for x in case_ids if x]
        children = _prefetch_ot_children(db, case_ids)

        # dedupe keys for OtOrder vs schedule
        schedule_keys: Set[str] = set()
        for sc in ot_schedules:
            pst = getattr(sc, "planned_start_time", None)
            if pst and hasattr(pst, "replace"):
                pst = pst.replace(second=0, microsecond=0)
            k = f"{sc.patient_id}|{sc.date}|{pst}|{(sc.procedure_name or '').strip().lower()}"
            schedule_keys.add(k)

        for sc in ot_schedules:
            case = case_by_schedule.get(sc.id)
            planned_dt = _planned_dt_from_schedule(sc)

            ts_raw = (getattr(case, "actual_end_time", None) if case else
                      None) or (getattr(case, "actual_start_time", None)
                                if case else None) or planned_dt or getattr(
                                    sc, "created_at", None) or getattr(
                                        sc, "date", None)
            ts = _safe_dt(ts_raw)
            if not _in_window(ts, dfrom, dto):
                continue

            proc_name = (getattr(case, "final_procedure_name", None) if case
                         else None) or getattr(sc, "procedure_name", None)
            surgeon_name = None
            if getattr(sc, "surgeon", None):
                surgeon_name = getattr(sc.surgeon, "full_name",
                                       None) or getattr(
                                           sc.surgeon, "name", None)

            ot_bundle = _ot_case_bundle_row(sc, case, children)

            out.append(
                TimelineItemOut(
                    type="ot",
                    ts=ts,
                    title=_title_for("ot"),
                    subtitle=proc_name or "OT Case",
                    status=_map_ui_status(getattr(sc, "status", None)),
                    doctor_name=surgeon_name,
                    data={
                        "source": "ot_schedule",
                        "ot_case": ot_bundle,
                        "schedule": _row(sc),
                        "case": _row(case) if case else None
                    },
                ))

        if HAS_OT_ORDERS:
            orders = (db.query(OtOrder).options(
                selectinload(OtOrder.attachments)).filter(
                    OtOrder.patient_id == patient_id).order_by(
                        OtOrder.id.desc()).limit(500).all())

            surgeon_ids = {
                o.surgeon_id
                for o in orders if getattr(o, "surgeon_id", None)
            }
            surgeons: Dict[int, User] = {}
            if surgeon_ids:
                for u in db.query(User).filter(User.id.in_(
                        list(surgeon_ids))).all():
                    surgeons[u.id] = u

            for o in orders:
                ts = _safe_dt(o.actual_end or o.actual_start
                              or o.scheduled_start or o.created_at)
                if not _in_window(ts, dfrom, dto):
                    continue

                if o.scheduled_start:
                    k = f"{o.patient_id}|{o.scheduled_start.date()}|{o.scheduled_start.time().replace(second=0, microsecond=0)}|{(o.surgery_name or '').strip().lower()}"
                    if k in schedule_keys:
                        continue

                atts = [
                    AttachmentOut(label=(a.note or "OT Attachment"),
                                  url=a.file_url,
                                  content_type=None,
                                  note=a.note or None,
                                  size_bytes=None)
                    for a in (o.attachments or [])
                ]
                su = surgeons.get(getattr(o, "surgeon_id", None))
                surgeon_name = (getattr(su, "full_name", None)
                                or getattr(su, "name", None)) if su else None

                out.append(
                    TimelineItemOut(
                        type="ot",
                        ts=ts,
                        title=_title_for("ot"),
                        subtitle=f"{o.surgery_name or 'OT'} • {o.status}",
                        status=_map_ui_status(o.status),
                        doctor_name=surgeon_name,
                        attachments=atts if atts else None,
                        data={
                            "source": "ot_order",
                            "ot_order": _row(o),
                            "attachments": _rows(o.attachments)
                        },
                    ))

    # --- Billing ---
    if _want("billing", allow):
        invs = (db.query(Invoice).options(joinedload(
            Invoice.items), joinedload(Invoice.payments)).filter(
                Invoice.patient_id == patient_id).order_by(
                    Invoice.id.desc()).limit(250).all())
        for inv in invs:
            ts = _safe_dt(inv.finalized_at or inv.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="billing",
                    ts=ts,
                    title=_title_for("billing"),
                    subtitle=
                    f"Invoice • {inv.status} • Net ₹{float(inv.net_total or 0):.2f}",
                    status=_map_ui_status(inv.status),
                    data={
                        "invoice": _row(inv),
                        "items": _rows(inv.items),
                        "payments": _rows(inv.payments)
                    },
                ))

    # --- General Attachments ---
    if _want("attachment", allow):
        files = (db.query(FileAttachment).filter(
            FileAttachment.patient_id == patient_id).order_by(
                FileAttachment.uploaded_at.desc()).limit(500).all())
        for f in files:
            ts = _safe_dt(f.uploaded_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="attachment",
                    ts=ts,
                    title=_title_for("attachment"),
                    subtitle=f.filename or (f.note or "Attachment"),
                    attachments=[
                        AttachmentOut(
                            label=f.filename or "file",
                            url=f.public_url or f.stored_path,
                            content_type=f.content_type or None,
                            note=f.note or None,
                            size_bytes=f.size_bytes or None,
                        )
                    ],
                    data={"file": _row(f)},
                ))

    # --- Consents ---
    if _want("consent", allow):
        cons = (db.query(PatientConsent).filter(
            PatientConsent.patient_id == patient_id).order_by(
                PatientConsent.captured_at.desc()).limit(500).all())
        for c in cons:
            ts = _safe_dt(c.captured_at)
            if not _in_window(ts, dfrom, dto):
                continue
            text = c.text or ""
            subtitle = (text[:160] + "…") if len(text) > 160 else text
            out.append(
                TimelineItemOut(type="consent",
                                ts=ts,
                                title=_title_for("consent"),
                                subtitle=subtitle,
                                status=c.type,
                                data={"consent": _row(c)}))

    out.sort(key=lambda x: x.ts, reverse=True)
    return out


# ---------------- FULL OPD HISTORY (4 sections) ----------------
def _build_opd_history(db: Session, patient: Patient,
                       dfrom: Optional[datetime],
                       dto: Optional[datetime]) -> Dict[str, Any]:
    pid = patient.id

    appts = (db.query(Appointment).options(joinedload(
        Appointment.doctor), joinedload(Appointment.department)).filter(
            Appointment.patient_id == pid).order_by(
                Appointment.date.desc(),
                Appointment.slot_start.desc()).limit(2000).all())
    visits = (db.query(Visit).options(
        joinedload(Visit.doctor), joinedload(Visit.department),
        joinedload(
            Visit.appointment)).filter(Visit.patient_id == pid).order_by(
                Visit.visit_at.desc()).limit(2000).all())

    if dfrom or dto:
        appts = [
            a for a in appts
            if _in_window(_safe_dt(a.created_at or a.date), dfrom, dto)
        ]
        visits = [
            v for v in visits if _in_window(_safe_dt(v.visit_at), dfrom, dto)
        ]

    visit_ids = [v.id for v in visits]

    vitals_all = (db.query(OpdVitals).options(joinedload(
        OpdVitals.appointment)).filter(OpdVitals.patient_id == pid).order_by(
            OpdVitals.created_at.desc()).limit(5000).all())
    if dfrom or dto:
        vitals_all = [
            x for x in vitals_all
            if _in_window(_safe_dt(x.created_at), dfrom, dto)
        ]

    vitals_by_appt: Dict[int, List[OpdVitals]] = {}
    if hasattr(OpdVitals, "appointment_id"):
        for vt in vitals_all:
            aid = getattr(vt, "appointment_id", None)
            if aid:
                vitals_by_appt.setdefault(aid, []).append(vt)
        for aid in list(vitals_by_appt.keys()):
            vitals_by_appt[aid].sort(
                key=lambda z: _safe_dt(getattr(z, "created_at", None)),
                reverse=True)

    vitals_by_patient = sorted(
        vitals_all,
        key=lambda z: _safe_dt(getattr(z, "created_at", None)),
        reverse=True)

    rx_by_visit: Dict[int, Any] = {}
    if visit_ids:
        rxs = (db.query(OpdRx).options(selectinload(
            OpdRx.items), joinedload(OpdRx.signer)).filter(
                OpdRx.visit_id.in_(visit_ids)).order_by(OpdRx.id.desc()).all())
        for rx in rxs:
            vid = getattr(rx, "visit_id", None)
            if vid and vid not in rx_by_visit:
                rx_by_visit[vid] = rx

    lab_by_visit: Dict[int, List[Any]] = {}
    rad_by_visit: Dict[int, List[Any]] = {}
    if visit_ids:
        lab_orders = (db.query(LabOrder).options(joinedload(
            LabOrder.test)).filter(LabOrder.visit_id.in_(visit_ids)).order_by(
                LabOrder.id.desc()).all())
        for lo in lab_orders:
            lab_by_visit.setdefault(lo.visit_id, []).append(lo)

        rad_orders = (db.query(RadiologyOrder).options(
            joinedload(RadiologyOrder.test)).filter(
                RadiologyOrder.visit_id.in_(visit_ids)).order_by(
                    RadiologyOrder.id.desc()).all())
        for ro in rad_orders:
            rad_by_visit.setdefault(ro.visit_id, []).append(ro)

    fus = (db.query(FollowUp).options(joinedload(
        FollowUp.source_visit), joinedload(
            FollowUp.appointment)).filter(FollowUp.patient_id == pid).order_by(
                FollowUp.due_date.desc(),
                FollowUp.id.desc()).limit(2000).all())
    if dfrom or dto:
        fus = [
            f for f in fus if _in_window(
                _safe_dt(
                    getattr(f, "updated_at", None) or getattr(
                        f, "created_at", None) or f.due_date), dfrom, dto)
        ]

    fu_by_visit: Dict[int, List[Any]] = {}
    for f in fus:
        sid = getattr(f, "source_visit_id", None)
        if sid:
            fu_by_visit.setdefault(sid, []).append(f)

    visit_by_appt: Dict[int, Visit] = {}
    for v in visits:
        if v.appointment_id and v.appointment_id not in visit_by_appt:
            visit_by_appt[v.appointment_id] = v

    appointment_history = [
        _enrich_row(
            _row(a),
            slot_start_hhmm=_hhmm(getattr(a, "slot_start", None)),
            slot_end_hhmm=_hhmm(getattr(a, "slot_end", None)),
            doctor_name=_user_display_name(getattr(a, "doctor", None)),
            department_name=_dept_display_name(getattr(a, "department", None)),
            linked_visit_id=(visit_by_appt.get(a.id).id
                             if a.id in visit_by_appt else None),
        ) for a in appts
    ]

    vital_history = [
        _enrich_row(
            _row(vt),
            bmi=_bmi(getattr(vt, "height_cm", None),
                     getattr(vt, "weight_kg", None)),
            appointment=_row(getattr(vt, "appointment", None)) if getattr(
                vt, "appointment", None) else None,
        ) for vt in vitals_by_patient
    ]

    visit_history = []
    for v in visits:
        chosen_vitals = _pick_visit_vitals(v, vitals_by_appt,
                                           vitals_by_patient)
        rx = rx_by_visit.get(v.id)

        visit_history.append({
            "visit":
            _enrich_row(
                _row(v),
                doctor_name=_user_display_name(getattr(v, "doctor", None)),
                department_name=_dept_display_name(
                    getattr(v, "department", None)),
            ),
            "appointment":
            _enrich_row(
                _row(getattr(v, "appointment", None)),
                slot_start_hhmm=_hhmm(
                    getattr(getattr(v, "appointment", None), "slot_start",
                            None)),
                slot_end_hhmm=_hhmm(
                    getattr(getattr(v, "appointment", None), "slot_end",
                            None)),
            ) if getattr(v, "appointment", None) else None,
            "case_sheet":
            _visit_case_sheet(v),
            "vitals":
            _row(chosen_vitals) if chosen_vitals else None,
            "bmi":
            _bmi(getattr(chosen_vitals, "height_cm", None),
                 getattr(chosen_vitals, "weight_kg", None))
            if chosen_vitals else None,
            "prescription":
            _row(rx) if rx else None,
            "prescription_items":
            _rows(getattr(rx, "items", None)) if rx else [],
            "lab_orders": [{
                "order":
                _row(x),
                "test":
                _row(getattr(x, "test", None))
                if getattr(x, "test", None) else None
            } for x in (lab_by_visit.get(v.id, []) or [])],
            "radiology_orders": [{
                "order":
                _row(x),
                "test":
                _row(getattr(x, "test", None))
                if getattr(x, "test", None) else None
            } for x in (rad_by_visit.get(v.id, []) or [])],
            "followups":
            _rows(fu_by_visit.get(v.id, [])),
        })

    followup_history = [{
        "followup":
        _row(f),
        "source_visit":
        _row(getattr(f, "source_visit", None)) if getattr(
            f, "source_visit", None) else None,
        "appointment":
        _row(getattr(f, "appointment", None)) if getattr(
            f, "appointment", None) else None,
    } for f in fus]

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "date_from": dfrom.isoformat() if dfrom else None,
        "date_to": dto.isoformat() if dto else None,
        "patient": _patient_brief(patient),
        "opd_history": {
            "appointment_history": appointment_history,
            "vital_history": vital_history,
            "visit_history": visit_history,
            "followup_history": followup_history,
        },
    }


# ---------------- REAL-TIME SNAPSHOT ----------------
@router.get("/realtime")
def emr_realtime(
        patient_id: Optional[int] = Query(None),
        uhid: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])
    if not patient_id and not uhid:
        raise HTTPException(400, "Either patient_id or uhid is required")

    p = db.get(
        Patient,
        int(patient_id)) if patient_id is not None else _patient_by_uhid(
            db, uhid or "")
    if not p:
        raise HTTPException(404, "Patient not found")

    last_opd_v = db.query(OpdVitals).filter(
        OpdVitals.patient_id == p.id).order_by(
            OpdVitals.created_at.desc()).first()

    active_adm = (db.query(IpdAdmission).filter(
        IpdAdmission.patient_id == p.id,
        IpdAdmission.status.in_(["admitted", "transferred"
                                 ])).order_by(IpdAdmission.id.desc()).first())
    last_ipd_v = None
    if active_adm:
        last_ipd_v = db.query(IpdVital).filter(
            IpdVital.admission_id == active_adm.id).order_by(
                IpdVital.recorded_at.desc()).first()

    last_lis_item = (db.query(LisOrderItem).join(
        LisOrder, LisOrderItem.order_id == LisOrder.id).filter(
            LisOrder.patient_id == p.id).order_by(
                LisOrderItem.result_at.desc().nullslast(),
                LisOrderItem.id.desc()).first())

    last_ris = db.query(RisOrder).filter(RisOrder.patient_id == p.id).order_by(
        RisOrder.reported_at.desc().nullslast(), RisOrder.id.desc()).first()

    active_ipd_meds = []
    if active_adm:
        active_ipd_meds = (db.query(IpdMedicationOrder).filter(
            IpdMedicationOrder.admission_id == active_adm.id,
            IpdMedicationOrder.order_status == "active").order_by(
                IpdMedicationOrder.id.desc()).limit(200).all())

    return JSONResponse({
        "patient":
        _patient_brief(p),
        "active_ipd_admission":
        _row(active_adm) if active_adm else None,
        "latest_opd_vitals": ({
            "vitals":
            _row(last_opd_v),
            "bmi":
            _bmi(getattr(last_opd_v, "height_cm", None),
                 getattr(last_opd_v, "weight_kg", None))
            if last_opd_v else None,
        } if last_opd_v else None),
        "latest_ipd_vitals":
        _row(last_ipd_v) if last_ipd_v else None,
        "latest_lab_result_item":
        _row(last_lis_item) if last_lis_item else None,
        "latest_radiology_order":
        _row(last_ris) if last_ris else None,
        "active_ipd_medication_orders":
        _rows(active_ipd_meds),
        "generated_at":
        datetime.utcnow().isoformat(),
    })


# ---------------- API: Patient lookup ----------------
@router.get("/patients/lookup", response_model=PatientLookupOut)
def patient_lookup(
        q: str = Query(..., min_length=1),
        limit: int = Query(12, ge=1, le=50),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["patients.view", "emr.view"])
    qlike = f"%{q.strip()}%"
    rows = (db.query(Patient).filter(
        or_(
            Patient.uhid.ilike(qlike),
            Patient.first_name.ilike(qlike),
            Patient.last_name.ilike(qlike),
            Patient.phone.ilike(qlike),
            Patient.email.ilike(qlike),
        )).order_by(Patient.id.desc()).limit(limit).all())
    results: List[PatientMiniOut] = []
    for p in rows:
        name = " ".join([x for x in [p.first_name, p.last_name] if x
                         ]).strip() or p.first_name or ""
        results.append(
            PatientMiniOut(id=p.id,
                           uhid=p.uhid,
                           abha_number=p.abha_number,
                           name=name,
                           gender=p.gender,
                           dob=p.dob,
                           phone=p.phone))
    return PatientLookupOut(results=results)


# ---------------- API: Timeline ----------------
@router.get("/timeline", response_model=List[TimelineItemOut])
def emr_timeline(
        patient_id: Optional[int] = Query(None),
        uhid: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
        types:
    Optional[str] = Query(
        None,
        description=
        ("comma-separated: "
         "opd_appointment,opd_visit,opd_vitals,rx,opd_lab_order,opd_radiology_order,followup,"
         "lab,radiology,pharmacy_rx,pharmacy,"
         "ipd_admission,ipd_transfer,ipd_discharge,ipd_vitals,ipd_nursing_note,ipd_intake_output,"
         "ipd_round,ipd_progress,ipd_risk,ipd_med_order,ipd_mar,ipd_iv_fluid,ot,billing,attachment,consent"
         ),
    ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])
    if not patient_id and not uhid:
        raise HTTPException(400, "Either patient_id or uhid is required")

    if not patient_id and uhid:
        p = _patient_by_uhid(db, uhid)
        if not p:
            raise HTTPException(404, "Patient not found for given UHID")
        patient_id = p.id

    dfrom, dto = _date_window(date_from, date_to)
    allow: Optional[Set[str]] = None
    if types:
        allow = {t.strip() for t in types.split(",") if t.strip()}
    return _build_timeline(db, int(patient_id), dfrom, dto, allow)


# ---------------- API: OPD History (FULL) ----------------
@router.get("/opd/history")
def emr_opd_history(
        patient_id: Optional[int] = Query(None),
        uhid: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])
    if not patient_id and not uhid:
        raise HTTPException(400, "Either patient_id or uhid is required")

    p = db.get(
        Patient,
        int(patient_id)) if patient_id is not None else _patient_by_uhid(
            db, uhid or "")
    if not p:
        raise HTTPException(404, "Patient not found")

    dfrom, dto = _date_window(date_from, date_to)
    data = _build_opd_history(db, p, dfrom, dto)
    return JSONResponse(data)


# ---------------- API: OPD PDF (Preview + Download) ----------------
@router.get("/opd/history/pdf")
def emr_opd_history_pdf(
        patient_id: Optional[int] = Query(None),
        uhid: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
        consent_required: Optional[str] = Query("1"),
        preview: Optional[str] = Query(
            "1", description="1=inline preview, 0=download"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])

    p = db.get(
        Patient,
        int(patient_id)) if patient_id is not None else _patient_by_uhid(
            db, uhid or "")
    if not p:
        raise HTTPException(404, "Patient not found")

    need_consent = _is_truthy(consent_required)
    if need_consent:
        has_consent = db.query(PatientConsent).filter(
            PatientConsent.patient_id == p.id).first()
        if not has_consent:
            raise HTTPException(
                status_code=412,
                detail="Active consent is required to export EMR.")

    dfrom, dto = _date_window(date_from, date_to)

    # Build OPD-only timeline items (for PDF generator)
    allow_types = {
        "opd_appointment", "opd_visit", "opd_vitals", "rx", "opd_lab_order",
        "opd_radiology_order", "followup"
    }
    items = [
        x.dict()
        for x in _build_timeline(db, p.id, dfrom, dto, allow=allow_types)
    ]

    branding = get_ui_branding(db)
    pdf_bytes = _call_emr_pdf_generator(
        patient=_patient_brief(p),
        items=items,
        sections_selected={
            "opd", "vitals", "prescriptions", "lab", "radiology"
        },  # generator decides how to render
        letterhead_bytes=None,
        branding=branding,
    )

    filename = f"OPD_HISTORY_{p.uhid or p.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    disp = "inline" if _is_truthy(preview) else "attachment"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disp}; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------- API: Export EMR PDF (multipart) ----------------
@router.post("/export/pdf")
async def export_emr_pdf(
        patient_id: Optional[int] = Form(None),
        uhid: Optional[str] = Form(None),
        date_from: Optional[str] = Form(None),
        date_to: Optional[str] = Form(None),
        sections: Optional[str] = Form(None),
        consent_required: Optional[str] = Form("1"),
        preview: Optional[str] = Form("0"),  # ✅ 1=inline preview, 0=download
        letterhead: Optional[UploadFile] = File(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])

    p = db.get(
        Patient,
        int(patient_id)) if patient_id is not None else _patient_by_uhid(
            db, uhid or "")
    if not p:
        raise HTTPException(404, "Patient not found")

    need_consent = _is_truthy(consent_required)
    if need_consent:
        has_consent = db.query(PatientConsent).filter(
            PatientConsent.patient_id == p.id).first()
        if not has_consent:
            raise HTTPException(
                status_code=412,
                detail="Active consent is required to export EMR.")

    dfrom, dto = _date_window(date_from, date_to)
    items = [
        x.dict() for x in _build_timeline(db, p.id, dfrom, dto, allow=None)
    ]

    sections_selected: Optional[Set[str]] = None
    if sections:
        sections_selected = {
            s.strip()
            for s in sections.split(",") if s.strip()
        }

    letter_bytes = await letterhead.read() if letterhead is not None else None
    branding = get_ui_branding(db)

    pdf_bytes = _call_emr_pdf_generator(
        patient=_patient_brief(p),
        items=items,
        sections_selected=sections_selected,
        letterhead_bytes=letter_bytes,
        branding=branding,
    )

    filename = f"EMR_{p.uhid or p.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    disp = "inline" if _is_truthy(preview) else "attachment"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disp}; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------- API: Export EMR PDF (JSON body) ----------------
@router.post("/export/pdf-json")
def export_emr_pdf_json(
        payload: EmrExportRequest = Body(...),
        preview: Optional[str] = Query(
            "0", description="1=inline preview, 0=download"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])

    p = db.get(Patient, int(payload.patient_id)
               ) if payload.patient_id is not None else _patient_by_uhid(
                   db, payload.uhid or "")
    if not p:
        raise HTTPException(404, "Patient not found")

    if payload.consent_required:
        has_consent = db.query(PatientConsent).filter(
            PatientConsent.patient_id == p.id).first()
        if not has_consent:
            raise HTTPException(
                status_code=412,
                detail="Active consent is required to export EMR.")

    dfrom = payload.date_from and datetime(
        payload.date_from.year, payload.date_from.month, payload.date_from.day)
    dto = payload.date_to and (datetime(
        payload.date_to.year, payload.date_to.month, payload.date_to.day) +
                               timedelta(hours=23, minutes=59, seconds=59))

    items = [
        x.dict() for x in _build_timeline(db, p.id, dfrom, dto, allow=None)
    ]

    allow_sections: Optional[Set[str]] = None
    secs = payload.sections
    if secs is not None:
        allow_sections = {
            k
            for k, v in {
                "opd": secs.opd,
                "ipd": secs.ipd,
                "vitals": secs.vitals,
                "prescriptions": secs.prescriptions,
                "lab": secs.lab,
                "radiology": secs.radiology,
                "pharmacy": secs.pharmacy,
                "ot": secs.ot,
                "billing": secs.billing,
                "attachments": secs.attachments,
                "consents": secs.consents,
            }.items() if v
        }

    branding = get_ui_branding(db)

    pdf_bytes = _call_emr_pdf_generator(
        patient=_patient_brief(p),
        items=items,
        sections_selected=allow_sections,
        letterhead_bytes=None,
        branding=branding,
    )

    filename = f"EMR_{p.uhid or p.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    disp = "inline" if _is_truthy(preview) else "attachment"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disp}; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------- OT Case PDF (Download + Preview) ----------------
def _call_ot_case_pdf_generator(db: Session, *, case_id: int,
                                schedule_id: Optional[int]) -> bytes:
    """
    Calls app/services/ot_case_pdf.py generator safely, even if the function name/signature varies.
    """
    mod = importlib.import_module("app.services.ot_case_pdf")

    fn = None
    for name in (
            "build_ot_case_pdf",
            "generate_ot_case_pdf",
            "render_ot_case_pdf",
            "make_ot_case_pdf",
            "create_ot_case_pdf",
    ):
        if hasattr(mod, name):
            fn = getattr(mod, name)
            break
    if fn is None:
        raise HTTPException(
            500, "OT PDF generator not found in app.services.ot_case_pdf")

    sig = pyinspect.signature(fn)
    params = sig.parameters

    kwargs: Dict[str, Any] = {}
    if "db" in params:
        kwargs["db"] = db

    if "case_id" in params:
        kwargs["case_id"] = case_id
    elif "ot_case_id" in params:
        kwargs["ot_case_id"] = case_id
    elif "schedule_id" in params:
        if not schedule_id:
            raise HTTPException(400,
                                "schedule_id not available for this OT case")
        kwargs["schedule_id"] = schedule_id
    elif "id" in params:
        kwargs["id"] = case_id

    try:
        out = fn(**kwargs) if kwargs else fn()
    except TypeError:
        try:
            out = fn(db, case_id)
        except Exception:
            out = fn(case_id)

    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if hasattr(out, "getvalue"):
        return out.getvalue()
    raise HTTPException(500, "OT PDF generator returned unsupported type")


@router.get("/ot/cases/{case_id}/pdf")
def emr_ot_case_pdf(
        case_id: int,
        preview: Optional[str] = Query(
            "0", description="1=inline preview, 0=download"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view", "ot.view", "ot.cases.view"])

    case = db.query(OtCase).filter(OtCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "OT case not found")

    schedule_id = getattr(case, "schedule_id", None)
    pdf_bytes = _call_ot_case_pdf_generator(db,
                                            case_id=case_id,
                                            schedule_id=schedule_id)

    filename = f"OT_CASE_{case_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    disp = "inline" if _is_truthy(preview) else "attachment"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disp}; filename="{filename}"',
            "Cache-Control": "no-store"
        },
    )


# ---------------- API: Minimal FHIR Bundle ----------------
@router.get("/fhir/{patient_id}", response_model=FhirBundleOut)
def emr_fhir_bundle(
        patient_id: int,
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])
    p = db.get(Patient, int(patient_id))
    if not p:
        raise HTTPException(404, "Patient not found")

    dfrom, dto = _date_window(date_from, date_to)
    items = _build_timeline(db, p.id, dfrom, dto, allow=None)

    def entry(resource):
        return {"resource": resource}

    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": datetime.utcnow().isoformat(),
        "entry": [],
    }

    name = [{
        "use": "official",
        "text": " ".join([x for x in [p.first_name, p.last_name] if x])
    }]
    identifiers = [{"system": "urn:uhid", "value": p.uhid}]
    if p.abha_number:
        identifiers.append({
            "system": "https://healthid.ndhm.gov.in",
            "value": p.abha_number
        })

    patient_res = {
        "resourceType": "Patient",
        "id": f"patient-{p.id}",
        "identifier": identifiers,
        "name": name,
        "gender": (p.gender or "").lower() or None,
        "birthDate": p.dob.isoformat() if getattr(p, "dob", None) else None,
        "telecom": [{
            "system": "phone",
            "value": p.phone
        }] if p.phone else [],
    }
    bundle["entry"].append(entry(patient_res))

    for it in items:
        t = it.type
        ts = it.ts.isoformat()
        if t in {"opd_visit", "ipd_admission"}:
            enc = {
                "resourceType": "Encounter",
                "status": "finished" if t == "opd_visit" else "in-progress",
                "class": {
                    "code": "AMB" if t == "opd_visit" else "IMP"
                },
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "period": {
                    "start": ts
                },
            }
            bundle["entry"].append(entry(enc))
        if t in {"opd_vitals", "ipd_vitals"}:
            obs = {
                "resourceType": "Observation",
                "status": "final",
                "code": {
                    "text": "Vital signs"
                },
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "effectiveDateTime": ts,
                "note": [{
                    "text": it.subtitle
                }],
            }
            bundle["entry"].append(entry(obs))
        if t in {"rx", "pharmacy_rx"}:
            mr = {
                "resourceType": "MedicationRequest",
                "status": "active" if it.status != "cancelled" else "stopped",
                "intent": "order",
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "authoredOn": ts,
                "note": [{
                    "text": it.subtitle
                }],
            }
            bundle["entry"].append(entry(mr))
        if t in {"lab", "radiology"}:
            dr = {
                "resourceType":
                "DiagnosticReport",
                "status":
                "final" if (it.status in {
                    "reported", "approved", "completed", "dispensed"
                }) else "partial",
                "code": {
                    "text": "Lab Test" if t == "lab" else "Radiology"
                },
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "effectiveDateTime":
                ts,
                "conclusion":
                it.subtitle,
            }
            bundle["entry"].append(entry(dr))
        if t == "pharmacy":
            md = {
                "resourceType": "MedicationDispense",
                "status": "completed",
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "whenHandedOver": ts,
                "note": [{
                    "text": it.subtitle
                }],
            }
            bundle["entry"].append(entry(md))
        if t == "billing":
            inv = {
                "resourceType": "Invoice",
                "status": "issued",
                "subject": {
                    "reference": f"Patient/{patient_res['id']}"
                },
                "date": ts,
                "note": [{
                    "text": it.subtitle
                }],
            }
            bundle["entry"].append(entry(inv))

    return JSONResponse(bundle)
