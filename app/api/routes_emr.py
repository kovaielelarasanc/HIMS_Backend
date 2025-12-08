# FILE: app/api/routes_emr.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from decimal import Decimal
from io import BytesIO
from typing import List, Optional, Set, Dict

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
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient, PatientConsent
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
)
from app.models.lis import LisOrder, LisOrderItem, LisAttachment
from app.models.ris import RisOrder, RisAttachment
from app.models.ipd import (
    IpdAdmission,
    IpdTransfer,
    IpdDischargeSummary,
    IpdBed,
    IpdBedAssignment,
)
from app.models.ot import OtSchedule, OtCase
from app.models.billing import Invoice, InvoiceItem, Payment

# Pharmacy (optional, NEW models)
HAS_PHARMACY = False
try:
    # ✅ use new pharmacy_prescription models
    from app.models.pharmacy_prescription import PharmacySale, PharmacySaleItem

    HAS_PHARMACY = True
except Exception:
    HAS_PHARMACY = False

from app.schemas.emr import (
    TimelineItemOut,
    AttachmentOut,
    PatientMiniOut,
    PatientLookupOut,
    EmrExportRequest,
    FhirBundleOut,
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


def _title_for(t: str) -> str:
    return {
        "opd_visit": "OPD Visit",
        "opd_vitals": "Vitals",
        "rx": "Prescription",
        "lab": "Lab Test / Result",
        "radiology": "Radiology",
        "pharmacy": "Pharmacy Dispense",
        "ipd_admission": "IPD Admission",
        "ipd_transfer": "IPD Transfer",
        "ipd_discharge": "IPD Discharge",
        "ot": "OT Case",
        "billing": "Invoice",
        "attachment": "Attachment",
        "consent": "Consent",
    }.get(t, "Event")


def _want(typ: str, allow: Optional[Set[str]]) -> bool:
    return (not allow) or (typ in allow)


# ---------------- core query ----------------
def _build_timeline(
    db: Session,
    patient_id: int,
    dfrom: Optional[datetime],
    dto: Optional[datetime],
    allow: Optional[Set[str]],
) -> list[TimelineItemOut]:
    out: list[TimelineItemOut] = []

    # --- OPD Visits (full SOAP + Episode + Appointment) ---
    if _want("opd_visit", allow):
        visits = (db.query(Visit).options(
            joinedload(Visit.doctor),
            joinedload(Visit.department),
            joinedload(Visit.appointment),
        ).filter(Visit.patient_id == patient_id).order_by(
            Visit.visit_at.desc()).limit(500).all())
        for v in visits:
            ts = _safe_dt(v.visit_at)
            if not _in_window(ts, dfrom, dto):
                continue
            appt = v.appointment
            slot = None
            if appt:
                slot = {
                    "date":
                    appt.date.isoformat() if appt.date else None,
                    "slot_start":
                    appt.slot_start.isoformat() if appt.slot_start else None,
                    "slot_end":
                    appt.slot_end.isoformat() if appt.slot_end else None,
                    "purpose":
                    appt.purpose,
                }
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
                        "episode_id": v.episode_id,
                        "visit_at": v.visit_at,
                        "chief_complaint": v.chief_complaint,
                        "symptoms": v.symptoms,
                        "subjective": v.soap_subjective,
                        "objective": v.soap_objective,
                        "assessment": v.soap_assessment,
                        "plan": v.plan,
                        "appointment": slot,
                    },
                ))

    # --- OPD Vitals (full metrics + BMI + appointment linkage) ---
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
            appt = vt.appointment
            appt_data = None
            if appt:
                appt_data = {
                    "date":
                    appt.date.isoformat() if appt.date else None,
                    "slot_start":
                    appt.slot_start.isoformat() if appt.slot_start else None,
                    "slot_end":
                    appt.slot_end.isoformat() if appt.slot_end else None,
                }
            out.append(
                TimelineItemOut(
                    type="opd_vitals",
                    ts=ts,
                    title=_title_for("opd_vitals"),
                    subtitle="  ·  ".join(chips)
                    if chips else "Vitals recorded",
                    data={
                        "recorded_at": vt.created_at,
                        "height_cm": _as_float(vt.height_cm),
                        "weight_kg": _as_float(vt.weight_kg),
                        "bmi": _bmi(vt.height_cm, vt.weight_kg),
                        "bp_systolic": vt.bp_systolic,
                        "bp_diastolic": vt.bp_diastolic,
                        "pulse": vt.pulse,
                        "rr": vt.rr,
                        "temp_c": _as_float(vt.temp_c),
                        "spo2": vt.spo2,
                        "notes": vt.notes,
                        "appointment": appt_data,
                    },
                ))

    # --- OPD Prescriptions (items + signer) ---
    if _want("rx", allow):
        rxs = (db.query(OpdRx).options(
            joinedload(OpdRx.visit).joinedload(Visit.doctor),
            joinedload(OpdRx.items),
            joinedload(OpdRx.signer),
        ).join(Visit, OpdRx.visit_id == Visit.id).filter(
            Visit.patient_id == patient_id).order_by(
                OpdRx.id.desc()).limit(500).all())
        for rx in rxs:
            ts = rx.signed_at or (rx.visit.visit_at
                                  if rx.visit else None) or rx.visit.created_at
            ts = _safe_dt(ts)
            if not _in_window(ts, dfrom, dto):
                continue
            items = []
            for it in rx.items or []:
                items.append({
                    "drug_name":
                    it.drug_name,
                    "strength":
                    it.strength,
                    "frequency":
                    it.frequency,
                    "duration_days":
                    it.duration_days,
                    "quantity":
                    it.quantity,
                    "unit_price":
                    _as_float(it.unit_price),
                    "line_total":
                    (_as_float(it.unit_price) or 0) * (it.quantity or 0),
                })
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
                    ref_kind="opd_visit",
                    ref_display="Prescription",
                    data={
                        "notes": rx.notes,
                        "signed_at": rx.signed_at,
                        "signed_by": rx.signer.name if rx.signer else None,
                        "items": items,
                    },
                ))

    # --- LIS (each item + attachments, ranges, specimen, critical) ---
    if _want("lab", allow):
        lis_orders = (db.query(LisOrder).options(
            joinedload(LisOrder.items).joinedload(
                LisOrderItem.attachments)).filter(
                    LisOrder.patient_id == patient_id).order_by(
                        LisOrder.id.desc()).limit(250).all())
        for lo in lis_orders:
            for it in (lo.items or []):
                ts = it.result_at or lo.reported_at or lo.created_at
                ts = _safe_dt(ts)
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
                        ref_kind="lab_test",
                        ref_display=f"{it.test_name}",
                        attachments=atts,
                        data={
                            "order_id": lo.id,
                            "priority": lo.priority,
                            "collected_at": lo.collected_at,
                            "reported_at": lo.reported_at,
                            "item": {
                                "test_id": it.test_id,
                                "test_name": it.test_name,
                                "test_code": it.test_code,
                                "unit": it.unit,
                                "normal_range": it.normal_range,
                                "specimen_type": it.specimen_type,
                                "status": it.status,
                                "result_value": it.result_value,
                                "is_critical": it.is_critical,
                                "result_at": it.result_at,
                            },
                        },
                    ))

    # --- RIS (modality + report text + signoff + attachments) ---
    if _want("radiology", allow):
        ris = (db.query(RisOrder).options(joinedload(
            RisOrder.attachments)).filter(
                RisOrder.patient_id == patient_id).order_by(
                    RisOrder.id.desc()).limit(250).all())
        for ro in ris:
            ts = ro.reported_at or ro.scanned_at or ro.created_at
            ts = _safe_dt(ts)
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
                    ref_kind="radiology_test",
                    ref_display=ro.test_name,
                    attachments=atts,
                    data={
                        "test_id": ro.test_id,
                        "test_name": ro.test_name,
                        "test_code": ro.test_code,
                        "modality": ro.modality,
                        "status": ro.status,
                        "scheduled_at": ro.scheduled_at,
                        "scanned_at": ro.scanned_at,
                        "reported_at": ro.reported_at,
                        "report_text": ro.report_text,
                        "approved_at": ro.approved_at,
                        "primary_signoff_by": ro.primary_signoff_by,
                        "secondary_signoff_by": ro.secondary_signoff_by,
                    },
                ))

    # --- Pharmacy sale (NEW models: items list with qty & amounts) ---
    if HAS_PHARMACY and _want("pharmacy", allow):
        sales = (
            db.query(PharmacySale).filter(
                PharmacySale.patient_id == patient_id,
                PharmacySale.invoice_status
                != "CANCELLED",  # skip cancelled bills
            ).order_by(PharmacySale.id.desc()).limit(250).all())

        sale_ids = [s.id for s in sales]
        items_by_sale: Dict[int, List[PharmacySaleItem]] = {}
        if sale_ids:
            items = (db.query(PharmacySaleItem).filter(
                PharmacySaleItem.sale_id.in_(sale_ids)).all())
            for it in items:
                items_by_sale.setdefault(it.sale_id, []).append(it)

        for s in sales:
            ts = _safe_dt(s.created_at)
            if not _in_window(ts, dfrom, dto):
                continue

            line_items = []
            for it in items_by_sale.get(s.id, []):
                line_items.append({
                    # snapshot name/code from sale item itself
                    "item_name":
                    getattr(it, "item_name", None),
                    "item_code":
                    getattr(it, "item_code", None),
                    "quantity":
                    getattr(it, "quantity", None),
                    "unit_price":
                    _as_float(getattr(it, "unit_price", None)),
                    "discount_percent":
                    _as_float(getattr(it, "discount_percent", None)),
                    "tax_percent":
                    _as_float(getattr(it, "tax_percent", None)),
                    "total_amount":
                    _as_float(getattr(it, "total_amount", None)),
                })

            total_amount = _as_float(
                getattr(s, "net_amount", None)
                or getattr(s, "total_amount", None) or 0) or 0.0

            status_raw = (getattr(s, "status", "") or "").lower()
            status_ui = _map_ui_status(status_raw)

            out.append(
                TimelineItemOut(
                    type="pharmacy",
                    ts=ts,
                    title=_title_for("pharmacy"),
                    subtitle=f"Dispense • Net ₹{float(total_amount):.2f}",
                    status=status_ui or "completed",
                    ref_kind="pharmacy_sale",
                    ref_display="Pharmacy dispense",
                    data={
                        "sale_id":
                        s.id,
                        "status":
                        getattr(s, "status", None),
                        "context_type":
                        getattr(s, "context_type", None),
                        "visit_id":
                        getattr(s, "visit_id", None),
                        "admission_id":
                        getattr(s, "admission_id", None),
                        "location_id":
                        getattr(s, "location_id", None),
                        "payment_mode":
                        getattr(s, "payment_mode", None),
                        "gross_amount":
                        _as_float(getattr(s, "gross_amount", None)),
                        "discount_amount":
                        _as_float(getattr(s, "discount_amount", None)),
                        "tax_amount":
                        _as_float(getattr(s, "tax_amount", None)),
                        "net_amount":
                        _as_float(getattr(s, "net_amount", None))
                        or total_amount,
                        "items":
                        line_items,
                    },
                ))

    # --- IPD Admission / Transfer / Discharge (full details) ---
    if _want("ipd_admission", allow):
        adms = (db.query(IpdAdmission).filter(
            IpdAdmission.patient_id == patient_id).order_by(
                IpdAdmission.id.desc()).limit(200).all())
        # current bed code lookup
        bed_ids = [a.current_bed_id for a in adms if a.current_bed_id]
        beds = {}
        if bed_ids:
            rows = db.query(IpdBed).filter(IpdBed.id.in_(bed_ids)).all()
            beds = {b.id: b.code for b in rows}
        for a in adms:
            ts = _safe_dt(a.admitted_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_admission",
                    ts=ts,
                    title=_title_for("ipd_admission"),
                    subtitle=f"Admission {a.display_code}",
                    status=_map_ui_status(a.status),
                    ref_kind="ipd_admission",
                    ref_display=a.display_code,
                    data={
                        "admission_code": a.display_code,
                        "department_id": a.department_id,
                        "practitioner_user_id": a.practitioner_user_id,
                        "primary_nurse_user_id": a.primary_nurse_user_id,
                        "admission_type": a.admission_type,
                        "admitted_at": a.admitted_at,
                        "expected_discharge_at": a.expected_discharge_at,
                        "package_id": a.package_id,
                        "payor_type": a.payor_type,
                        "insurer_name": a.insurer_name,
                        "policy_number": a.policy_number,
                        "preliminary_diagnosis": a.preliminary_diagnosis,
                        "history": a.history,
                        "care_plan": a.care_plan,
                        "current_bed_id": a.current_bed_id,
                        "current_bed_code": beds.get(a.current_bed_id),
                        "status": a.status,
                    },
                ))

    if _want("ipd_transfer", allow):
        trs = (db.query(IpdTransfer).filter(
            IpdTransfer.admission_id.in_(
                db.query(IpdAdmission.id).filter(
                    IpdAdmission.patient_id == patient_id))).order_by(
                        IpdTransfer.id.desc()).limit(300).all())
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
                    ref_kind="ipd_transfer",
                    ref_display="Bed transfer",
                    data={
                        "admission_id": t.admission_id,
                        "from_bed_id": t.from_bed_id,
                        "to_bed_id": t.to_bed_id,
                        "reason": t.reason,
                        "requested_by": t.requested_by,
                        "approved_by": t.approved_by,
                        "transferred_at": t.transferred_at,
                    },
                ))

    if _want("ipd_discharge", allow):
        ds = (db.query(IpdDischargeSummary).join(
            IpdAdmission,
            IpdDischargeSummary.admission_id == IpdAdmission.id).filter(
                IpdAdmission.patient_id == patient_id).order_by(
                    IpdDischargeSummary.id.desc()).limit(200).all())
        for d in ds:
            ts = _safe_dt(d.finalized_at or d.created_at)
            if not _in_window(ts, dfrom, dto):
                continue
            out.append(
                TimelineItemOut(
                    type="ipd_discharge",
                    ts=ts,
                    title=_title_for("ipd_discharge"),
                    subtitle="Discharge Summary",
                    status="completed" if d.finalized else "new",
                    ref_kind="ipd_discharge",
                    ref_display="Discharge summary",
                    data={
                        "admission_id": d.admission_id,
                        "finalized": d.finalized,
                        "finalized_by": d.finalized_by,
                        "finalized_at": d.finalized_at,
                        "demographics": d.demographics,
                        "medical_history": d.medical_history,
                        "treatment_summary": d.treatment_summary,
                        "medications": d.medications,
                        "follow_up": d.follow_up,
                        "icd10_codes": d.icd10_codes,
                    },
                ))

    # --- OT Case ---
    # --- OT (Schedule + Case) ---
    if _want("ot", allow):
        # New OT is bed-based; we use schedules and join to case
        ot_schedules = (db.query(OtSchedule).options(
            joinedload(OtSchedule.case),
            joinedload(OtSchedule.surgeon),
            joinedload(OtSchedule.anaesthetist),
            joinedload(OtSchedule.bed),
        ).filter(OtSchedule.patient_id == patient_id).order_by(
            OtSchedule.date.desc(),
            OtSchedule.planned_start_time.desc(),
        ).limit(200).all())

        for oc in ot_schedules:
            case = oc.case

            # planned datetime (combine date + time)
            planned_dt = None
            if oc.date and oc.planned_start_time:
                planned_dt = datetime(
                    oc.date.year,
                    oc.date.month,
                    oc.date.day,
                    oc.planned_start_time.hour,
                    oc.planned_start_time.minute,
                    oc.planned_start_time.second if hasattr(
                        oc.planned_start_time, "second") else 0,
                )

            ts_raw = ((case.actual_end_time if case else None)
                      or (case.actual_start_time if case else None)
                      or planned_dt or oc.created_at)
            ts = _safe_dt(ts_raw)
            if not _in_window(ts, dfrom, dto):
                continue

            # Procedure name preference: final_procedure_name > schedule.procedure_name
            proc_name = (case.final_procedure_name if case
                         and case.final_procedure_name else oc.procedure_name)
            subtitle = proc_name or "OT Case"

            # Surgeon name (supports full_name hybrid if present)
            surgeon_name = None
            if oc.surgeon is not None:
                surgeon_name = (getattr(oc.surgeon, "full_name", None)
                                or getattr(oc.surgeon, "name", None)
                                or getattr(oc.surgeon, "first_name", None))

            bed_code = oc.bed.code if oc.bed is not None else None

            out.append(
                TimelineItemOut(
                    type="ot",
                    ts=ts,
                    title=_title_for("ot"),
                    subtitle=subtitle,
                    status=_map_ui_status(oc.status),
                    doctor_name=surgeon_name,
                    ref_kind="ot_case",
                    ref_display=subtitle,
                    attachments=[],  # no dedicated OT attachments model yet
                    data={
                        # identifiers
                        "schedule_id":
                        oc.id,
                        "case_id":
                        case.id if case else None,
                        "patient_id":
                        oc.patient_id,
                        "admission_id":
                        oc.admission_id,

                        # location
                        "bed_id":
                        oc.bed_id,
                        "bed_code":
                        bed_code,

                        # timing
                        "date":
                        oc.date,
                        "planned_start_time":
                        oc.planned_start_time,
                        "planned_end_time":
                        oc.planned_end_time,
                        "actual_start_time":
                        case.actual_start_time if case else None,
                        "actual_end_time":
                        case.actual_end_time if case else None,

                        # status / priority
                        "status":
                        oc.status,
                        "priority":
                        oc.priority,

                        # clinical summary
                        "procedure_name":
                        proc_name,
                        "final_procedure_name":
                        case.final_procedure_name if case else None,
                        "preop_diagnosis":
                        case.preop_diagnosis if case else None,
                        "postop_diagnosis":
                        case.postop_diagnosis if case else None,
                        "outcome":
                        case.outcome if case else None,
                        "icu_required":
                        case.icu_required if case else None,

                        # surgeon / anaesthetist ids
                        "surgeon_user_id":
                        oc.surgeon_user_id,
                        "anaesthetist_user_id":
                        oc.anaesthetist_user_id,
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
            items = []
            for li in (inv.items or []):
                items.append({
                    "service_type": li.service_type,
                    "service_ref_id": li.service_ref_id,
                    "description": li.description,
                    "quantity": li.quantity,
                    "unit_price": _as_float(li.unit_price),
                    "tax_rate": _as_float(li.tax_rate),
                    "tax_amount": _as_float(li.tax_amount),
                    "line_total": _as_float(li.line_total),
                    "is_voided": li.is_voided,
                    "void_reason": li.void_reason,
                    "voided_by": li.voided_by,
                    "voided_at": li.voided_at,
                })
            pays = []
            for p in (inv.payments or []):
                pays.append({
                    "amount": _as_float(p.amount),
                    "mode": p.mode,
                    "reference_no": p.reference_no,
                    "paid_at": p.paid_at,
                })
            out.append(
                TimelineItemOut(
                    type="billing",
                    ts=ts,
                    title=_title_for("billing"),
                    subtitle=
                    f"Invoice • {inv.status} • Net ₹{float(inv.net_total or 0):.2f}",
                    status=_map_ui_status(inv.status),
                    ref_kind="invoice",
                    ref_display="Invoice",
                    data={
                        "invoice_id": inv.id,
                        "status": inv.status,
                        "gross_total": _as_float(inv.gross_total),
                        "tax_total": _as_float(inv.tax_total),
                        "net_total": _as_float(inv.net_total),
                        "amount_paid": _as_float(inv.amount_paid),
                        "balance_due": _as_float(inv.balance_due),
                        "finalized_at": inv.finalized_at,
                        "items": items,
                        "payments": pays,
                    },
                ))

    # --- General Attachments ---
    if _want("attachment", allow):
        files = (db.query(FileAttachment).filter(
            FileAttachment.patient_id == patient_id).order_by(
                FileAttachment.uploaded_at.desc()).limit(250).all())
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
                    data={
                        "filename": f.filename,
                        "content_type": f.content_type,
                        "note": f.note,
                        "size_bytes": f.size_bytes,
                    },
                ))

    # --- Consents ---
    if _want("consent", allow):
        cons = (db.query(PatientConsent).filter(
            PatientConsent.patient_id == patient_id).order_by(
                PatientConsent.captured_at.desc()).limit(200).all())
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
                    data={
                        "type": c.type,
                        "text": c.text,
                        "captured_at": c.captured_at,
                    },
                ))

    out.sort(key=lambda x: x.ts, reverse=True)
    return out


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
         "opd_visit,opd_vitals,rx,lab,radiology,pharmacy,"
         "ipd_admission,ipd_transfer,ipd_discharge,ot,billing,attachment,consent"
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
        p = db.query(Patient).get(int(patient_id))
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
                detail="Active consent is required to export EMR.",
            )

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

    # load UiBranding and pass into PDF generator
    branding = get_ui_branding(db)  # may be None

    pdf_bytes = generate_emr_pdf(
        patient=_patient_brief(p),
        items=items,
        sections_selected=sections_selected,
        letterhead_bytes=
        letter_bytes,  # still supported (fallback/header override)
        branding=branding,  # branding-based header/footer
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
        p = db.query(Patient).get(int(payload.patient_id))
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
                detail="Active consent is required to export EMR.",
            )

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

    # load UiBranding for JSON-based export as well
    branding = get_ui_branding(db)  # may be None

    pdf_bytes = generate_emr_pdf(
        patient=_patient_brief(p),
        items=items,
        sections_selected=allow_sections,
        letterhead_bytes=None,  # JSON export doesn't upload letterhead
        branding=branding,  # branding-based header/footer
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
    p = db.query(Patient).get(int(patient_id))
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

    # Patient resource
    name = [{
        "use": "official",
        "text": " ".join([x for x in [p.first_name, p.last_name] if x]),
    }]
    identifiers = [{"system": "urn:uhid", "value": p.uhid}]
    if p.abha_number:
        identifiers.append({
            "system": "https://healthid.ndhm.gov.in",
            "value": p.abha_number,
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

    # Map a few essentials (you can expand later)
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
        if t == "opd_vitals":
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
        if t == "rx":
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
