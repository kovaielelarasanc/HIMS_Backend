# app/api/routes_opd_clinical.py
from __future__ import annotations
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Appointment, Vitals, Visit
from sqlalchemy import func

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


# ----- VITALS -----
from pydantic import BaseModel, Field, validator


class VitalsCreate(BaseModel):
    appointment_id: Optional[int] = Field(None)
    patient_id: Optional[int] = Field(None)

    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    temp_c: Optional[float] = None
    pulse: Optional[int] = None
    resp_rate: Optional[int] = None
    spo2: Optional[float] = None
    bp_sys: Optional[int] = None
    bp_dia: Optional[int] = None
    notes: Optional[str] = None

    @validator("appointment_id", always=True)
    def at_least_one_id(cls, v, values):
        if not v and not values.get("patient_id"):
            raise ValueError("Either appointment_id or patient_id is required")
        return v


@router.post("/vitals")
def record_vitals(
        payload: VitalsCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "vitals.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient_id = payload.patient_id
    appt = None

    if not patient_id and payload.appointment_id:
        appt = db.query(Appointment).get(payload.appointment_id)
        if not appt:
            raise HTTPException(status_code=404,
                                detail="Appointment not found")
        patient_id = appt.patient_id

    if not patient_id:
        raise HTTPException(
            status_code=400,
            detail="patient_id could not be resolved",
        )

    vit_kwargs = dict(
        patient_id=patient_id,
        height_cm=payload.height_cm,
        weight_kg=payload.weight_kg,
        temp_c=payload.temp_c,
        pulse=payload.pulse,
        rr=payload.resp_rate,
        spo2=int(payload.spo2) if payload.spo2 is not None else None,
        bp_systolic=payload.bp_sys,
        bp_diastolic=payload.bp_dia,
        notes=payload.notes or "",
        created_at=datetime.utcnow(),
    )

    if hasattr(Vitals, "appointment_id") and payload.appointment_id:
        vit_kwargs["appointment_id"] = payload.appointment_id

    vit = Vitals(**vit_kwargs)
    db.add(vit)
    db.commit()
    db.refresh(vit)

    return {
        "message": "Vitals recorded",
        "id": vit.id,
        "patient_id": vit.patient_id,
        "appointment_id": getattr(vit, "appointment_id", None),
        "created_at": vit.created_at,
    }


# ----- VITALS (GET latest) -----


def _vitals_out(v: Vitals):
    return {
        "id": v.id,
        "created_at": v.created_at,
        "appointment_id": getattr(v, "appointment_id", None),
        "patient_id": v.patient_id,
        "height_cm": v.height_cm,
        "weight_kg": v.weight_kg,
        "temp_c": v.temp_c,
        "pulse": v.pulse,
        "resp_rate": getattr(v, "rr", None),
        "spo2": v.spo2,
        "bp_sys": getattr(v, "bp_systolic", None),
        "bp_dia": getattr(v, "bp_diastolic", None),
        "notes": v.notes or "",
    }


@router.get("/vitals/latest")
def get_latest_vitals(
        appointment_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        for_date: Optional[date] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "vitals.create")
            or has_perm(user, "appointments.view") or user.is_admin):
        raise HTTPException(status_code=403, detail="Not permitted")

    if not appointment_id and not patient_id:
        raise HTTPException(status_code=400,
                            detail="appointment_id or patient_id required")

    q = db.query(Vitals)

    if appointment_id and hasattr(Vitals, "appointment_id"):
        q = q.filter(Vitals.appointment_id == appointment_id)
    else:
        # resolve patient_id from appointment if needed
        if not patient_id and appointment_id:
            appt = db.query(Appointment).get(appointment_id)
            if not appt:
                raise HTTPException(status_code=404,
                                    detail="Appointment not found")
            patient_id = appt.patient_id
        q = q.filter(Vitals.patient_id == patient_id)

    if for_date:
        start = datetime(for_date.year, for_date.month, for_date.day, 0, 0, 0)
        end = datetime(for_date.year, for_date.month, for_date.day, 23, 59, 59)
        q = q.filter(Vitals.created_at >= start, Vitals.created_at <= end)

    v = q.order_by(Vitals.created_at.desc()).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vitals not found")

    return _vitals_out(v)


@router.get("/vitals/history")
def get_vitals_history(
        appointment_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        for_date: Optional[date] = Query(None),
        limit: int = Query(3, ge=1, le=20),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "vitals.create")
            or has_perm(user, "appointments.view") or user.is_admin):
        raise HTTPException(status_code=403, detail="Not permitted")

    if not appointment_id and not patient_id:
        raise HTTPException(status_code=400,
                            detail="appointment_id or patient_id required")

    q = db.query(Vitals)

    if appointment_id and hasattr(Vitals, "appointment_id"):
        q = q.filter(Vitals.appointment_id == appointment_id)
    else:
        if not patient_id and appointment_id:
            appt = db.query(Appointment).get(appointment_id)
            if not appt:
                raise HTTPException(status_code=404,
                                    detail="Appointment not found")
            patient_id = appt.patient_id
        q = q.filter(Vitals.patient_id == patient_id)

    if for_date:
        start = datetime(for_date.year, for_date.month, for_date.day, 0, 0, 0)
        end = datetime(for_date.year, for_date.month, for_date.day, 23, 59, 59)
        q = q.filter(Vitals.created_at >= start, Vitals.created_at <= end)

    rows = q.order_by(Vitals.created_at.desc()).limit(limit).all()
    return [_vitals_out(v) for v in rows]


# ----- QUEUE -----
# ----- QUEUE -----
@router.get("/queue")
def get_queue(
        doctor_user_id: Optional[int] = Query(
            None,
            description=
            "If omitted and current user is a doctor, uses current user's id",
        ),
        for_date: date = Query(default_factory=date.today),

        # ✅ NEW: force current doctor queue
        my_only: bool = Query(
            False,
            description=
            "If true, returns ONLY current logged-in doctor's appointments"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "appointments.view") or has_perm(
            user, "visits.view") or user.is_admin or user.is_doctor):
        raise HTTPException(status_code=403, detail="Not permitted")

    # ✅ If my_only ON → only doctors allowed and FORCE target doctor = current user
    if my_only:
        if not user.is_doctor:
            raise HTTPException(status_code=403,
                                detail="My appointments is only for doctors")
        target_doctor_id = user.id
    else:
        target_doctor_id = doctor_user_id
        if target_doctor_id is None:
            if user.is_doctor:
                target_doctor_id = user.id
            else:
                raise HTTPException(
                    status_code=400,
                    detail="doctor_user_id is required for non-doctor users",
                )
        else:
            if user.is_doctor and target_doctor_id != user.id and not has_perm(
                    user, "appointments.view"):
                raise HTTPException(
                    status_code=403,
                    detail="Not permitted to view other doctor's queue",
                )

    appts = (db.query(Appointment).options(joinedload(
        Appointment.patient)).filter(
            Appointment.doctor_user_id == target_doctor_id,
            Appointment.date == for_date,
        ).order_by(Appointment.slot_start.asc()).all())

    appt_ids = [a.id for a in appts]

    vis_map = {}
    if appt_ids:
        vis_map = {
            v.appointment_id: v.id
            for v in db.query(Visit).filter(Visit.appointment_id.in_(
                appt_ids)).all()
        }

    vitals_map = {}
    vitals_last_at_map = {}

    if appt_ids:
        rows = (db.query(Vitals.appointment_id,
                         func.max(Vitals.created_at)).filter(
                             Vitals.appointment_id.in_(appt_ids)).group_by(
                                 Vitals.appointment_id).all())
        for aid, last_at in rows:
            vitals_map[aid] = True
            vitals_last_at_map[aid] = last_at

    resp = []
    for a in appts:
        p: Patient = a.patient
        resp.append({
            "appointment_id": a.id,
            "time": a.slot_start.strftime("%H:%M"),
            "status": a.status,
            "visit_id": vis_map.get(a.id),
            "doctor_user_id": a.doctor_user_id,
            "department_id": a.department_id,
            "booked_by": getattr(a, "booked_by", None),
            "patient": {
                "id": p.id,
                "uhid": p.uhid,
                "name": f"{p.first_name} {p.last_name or ''}".strip(),
                "phone": p.phone,
            },
            "has_vitals": bool(vitals_map.get(a.id, False)),
            "vitals_last_at": vitals_last_at_map.get(a.id),
            "visit_purpose": a.purpose or "",
        })

    return resp
