from __future__ import annotations
from datetime import datetime, date as dt_date, time as dt_time, timedelta, date
from typing import Optional, List, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.department import Department
from app.models.opd import (Appointment, Visit, Vitals, Prescription,
                            PrescriptionItem, LabOrder, RadiologyOrder,
                            OpdSchedule)
from app.schemas.opd import (AppointmentCreate, AppointmentRow, VisitCreate,
                             VisitOut, VisitUpdate, VitalsIn, PrescriptionIn,
                             OrderIdsIn, SlotOut)
from app.models.role import Role
from app.services.billing_auto import auto_add_item_for_event, maybe_finalize_visit_invoice
from zoneinfo import ZoneInfo
import os

router = APIRouter()

LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Kolkata"))
ACTIVE_STATUSES = ("booked", "checked_in", "in_progress")
NON_BLOCKING_STATUSES = ("completed", "cancelled", "no_show")


def now_local():
    return datetime.now(LOCAL_TZ)


def slot_dt_ist(d: date, hhmm: str) -> datetime:
    try:
        HH, MM = map(int, hhmm.split(":"))
        return datetime(d.year, d.month, d.day, HH, MM, tzinfo=IST)
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Invalid slot_start format. Expected HH:MM")


# helper: quick computed slot_end (15 minutes) if your system doesn’t set it elsewhere
def compute_slot_end(hhmm: str, minutes: int = 15) -> str:
    HH, MM = map(int, hhmm.split(":"))
    base = datetime(2000, 1, 1, HH, MM)
    end = base + timedelta(minutes=minutes)
    return end.strftime("%H:%M")


from datetime import timedelta


# ---------------- permissions ----------------
def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _has_any_perm(user: User, codes: set[str]) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code in codes:
                return True
    return False


# -------- constants / helpers --------
ALLOWED_STATUS = {
    "booked", "checked_in", "in_progress", "completed", "no_show", "cancelled"
}
TRANSITIONS = {
    "booked": {"checked_in", "cancelled", "no_show"},
    "checked_in": {"in_progress", "cancelled"},
    "in_progress": {"completed"},
    "completed": set(),
    "cancelled": set(),
    "no_show": set(),
}
BUSY_STATUS = {"booked", "checked_in", "in_progress"}


def vitals_done_on(db: Session, patient_id: int, d: dt_date) -> bool:
    start = datetime(d.year, d.month, d.day)
    end = start.replace(hour=23, minute=59, second=59)
    row = db.query(Vitals).filter(
        Vitals.patient_id == patient_id,
        Vitals.created_at >= start,
        Vitals.created_at <= end,
    ).first()
    return bool(row)


def episode_id_for_month(db: Session) -> str:
    ym = datetime.utcnow().strftime("%Y%m")
    count = db.query(Visit).filter(Visit.episode_id.like(f"OP-{ym}-%")).count()
    return f"OP-{ym}-{count+1:04d}"


# ------------------- LOOKUPS -------------------
@router.get("/departments", response_model=List[dict])
def opd_departments(db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    if not _has_any_perm(user, {
            "schedules.manage", "appointments.view", "visits.view",
            "patients.view"
    }):
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
            user, {
                "schedules.manage", "appointments.view", "appointments.create",
                "visits.view", "vitals.create"
            }):
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
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not _has_any_perm(
            user, {
                "schedules.manage", "appointments.create", "appointments.view",
                "visits.create", "visits.view", "vitals.create"
            }):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(User).filter(User.is_active.is_(True),
                              User.department_id == department_id)
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
        "roles": [r.name for r in (u.roles or [])]
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
):
    # accept either ?date_str=YYYY-MM-DD or ?date=YYYY-MM-DD
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

    # Busy (booked/checked_in/in_progress)
    busy = set(
        t[0].strftime("%H:%M")
        for t in db.query(Appointment.slot_start).filter(
            Appointment.doctor_user_id == doctor_user_id,
            Appointment.date == d,
            Appointment.status.in_({"booked", "checked_in", "in_progress"}),
        ).all())

    now = now_local()
    out = []
    for sch in schedules:
        step = timedelta(minutes=sch.slot_minutes or slot_minutes)
        cur = datetime.combine(d, sch.start_time, LOCAL_TZ)
        end_dt = datetime.combine(d, sch.end_time, LOCAL_TZ)
        while cur + step <= end_dt:
            hhmm = cur.strftime("%H:%M")
            # compute slot status
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
                # legacy behavior: only future free slots
                if status == "free":
                    out.append({
                        "start": hhmm,
                        "end": (cur + step).strftime("%H:%M")
                    })
            cur += step

    # legacy: return list[SlotOut] (start/end only). detailed: wrap in object
    if detailed:
        return {"slots": out}
    # de-dup & sort (legacy)
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

    # parse slot_start "HH:MM"
    try:
        slot_start = datetime.strptime(payload.slot_start, "%H:%M").time()
    except Exception:
        raise HTTPException(400, "Invalid slot_start")

    # must match an active schedule window for that weekday
    weekday = payload.date.weekday()
    sch = (db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == payload.doctor_user_id,
        OpdSchedule.weekday == weekday,
        OpdSchedule.is_active.is_(True),
    ).order_by(OpdSchedule.start_time.asc()).first())
    if not sch:
        raise HTTPException(400,
                            "No active schedule for the doctor on that day")

    if not (sch.start_time <= slot_start < sch.end_time):
        raise HTTPException(400, "slot_start outside schedule window")

    # compute slot_end using schedule slot minutes
    step = timedelta(minutes=sch.slot_minutes or 15)
    slot_dt = datetime.combine(payload.date, slot_start)
    slot_end = (slot_dt + step).time()
    if slot_end > sch.end_time:
        raise HTTPException(400, "slot_end exceeds schedule window")

    # block past date/time (local tz)
    req_dt_local = datetime.combine(payload.date, slot_start, LOCAL_TZ)
    if req_dt_local < now_local():
        raise HTTPException(400, "Cannot book a past slot")

    # 🔒 NEW: duplicate active booking check for same patient + same date
    dup = db.query(Appointment).filter(
        Appointment.patient_id == payload.patient_id,
        Appointment.date == payload.date,
        Appointment.status.in_(
            BUSY_STATUS),  # {"booked","checked_in","in_progress"}
    ).first()
    if dup:
        raise HTTPException(
            400,
            f"Duplicate booking blocked: patient already has an active appointment at "
            f"{dup.slot_start.strftime('%H:%M')} (status: {dup.status}) on {payload.date}."
        )

    # existing: doctor/time conflict (busy statuses only)
    exists = db.query(Appointment).filter(
        Appointment.doctor_user_id == payload.doctor_user_id,
        Appointment.date == payload.date,
        Appointment.slot_start == slot_start,
        Appointment.status.in_(BUSY_STATUS),
    ).first()
    if exists:
        raise HTTPException(409, "Slot already booked")

    # create
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
        joinedload(Appointment.patient), joinedload(Appointment.doctor),
        joinedload(Appointment.department)).filter(Appointment.date == date))
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

    visit = db.query(Visit).filter(Visit.appointment_id == ap.id).first()
    if new_status == "checked_in":
        if ap.status in {"cancelled", "completed", "no_show"}:
            raise HTTPException(400, "Cannot check-in from current status")
        if not visit:
            epi = episode_id_for_month(db)
            visit = Visit(
                appointment_id=ap.id,
                patient_id=ap.patient_id,
                department_id=ap.department_id,
                doctor_user_id=ap.doctor_user_id,
                episode_id=epi,
            )
            db.add(visit)
            db.flush()

    ap.status = new_status

    if new_status == "completed":
        if not visit:
            epi = episode_id_for_month(db)
            visit = Visit(
                appointment_id=ap.id,
                patient_id=ap.patient_id,
                department_id=ap.department_id,
                doctor_user_id=ap.doctor_user_id,
                episode_id=epi,
            )
            db.add(visit)
            db.flush()

        # Auto-billing OPD consultation
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
        # very rare: episode_id collision -> retry id
        if visit:
            visit.episode_id = episode_id_for_month(db)
            db.commit()
    return {
        "message": "Updated",
        "status": ap.status,
        "visit_id": visit.id if visit else None
    }


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
    v = Visit(
        appointment_id=ap.id,
        patient_id=ap.patient_id,
        department_id=ap.department_id,
        doctor_user_id=ap.doctor_user_id,
        episode_id=epi,
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
        visit_at=v.visit_at.isoformat(timespec="minutes"),
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
        raise HTTPException(403, "Not permitted")
    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(404, "Visit not found")

    # single insert loop (no duplicates)
    for tid in payload.test_ids:
        order = LabOrder(visit_id=visit_id, test_id=tid)
        db.add(order)
        db.flush()
        auto_add_item_for_event(db,
                                service_type="lab",
                                ref_id=order.id,
                                patient_id=v.patient_id,
                                context_type="opd",
                                context_id=v.id,
                                user_id=user.id)
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
        raise HTTPException(403, "Not permitted")
    v = db.get(Visit, visit_id)
    if not v:
        raise HTTPException(404, "Visit not found")

    for tid in payload.test_ids:
        order = RadiologyOrder(visit_id=visit_id, test_id=tid)
        db.add(order)
        db.flush()
        auto_add_item_for_event(db,
                                service_type="radiology",
                                ref_id=order.id,
                                patient_id=v.patient_id,
                                context_type="opd",
                                context_id=v.id,
                                user_id=user.id)
    db.commit()
    return {"message": "Radiology orders added"}
