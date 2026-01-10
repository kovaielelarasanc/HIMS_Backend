# FILE: app/api/routes_opd.py
from __future__ import annotations

import os
import re
from datetime import datetime, date as dt_date, time as dt_time, timedelta, date
from typing import Optional, List, Set, Dict, Any
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
import secrets
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, case, and_

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.department import Department
from app.models.role import Role
from app.models.ui_branding import UiBranding
from sqlalchemy import desc
from app.models.opd import (
    Appointment,
    Visit,
    Vitals,
    Prescription,
    PrescriptionItem,
    LabOrder,
    RadiologyOrder,
    OpdSchedule,
    FollowUp,
    Medicine,
    LabTest,
    RadiologyTest,
    DoctorFee,
    OpdQueueCounter,
)

from app.schemas.opd import (
    AppointmentCreate,
    AppointmentRow,
    VisitCreate,
    VisitOut,
    VisitUpdate,
    VitalsIn,
    PrescriptionIn,
    OrderIdsIn,
    FollowUpCreate,
    FollowUpUpdate,
    FollowUpRow,
    AppointmentRescheduleIn,
    MedicineOut,
    TestOut,
    DoctorFeeCreate,
    DoctorFeeUpdate,
    DoctorFeeOut,
)
from app.schemas.opd import FollowUpListItem
from app.services.billing_hooks import autobill_opd_consultation
from app.services.pdf_opd_summary import build_visit_summary_pdf
from app.schemas.opd import VitalsLatestResponse, VitalsOut

router = APIRouter()

import logging

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Kolkata"))
BUSY_STATUS = {"booked", "checked_in", "in_progress"}

ALLOWED_STATUS = {
    "booked",
    "checked_in",
    "in_progress",
    "completed",
    "no_show",
    "cancelled",
}
TRANSITIONS: Dict[str, Set[str]] = {
    "booked": {"checked_in", "cancelled", "no_show"},
    "checked_in": {"in_progress", "cancelled"},
    "in_progress": {"completed"},
    "completed": set(),
    "cancelled": set(),
    "no_show": set(),
}

# routes_opd.py (add helpers near VITALS section)


def _pick_attr(obj, names: List[str]):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _set_first_attr(obj, names: List[str], value) -> None:
    if value is None:
        return
    for n in names:
        if hasattr(obj, n):
            setattr(obj, n, value)
            return


def _ts_col():
    return getattr(Vitals, "recorded_at", None) or getattr(
        Vitals, "created_at", None)


def _vitals_to_out(v: Vitals) -> dict:
    # timestamps
    created = _pick_attr(v, ["recorded_at", "created_at"])
    created_at = created.isoformat() if created else None

    # numeric
    height = _pick_attr(v, ["height_cm"])
    weight = _pick_attr(v, ["weight_kg"])
    bmi = _pick_attr(v, ["bmi"])

    # compute bmi if not stored
    if bmi is None and height and weight and float(height) > 0:
        m = float(height) / 100.0
        bmi = round(float(weight) / (m * m), 1)

    temp = _pick_attr(v, ["temp_c", "temperature_c"])
    rr = _pick_attr(v, ["resp_rate", "rr", "respiration"])
    sys = _pick_attr(v, ["bp_sys", "bp_systolic", "systolic"])
    dia = _pick_attr(v, ["bp_dia", "bp_diastolic", "diastolic"])
    notes = _pick_attr(v, ["notes", "remarks"])

    return {
        "id": v.id,
        "patient_id": v.patient_id,
        "appointment_id": getattr(v, "appointment_id", None),
        "created_at": created_at,
        "height_cm": float(height) if height is not None else None,
        "weight_kg": float(weight) if weight is not None else None,
        "bmi": float(bmi) if bmi is not None else None,
        "temp_c": float(temp) if temp is not None else None,
        "pulse": _pick_attr(v, ["pulse"]),
        "resp_rate": rr,
        "spo2": _pick_attr(v, ["spo2"]),
        "bp_sys": sys,
        "bp_dia": dia,
        "notes": notes,
    }


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


# ---------------- permissions ----------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) == code:
                return True
    return False


def _has_any_perm(user: User, codes: Set[str]) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
                return True
    return False


def _fmt_time(t: Optional[dt_time]) -> str:
    return t.strftime("%H:%M") if t else "—"


# ---------------- helpers ----------------
def _ensure_not_past_date_only(d: dt_date) -> None:
    if d < now_local().date():
        raise HTTPException(status_code=400,
                            detail="Cannot book to a past date")


def _ensure_not_past(d: dt_date, slot_start: dt_time) -> None:
    req_dt_local = datetime.combine(d, slot_start, LOCAL_TZ)
    if req_dt_local < now_local():
        raise HTTPException(status_code=400,
                            detail="Cannot book or reschedule to a past slot")


def _parse_slot(slot_str: str) -> dt_time:
    try:
        return datetime.strptime(slot_str, "%H:%M").time()
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Invalid slot_start (HH:MM)")


def _get_active_schedule_for_doctor_and_date(db: Session, doctor_user_id: int,
                                             d: dt_date) -> OpdSchedule:
    weekday = d.weekday()
    sch = (db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == doctor_user_id,
        OpdSchedule.weekday == weekday,
        OpdSchedule.is_active.is_(True),
    ).order_by(OpdSchedule.start_time.asc()).first())
    if not sch:
        raise HTTPException(
            status_code=400,
            detail="No active OPD schedule for the doctor on that day",
        )
    return sch


def _compute_slot_end(d: dt_date, slot_start: dt_time,
                      minutes: int) -> dt_time:
    base = datetime(d.year, d.month, d.day, slot_start.hour, slot_start.minute)
    end = base + timedelta(minutes=minutes or 15)
    return end.time()


def _check_slot_in_schedule(sch: OpdSchedule, d: dt_date, slot_start: dt_time,
                            slot_end: dt_time) -> None:
    if not (sch.start_time <= slot_start < sch.end_time):
        raise HTTPException(
            status_code=400,
            detail="slot_start outside doctor's schedule window")
    if slot_end > sch.end_time:
        raise HTTPException(status_code=400,
                            detail="slot_end exceeds doctor's schedule window")


def _ensure_no_patient_duplicate(
    db: Session,
    patient_id: int,
    d: dt_date,
    exclude_appointment_id: Optional[int] = None,
) -> None:
    q = db.query(Appointment).filter(
        Appointment.patient_id == patient_id,
        Appointment.date == d,
        Appointment.status.in_(BUSY_STATUS),
    )
    if exclude_appointment_id:
        q = q.filter(Appointment.id != exclude_appointment_id)
    dup = q.first()
    if dup:
        slot = dup.slot_start.strftime("%H:%M") if dup.slot_start else "—"
        raise HTTPException(
            status_code=400,
            detail=("Duplicate booking blocked: patient already has an active "
                    f"appointment at {slot} (status: {dup.status}) on {d}."),
        )


def _ensure_slot_free_for_doctor(
    db: Session,
    doctor_user_id: int,
    d: dt_date,
    slot_start: dt_time,
    exclude_appointment_id: Optional[int] = None,
) -> None:
    q = db.query(Appointment).filter(
        Appointment.doctor_user_id == doctor_user_id,
        Appointment.date == d,
        Appointment.slot_start == slot_start,
        Appointment.status.in_(BUSY_STATUS),
    )
    if exclude_appointment_id:
        q = q.filter(Appointment.id != exclude_appointment_id)
    exists = q.first()
    if exists:
        raise HTTPException(status_code=409, detail="Slot already booked")


def _next_queue_no(db: Session, doctor_user_id: int, d: dt_date) -> int:
    """
    Atomic token generator per doctor+date using row lock.
    Requires a UNIQUE(doctor_user_id, date) constraint on OpdQueueCounter.
    """
    for _ in range(3):
        try:
            row = (db.query(OpdQueueCounter).filter(
                OpdQueueCounter.doctor_user_id == doctor_user_id,
                OpdQueueCounter.date == d,
            ).with_for_update().first())
            if not row:
                row = OpdQueueCounter(doctor_user_id=doctor_user_id,
                                      date=d,
                                      last_queue_no=0)
                db.add(row)
                db.flush()

            row.last_queue_no = int(row.last_queue_no or 0) + 1
            return row.last_queue_no
        except IntegrityError:
            db.rollback()

    raise HTTPException(status_code=500,
                        detail="Could not allocate queue number")


def vitals_done_on(db: Session, patient_id: int, d: dt_date) -> bool:
    """
    Legacy helper: 'Did this patient have any vitals on this calendar date?'
    Uses DATE(created_at) so it's robust.
    """
    row = (db.query(Vitals.id).filter(
        Vitals.patient_id == patient_id,
        func.date(Vitals.created_at) == d,
    ).first())
    return bool(row)


def vitals_done_for_appointment(db: Session, ap: Appointment) -> bool:
    """
    Preferred helper if you have Vitals.appointment_id column.
    Falls back to legacy patient+date check.
    """
    if hasattr(Vitals, "appointment_id"):
        exists = db.query(
            Vitals.id).filter(Vitals.appointment_id == ap.id).first()
        if exists:
            return True
    return vitals_done_on(db, ap.patient_id, ap.date)


def _format_visit_time(v: Visit) -> str:
    dtv = getattr(v, "visit_at", None)
    if dtv is None and getattr(v, "appointment", None) is not None:
        ap = v.appointment
        if getattr(ap, "slot_start", None) is not None:
            dtv = datetime(ap.date.year, ap.date.month, ap.date.day,
                           ap.slot_start.hour, ap.slot_start.minute)
    if dtv is None:
        return ""
    return dtv.strftime("%d %b %Y, %I:%M %p")


def _acronym_from_org_name(org_name: str, max_len: int = 3) -> str:
    name = (org_name or "").strip()
    if not name:
        return "NH"
    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    if not words:
        return "NH"
    if len(words) >= 2:
        code = "".join(w[0] for w in words[:max_len])
    else:
        code = words[0][:max_len]
    return code or "NH"


def _org_code_from_branding(db: Session, max_len: int = 3) -> str:
    b = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not b:
        return "NH"

    direct = (getattr(b, "org_code", None)
              or getattr(b, "org_short_code", None)
              or getattr(b, "short_code", None))
    if isinstance(direct, str) and direct.strip():
        cleaned = re.sub(r"[^A-Za-z0-9]", "", direct.strip().upper())
        return cleaned[:max_len] or "NH"

    return _acronym_from_org_name(getattr(b, "org_name", "") or "",
                                  max_len=max_len)


def make_op_episode_id(
    db: Session,
    visit_id: int,
    *,
    on_date: Optional[date] = None,
    id_width: int = 4,
) -> str:
    code = _org_code_from_branding(db, max_len=3)
    d = on_date or now_local().date()
    dt = d.strftime("%d%m%Y")
    return f"{code}OP{dt}{visit_id:0{id_width}d}"


def episode_id_for_month(db: Session) -> str:
    ym = datetime.utcnow().strftime("%Y%m")
    count = db.query(Visit).filter(Visit.episode_id.like(f"OP-{ym}-%")).count()
    return f"OP-{ym}-{count + 1:04d}"


# ------------------- LOOKUPS -------------------
@router.get("/departments", response_model=List[dict])
def opd_departments(db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    if not _has_any_perm(
            user,
        {
            "schedules.manage", "appointments.view", "visits.view",
            "patients.view"
        },
    ):
        raise HTTPException(status_code=403, detail="Not permitted")
    rows = db.query(Department).order_by(Department.name.asc()).all()
    return [{"id": d.id, "name": d.name} for d in rows]


@router.get("/roles", response_model=List[dict])
def opd_roles(
        department_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user,
        {
            "schedules.manage", "appointments.view", "appointments.create",
            "visits.view", "vitals.create"
        },
    ):
        raise HTTPException(status_code=403, detail="Not permitted")

    roles = (db.query(Role).join(Role.users).filter(
        User.is_active.is_(True),
        User.department_id == department_id).distinct().order_by(
            Role.name.asc()).all())
    return [{"id": r.id, "name": r.name} for r in roles]


@router.get("/users", response_model=List[dict])
def opd_department_users(
        department_id: int = Query(...),
        role_id: Optional[int] = Query(None),
        role: Optional[str] = Query(None),
        is_doctor: Optional[bool] = Query(
            None,
            description=
            "If true, only doctor users (User.is_doctor = 1) will be returned",
        ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user,
        {
            "schedules.manage", "appointments.create", "appointments.view",
            "visits.create", "visits.view", "vitals.create"
        },
    ):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(User).filter(User.is_active.is_(True),
                              User.department_id == department_id)

    if is_doctor is not None:
        q = q.filter(User.is_doctor.is_(is_doctor))

    if role_id:
        q = q.join(User.roles).filter(Role.id == role_id)
    elif role:
        q = q.join(User.roles).filter(Role.name.ilike(role))

    q = q.options(joinedload(User.roles)).order_by(User.name.asc()).distinct()
    users = q.all()
    return [{
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "roles": [r.name for r in (u.roles or [])],
        "is_doctor": u.is_doctor,
    } for u in users]


@router.get("/doctor-weekdays", response_model=dict)
def doctor_weekdays(
        doctor_user_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(user, {"schedules.manage", "appointments.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")
    qs = (db.query(OpdSchedule.weekday).filter(
        OpdSchedule.doctor_user_id == doctor_user_id,
        OpdSchedule.is_active.is_(True)).distinct().all())
    return {
        "doctor_user_id": doctor_user_id,
        "weekdays": sorted({w
                            for (w, ) in qs})
    }


# ------------------- SLOTS (schedule-backed) -------------------
@router.get("/slots")
def get_slots(
        doctor_user_id: int,
        date_str: Optional[str] = Query(None),
        date_param: Optional[date] = Query(None, alias="date"),
        slot_minutes: int = 15,
        detailed: bool = Query(
            False, description="If true, return status for each slot"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user,
        {"appointments.view", "appointments.create", "schedules.manage"}):
        raise HTTPException(status_code=403, detail="Not permitted")

    if date_param:
        d = date_param
    elif date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid date")
    else:
        raise HTTPException(status_code=400, detail="date required")

    weekday = d.weekday()
    schedules = (db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == doctor_user_id,
        OpdSchedule.weekday == weekday,
        OpdSchedule.is_active.is_(True),
    ).all())
    if not schedules:
        return [] if not detailed else {"slots": []}

    busy = {
        t[0].strftime("%H:%M")
        for t in db.query(Appointment.slot_start).filter(
            Appointment.doctor_user_id == doctor_user_id,
            Appointment.date == d,
            Appointment.status.in_(BUSY_STATUS),
            Appointment.slot_start.isnot(None),
        ).all()
    }

    now = now_local()
    out: List[Dict[str, Any]] = []
    for sch in schedules:
        step = timedelta(
            minutes=int(getattr(sch, "slot_minutes", None) or slot_minutes))
        cur = datetime.combine(d, sch.start_time, LOCAL_TZ)
        end_dt = datetime.combine(d, sch.end_time, LOCAL_TZ)

        while cur + step <= end_dt:
            hhmm = cur.strftime("%H:%M")
            if cur < now:
                status = "past"
            elif hhmm in busy:
                status = "booked"
            else:
                status = "free"

            if detailed:
                out.append({
                    "start": hhmm,
                    "end": (cur + step).strftime("%H:%M"),
                    "status": status
                })
            else:
                if status == "free":
                    out.append({
                        "start": hhmm,
                        "end": (cur + step).strftime("%H:%M")
                    })
            cur += step

    if detailed:
        return {"slots": out}

    uniq = {(s["start"], s["end"]) for s in out}
    return [{"start": a, "end": b} for (a, b) in sorted(uniq)]


# ------------------- APPOINTMENTS -------------------
@router.post("/appointments")
def create_appointment(
        payload: AppointmentCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "appointments.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.get(Patient, payload.patient_id)
    if not patient or not getattr(patient, "is_active", True):
        raise HTTPException(status_code=404, detail="Patient not found")

    appt_type = (getattr(payload, "appointment_type", None)
                 or "slot").strip().lower()
    if appt_type not in {"slot", "free"}:
        raise HTTPException(status_code=400,
                            detail="appointment_type must be 'slot' or 'free'")

    _ensure_no_patient_duplicate(db, payload.patient_id, payload.date)

    slot_start = None
    slot_end = None

    if appt_type == "slot":
        if not getattr(payload, "slot_start", None):
            raise HTTPException(
                status_code=400,
                detail="slot_start is required for slot booking")

        slot_start = _parse_slot(payload.slot_start)
        sch = _get_active_schedule_for_doctor_and_date(db,
                                                       payload.doctor_user_id,
                                                       payload.date)
        slot_end = _compute_slot_end(
            payload.date, slot_start,
            int(getattr(sch, "slot_minutes", 15) or 15))

        _check_slot_in_schedule(sch, payload.date, slot_start, slot_end)
        _ensure_not_past(payload.date, slot_start)
        _ensure_slot_free_for_doctor(db, payload.doctor_user_id, payload.date,
                                     slot_start)
    else:
        _ensure_not_past_date_only(payload.date)

    queue_no = _next_queue_no(db, payload.doctor_user_id, payload.date)

    ap = Appointment(
        patient_id=payload.patient_id,
        department_id=payload.department_id,
        doctor_user_id=payload.doctor_user_id,
        date=payload.date,
        appointment_type=appt_type,
        queue_no=queue_no,
        slot_start=slot_start,
        slot_end=slot_end,
        purpose=getattr(payload, "purpose", None) or "Consultation",
        status="booked",
    )

    if hasattr(Appointment, "booked_by"):
        ap.booked_by = user.id

    db.add(ap)
    try:
        db.commit()
        db.refresh(ap)
    except IntegrityError:
        db.rollback()
        if appt_type == "slot":
            raise HTTPException(status_code=409, detail="Slot already booked")
        queue_no = _next_queue_no(db, payload.doctor_user_id, payload.date)
        ap.queue_no = queue_no
        db.add(ap)
        db.commit()
        db.refresh(ap)

    return {
        "id": ap.id,
        "message": "Booked",
        "appointment_type": ap.appointment_type,
        "queue_no": ap.queue_no
    }


@router.get("/appointments", response_model=List[AppointmentRow])
def list_appointments(
        date: Optional[dt_date] = Query(None),
        date_str: Optional[str] = Query(None),
        doctor_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "appointments.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    if date is None:
        if not date_str:
            raise HTTPException(status_code=400, detail="date is required")
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid date")

    q = (db.query(Appointment).options(
        joinedload(Appointment.patient),
        joinedload(Appointment.doctor),
        joinedload(Appointment.department),
    ).filter(Appointment.date == date))
    if doctor_id:
        q = q.filter(Appointment.doctor_user_id == doctor_id)

    rows: List[Appointment] = q.order_by(Appointment.queue_no.asc(),
                                         Appointment.id.asc()).all()

    appt_ids = [r.id for r in rows]
    vis_by_appt: Dict[int, int] = {}
    if appt_ids:
        vis_by_appt = {
            v.appointment_id: v.id
            for v in db.query(Visit).filter(Visit.appointment_id.in_(
                appt_ids)).all()
        }

    vitals_by_appt: Set[int] = set()
    vitals_by_patient: Set[int] = set()
    if appt_ids and hasattr(Vitals, "appointment_id"):
        vit_rows = (db.query(Vitals.appointment_id).filter(
            Vitals.appointment_id.in_(appt_ids)).distinct().all())
        vitals_by_appt = {aid for (aid, ) in vit_rows if aid is not None}
    else:
        patient_ids = [r.patient_id for r in rows]
        if patient_ids:
            vit_pat_rows = (db.query(Vitals.patient_id).filter(
                Vitals.patient_id.in_(patient_ids),
                func.date(Vitals.created_at) == date).distinct().all())
            vitals_by_patient = {
                pid
                for (pid, ) in vit_pat_rows if pid is not None
            }

    out: List[AppointmentRow] = []
    for r in rows:
        vitals_flag = (r.id in vitals_by_appt) or (r.patient_id
                                                   in vitals_by_patient)
        out.append(
            AppointmentRow(
                id=r.id,
                queue_no=getattr(r, "queue_no", None),
                appointment_type=getattr(r, "appointment_type", None),
                uhid=r.patient.uhid if r.patient else "",
                patient_name=(
                    f"{r.patient.first_name} {r.patient.last_name or ''}".
                    strip() if r.patient else ""),
                doctor_name=r.doctor.name if r.doctor else "",
                department_name=r.department.name if r.department else "",
                date=r.date.isoformat(),
                slot_start=_fmt_time(r.slot_start),
                slot_end=_fmt_time(r.slot_end),
                status=r.status,
                visit_id=vis_by_appt.get(r.id),
                vitals_registered=vitals_flag,
                purpose=r.purpose or "Consultation",
            ))
    return out


class AppointmentStatusUpdateLocal(BaseModel):
    status: str


@router.patch("/appointments/{appointment_id}/status")
def update_appointment_status(
        appointment_id: int,
        payload: AppointmentStatusUpdateLocal,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    ap = db.get(Appointment, appointment_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Appointment not found")

    allowed = bool(
        getattr(user, "is_admin", False) or (user.id == ap.doctor_user_id)
        or has_perm(user, "appointments.update"))
    if not allowed:
        raise HTTPException(status_code=403, detail="Not permitted")

    new_status = (payload.status or "").lower().strip()
    if new_status not in ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail="Invalid status")

    if ap.status not in TRANSITIONS or new_status not in TRANSITIONS[
            ap.status]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot move from {ap.status} to {new_status}",
        )

    visit = db.query(Visit).filter(Visit.appointment_id == ap.id).first()

    def _ensure_visit() -> Visit:
        nonlocal visit
        if not visit:
            visit = Visit(
                appointment_id=ap.id,
                patient_id=ap.patient_id,
                department_id=ap.department_id,
                doctor_user_id=ap.doctor_user_id,
                episode_id="TEMP",
                visit_at=now_local().replace(tzinfo=None),
            )
            db.add(visit)
            db.flush()  # assigns visit.id
            visit.episode_id = make_op_episode_id(
                db, visit.id, on_date=visit.visit_at.date(), id_width=4)
        return visit

    # If checked_in, ensure visit
    if new_status == "checked_in":
        _ensure_visit()

    # Apply OPD status change
    ap.status = new_status

    # If completed, ensure visit + visit_at
    if new_status == "completed":
        v = _ensure_visit()
        if getattr(v, "visit_at", None) is None:
            v.visit_at = now_local().replace(tzinfo=None)

        try:
            with db.begin_nested():  # ✅ SAVEPOINT
                autobill_opd_consultation(db, appointment=ap, visit=v, user=user)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Auto-bill OPD consultation failed",
                extra={"appointment_id": ap.id, "visit_id": v.id},
        )



    # ✅ Capture IDs BEFORE any billing attempt (avoid expired attrs after rollback)
    ap_id = int(ap.id)
    visit_id = int(visit.id) if visit else None

    # ✅ First commit OPD changes safely (so OPD never fails because billing fails)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # keep your old fallback if needed (episode collisions etc.)
        if visit:
            visit.episode_id = episode_id_for_month(db)
        db.commit()

    billing_warning = None

    # ✅ Second phase: Billing in a fresh transaction
    if new_status == "completed" and visit_id:
        try:
            ap2 = db.get(Appointment, ap_id)
            v2 = db.get(Visit, visit_id)
            if ap2 and v2:
                autobill_opd_consultation(db,
                                          appointment=ap2,
                                          visit=v2,
                                          user=user)
                db.commit()
        except Exception:
            # THIS is the missing piece in your current code
            db.rollback()
            billing_warning = "Auto-bill OPD consultation failed"
            logger.exception(
                "Auto-bill OPD consultation failed",
                extra={
                    "appointment_id": ap_id,
                    "visit_id": visit_id
                },
            )

    return {
        "message": "Updated",
        "status": new_status,
        "visit_id": visit_id,
        "billing_warning": billing_warning,
    }


@router.post("/appointments/{appointment_id}/reschedule")
def reschedule_appointment(
        appointment_id: int,
        payload: AppointmentRescheduleIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    ap = db.get(Appointment, appointment_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Appointment not found")

    allowed = bool(
        getattr(user, "is_admin", False) or user.id == ap.doctor_user_id
        or has_perm(user, "appointments.update"))
    if not allowed:
        raise HTTPException(status_code=403, detail="Not permitted")

    new_date = payload.date
    appt_type = (getattr(ap, "appointment_type", None)
                 or "slot").strip().lower()
    if appt_type not in {"slot", "free"}:
        appt_type = "slot"

    create_new = bool(getattr(payload, "create_new", False)) or (ap.status
                                                                 == "no_show")

    new_slot_start = None
    new_slot_end = None

    if appt_type == "slot":
        if not getattr(payload, "slot_start", None):
            raise HTTPException(
                status_code=400,
                detail="slot_start is required for slot reschedule")

        new_slot_start = _parse_slot(payload.slot_start)
        sch = _get_active_schedule_for_doctor_and_date(db, ap.doctor_user_id,
                                                       new_date)
        new_slot_end = _compute_slot_end(
            new_date, new_slot_start,
            int(getattr(sch, "slot_minutes", 15) or 15))

        _check_slot_in_schedule(sch, new_date, new_slot_start, new_slot_end)
        _ensure_not_past(new_date, new_slot_start)
    else:
        _ensure_not_past_date_only(new_date)

    if create_new:
        if ap.status in BUSY_STATUS and ap.status != "no_show":
            ap.status = "cancelled"

        _ensure_no_patient_duplicate(db, ap.patient_id, new_date)
        if appt_type == "slot":
            _ensure_slot_free_for_doctor(db, ap.doctor_user_id, new_date,
                                         new_slot_start)

        queue_no = _next_queue_no(db, ap.doctor_user_id, new_date)
        new_ap = Appointment(
            patient_id=ap.patient_id,
            department_id=ap.department_id,
            doctor_user_id=ap.doctor_user_id,
            date=new_date,
            appointment_type=appt_type,
            queue_no=queue_no,
            slot_start=new_slot_start,
            slot_end=new_slot_end,
            purpose=ap.purpose or "Consultation",
            status="booked",
        )
        if hasattr(Appointment, "booked_by"):
            new_ap.booked_by = user.id

        db.add(new_ap)
        db.commit()
        db.refresh(new_ap)

        return {
            "message": "Rescheduled as new appointment",
            "old_appointment_id": ap.id,
            "appointment_id": new_ap.id,
            "appointment_type": new_ap.appointment_type,
            "queue_no": new_ap.queue_no,
        }

    _ensure_no_patient_duplicate(db,
                                 ap.patient_id,
                                 new_date,
                                 exclude_appointment_id=ap.id)
    if appt_type == "slot":
        _ensure_slot_free_for_doctor(db,
                                     ap.doctor_user_id,
                                     new_date,
                                     new_slot_start,
                                     exclude_appointment_id=ap.id)

    if new_date != ap.date:
        ap.queue_no = _next_queue_no(db, ap.doctor_user_id, new_date)

    ap.date = new_date
    ap.slot_start = new_slot_start
    ap.slot_end = new_slot_end

    if ap.status in {"cancelled", "no_show"}:
        ap.status = "booked"

    db.commit()

    return {
        "message": "Appointment rescheduled",
        "appointment_id": ap.id,
        "appointment_type": ap.appointment_type,
        "queue_no": ap.queue_no,
        "date": str(ap.date),
        "slot_start": _fmt_time(ap.slot_start),
        "slot_end": _fmt_time(ap.slot_end),
        "status": ap.status,
    }


@router.get("/appointments/noshow", response_model=List[AppointmentRow])
def list_no_show_appointments(
        for_date: Optional[dt_date] = Query(
            None, description="If omitted, today's date will be used"),
        doctor_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "appointments.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    if for_date is None:
        for_date = dt_date.today()

    q = (db.query(Appointment).options(
        joinedload(Appointment.patient), joinedload(Appointment.doctor),
        joinedload(Appointment.department)).filter(
            Appointment.date == for_date, Appointment.status == "no_show"))
    if doctor_id:
        q = q.filter(Appointment.doctor_user_id == doctor_id)

    rows = q.order_by(Appointment.queue_no.asc(), Appointment.id.asc()).all()

    appt_ids = [r.id for r in rows]
    vis_by_appt = {}
    if appt_ids:
        vis_by_appt = {
            v.appointment_id: v.id
            for v in db.query(Visit).filter(Visit.appointment_id.in_(
                appt_ids)).all()
        }

    out: List[AppointmentRow] = []
    for r in rows:
        out.append(
            AppointmentRow(
                id=r.id,
                queue_no=getattr(r, "queue_no", None),
                appointment_type=getattr(r, "appointment_type", None),
                uhid=r.patient.uhid if r.patient else "",
                patient_name=(
                    f"{r.patient.first_name} {r.patient.last_name or ''}".
                    strip() if r.patient else ""),
                doctor_name=r.doctor.name if r.doctor else "",
                department_name=r.department.name if r.department else "",
                date=r.date.isoformat(),
                slot_start=_fmt_time(r.slot_start),
                slot_end=_fmt_time(r.slot_end),
                status=r.status,
                visit_id=vis_by_appt.get(r.id),
                vitals_registered=vitals_done_for_appointment(db, r),
                purpose=r.purpose or "Consultation",
            ))
    return out


# ------------------- VITALS -------------------
# ------------------- VITALS -------------------

from app.schemas.opd import VitalsIn, VitalsOut, VitalsLatestResponse


@router.post("/vitals/{patient_id}")
def record_vitals(
        patient_id: int,
        payload: VitalsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ Legacy endpoint (KEEP AS-IS for your existing frontend)
    POST /api/opd/vitals/{patient_id}
    Body: VitalsIn (height_cm, weight_kg, bp_systolic, bp_diastolic, pulse, rr, temp_c, spo2, notes, etc.)
    """
    if not has_perm(user, "vitals.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    # Works for both Pydantic v1 and v2
    data = payload.model_dump(exclude_unset=True) if hasattr(
        payload, "model_dump") else payload.dict(exclude_unset=True)

    v = Vitals(patient_id=patient_id)

    # ✅ Optional: link to appointment if your model supports it (does NOT change endpoint)
    appt_id = data.get("appointment_id")
    if appt_id and hasattr(Vitals, "appointment_id"):
        v.appointment_id = int(appt_id)

    # ✅ Assign only fields that actually exist in your Vitals model
    for k, val in data.items():
        if k in {"patient_id"}:
            continue
        if hasattr(v, k):
            setattr(v, k, val)

    db.add(v)
    db.commit()
    db.refresh(v)
    return {"id": v.id, "message": "Vitals saved"}


@router.post("/vitals/{patient_id}", response_model=VitalsOut)
def record_vitals_for_patient(
        patient_id: int,
        payload: VitalsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # keep compatibility, but route to POST /vitals logic
    patched = payload.model_copy(update={"patient_id": patient_id})
    return record_vitals(patched, db=db, user=user)


from sqlalchemy import and_, func


@router.get("/vitals/latest", response_model=VitalsLatestResponse)
def get_latest_vitals(
        appointment_id: Optional[int] = Query(None, ge=1),
        patient_id: Optional[int] = Query(None, ge=1),
        for_date: Optional[date] = Query(None),
        db: Session = Depends(get_db),
        current_user: User = Depends(current_user),
) -> VitalsLatestResponse:

    if not _has_any_perm(
            current_user,
        {"appointments.view", "vitals.create", "visits.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")

    ap = None
    if appointment_id:
        ap = db.get(Appointment, int(appointment_id))
        if not ap:
            raise HTTPException(status_code=404,
                                detail="Appointment not found")
        patient_id = ap.patient_id

    if not patient_id:
        raise HTTPException(status_code=400,
                            detail="appointment_id or patient_id required")

    ts = Vitals.created_at  # only column you have

    def apply_date_and_order(q):
        if for_date:
            start_dt = datetime.combine(for_date, time.min)
            end_dt = start_dt + timedelta(days=1)
            q = q.filter(and_(ts >= start_dt, ts < end_dt))
        return q.order_by(ts.desc(), Vitals.id.desc())

    v = None

    # 1) try appointment_id first
    if appointment_id:
        v = apply_date_and_order(
            db.query(Vitals).filter(
                Vitals.appointment_id == int(appointment_id))).first()

    # 2) fallback to patient_id (covers old rows with appointment_id NULL)
    if not v:
        v = apply_date_and_order(
            db.query(Vitals).filter(
                Vitals.patient_id == int(patient_id))).first()

    if not v:
        return VitalsLatestResponse(exists=False, vitals=None)

    return VitalsLatestResponse(exists=True,
                                vitals=VitalsOut(**_vitals_to_out(v)))


@router.get("/vitals/history", response_model=List[VitalsOut])
def get_vitals_history(
        appointment_id: Optional[int] = Query(None, ge=1),
        patient_id: Optional[int] = Query(None, ge=1),
        for_date: Optional[date] = Query(None),
        limit: int = Query(14, ge=1, le=200),
        db: Session = Depends(get_db),
        current_user: User = Depends(current_user),
):
    if not _has_any_perm(
            current_user,
        {"appointments.view", "vitals.create", "visits.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")

    ap = None
    if appointment_id:
        ap = db.get(Appointment, int(appointment_id))
        if not ap:
            raise HTTPException(status_code=404,
                                detail="Appointment not found")
        patient_id = ap.patient_id

    if not patient_id:
        raise HTTPException(status_code=400,
                            detail="appointment_id or patient_id required")

    q = db.query(Vitals)

    if appointment_id and hasattr(Vitals, "appointment_id"):
        q = q.filter(Vitals.appointment_id == int(appointment_id))
    else:
        q = q.filter(Vitals.patient_id == int(patient_id))

    col = _ts_col()
    if for_date and col is not None:
        start_dt = datetime.combine(for_date, time.min)
        end_dt = start_dt + timedelta(days=1)
        q = q.filter(and_(col >= start_dt, col < end_dt))

    if col is not None:
        q = q.order_by(col.desc(), Vitals.id.desc())
    else:
        q = q.order_by(Vitals.id.desc())

    rows = q.limit(limit).all()
    return [VitalsOut(**_vitals_to_out(r)) for r in rows]


# ------------------- VISITS -------------------
@router.post("/visits")
def create_visit(
        payload: VisitCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    ap = db.get(Appointment, payload.appointment_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Appointment not found")

    visit_at_local = now_local().replace(tzinfo=None)

    v = Visit(
        appointment_id=ap.id,
        patient_id=ap.patient_id,
        department_id=ap.department_id,
        doctor_user_id=ap.doctor_user_id,
        episode_id=f"TMP-{secrets.token_hex(6).upper()}",
        visit_at=visit_at_local,
    )
    ap.status = "checked_in"
    db.add(v)
    db.flush()
    v.episode_id = make_op_episode_id(db,
                                      v.id,
                                      on_date=visit_at_local.date(),
                                      id_width=4)

    db.commit()
    db.refresh(v)
    return {"id": v.id}


@router.get("/visits/{visit_id}", response_model=VisitOut)
def get_visit(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    v = (db.query(Visit).options(
        joinedload(Visit.patient),
        joinedload(Visit.department),
        joinedload(Visit.doctor),
        joinedload(Visit.appointment),
    ).filter(Visit.id == visit_id).first())
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    if not (getattr(user, "is_admin", False) or v.doctor_user_id == user.id
            or user.department_id == v.department_id
            or has_perm(user, "visits.view")):
        raise HTTPException(status_code=403, detail="Not permitted")

    latest_vitals = None

    if v.appointment_id and hasattr(Vitals, "appointment_id"):
        latest_vitals = (db.query(Vitals).filter(
            Vitals.appointment_id == v.appointment_id).order_by(
                Vitals.created_at.desc()).first())

    if not latest_vitals:
        latest_vitals = (db.query(Vitals).filter(
            Vitals.patient_id == v.patient_id).order_by(
                Vitals.created_at.desc()).first())

    vitals_dict = None
    if latest_vitals:
        vitals_dict = {
            "height_cm":
            float(latest_vitals.height_cm)
            if latest_vitals.height_cm is not None else None,
            "weight_kg":
            float(latest_vitals.weight_kg)
            if latest_vitals.weight_kg is not None else None,
            "bmi":
            None,
            "bp_systolic":
            latest_vitals.bp_systolic,
            "bp_diastolic":
            latest_vitals.bp_diastolic,
            "pulse":
            latest_vitals.pulse,
            "rr":
            latest_vitals.rr,
            "temp_c":
            float(latest_vitals.temp_c)
            if latest_vitals.temp_c is not None else None,
            "spo2":
            latest_vitals.spo2,
            "notes":
            latest_vitals.notes,
            "created_at":
            latest_vitals.created_at.isoformat()
            if latest_vitals.created_at else None,
        }

    return VisitOut(
        id=v.id,
        uhid=v.patient.uhid,
        patient_name=f"{v.patient.first_name} {v.patient.last_name or ''}".
        strip(),
        department_name=v.department.name if v.department else "",
        doctor_name=v.doctor.name if v.doctor else "",
        episode_id=v.episode_id,
        visit_at=_format_visit_time(v),
        chief_complaint=v.chief_complaint,
        symptoms=v.symptoms,
        soap_subjective=v.soap_subjective,
        soap_objective=v.soap_objective,
        soap_assessment=v.soap_assessment,
        plan=v.plan,
        patient_id=v.patient_id,
        doctor_id=v.doctor_user_id,
        appointment_id=v.appointment_id,
        appointment_status=v.appointment.status if v.appointment else None,
        current_vitals=vitals_dict,
        presenting_illness=v.presenting_illness,
        review_of_systems=v.review_of_systems,
        medical_history=v.medical_history,
        surgical_history=v.surgical_history,
        medication_history=v.medication_history,
        drug_allergy=v.drug_allergy,
        family_history=v.family_history,
        personal_history=v.personal_history,
        menstrual_history=v.menstrual_history,
        obstetric_history=v.obstetric_history,
        immunization_history=v.immunization_history,
        general_examination=v.general_examination,
        systemic_examination=v.systemic_examination,
        local_examination=v.local_examination,
        provisional_diagnosis=v.provisional_diagnosis,
        differential_diagnosis=v.differential_diagnosis,
        final_diagnosis=v.final_diagnosis,
        diagnosis_codes=v.diagnosis_codes,
        investigations=v.investigations,
        treatment_plan=v.treatment_plan,
        advice=v.advice,
        followup_plan=v.followup_plan,
        referral_notes=v.referral_notes,
        procedure_notes=v.procedure_notes,
        counselling_notes=v.counselling_notes,
    )


@router.put("/visits/{visit_id}")
def update_visit(
        visit_id: int,
        payload: VisitUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    for k, val in payload.model_dump(exclude_unset=True).items():
        setattr(v, k, val)

    if hasattr(v, "updated_at"):
        v.updated_at = datetime.utcnow()

    db.commit()
    return {"message": "Updated"}


@router.get("/visits")
def list_visits(
        patient_id: Optional[int] = Query(None),
        limit: int = Query(20, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "appointments.view") or has_perm(user, "opd.view")
            or has_perm(user, "visits.view")):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(Visit).order_by(Visit.id.desc())
    if patient_id:
        q = q.filter(Visit.patient_id == patient_id)

    rows = q.limit(limit).all()
    return [{
        "id": v.id,
        "patient_id": v.patient_id,
        "doctor_user_id": v.doctor_user_id,
        "department_id": v.department_id,
        "visit_at": v.visit_at,
        "episode_id": v.episode_id,
    } for v in rows]


# ------------------- PRESCRIPTION -------------------
@router.post("/visits/{visit_id}/prescription")
def create_prescription(
        visit_id: int,
        payload: PrescriptionIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    rx = db.query(Prescription).filter(
        Prescription.visit_id == visit_id).first()
    if not rx:
        rx = Prescription(visit_id=visit_id, notes=payload.notes or None)
        db.add(rx)
        db.flush()
    else:
        rx.notes = payload.notes or None
        for it in list(getattr(rx, "items", []) or []):
            db.delete(it)

    for item in payload.items:
        db.add(
            PrescriptionItem(
                prescription_id=rx.id,
                drug_name=item.drug_name,
                strength=item.strength or "",
                frequency=item.frequency or "",
                duration_days=item.duration_days or 0,
                quantity=item.quantity or 0,
                unit_price=item.unit_price or 0,
            ))

    db.commit()
    return {"id": rx.id, "message": "Prescription saved"}


@router.post("/visits/{visit_id}/prescription/esign")
def esign_prescription(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "prescriptions.esign"):
        raise HTTPException(status_code=403, detail="Not permitted")

    rx = db.query(Prescription).filter(
        Prescription.visit_id == visit_id).first()
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found")

    rx.signed_at = datetime.utcnow()
    rx.signed_by = user.id
    db.commit()
    return {"message": "Prescription signed"}


@router.get("/visits/{visit_id}/prescription")
def get_prescription(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    rx = db.query(Prescription).filter(
        Prescription.visit_id == visit_id).first()
    if not rx:
        return {"notes": "", "items": []}

    items = (db.query(PrescriptionItem).filter(
        PrescriptionItem.prescription_id == rx.id).order_by(
            PrescriptionItem.id.asc()).all())

    return {
        "notes":
        rx.notes or "",
        "items": [{
            "drug_name": it.drug_name,
            "strength": it.strength or "",
            "frequency": it.frequency or "",
            "duration_days": int(it.duration_days or 0),
            "quantity": int(it.quantity or 0),
            "unit_price": float(it.unit_price or 0),
        } for it in items],
    }


# ------------------- ORDERS -------------------
@router.post("/visits/{visit_id}/orders/lab")
def add_lab_orders(
        visit_id: int,
        payload: OrderIdsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    for tid in payload.test_ids:
        order = LabOrder(visit_id=visit_id, test_id=tid)
        db.add(order)
        db.flush()
        auto_add_item_for_event(
            db,
            service_type="lab",
            ref_id=order.id,
            patient_id=v.patient_id,
            context_type="opd",
            context_id=v.id,
            user_id=user.id,
        )

    db.commit()
    return {"message": "Lab orders added"}


@router.post("/visits/{visit_id}/orders/radiology")
@router.post("/visits/{visit_id}/orders/ris")
def add_rad_orders(
        visit_id: int,
        payload: OrderIdsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    for tid in payload.test_ids:
        order = RadiologyOrder(visit_id=visit_id, test_id=tid)
        db.add(order)
        db.flush()
        auto_add_item_for_event(
            db,
            service_type="radiology",
            ref_id=order.id,
            patient_id=v.patient_id,
            context_type="opd",
            context_id=v.id,
            user_id=user.id,
        )

    db.commit()
    return {"message": "Radiology orders added"}


# ------------------- FOLLOW-UP MODULE -------------------
def _build_followup_row(fu: FollowUp) -> FollowUpRow:
    p = fu.patient
    d = fu.doctor
    dept = fu.department
    name = f"{p.first_name} {p.last_name or ''}".strip()
    return FollowUpRow(
        id=fu.id,
        visit_id=fu.source_visit_id,
        appointment_id=fu.appointment_id,
        due_date=fu.due_date,
        status=fu.status,
        patient_id=p.id,
        patient_uhid=p.uhid,
        patient_name=name,
        doctor_id=d.id,
        doctor_name=d.name,
        department_id=dept.id,
        department_name=dept.name,
        note=fu.note,
    )


@router.post("/visits/{visit_id}/followup", response_model=FollowUpRow)
def create_followup(
        visit_id: int,
        payload: FollowUpCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    v = (db.query(Visit).options(
        joinedload(Visit.patient), joinedload(Visit.department),
        joinedload(Visit.doctor)).filter(Visit.id == visit_id).first())
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    if not (getattr(user, "is_admin", False) or user.id == v.doctor_user_id
            or has_perm(user, "visits.update")):
        raise HTTPException(status_code=403, detail="Not permitted")

    fu = FollowUp(
        patient_id=v.patient_id,
        department_id=v.department_id,
        doctor_user_id=v.doctor_user_id,
        source_visit_id=v.id,
        due_date=payload.due_date,
        status="waiting",
        note=payload.note or None,
    )
    db.add(fu)
    db.commit()
    db.refresh(fu)
    return _build_followup_row(fu)


@router.get("/followups", response_model=List[FollowUpRow])
def list_followups(
        status: Optional[str] = Query(
            "waiting",
            description="waiting | scheduled | completed | cancelled or *"),
        doctor_id: Optional[int] = Query(None),
        date_from: Optional[dt_date] = Query(None),
        date_to: Optional[dt_date] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "appointments.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = (db.query(FollowUp).options(joinedload(FollowUp.patient),
                                    joinedload(FollowUp.doctor),
                                    joinedload(FollowUp.department)).order_by(
                                        FollowUp.due_date.asc(),
                                        FollowUp.id.asc()))

    if status and status != "*":
        q = q.filter(FollowUp.status == status)
    if doctor_id:
        q = q.filter(FollowUp.doctor_user_id == doctor_id)
    if date_from:
        q = q.filter(FollowUp.due_date >= date_from)
    if date_to:
        q = q.filter(FollowUp.due_date <= date_to)

    rows = q.all()
    return [_build_followup_row(fu) for fu in rows]


@router.put("/followups/{followup_id}", response_model=FollowUpRow)
def update_followup(
        followup_id: int,
        payload: FollowUpUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    fu = (db.query(FollowUp).options(joinedload(FollowUp.patient),
                                     joinedload(FollowUp.doctor),
                                     joinedload(FollowUp.department)).filter(
                                         FollowUp.id == followup_id).first())
    if not fu:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    if not (getattr(user, "is_admin", False) or user.id == fu.doctor_user_id
            or has_perm(user, "appointments.update")):
        raise HTTPException(status_code=403, detail="Not permitted")

    fu.due_date = payload.due_date
    fu.note = payload.note or None
    db.commit()
    db.refresh(fu)
    return _build_followup_row(fu)


class FollowUpSchedulePayload(BaseModel):
    date: Optional[dt_date] = None
    slot_start: Optional[
        str] = None  # "HH:MM" for slot booking; if omitted => free booking


@router.post("/followups/{followup_id}/schedule")
def schedule_followup(
        followup_id: int,
        payload: FollowUpSchedulePayload = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    fu = (db.query(FollowUp).options(joinedload(FollowUp.patient),
                                     joinedload(FollowUp.doctor),
                                     joinedload(FollowUp.department)).filter(
                                         FollowUp.id == followup_id).first())
    if not fu:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    if fu.status not in {"waiting", "cancelled"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot schedule follow-up in status {fu.status}")

    if not (getattr(user, "is_admin", False) or user.id == fu.doctor_user_id
            or has_perm(user, "appointments.create")):
        raise HTTPException(status_code=403, detail="Not permitted")

    d = payload.date or fu.due_date
    if not d:
        raise HTTPException(status_code=400, detail="date required")

    _ensure_no_patient_duplicate(db, fu.patient_id, d)

    appt_type = "slot" if (payload.slot_start
                           and payload.slot_start.strip()) else "free"

    slot_start = None
    slot_end = None
    if appt_type == "slot":
        slot_start = _parse_slot(payload.slot_start.strip())
        sch = _get_active_schedule_for_doctor_and_date(db, fu.doctor_user_id,
                                                       d)
        slot_end = _compute_slot_end(
            d, slot_start, int(getattr(sch, "slot_minutes", 15) or 15))

        _check_slot_in_schedule(sch, d, slot_start, slot_end)
        _ensure_not_past(d, slot_start)
        _ensure_slot_free_for_doctor(db, fu.doctor_user_id, d, slot_start)
    else:
        _ensure_not_past_date_only(d)

    queue_no = _next_queue_no(db, fu.doctor_user_id, d)

    ap = Appointment(
        patient_id=fu.patient_id,
        department_id=fu.department_id,
        doctor_user_id=fu.doctor_user_id,
        date=d,
        appointment_type=appt_type,
        queue_no=queue_no,
        slot_start=slot_start,
        slot_end=slot_end,
        purpose="Follow-up",
        status="booked",
    )
    if hasattr(Appointment, "booked_by"):
        ap.booked_by = user.id

    db.add(ap)
    db.flush()

    fu.appointment_id = ap.id
    fu.due_date = d
    fu.status = "scheduled"

    db.commit()

    return {
        "message": "Follow-up confirmed"
        if appt_type == "free" else "Follow-up scheduled",
        "followup_id": fu.id,
        "appointment_id": ap.id,
        "appointment_type": appt_type,
        "queue_no": queue_no,
        "date": str(d),
        "slot_start": slot_start.strftime("%H:%M") if slot_start else None,
        "slot_end": slot_end.strftime("%H:%M") if slot_end else None,
    }


# ------------------- DOCTOR FEES MASTER -------------------
def _doctor_fee_to_out(row: DoctorFee,
                       doctor_name: Optional[str] = None) -> DoctorFeeOut:
    dto = DoctorFeeOut.model_validate(row, from_attributes=True)
    if doctor_name:
        dto.doctor_name = doctor_name
    return dto


@router.get("/doctor-fees", response_model=List[DoctorFeeOut])
def list_doctor_fees(
        doctor_user_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user, {"schedules.manage", "billing.view", "appointments.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = (db.query(DoctorFee, User.name).join(
        User, User.id == DoctorFee.doctor_user_id).order_by(User.name.asc()))
    if doctor_user_id:
        q = q.filter(DoctorFee.doctor_user_id == doctor_user_id)

    rows = q.all()
    return [_doctor_fee_to_out(fee, doc_name) for fee, doc_name in rows]


@router.post("/doctor-fees", response_model=DoctorFeeOut)
def create_doctor_fee(
        payload: DoctorFeeCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "schedules.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    doc = db.get(User, payload.doctor_user_id)
    if not doc or not getattr(doc, "is_active", True):
        raise HTTPException(status_code=404, detail="Doctor user not found")

    existing = db.query(DoctorFee).filter(
        DoctorFee.doctor_user_id == payload.doctor_user_id).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Fee already exists for this doctor. Use update instead.")

    fee = DoctorFee(
        doctor_user_id=payload.doctor_user_id,
        base_fee=payload.base_fee,
        followup_fee=payload.followup_fee,
        currency=payload.currency or "INR",
        is_active=payload.is_active,
        notes=payload.notes or None,
    )
    db.add(fee)
    db.commit()
    db.refresh(fee)
    return _doctor_fee_to_out(fee, doctor_name=doc.name)


@router.get("/doctor-fees/{fee_id}", response_model=DoctorFeeOut)
def get_doctor_fee(
        fee_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user, {"schedules.manage", "billing.view", "appointments.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")

    fee = db.get(DoctorFee, fee_id)
    if not fee:
        raise HTTPException(status_code=404, detail="Not found")
    doc = db.get(User, fee.doctor_user_id)
    return _doctor_fee_to_out(fee, doctor_name=doc.name if doc else None)


@router.put("/doctor-fees/{fee_id}", response_model=DoctorFeeOut)
def update_doctor_fee(
        fee_id: int,
        payload: DoctorFeeUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "schedules.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    fee = db.get(DoctorFee, fee_id)
    if not fee:
        raise HTTPException(status_code=404, detail="Not found")

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(fee, k, v)

    if hasattr(fee, "updated_at"):
        fee.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(fee)
    doc = db.get(User, fee.doctor_user_id)
    return _doctor_fee_to_out(fee, doctor_name=doc.name if doc else None)


@router.delete("/doctor-fees/{fee_id}")
def delete_doctor_fee(
        fee_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "schedules.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    fee = db.get(DoctorFee, fee_id)
    if not fee:
        raise HTTPException(status_code=404, detail="Not found")

    db.delete(fee)
    db.commit()
    return {"message": "Deleted"}


# ------------------- OPD DASHBOARD (Summary) -------------------
@router.get("/dashboard")
def opd_dashboard_summary(
        date_from: Optional[dt_date] = Query(
            None, description="From date (YYYY-MM-DD), default = last 7 days"),
        date_to: Optional[dt_date] = Query(
            None, description="To date (YYYY-MM-DD), default = today"),
        doctor_id: Optional[int] = Query(
            None, description="Optional filter for a single doctor_user_id"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "appointments.view")
            or has_perm(user, "mis.opd.view")):
        raise HTTPException(status_code=403, detail="Not permitted")

    if date_to is None:
        date_to = dt_date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=6)

    base_filters = [Appointment.date >= date_from, Appointment.date <= date_to]
    if doctor_id:
        base_filters.append(Appointment.doctor_user_id == doctor_id)

    ap_base = db.query(Appointment).filter(*base_filters)

    total_appointments = ap_base.count()
    total_completed = ap_base.filter(Appointment.status == "completed").count()
    total_no_show = ap_base.filter(Appointment.status == "no_show").count()
    total_cancelled = ap_base.filter(Appointment.status == "cancelled").count()
    total_checked_in = ap_base.filter(
        Appointment.status == "checked_in").count()
    total_in_progress = ap_base.filter(
        Appointment.status == "in_progress").count()

    unique_patients = (db.query(
        func.count(func.distinct(
            Appointment.patient_id))).filter(*base_filters).scalar() or 0)

    fu_filters = [FollowUp.due_date >= date_from, FollowUp.due_date <= date_to]
    if doctor_id:
        fu_filters.append(FollowUp.doctor_user_id == doctor_id)

    fu_base = db.query(FollowUp).filter(*fu_filters)

    total_followups = fu_base.count()
    followups_waiting = fu_base.filter(FollowUp.status == "waiting").count()
    followups_scheduled = fu_base.filter(
        FollowUp.status == "scheduled").count()
    followups_completed = fu_base.filter(
        FollowUp.status == "completed").count()
    followups_cancelled = fu_base.filter(
        FollowUp.status == "cancelled").count()

    fu_per_doc_rows = (db.query(
        FollowUp.doctor_user_id.label("doctor_id"),
        func.count(FollowUp.id).label("total")).filter(*fu_filters).group_by(
            FollowUp.doctor_user_id).all())
    fu_per_doc = {row.doctor_id: row.total for row in fu_per_doc_rows}

    doc_rows = (db.query(
        Appointment.doctor_user_id.label("doctor_id"),
        User.name.label("doctor_name"),
        Department.name.label("department_name"),
        func.count(Appointment.id).label("total"),
        func.sum(case((Appointment.status == "completed", 1),
                      else_=0)).label("completed"),
        func.sum(case((Appointment.status == "no_show", 1),
                      else_=0)).label("no_show"),
        func.sum(case((Appointment.status == "cancelled", 1),
                      else_=0)).label("cancelled"),
    ).join(User, User.id == Appointment.doctor_user_id).outerjoin(
        Department, Department.id == Appointment.department_id).filter(
            *base_filters).group_by(Appointment.doctor_user_id, User.name,
                                    Department.name).order_by(
                                        func.count(
                                            Appointment.id).desc()).all())

    doctor_stats: List[Dict[str, Any]] = []
    for row in doc_rows:
        doc_id = row.doctor_id
        doctor_stats.append({
            "doctor_id":
            doc_id,
            "doctor_name":
            row.doctor_name,
            "department_name":
            row.department_name,
            "total_appointments":
            int(row.total or 0),
            "completed":
            int(row.completed or 0),
            "no_show":
            int(row.no_show or 0),
            "cancelled":
            int(row.cancelled or 0),
            "followups_in_range":
            int(fu_per_doc.get(doc_id, 0)),
        })

    top_by_appointments = max(
        doctor_stats,
        key=lambda d: d["total_appointments"]) if doctor_stats else None
    top_by_completed = max(
        doctor_stats, key=lambda d: d["completed"]) if doctor_stats else None

    return {
        "range": {
            "date_from": str(date_from),
            "date_to": str(date_to)
        },
        "appointments": {
            "total": total_appointments,
            "completed": total_completed,
            "no_show": total_no_show,
            "cancelled": total_cancelled,
            "checked_in": total_checked_in,
            "in_progress": total_in_progress,
            "unique_patients": unique_patients,
        },
        "followups": {
            "total": total_followups,
            "waiting": followups_waiting,
            "scheduled": followups_scheduled,
            "completed": followups_completed,
            "cancelled": followups_cancelled,
        },
        "doctor_stats": doctor_stats,
        "top_doctor_by_appointments": top_by_appointments,
        "top_doctor_by_completed": top_by_completed,
    }


# ------------------------------------------------------------
# Follow-up History for a Visit
# GET /api/opd/visits/{visit_id}/followups
# ------------------------------------------------------------
@router.get("/visits/{visit_id}/followups",
            response_model=List[FollowUpListItem])
def list_visit_followups(
        visit_id: int,
        scope: str = Query("patient", pattern="^(patient|visit)$"),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    v = db.query(Visit).filter(Visit.id == visit_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    q = db.query(FollowUp).options(joinedload(FollowUp.appointment))

    if scope == "visit":
        q = q.filter(FollowUp.source_visit_id == visit_id)
    else:
        if hasattr(FollowUp, "patient_id"):
            q = q.filter(FollowUp.patient_id == v.patient_id)
        else:
            q = q.join(Visit, FollowUp.source_visit_id == Visit.id).filter(
                Visit.patient_id == v.patient_id)

    rows = q.order_by(desc(FollowUp.due_date),
                      desc(FollowUp.id)).limit(limit).all()

    # map episode_id (optional)
    src_ids = list({r.source_visit_id for r in rows if r.source_visit_id})
    ep_map = {}
    if src_ids:
        for vid, ep in db.query(Visit.id, Visit.episode_id).filter(
                Visit.id.in_(src_ids)).all():
            ep_map[int(vid)] = ep

    out = []
    for r in rows:
        ap = getattr(r, "appointment", None)
        out.append({
            "id":
            r.id,
            "due_date":
            r.due_date,
            "status":
            r.status,
            "note":
            r.note,
            "created_at":
            getattr(r, "created_at", None),
            "source_visit_id":
            r.source_visit_id,
            "source_episode_id":
            ep_map.get(r.source_visit_id),

            # ✅ bring back old UI fields
            "appointment_id":
            r.appointment_id,
            "appointment_date":
            ap.date if ap else None,
            "slot_start":
            ap.slot_start.strftime("%H:%M") if ap and ap.slot_start else None,
            "slot_end":
            ap.slot_end.strftime("%H:%M") if ap and ap.slot_end else None,
        })
    return out


@router.get("/visits/{visit_id}/summary-pdf")
@router.get(
    "/visits/{visit_id}/summary.pdf")  # ✅ alias for frontend compatibility
def get_visit_summary_pdf(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "visits.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    buff = build_visit_summary_pdf(db, visit_id)
    filename = f"opd_visit_{visit_id}_summary.pdf"

    return StreamingResponse(
        buff,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
