# FILE: app/api/routes_emr.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from decimal import Decimal
from io import BytesIO
from typing import List, Optional, Set, Dict, Any, Tuple
import importlib
import inspect as pyinspect
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    File,
    Form,
    Body,
)
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.encoders import jsonable_encoder

from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_, func
from sqlalchemy.inspection import inspect as sa_inspect

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient, PatientConsent, PatientAddress, PatientDocument
from app.models.common import FileAttachment
from app.models.department import Department
from app.models.opd import (
    Visit,
    Vitals as OpdVitals,
    Prescription as OpdRx,
    PrescriptionItem as OpdRxItem,
    Appointment,
    LabOrder,
    RadiologyOrder,
    FollowUp,
)

from app.models.lis import (
    LisOrder,
    LisOrderItem,
    LisAttachment,
    LisResultLine,
)
from app.models.ris import RisOrder, RisAttachment

from app.models.ipd import (
    IpdAdmission,
    IpdTransfer,
    IpdDischargeSummary,
    IpdDischargeChecklist,
    IpdDischargeMedication,
    IpdBed,
    IpdBedAssignment,

    # Nursing / clinical
    IpdNursingNote,
    IpdShiftHandover,
    IpdVital,
    IpdIntakeOutput,
    IpdRound,
    IpdProgressNote,

    # Risk / assessments
    IpdPainAssessment,
    IpdFallRiskAssessment,
    IpdPressureUlcerAssessment,
    IpdNutritionAssessment,
    IpdAssessment,

    # Orders / drugs
    IpdOrder,
    IpdMedication,
    IpdMedicationOrder,
    IpdMedicationAdministration,
    IpdDrugChartMeta,
    IpdIvFluidOrder,
    IpdDrugChartNurseRow,
    IpdDrugChartDoctorAuth,

    # Procedures / ICU
    IpdDressingRecord,
    IpdBloodTransfusion,
    IpdRestraintRecord,
    IpdIsolationPrecaution,
    IcuFlowSheet,

    # Feedback / referrals / IPD OT
    IpdFeedback,
    IpdAdmissionFeedback,
    IpdReferral,
    IpdOtCase,
    IpdAnaesthesiaRecord,

    # Combined dressing/transfusion log
    IpdDressingTransfusion,
)

from app.models.ot import (
    OtSchedule,
    OtCase,
    OtScheduleProcedure,
    OtProcedure,

    # OT case-linked clinical records
    PreAnaesthesiaEvaluation,
    PreOpChecklist,
    SurgicalSafetyChecklist,
    AnaesthesiaRecord,
    AnaesthesiaVitalLog,
    AnaesthesiaDrugLog,
    OtNursingRecord,
    OtSpongeInstrumentCount,
    OtImplantRecord,
    OperationNote,
    OtBloodTransfusionRecord,
    PacuRecord,
    OtCleaningLog,
    OtEnvironmentLog,
    OtEquipmentDailyChecklist,
)

from app.models.billing import Invoice, InvoiceItem, Payment

HAS_PHARMACY = False
try:
    from app.models.pharmacy_prescription import (
        PharmacyPrescription,
        PharmacyPrescriptionLine,
        PharmacySale,
        PharmacySaleItem,
    )
    HAS_PHARMACY = True
except Exception:
    HAS_PHARMACY = False

# ✅ NEW: OT Orders (your "OtOrder" module) support
HAS_OT_ORDERS = False
try:
    # You said your OT module uses: from app.models.ot import OtOrder, OtAttachment
    from app.models.ot import OtOrder, OtAttachment  # type: ignore
    HAS_OT_ORDERS = True
except Exception:
    HAS_OT_ORDERS = False

from app.schemas.emr import (
    TimelineItemOut,
    AttachmentOut,
    PatientMiniOut,
    PatientLookupOut,
    EmrExportRequest,
    FhirBundleOut,
    TimelineType,
    TimelineFilterIn,
)

from app.services.pdf_emr import generate_emr_pdf
from app.services.ui_branding import get_ui_branding  # branding helper

router = APIRouter()


# ---------------- RBAC ----------------
def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


# =========================
# ✅ OT bundle helpers (PDF-aligned)
# =========================


def _resolve_ot_cases_for_schedules(
        db: Session,
        schedules: list[OtSchedule]) -> Dict[int, Optional[OtCase]]:
    """
    Returns {schedule_id: OtCase or None}
    Robust even if schedule.case_id is NULL or relationship not populated.
    """
    if not schedules:
        return {}

    schedule_ids = [s.id for s in schedules if getattr(s, "id", None)]
    # 1) Prefer relationship-loaded case
    by_schedule: Dict[int, OtCase] = {}
    for s in schedules:
        c = getattr(s, "case", None)
        if c is not None:
            by_schedule[s.id] = c

    # 2) Fallback: schedule.case_id -> OtCase.id
    case_ids = [getattr(s, "case_id", None) for s in schedules]
    case_ids = [x for x in case_ids if x]
    by_id: Dict[int, OtCase] = {}
    if case_ids:
        rows = db.query(OtCase).filter(OtCase.id.in_(case_ids)).all()
        by_id = {c.id: c for c in rows}
        for s in schedules:
            if s.id in by_schedule:
                continue
            cid = getattr(s, "case_id", None)
            if cid and cid in by_id:
                by_schedule[s.id] = by_id[cid]

    # 3) Fallback: OtCase.schedule_id -> schedule.id  (CRITICAL)
    missing_schedule_ids = [
        sid for sid in schedule_ids if sid not in by_schedule
    ]
    if missing_schedule_ids and hasattr(OtCase, "schedule_id"):
        rows = db.query(OtCase).filter(
            OtCase.schedule_id.in_(missing_schedule_ids)).all()
        for c in rows:
            sid = getattr(c, "schedule_id", None)
            if sid:
                by_schedule[sid] = c

    out: Dict[int, Optional[OtCase]] = {}
    for s in schedules:
        out[s.id] = by_schedule.get(s.id)
    return out


def _prefetch_ot_children(
    db: Session,
    case_ids: list[int],
) -> Dict[str, Any]:
    """
    Bulk prefetch child records for many OT case_ids.
    Returns mapping dicts keyed by case_id, plus vitals/drugs keyed by anaesthesia_record_id.
    """
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

    # Anaesthesia logs are linked by record_id (NOT case_id)
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


def _ot_case_bundle_row(
    sc: OtSchedule,
    case: Optional[OtCase],
    children: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Returns one OT bundle object aligned with your ot_case_pdf.py expectations.
    Includes backward-compatible aliases too.
    """
    schedule_procs = [{
        "link":
        _row(x),
        "procedure":
        _row(x.procedure) if getattr(x, "procedure", None) else None,
    } for x in (getattr(sc, "procedures", None) or [])]

    base = {
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

    # ✅ PDF-aligned names + aliases
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


# ---------------- helpers ----------------
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
    return (not allow) or (typ in allow)


# --- 핵심: "DON'T MISS ANY FIELD" serializer (all table columns) ---
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


def _pick_ts(obj, fields: Tuple[str, ...]) -> datetime:
    for f in fields:
        v = getattr(obj, f, None)
        if v:
            return _safe_dt(v)
    return datetime.utcnow()


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


# ---------------- core timeline ----------------
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
        for v in visits:
            ts = _safe_dt(v.visit_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="opd_visit",
                    ts=ts,
                    title=_title_for("opd_visit"),
                    subtitle=v.chief_complaint or v.symptoms or "Consultation",
                    doctor_name=getattr(v.doctor, "name", None),
                    department_name=getattr(v.department, "name", None),
                    status=None,
                    data={
                        "visit":
                        _row(v),
                        "appointment":
                        _row(v.appointment) if v.appointment else None,
                    },
                ))

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

    # --- OPD Lab Orders (ordered stage) ---
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
                        "visit": _row(lo.visit) if lo.visit else None,
                    },
                ))

    # --- OPD Radiology Orders (ordered stage) ---
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
                        "visit": _row(ro.visit) if ro.visit else None,
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
            subtitle = " • ".join([
                x for x in [
                    f"BP {v.bp_systolic}/{v.bp_diastolic}"
                    if v.bp_systolic and v.bp_diastolic else None,
                    f"T {_as_float(v.temp_c)}°C" if v.
                    temp_c is not None else None,
                    f"Pulse {v.pulse}/min" if v.pulse else None,
                    f"SpO₂ {v.spo2}%" if v.spo2 else None,
                    f"RR {v.rr}/min" if v.rr else None,
                ] if x
            ]) or "Vitals"
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
                        if v.recorder else None,
                    },
                ))

    # --- IPD Nursing Notes (includes linked_vital) ---
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
        ios = (db.query(IpdIntakeOutput).join(
            IpdAdmission,
            IpdIntakeOutput.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdIntakeOutput.recorded_at.desc()).limit(2000).all())
        for io in ios:
            ts = _safe_dt(io.recorded_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_intake_output",
                    ts=ts,
                    title=_title_for("ipd_intake_output"),
                    subtitle=
                    f"Intake {io.intake_ml} ml • Urine {io.urine_ml} ml",
                    status=None,
                    data={"intake_output": _row(io)},
                ))

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
                TimelineItemOut(
                    type="ipd_round",
                    ts=ts,
                    title=_title_for("ipd_round"),
                    subtitle="Doctor round",
                    data={"round": _row(r)},
                ))

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
                TimelineItemOut(
                    type="ipd_progress",
                    ts=ts,
                    title=_title_for("ipd_progress"),
                    subtitle="Progress note",
                    data={"progress_note": _row(pn)},
                ))

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
                    TimelineItemOut(
                        type="ipd_risk",
                        ts=ts,
                        title=_title_for("ipd_risk"),
                        subtitle=typ,
                        data={
                            "assessment_type": typ,
                            "row": _row(x)
                        },
                    ))

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
                TimelineItemOut(
                    type="ipd_iv_fluid",
                    ts=ts,
                    title=_title_for("ipd_iv_fluid"),
                    subtitle=iv.fluid,
                    data={"iv_fluid": _row(iv)},
                ))

    # =========================
    # ✅ OT (Schedule+Case) + ✅ OT Orders (your new module)
    # =========================
    # =========================
    # =========================
    # ✅ OT (Schedule+Case) + ✅ OT Orders (your new module)
    # =========================
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

        # ✅ robust case resolve (even if schedule.case_id missing)
        case_by_schedule = _resolve_ot_cases_for_schedules(db, ot_schedules)
        case_ids = [
            getattr(c, "id", None) for c in case_by_schedule.values() if c
        ]
        case_ids = [x for x in case_ids if x]
        children = _prefetch_ot_children(db, case_ids)

        # ✅ dedupe keys for OtOrder vs schedule
        schedule_keys: Set[str] = set()
        for sc in ot_schedules:
            pst = getattr(sc, "planned_start_time", None)
            if pst and hasattr(pst, "replace"):
                pst = pst.replace(second=0, microsecond=0)
            k = f"{sc.patient_id}|{sc.date}|{pst}|{(sc.procedure_name or '').strip().lower()}"
            schedule_keys.add(k)

        # 1) OT schedule items
        for sc in ot_schedules:
            case = case_by_schedule.get(sc.id)
            planned_dt = _planned_dt_from_schedule(sc)

            ts_raw = (
                (getattr(case, "actual_end_time", None) if case else None)
                or (getattr(case, "actual_start_time", None) if case else None)
                or planned_dt or getattr(sc, "created_at", None)
                or getattr(sc, "date", None))
            ts = _safe_dt(ts_raw)
            if not _in_window(ts, dfrom, dto):
                continue

            proc_name = (
                (getattr(case, "final_procedure_name", None) if case else None)
                or getattr(sc, "procedure_name", None))
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
                        "ot_case": ot_bundle,  # ✅ for UI/PDF
                        "schedule": _row(sc),  # backward compatible
                        "case": _row(case) if case else None,
                    },
                ))

        # 2) ALSO load OtOrder (new OT module) and show it in EMR
        if HAS_OT_ORDERS:
            orders = (db.query(OtOrder).options(
                selectinload(OtOrder.attachments)).filter(
                    OtOrder.patient_id == patient_id).order_by(
                        OtOrder.id.desc()).limit(500).all())

            # surgeon name map (best effort)
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

                # dedupe against schedules if same planned slot
                if o.scheduled_start:
                    k = (
                        f"{o.patient_id}|{o.scheduled_start.date()}|"
                        f"{o.scheduled_start.time().replace(second=0, microsecond=0)}|"
                        f"{(o.surgery_name or '').strip().lower()}")
                    if k in schedule_keys:
                        continue

                atts = [
                    AttachmentOut(
                        label=(a.note or "OT Attachment"),
                        url=a.file_url,
                        content_type=None,
                        note=a.note or None,
                        size_bytes=None,
                    ) for a in (o.attachments or [])
                ]

                su = surgeons.get(getattr(o, "surgeon_id", None))
                surgeon_name = None
                if su:
                    surgeon_name = getattr(su, "full_name", None) or getattr(
                        su, "name", None)

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
                            "attachments": _rows(o.attachments),
                        },
                    ))

    # --- Billing (Invoice lines + payments) ---
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
                TimelineItemOut(
                    type="consent",
                    ts=ts,
                    title=_title_for("consent"),
                    subtitle=subtitle,
                    status=c.type,
                    data={"consent": _row(c)},
                ))

    out.sort(key=lambda x: x.ts, reverse=True)
    return out


# ---------------- FULL HISTORY (ALL SECTIONS, ALL FIELDS) ----------------
def _build_full_history(
    db: Session,
    patient: Patient,
    dfrom: Optional[datetime],
    dto: Optional[datetime],
    include: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Returns a single JSON object containing:
    - Patient demographics (all columns) + addresses + documents + consents
    - OPD: appointments, visits, vitals, prescriptions+items, followups, OPD orders
    - LIS: orders, items, attachments, result_lines
    - RIS: orders, attachments
    - Pharmacy: prescriptions+lines, sales+items (if enabled)
    - IPD: admissions + ALL linked tables you shared
    - OT: schedules + cases + ALL OT child tables you shared
    - ✅ OT Orders (OtOrder) + its attachments (NEW)
    - Billing: invoices + items + payments
    - Attachments: FileAttachment
    """

    def allow(sec: str) -> bool:
        return (include is None) or (sec in include)

    pid = patient.id
    payload: Dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat(),
        "date_from": dfrom.isoformat() if dfrom else None,
        "date_to": dto.isoformat() if dto else None,
        "patient": {
            "core": _row(patient),
            "addresses": _rows(getattr(patient, "addresses", None)),
            "documents": _rows(getattr(patient, "documents", None)),
            "consents": _rows(getattr(patient, "consents", None)),
        },
    }

    # -------- OPD --------
    if allow("opd"):
        appts = (db.query(Appointment).filter(
            Appointment.patient_id == pid).order_by(
                Appointment.date.desc(), Appointment.slot_start.desc()).all())
        visits = db.query(Visit).filter(Visit.patient_id == pid).order_by(
            Visit.visit_at.desc()).all()
        vitals = db.query(OpdVitals).filter(
            OpdVitals.patient_id == pid).order_by(
                OpdVitals.created_at.desc()).all()

        rxs = (db.query(OpdRx).options(selectinload(
            OpdRx.items), joinedload(OpdRx.visit)).join(
                Visit, OpdRx.visit_id == Visit.id).filter(
                    Visit.patient_id == pid).order_by(OpdRx.id.desc()).all())

        lab_orders = (db.query(LabOrder).join(
            Visit, LabOrder.visit_id == Visit.id).filter(
                Visit.patient_id == pid).order_by(
                    LabOrder.ordered_at.desc()).all())
        rad_orders = (db.query(RadiologyOrder).join(
            Visit, RadiologyOrder.visit_id == Visit.id).filter(
                Visit.patient_id == pid).order_by(
                    RadiologyOrder.ordered_at.desc()).all())
        followups = (db.query(FollowUp).filter(
            FollowUp.patient_id == pid).order_by(FollowUp.due_date.desc(),
                                                 FollowUp.id.desc()).all())

        if dfrom or dto:
            appts = [
                x for x in appts
                if _in_window(_safe_dt(x.created_at or x.date), dfrom, dto)
            ]
            visits = [
                x for x in visits
                if _in_window(_safe_dt(x.visit_at), dfrom, dto)
            ]
            vitals = [
                x for x in vitals
                if _in_window(_safe_dt(x.created_at), dfrom, dto)
            ]
            rxs = [
                x for x in rxs if _in_window(
                    _safe_dt(x.signed_at or (x.visit.visit_at if x.visit else
                                             None) or datetime.utcnow()),
                    dfrom,
                    dto,
                )
            ]
            lab_orders = [
                x for x in lab_orders
                if _in_window(_safe_dt(x.ordered_at), dfrom, dto)
            ]
            rad_orders = [
                x for x in rad_orders
                if _in_window(_safe_dt(x.ordered_at), dfrom, dto)
            ]
            followups = [
                x for x in followups if _in_window(
                    _safe_dt(
                        getattr(x, "updated_at", None)
                        or getattr(x, "created_at", None) or x.due_date),
                    dfrom,
                    dto,
                )
            ]

        payload["opd"] = {
            "appointments":
            _rows(appts),
            "visits":
            _rows(visits),
            "vitals":
            _rows(vitals),
            "prescriptions": [{
                "prescription": _row(rx),
                "items": _rows(rx.items)
            } for rx in rxs],
            "opd_lab_orders":
            _rows(lab_orders),
            "opd_radiology_orders":
            _rows(rad_orders),
            "followups":
            _rows(followups),
        }

    # -------- LIS --------
    if allow("lis"):
        lis_orders = (db.query(LisOrder).options(
            selectinload(LisOrder.items).selectinload(
                LisOrderItem.attachments)).filter(
                    LisOrder.patient_id == pid).order_by(
                        LisOrder.id.desc()).all())
        order_ids = [o.id for o in lis_orders]
        res_lines = []
        if order_ids:
            res_lines = (db.query(LisResultLine).filter(
                LisResultLine.order_id.in_(order_ids)).order_by(
                    LisResultLine.id.asc()).all())

        res_lines_by_order: Dict[int, List[LisResultLine]] = {}
        for rl in res_lines:
            res_lines_by_order.setdefault(rl.order_id, []).append(rl)

        if dfrom or dto:
            lis_orders = [
                o for o in lis_orders if _in_window(
                    _safe_dt(o.reported_at or o.created_at), dfrom, dto)
            ]

        payload["lis"] = {
            "orders": [{
                "order":
                _row(o),
                "items": [{
                    "item": _row(it),
                    "attachments": _rows(it.attachments)
                } for it in (o.items or [])],
                "result_lines":
                _rows(res_lines_by_order.get(o.id, [])),
            } for o in lis_orders]
        }

    # -------- RIS --------
    if allow("ris"):
        ris_orders = (db.query(RisOrder).options(
            selectinload(RisOrder.attachments)).filter(
                RisOrder.patient_id == pid).order_by(RisOrder.id.desc()).all())
        if dfrom or dto:
            ris_orders = [
                o for o in ris_orders if _in_window(
                    _safe_dt(o.reported_at or o.scanned_at or o.created_at),
                    dfrom, dto)
            ]

        payload["ris"] = {
            "orders": [{
                "order": _row(o),
                "attachments": _rows(o.attachments)
            } for o in ris_orders]
        }

    # -------- Pharmacy --------
    if allow("pharmacy") and HAS_PHARMACY:
        prxs = (db.query(PharmacyPrescription).options(
            selectinload(PharmacyPrescription.lines)).filter(
                PharmacyPrescription.patient_id == pid).order_by(
                    PharmacyPrescription.id.desc()).all())
        sales = db.query(PharmacySale).filter(
            PharmacySale.patient_id == pid).order_by(
                PharmacySale.id.desc()).all()

        sale_ids = [s.id for s in sales]
        sale_items = []
        if sale_ids:
            sale_items = db.query(PharmacySaleItem).filter(
                PharmacySaleItem.sale_id.in_(sale_ids)).all()
        items_by_sale: Dict[int, List[PharmacySaleItem]] = {}
        for it in sale_items:
            items_by_sale.setdefault(it.sale_id, []).append(it)

        if dfrom or dto:
            prxs = [
                x for x in prxs if _in_window(
                    _safe_dt(x.signed_at or x.created_at), dfrom, dto)
            ]
            sales = [
                x for x in sales if _in_window(
                    _safe_dt(
                        getattr(x, "created_at", None)
                        or getattr(x, "bill_datetime", None)), dfrom, dto)
            ]

        payload["pharmacy"] = {
            "prescriptions": [{
                "prescription": _row(p),
                "lines": _rows(p.lines)
            } for p in prxs],
            "sales": [{
                "sale": _row(s),
                "items": _rows(items_by_sale.get(s.id, []))
            } for s in sales],
        }

    # -------- IPD (ALL your models) --------
    if allow("ipd"):
        adms = db.query(IpdAdmission).filter(
            IpdAdmission.patient_id == pid).order_by(
                IpdAdmission.id.desc()).all()
        adm_ids = [a.id for a in adms]

        def by_adm(model, dt_field: str, order_desc: bool = True):
            if not adm_ids:
                return {}
            q = db.query(model).filter(
                getattr(model, "admission_id").in_(adm_ids))
            if hasattr(model, dt_field):
                q = q.order_by(
                    getattr(model, dt_field).desc(
                    ) if order_desc else getattr(model, dt_field).asc())
            rows = q.all()
            if dfrom or dto:
                rows = [
                    x for x in rows if _in_window(
                        _safe_dt(
                            getattr(x, dt_field, None) or getattr(
                                x, "created_at", None) or datetime.utcnow()),
                        dfrom,
                        dto,
                    )
                ]
            mp: Dict[int, List[Any]] = {}
            for r in rows:
                mp.setdefault(getattr(r, "admission_id"), []).append(r)
            return mp

        transfers_by = by_adm(IpdTransfer, "transferred_at")
        vitals_by = by_adm(IpdVital, "recorded_at")
        notes_by = by_adm(IpdNursingNote, "entry_time")
        handovers_by = by_adm(IpdShiftHandover, "created_at")
        io_by = by_adm(IpdIntakeOutput, "recorded_at")
        rounds_by = by_adm(IpdRound, "created_at")
        progress_by = by_adm(IpdProgressNote, "created_at")

        pain_by = by_adm(IpdPainAssessment, "recorded_at")
        fall_by = by_adm(IpdFallRiskAssessment, "recorded_at")
        pressure_by = by_adm(IpdPressureUlcerAssessment, "recorded_at")
        nutr_by = by_adm(IpdNutritionAssessment, "recorded_at")
        generic_assess_by = by_adm(IpdAssessment, "assessed_at")

        orders_by = by_adm(IpdOrder, "ordered_at")
        meds_simple_by = by_adm(IpdMedication, "created_at")
        med_orders_by = by_adm(IpdMedicationOrder, "start_datetime")
        mar_by = by_adm(IpdMedicationAdministration, "scheduled_datetime")
        drug_meta_by = by_adm(IpdDrugChartMeta, "updated_at")
        iv_by = by_adm(IpdIvFluidOrder, "ordered_datetime")
        nurse_rows_by = by_adm(IpdDrugChartNurseRow, "id", order_desc=False)
        doctor_auth_by = by_adm(IpdDrugChartDoctorAuth, "created_at")

        dressing_by = by_adm(IpdDressingRecord, "date_time")
        blood_by = by_adm(IpdBloodTransfusion, "start_time")
        restraint_by = by_adm(IpdRestraintRecord, "start_time")
        isolation_by = by_adm(IpdIsolationPrecaution, "start_date")
        icu_by = by_adm(IcuFlowSheet, "recorded_at")

        feedback_entries_by = by_adm(IpdFeedback, "collected_at")
        referrals_by = by_adm(IpdReferral, "created_at")
        ipd_ot_by = by_adm(IpdOtCase, "scheduled_start")

        summaries = []
        checklists = []
        dis_meds = []
        if adm_ids:
            summaries = db.query(IpdDischargeSummary).filter(
                IpdDischargeSummary.admission_id.in_(adm_ids)).all()
            checklists = db.query(IpdDischargeChecklist).filter(
                IpdDischargeChecklist.admission_id.in_(adm_ids)).all()
            dis_meds = db.query(IpdDischargeMedication).filter(
                IpdDischargeMedication.admission_id.in_(adm_ids)).all()

        summary_by: Dict[int, Any] = {s.admission_id: s for s in summaries}
        checklist_by: Dict[int, Any] = {c.admission_id: c for c in checklists}
        meds_by_adm: Dict[int, List[Any]] = {}
        for m in dis_meds:
            meds_by_adm.setdefault(m.admission_id, []).append(m)

        adm_feedback = []
        if adm_ids:
            adm_feedback = db.query(IpdAdmissionFeedback).filter(
                IpdAdmissionFeedback.admission_id.in_(adm_ids)).all()
        adm_feedback_by: Dict[int, Any] = {
            x.admission_id: x
            for x in adm_feedback
        }

        bed_ids = [a.current_bed_id for a in adms if a.current_bed_id]
        beds = {}
        if bed_ids:
            beds = {
                b.id: b
                for b in db.query(IpdBed).filter(IpdBed.id.in_(bed_ids)).all()
            }

        payload["ipd"] = {
            "admissions": [{
                "admission":
                _row(a),
                "current_bed":
                _row(beds.get(a.current_bed_id)) if a.current_bed_id else None,
                "transfers":
                _rows(transfers_by.get(a.id, [])),
                "vitals":
                _rows(vitals_by.get(a.id, [])),
                "nursing_notes":
                _rows(notes_by.get(a.id, [])),
                "shift_handovers":
                _rows(handovers_by.get(a.id, [])),
                "intake_outputs":
                _rows(io_by.get(a.id, [])),
                "rounds":
                _rows(rounds_by.get(a.id, [])),
                "progress_notes":
                _rows(progress_by.get(a.id, [])),
                "pain_assessments":
                _rows(pain_by.get(a.id, [])),
                "fall_risk_assessments":
                _rows(fall_by.get(a.id, [])),
                "pressure_ulcer_assessments":
                _rows(pressure_by.get(a.id, [])),
                "nutrition_assessments":
                _rows(nutr_by.get(a.id, [])),
                "assessments":
                _rows(generic_assess_by.get(a.id, [])),
                "orders":
                _rows(orders_by.get(a.id, [])),
                "medications_simple":
                _rows(meds_simple_by.get(a.id, [])),
                "medication_orders":
                _rows(med_orders_by.get(a.id, [])),
                "medication_administrations":
                _rows(mar_by.get(a.id, [])),
                "drug_chart_meta":
                _rows(drug_meta_by.get(a.id, [])),
                "iv_fluid_orders":
                _rows(iv_by.get(a.id, [])),
                "drug_chart_nurse_rows":
                _rows(nurse_rows_by.get(a.id, [])),
                "drug_chart_doctor_auth_rows":
                _rows(doctor_auth_by.get(a.id, [])),
                "dressing_records":
                _rows(dressing_by.get(a.id, [])),
                "blood_transfusions":
                _rows(blood_by.get(a.id, [])),
                "restraints":
                _rows(restraint_by.get(a.id, [])),
                "isolations":
                _rows(isolation_by.get(a.id, [])),
                "icu_flows":
                _rows(icu_by.get(a.id, [])),
                "feedback_entries":
                _rows(feedback_entries_by.get(a.id, [])),
                "feedback_summary":
                _row(adm_feedback_by.get(a.id))
                if adm_feedback_by.get(a.id) else None,
                "referrals":
                _rows(referrals_by.get(a.id, [])),
                "ipd_ot_cases":
                _rows(ipd_ot_by.get(a.id, [])),
                "discharge_summary":
                _row(summary_by.get(a.id)) if summary_by.get(a.id) else None,
                "discharge_checklist":
                _row(checklist_by.get(a.id))
                if checklist_by.get(a.id) else None,
                "discharge_medications":
                _rows(meds_by_adm.get(a.id, [])),
            } for a in adms if (not (dfrom or dto))
                           or _in_window(_safe_dt(a.admitted_at), dfrom, dto)]
        }

    # -------- OT (Theatre) detailed + ✅ OtOrder list --------
    # -------- OT (Theatre) detailed + ✅ OtOrder list --------

    # -------- OT (Theatre) detailed + ✅ OtOrder list --------
    if allow("ot"):
        schedules = (db.query(OtSchedule).options(
            selectinload(OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure),
            joinedload(OtSchedule.case),
            joinedload(OtSchedule.ot_bed),
            joinedload(OtSchedule.surgeon),
            joinedload(OtSchedule.anaesthetist),
            joinedload(OtSchedule.admission),
        ).filter(OtSchedule.patient_id == pid).order_by(
            OtSchedule.date.desc(),
            OtSchedule.planned_start_time.desc()).all())

        # ✅ robust case mapping
        case_by_schedule = _resolve_ot_cases_for_schedules(db, schedules)
        case_ids = [
            getattr(c, "id", None) for c in case_by_schedule.values() if c
        ]
        case_ids = [x for x in case_ids if x]
        children = _prefetch_ot_children(db, case_ids)

        # filter schedules by window
        filtered: list[OtSchedule] = []
        for s in schedules:
            c = case_by_schedule.get(s.id)
            planned_dt = _planned_dt_from_schedule(s)
            ts_raw = ((getattr(c, "actual_end_time", None) if c else None)
                      or (getattr(c, "actual_start_time", None) if c else None)
                      or planned_dt or getattr(s, "created_at", None)
                      or getattr(s, "date", None))
            ts = _safe_dt(ts_raw)
            if (not (dfrom or dto)) or _in_window(ts, dfrom, dto):
                filtered.append(s)

        eq_chk = (db.query(OtEquipmentDailyChecklist).order_by(
            OtEquipmentDailyChecklist.date.desc(),
            OtEquipmentDailyChecklist.id.desc()).limit(5000).all())
        env_logs = (db.query(OtEnvironmentLog).order_by(
            OtEnvironmentLog.date.desc(),
            OtEnvironmentLog.time.desc()).limit(5000).all())
        if dfrom or dto:
            eq_chk = [
                x for x in eq_chk if _in_window(_safe_dt(x.date), dfrom, dto)
            ]
            env_logs = [
                x for x in env_logs if _in_window(_safe_dt(x.date), dfrom, dto)
            ]

        ot_payload: Dict[str, Any] = {
            "schedules": [
                _ot_case_bundle_row(s, case_by_schedule.get(s.id), children)
                for s in filtered
            ],
            "equipment_daily_checklists":
            _rows(eq_chk),
            "environment_logs":
            _rows(env_logs),
        }

        # ✅ OtOrder inclusion
        if HAS_OT_ORDERS:
            orders = (db.query(OtOrder).options(
                selectinload(OtOrder.attachments)).filter(
                    OtOrder.patient_id == pid).order_by(
                        OtOrder.id.desc()).all())
            if dfrom or dto:
                orders = [
                    o for o in orders if _in_window(
                        _safe_dt(o.actual_end or o.actual_start or o.
                                 scheduled_start or o.created_at), dfrom, dto)
                ]
            ot_payload["orders"] = [{
                "order": _row(o),
                "attachments": _rows(o.attachments)
            } for o in orders]

        payload["ot"] = ot_payload

    # -------- Billing --------
    if allow("billing"):
        invs = (db.query(Invoice).options(selectinload(
            Invoice.items), selectinload(Invoice.payments)).filter(
                Invoice.patient_id == pid).order_by(Invoice.id.desc()).all())
        if dfrom or dto:
            invs = [
                x for x in invs if _in_window(
                    _safe_dt(x.finalized_at or x.created_at), dfrom, dto)
            ]
        payload["billing"] = {
            "invoices": [{
                "invoice": _row(inv),
                "items": _rows(inv.items),
                "payments": _rows(inv.payments)
            } for inv in invs]
        }

    # -------- Attachments --------
    if allow("attachments"):
        files = db.query(FileAttachment).filter(
            FileAttachment.patient_id == pid).order_by(
                FileAttachment.uploaded_at.desc()).all()
        if dfrom or dto:
            files = [
                x for x in files
                if _in_window(_safe_dt(x.uploaded_at), dfrom, dto)
            ]
        payload["attachments"] = {"files": _rows(files)}

    # -------- Consents --------
    if allow("consents"):
        cons = db.query(PatientConsent).filter(
            PatientConsent.patient_id == pid).order_by(
                PatientConsent.captured_at.desc()).all()
        if dfrom or dto:
            cons = [
                x for x in cons
                if _in_window(_safe_dt(x.captured_at), dfrom, dto)
            ]
        payload["consents"] = {"consents": _rows(cons)}

    return payload


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

    p = None
    if patient_id is not None:
        p = db.get(Patient, int(patient_id))
    elif uhid:
        p = _patient_by_uhid(db, uhid)
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
        last_ipd_v = (db.query(IpdVital).filter(
            IpdVital.admission_id == active_adm.id).order_by(
                IpdVital.recorded_at.desc()).first())

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
            PatientMiniOut(
                id=p.id,
                uhid=p.uhid,
                abha_number=p.abha_number,
                name=name,
                gender=p.gender,
                dob=p.dob,
                phone=p.phone,
            ))
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
    items = _build_timeline(db, int(patient_id), dfrom, dto, allow)
    return items


# ---------------- API: FULL HISTORY ----------------
@router.get("/history")
def emr_history(
        patient_id: Optional[int] = Query(None),
        uhid: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
        include:
    Optional[str] = Query(
        None,
        description=
        "comma-separated sections: patient,opd,lis,ris,pharmacy,ipd,ot,billing,attachments,consents",
    ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])
    if not patient_id and not uhid:
        raise HTTPException(400, "Either patient_id or uhid is required")

    p = None
    if patient_id is not None:
        p = (db.query(Patient).options(
            selectinload(Patient.addresses), selectinload(Patient.documents),
            selectinload(Patient.consents)).filter(
                Patient.id == int(patient_id)).first())
    elif uhid:
        p = (db.query(Patient).options(selectinload(Patient.addresses),
                                       selectinload(Patient.documents),
                                       selectinload(Patient.consents)).filter(
                                           Patient.uhid == uhid).first())
    if not p:
        raise HTTPException(404, "Patient not found")

    dfrom, dto = _date_window(date_from, date_to)
    include_set: Optional[Set[str]] = None
    if include:
        include_set = {x.strip() for x in include.split(",") if x.strip()}

    data = _build_full_history(db, p, dfrom, dto, include_set)
    return JSONResponse(data)


def _ot_schedule_query_for_patient(db: Session, pid: int):
    adm_ids = [
        x[0] for x in db.query(IpdAdmission.id).filter(
            IpdAdmission.patient_id == pid).all()
    ]

    q = db.query(OtSchedule).options(
        joinedload(OtSchedule.case),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.ot_bed),
        joinedload(OtSchedule.admission),
        selectinload(OtSchedule.procedures).joinedload(
            OtScheduleProcedure.procedure),
    )

    conds = [OtSchedule.patient_id == pid]
    if adm_ids:
        conds.append(OtSchedule.admission_id.in_(adm_ids))

    return q.filter(or_(*conds))


def _call_ot_case_pdf_generator(db: Session, *, case_id: int,
                                schedule_id: Optional[int]) -> bytes:
    """
    Calls your app/services/ot_case_pdf.py generator safely, even if the function name/signature varies.
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

    # Build kwargs based on signature
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

    # Try keyword call
    try:
        out = fn(**kwargs) if kwargs else fn()
    except TypeError:
        # Try positional fallback: (db, case_id) or (case_id)
        try:
            out = fn(db, case_id)
        except Exception:
            out = fn(case_id)

    # Normalize to bytes
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if hasattr(out, "getvalue"):
        return out.getvalue()
    raise HTTPException(500, "OT PDF generator returned unsupported type")


@router.get("/ot/cases/{case_id}/pdf")
def emr_download_ot_case_pdf(
        case_id: int,
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

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- API: Export PDF (multipart) ----------------
@router.post("/export/pdf")
async def export_emr_pdf(
        patient_id: Optional[int] = Form(None),
        uhid: Optional[str] = Form(None),
        date_from: Optional[str] = Form(None),
        date_to: Optional[str] = Form(None),
        sections: Optional[str] = Form(None),
        consent_required: Optional[str] = Form("1"),
        letterhead: Optional[UploadFile] = File(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])

    p = None
    if patient_id is not None:
        p = db.get(Patient, int(patient_id))
    elif uhid:
        p = _patient_by_uhid(db, uhid)
    if not p:
        raise HTTPException(404, "Patient not found")

    need_consent = str(consent_required
                       or "1").strip().lower() in {"1", "true", "yes", "on"}
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

    sections_selected = None
    if sections:
        sections_selected = {
            s.strip()
            for s in sections.split(",") if s.strip()
        }

    letter_bytes = await letterhead.read() if letterhead is not None else None
    branding = get_ui_branding(db)

    pdf_bytes = generate_emr_pdf(
        patient=_patient_brief(p),
        items=items,
        sections_selected=sections_selected,
        letterhead_bytes=letter_bytes,
        branding=branding,
    )
    filename = f"EMR_{p.uhid or p.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- API: Export PDF (JSON body) ----------------
@router.post("/export/pdf-json")
def export_emr_pdf_json(
        payload: EmrExportRequest = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "patients.view"])

    p = None
    if payload.patient_id is not None:
        p = db.get(Patient, int(payload.patient_id))
    elif payload.uhid:
        p = _patient_by_uhid(db, payload.uhid)
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

    secs = payload.sections
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

    pdf_bytes = generate_emr_pdf(
        patient=_patient_brief(p),
        items=items,
        sections_selected=allow_sections,
        letterhead_bytes=None,
        branding=branding,
    )

    filename = f"EMR_{p.uhid or p.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
