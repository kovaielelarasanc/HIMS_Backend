# app/api/routes_opd.py
from __future__ import annotations
from datetime import datetime, date as dt_date, time as dt_time, timedelta, date
from typing import Optional, List, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, case

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.department import Department
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
    SlotOut,
    FollowUpCreate,
    FollowUpUpdate,
    FollowUpScheduleIn,
    FollowUpRow,
    AppointmentRescheduleIn,
)
from app.models.role import Role
from app.services.billing_auto import (
    auto_add_item_for_event,
    maybe_finalize_visit_invoice,
)
from zoneinfo import ZoneInfo
import os
from pydantic import BaseModel, Field

router = APIRouter()

LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Kolkata"))
ACTIVE_STATUSES = ("booked", "checked_in", "in_progress")
NON_BLOCKING_STATUSES = ("completed", "cancelled", "no_show")
BUSY_STATUS = {"booked", "checked_in", "in_progress"}


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


# ---------------- permissions ----------------
def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _has_any_perm(user: User, codes: Set[str]) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code in codes:
                return True
    return False


# -------- constants / helpers --------
ALLOWED_STATUS = {
    "booked",
    "checked_in",
    "in_progress",
    "completed",
    "no_show",
    "cancelled",
}
TRANSITIONS = {
    "booked": {"checked_in", "cancelled", "no_show"},
    "checked_in": {"in_progress", "cancelled"},
    "in_progress": {"completed"},
    "completed": set(),
    "cancelled": set(),
    "no_show": set(),
}


def _format_visit_time(v: Visit) -> str:
    """
    Format visit time for UI.

    Priority:
    1. Use Visit.visit_at if set.
    2. Fallback to appointment date + slot_start.
    """
    dt = v.visit_at

    if dt is None and v.appointment is not None:
        ap = v.appointment
        dt = datetime(
            ap.date.year,
            ap.date.month,
            ap.date.day,
            ap.slot_start.hour,
            ap.slot_start.minute,
        )

    if dt is None:
        return ""

    # Treat stored datetime as local naive and format
    return dt.strftime("%d %b %Y, %I:%M %p")


def vitals_done_on(db: Session, patient_id: int, d: dt_date) -> bool:
    """
    Legacy helper: 'Did this patient have any vitals on this calendar date?'

    Uses DATE(created_at) so it's robust against UTC/local differences.
    """
    row = (db.query(Vitals.id).filter(
        Vitals.patient_id == patient_id,
        func.date(Vitals.created_at) == d,
    ).first())
    return bool(row)


def episode_id_for_month(db: Session) -> str:
    ym = datetime.utcnow().strftime("%Y%m")
    count = (db.query(Visit).filter(
        Visit.episode_id.like(f"OP-{ym}-%")).count())
    return f"OP-{ym}-{count + 1:04d}"


# --------------- internal helpers for booking/reschedule ---------------


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
            detail="slot_start outside doctor's schedule window",
        )
    if slot_end > sch.end_time:
        raise HTTPException(
            status_code=400,
            detail="slot_end exceeds doctor's schedule window",
        )


def _ensure_not_past(d: dt_date, slot_start: dt_time) -> None:
    req_dt_local = datetime.combine(d, slot_start, LOCAL_TZ)
    if req_dt_local < now_local():
        raise HTTPException(status_code=400,
                            detail="Cannot book or reschedule to a past slot")


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
        raise HTTPException(
            status_code=400,
            detail=("Duplicate booking blocked: patient already has an active "
                    f"appointment at {dup.slot_start.strftime('%H:%M')} "
                    f"(status: {dup.status}) on {d}."),
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


# ------------------- LOOKUPS -------------------
@router.get("/departments", response_model=List[dict])
def opd_departments(
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user,
        {
            "schedules.manage",
            "appointments.view",
            "visits.view",
            "patients.view",
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
            "schedules.manage",
            "appointments.view",
            "appointments.create",
            "visits.view",
            "vitals.create",
        },
    ):
        raise HTTPException(status_code=403, detail="Not permitted")
    roles = (db.query(Role).join(Role.users).filter(
        User.is_active.is_(True),
        User.department_id == department_id,
    ).distinct().order_by(Role.name.asc()).all())
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
    """
    Department → Doctor filter:
    - Pass ?department_id= & is_doctor=true to get only consultant doctors list.
    """
    if not _has_any_perm(
            user,
        {
            "schedules.manage",
            "appointments.create",
            "appointments.view",
            "visits.create",
            "visits.view",
            "vitals.create",
        },
    ):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(User).filter(
        User.is_active.is_(True),
        User.department_id == department_id,
    )

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
        OpdSchedule.is_active.is_(True),
    ).distinct().all())
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
):
    if date_param:
        d = date_param
    elif date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(400, "Invalid date")
    else:
        raise HTTPException(400, "date required")

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
        ).all()
    }

    now = now_local()
    out = []
    for sch in schedules:
        step = timedelta(minutes=sch.slot_minutes or slot_minutes)
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
                    "status": status,
                })
            else:
                if status == "free":
                    out.append({
                        "start": hhmm,
                        "end": (cur + step).strftime("%H:%M"),
                    })
            cur += step

    if detailed:
        return {"slots": out}

    uniq = {(s["start"], s["end"]) for s in out}
    final = [{"start": a, "end": b} for (a, b) in sorted(uniq)]
    return final


# ------------------- APPOINTMENTS -------------------
@router.post("/appointments")
def create_appointment(
        payload: AppointmentCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "appointments.create"):
        raise HTTPException(403, "Not permitted")

    patient = db.get(Patient, payload.patient_id)
    if not patient or not patient.is_active:
        raise HTTPException(404, "Patient not found")

    slot_start = _parse_slot(payload.slot_start)
    sch = _get_active_schedule_for_doctor_and_date(db, payload.doctor_user_id,
                                                   payload.date)
    slot_end = _compute_slot_end(payload.date, slot_start, sch.slot_minutes)
    _check_slot_in_schedule(sch, payload.date, slot_start, slot_end)
    _ensure_not_past(payload.date, slot_start)
    _ensure_no_patient_duplicate(db, payload.patient_id, payload.date)
    _ensure_slot_free_for_doctor(db, payload.doctor_user_id, payload.date,
                                 slot_start)

    ap = Appointment(
        patient_id=payload.patient_id,
        department_id=payload.department_id,
        doctor_user_id=payload.doctor_user_id,
        date=payload.date,
        slot_start=slot_start,
        slot_end=slot_end,
        purpose=payload.purpose or "Consultation",
        status="booked",
    )
    db.add(ap)
    db.commit()
    return {"id": ap.id, "message": "Booked"}


def vitals_done_for_appointment(db: Session, ap: Appointment) -> bool:
    """
    Preferred helper now that Vitals can be linked to an Appointment.

    1. If Vitals.appointment_id exists, check that first.
    2. Fallback to legacy patient+date logic.
    """
    if hasattr(Vitals, "appointment_id"):
        exists = db.query(
            Vitals.id).filter(Vitals.appointment_id == ap.id).first()
        if exists:
            return True

    # Fallback: any vitals for this patient on that date
    return vitals_done_on(db, ap.patient_id, ap.date)


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
    rows = q.order_by(Appointment.slot_start).all()

    vis_by_appt = {
        v.appointment_id: v.id
        for v in db.query(Visit).filter(
            Visit.appointment_id.in_([r.id for r in rows])).all()
    }

    out: List[AppointmentRow] = []
    for r in rows:
        vitals_flag = vitals_done_for_appointment(db, r)

        out.append(
            AppointmentRow(
                id=r.id,
                uhid=r.patient.uhid,
                patient_name=
                f"{r.patient.first_name} {r.patient.last_name or ''}".strip(),
                doctor_name=r.doctor.name,
                department_name=r.department.name,
                date=r.date.isoformat(),
                slot_start=r.slot_start.strftime("%H:%M"),
                slot_end=r.slot_end.strftime("%H:%M"),
                status=r.status,
                visit_id=vis_by_appt.get(r.id),
                vitals_registered=vitals_flag,
                purpose=r.purpose or "Consultation",
            ))

    return out


class AppointmentStatusUpdate(BaseModel):
    status: str  # booked | checked_in | in_progress | completed | no_show | cancelled


@router.patch("/appointments/{appointment_id}/status")
def update_appointment_status(
        appointment_id: int,
        payload: AppointmentStatusUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    ap = db.get(Appointment, appointment_id)
    if not ap:
        raise HTTPException(404, "Appointment not found")

    allowed = user.is_admin or (user.id == ap.doctor_user_id) or has_perm(
        user, "appointments.update")
    if not allowed:
        raise HTTPException(403, "Not permitted")

    new_status = (payload.status or "").lower()
    if new_status not in ALLOWED_STATUS:
        raise HTTPException(400, "Invalid status")

    if ap.status not in TRANSITIONS or new_status not in TRANSITIONS[
            ap.status]:
        raise HTTPException(400,
                            f"Cannot move from {ap.status} to {new_status}")

    visit = db.query(Visit).filter(Visit.appointment_id == ap.id).first()

    # For checked_in → create Visit if missing
    if new_status == "checked_in":
        if not visit:
            epi = episode_id_for_month(db)
            visit = Visit(
                appointment_id=ap.id,
                patient_id=ap.patient_id,
                department_id=ap.department_id,
                doctor_user_id=ap.doctor_user_id,
                episode_id=epi,
                # local check-in time
                visit_at=now_local().replace(tzinfo=None),
            )
            db.add(visit)
            db.flush()

    ap.status = new_status

    # For completed → ensure Visit exists & billing
    if new_status == "completed":
        if not visit:
            epi = episode_id_for_month(db)
            visit = Visit(
                appointment_id=ap.id,
                patient_id=ap.patient_id,
                department_id=ap.department_id,
                doctor_user_id=ap.doctor_user_id,
                episode_id=epi,
                visit_at=now_local().replace(tzinfo=None),
            )
            db.add(visit)
            db.flush()
        elif visit.visit_at is None:
            # backfill visit_at if missing
            visit.visit_at = now_local().replace(tzinfo=None)

        auto_add_item_for_event(
            db,
            service_type="opd_consult",
            ref_id=visit.id,
            patient_id=visit.patient_id,
            context_type="opd",
            context_id=visit.id,
            user_id=user.id,
        )
        maybe_finalize_visit_invoice(db, visit.id)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if visit:
            visit.episode_id = episode_id_for_month(db)
            db.commit()
    return {
        "message": "Updated",
        "status": ap.status,
        "visit_id": visit.id if visit else None,
    }


# ---------- Appointment reschedule (waiting-time mgmt & normal) ----------
@router.post("/appointments/{appointment_id}/reschedule")
def reschedule_appointment(
        appointment_id: int,
        payload: AppointmentRescheduleIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Waiting-time Management:

    - For normal booked appointments: updates same row.
    - For no_show appointments: by default creates NEW appointment and keeps
      the old as history (unless create_new=False).
    """
    ap = db.get(Appointment, appointment_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Appointment not found")

    allowed = user.is_admin or user.id == ap.doctor_user_id or has_perm(
        user, "appointments.update")
    if not allowed:
        raise HTTPException(status_code=403, detail="Not permitted")

    new_date = payload.date
    new_slot_start = _parse_slot(payload.slot_start)

    sch = _get_active_schedule_for_doctor_and_date(db, ap.doctor_user_id,
                                                   new_date)
    slot_end = _compute_slot_end(new_date, new_slot_start, sch.slot_minutes)
    _check_slot_in_schedule(sch, new_date, new_slot_start, slot_end)
    _ensure_not_past(new_date, new_slot_start)

    if payload.create_new or ap.status == "no_show":
        # keep old as history, create new one
        _ensure_no_patient_duplicate(db, ap.patient_id, new_date)
        _ensure_slot_free_for_doctor(db, ap.doctor_user_id, new_date,
                                     new_slot_start)

        new_ap = Appointment(
            patient_id=ap.patient_id,
            department_id=ap.department_id,
            doctor_user_id=ap.doctor_user_id,
            date=new_date,
            slot_start=new_slot_start,
            slot_end=slot_end,
            purpose=ap.purpose or "Consultation",
            status="booked",
        )
        db.add(new_ap)
        db.commit()
        return {
            "message": "Rescheduled as new appointment",
            "old_appointment_id": ap.id,
            "appointment_id": new_ap.id,
        }

    # normal reschedule on same row
    _ensure_no_patient_duplicate(db, ap.patient_id, new_date, ap.id)
    _ensure_slot_free_for_doctor(
        db,
        ap.doctor_user_id,
        new_date,
        new_slot_start,
        exclude_appointment_id=ap.id,
    )

    ap.date = new_date
    ap.slot_start = new_slot_start
    ap.slot_end = slot_end
    if ap.status in {"cancelled", "no_show"}:
        ap.status = "booked"

    db.commit()
    return {
        "message": "Appointment rescheduled",
        "appointment_id": ap.id,
        "date": ap.date,
        "slot_start": ap.slot_start.strftime("%H:%M"),
        "slot_end": ap.slot_end.strftime("%H:%M"),
    }


# ---------- No-show listing screen ----------
@router.get("/appointments/noshow", response_model=List[AppointmentRow])
def list_no_show_appointments(
        for_date: Optional[dt_date] = Query(
            None, description="If omitted, today's date will be used"),
        doctor_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    For 'No-show appointments' management screen.
    """
    if not has_perm(user, "appointments.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    if for_date is None:
        for_date = dt_date.today()

    q = (db.query(Appointment).options(
        joinedload(Appointment.patient),
        joinedload(Appointment.doctor),
        joinedload(Appointment.department),
    ).filter(
        Appointment.date == for_date,
        Appointment.status == "no_show",
    ))
    if doctor_id:
        q = q.filter(Appointment.doctor_user_id == doctor_id)
    rows = q.order_by(Appointment.slot_start.asc()).all()

    vis_by_appt = {
        v.appointment_id: v.id
        for v in db.query(Visit).filter(
            Visit.appointment_id.in_([r.id for r in rows])).all()
    }

    out: List[AppointmentRow] = []
    for r in rows:
        out.append(
            AppointmentRow(
                id=r.id,
                uhid=r.patient.uhid,
                patient_name=
                f"{r.patient.first_name} {r.patient.last_name or ''}".strip(),
                doctor_name=r.doctor.name,
                department_name=r.department.name,
                date=r.date.isoformat(),
                slot_start=r.slot_start.strftime("%H:%M"),
                slot_end=r.slot_end.strftime("%H:%M"),
                status=r.status,
                visit_id=vis_by_appt.get(r.id),
                vitals_registered=vitals_done_on(db, r.patient_id, r.date),
                purpose=r.purpose or "Consultation",
            ))
    return out


# ------------------- VITALS -------------------
@router.post("/vitals/{patient_id}")
def record_vitals(
        patient_id: int,
        payload: VitalsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "vitals.create"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    v = Vitals(patient_id=patient_id, **payload.dict(exclude_unset=True))
    db.add(v)
    db.commit()
    return {"id": v.id, "message": "Vitals saved"}


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

    epi = episode_id_for_month(db)

    # actual check-in timestamp (local time, stored as naive)
    visit_at_local = now_local().replace(tzinfo=None)

    v = Visit(
        appointment_id=ap.id,
        patient_id=ap.patient_id,
        department_id=ap.department_id,
        doctor_user_id=ap.doctor_user_id,
        episode_id=epi,
        visit_at=visit_at_local,
    )
    ap.status = "checked_in"
    db.add(v)
    db.commit()
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
    ).get(visit_id))
    if not v:
        raise HTTPException(404, "Visit not found")

    if not (user.is_admin or v.doctor_user_id == user.id or user.department_id
            == v.department_id or has_perm(user, "visits.view")):
        raise HTTPException(403, "Not permitted")

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
            latest_vitals.created_at.isoformat(),
        }

    return VisitOut(
        id=v.id,
        uhid=v.patient.uhid,
        patient_name=f"{v.patient.first_name} {v.patient.last_name or ''}".
        strip(),
        department_name=v.department.name,
        doctor_name=v.doctor.name,
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
    for k, val in payload.dict(exclude_unset=True).items():
        setattr(v, k, val)
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
    if not (has_perm(user, "appointments.view") or has_perm(user, "opd.view")):
        raise HTTPException(403, "Not permitted")
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
        for it in list(rx.items):
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
    """
    Doctor finishes visit -> sets follow-up date.
    Creates a 'waiting' follow-up; no slot yet.
    """
    v = (db.query(Visit).options(
        joinedload(Visit.patient),
        joinedload(Visit.department),
        joinedload(Visit.doctor),
    ).get(visit_id))
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    if not (user.is_admin or user.id == v.doctor_user_id
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
            description="waiting | scheduled | completed | cancelled or *",
        ),
        doctor_id: Optional[int] = Query(None),
        date_from: Optional[dt_date] = Query(None),
        date_to: Optional[dt_date] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Waiting-time Management screen:
    - default shows waiting follow-ups (all doctors or filtered).
    """
    if not has_perm(user, "appointments.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = (db.query(FollowUp).options(
        joinedload(FollowUp.patient),
        joinedload(FollowUp.doctor),
        joinedload(FollowUp.department),
    ).order_by(FollowUp.due_date.asc(), FollowUp.id.asc()))

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
    """
    Simple change of due_date / note (still 'waiting').
    """
    fu = (db.query(FollowUp).options(
        joinedload(FollowUp.patient),
        joinedload(FollowUp.doctor),
        joinedload(FollowUp.department),
    ).get(followup_id))
    if not fu:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    if not (user.is_admin or user.id == fu.doctor_user_id
            or has_perm(user, "appointments.update")):
        raise HTTPException(status_code=403, detail="Not permitted")

    fu.due_date = payload.due_date
    fu.note = payload.note or None
    db.commit()
    db.refresh(fu)
    return _build_followup_row(fu)


@router.post("/followups/{followup_id}/schedule")
def schedule_followup(
        followup_id: int,
        body: dict = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Confirm a waiting follow-up into a real Appointment.

    This endpoint is intentionally lenient with payload shape to avoid 422
    validation errors. Expected JSON body:

    {
      "date": "YYYY-MM-DD",   # optional; if missing uses followup.due_date
      "slot_start": "HH:MM"   # required
    }
    """
    # Load follow-up with relations
    fu = (db.query(FollowUp).options(
        joinedload(FollowUp.patient),
        joinedload(FollowUp.doctor),
        joinedload(FollowUp.department),
    ).get(followup_id))
    if not fu:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    if fu.status not in {"waiting", "cancelled"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot schedule follow-up in status {fu.status}",
        )

    if not (user.is_admin or user.id == fu.doctor_user_id
            or has_perm(user, "appointments.create")):
        raise HTTPException(status_code=403, detail="Not permitted")

    # ---- Extract and normalize input fields from raw JSON ----
    raw_date = body.get("date")
    raw_slot = body.get("slot_start") or body.get("slot")

    if not raw_slot:
        raise HTTPException(status_code=400, detail="slot_start is required")

    # Resolve date
    if raw_date:
        if isinstance(raw_date, dt_date):
            d = raw_date
        elif isinstance(raw_date, datetime):
            d = raw_date.date()
        elif isinstance(raw_date, str):
            # Accept "YYYY-MM-DD" or an ISO string like "YYYY-MM-DDTHH:MM:SS"
            try:
                d = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid date format, expected YYYY-MM-DD",
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format, expected YYYY-MM-DD",
            )
    else:
        # Fallback: use existing follow-up due date
        d = fu.due_date

    # Parse time string into dt_time
    slot_start = _parse_slot(raw_slot)

    # ---- Apply normal booking rules ----
    sch = _get_active_schedule_for_doctor_and_date(db, fu.doctor_user_id, d)
    slot_end = _compute_slot_end(d, slot_start, sch.slot_minutes)
    _check_slot_in_schedule(sch, d, slot_start, slot_end)
    _ensure_not_past(d, slot_start)
    _ensure_no_patient_duplicate(db, fu.patient_id, d)
    _ensure_slot_free_for_doctor(db, fu.doctor_user_id, d, slot_start)

    # ---- Create appointment ----
    ap = Appointment(
        patient_id=fu.patient_id,
        department_id=fu.department_id,
        doctor_user_id=fu.doctor_user_id,
        date=d,
        slot_start=slot_start,
        slot_end=slot_end,
        purpose="Follow-up",
        status="booked",
    )
    db.add(ap)
    db.flush()

    # Link & update follow-up
    fu.appointment_id = ap.id
    fu.due_date = d
    fu.status = "scheduled"
    db.commit()

    return {
        "message": "Follow-up scheduled",
        "followup_id": fu.id,
        "appointment_id": ap.id,
        "date": str(d),
        "slot_start": slot_start.strftime("%H:%M"),
        "slot_end": slot_end.strftime("%H:%M"),
    }


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
    """
    OPD Dashboard summary:
    - Total appointments, by status
    - Unique patients
    - Follow-up counts
    - Doctor-wise appointment stats
    """

    # Permissions: either appointments.view OR mis.opd.view
    if not (has_perm(user, "appointments.view")
            or has_perm(user, "mis.opd.view")):
        raise HTTPException(status_code=403, detail="Not permitted")

    # Defaults: last 7 days
    if date_to is None:
        date_to = dt_date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=6)

    # Common filters
    base_filters = [
        Appointment.date >= date_from,
        Appointment.date <= date_to,
    ]
    if doctor_id:
        base_filters.append(Appointment.doctor_user_id == doctor_id)

    # ---- Appointment-level aggregates ----
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

    # ---- Follow-up aggregates ----
    fu_filters = [
        FollowUp.due_date >= date_from,
        FollowUp.due_date <= date_to,
    ]
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

    # Follow-ups per doctor (for mapping into doctor_stats)
    fu_per_doc_rows = (db.query(
        FollowUp.doctor_user_id.label("doctor_id"),
        func.count(FollowUp.id).label("total"),
    ).filter(*fu_filters).group_by(FollowUp.doctor_user_id).all())
    fu_per_doc = {row.doctor_id: row.total for row in fu_per_doc_rows}

    # ---- Doctor-wise appointment stats ----
    # Group by doctor & department
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
            *base_filters).group_by(
                Appointment.doctor_user_id,
                User.name,
                Department.name,
            ).order_by(func.count(Appointment.id).desc()).all())

    doctor_stats = []
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

    # Top doctors
    top_by_appointments = (max(doctor_stats,
                               key=lambda d: d["total_appointments"])
                           if doctor_stats else None)
    top_by_completed = (max(doctor_stats, key=lambda d: d["completed"])
                        if doctor_stats else None)

    return {
        "range": {
            "date_from": str(date_from),
            "date_to": str(date_to),
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
