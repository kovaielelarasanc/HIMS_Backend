# backend/app/api/routes_opd_clinical.py
from __future__ import annotations
from datetime import datetime, date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Appointment, Vitals, Visit

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
    # FE may send appointment_id; we use it only to resolve patient if patient_id omitted
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
    if not patient_id and payload.appointment_id:
        appt = db.query(Appointment).get(payload.appointment_id)
        if not appt:
            raise HTTPException(status_code=404,
                                detail="Appointment not found")
        patient_id = appt.patient_id

    if not patient_id:
        raise HTTPException(status_code=400,
                            detail="patient_id could not be resolved")

    vit = Vitals(
        patient_id=patient_id,
        height_cm=payload.height_cm,
        weight_kg=payload.weight_kg,
        temp_c=payload.temp_c,
        pulse=payload.pulse,
        rr=payload.resp_rate,  # map
        spo2=int(payload.spo2) if payload.spo2 is not None else None,
        bp_systolic=payload.bp_sys,  # map
        bp_diastolic=payload.bp_dia,  # map
        notes=payload.notes or "",
        created_at=datetime.utcnow(),
    )
    db.add(vit)
    db.commit()
    db.refresh(vit)
    return {
        "message": "Vitals recorded",
        "id": vit.id,
        "patient_id": vit.patient_id,
        "created_at": vit.created_at,
    }


# ----- QUEUE -----
@router.get("/queue")
def get_queue(
        doctor_user_id: int = Query(...),
        for_date: date = Query(default_factory=date.today),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "appointments.view")
            or has_perm(user, "visits.view") or user.is_admin):
        raise HTTPException(status_code=403, detail="Not permitted")

    appts = (db.query(Appointment).options(joinedload(
        Appointment.patient)).filter(
            Appointment.doctor_user_id == doctor_user_id,
            Appointment.date == for_date).order_by(
                Appointment.slot_start.asc()).all())

    vis_map = {
        v.appointment_id: v.id
        for v in db.query(Visit).filter(
            Visit.appointment_id.in_([a.id for a in appts])).all()
    }

    vitals_map = {}
    if hasattr(Vitals, "appointment_id"):
        rows = (db.query(Vitals.appointment_id).filter(
            Vitals.appointment_id.in_([a.id for a in appts])).distinct().all())
        for (aid, ) in rows:
            vitals_map[aid] = True
    else:
        start = datetime(for_date.year, for_date.month, for_date.day, 0, 0, 0)
        end = datetime(for_date.year, for_date.month, for_date.day, 23, 59, 59)
        patient_ids = list({a.patient_id for a in appts})
        rows = (db.query(Vitals.patient_id).filter(
            Vitals.patient_id.in_(patient_ids)).filter(
                Vitals.created_at >= start, Vitals.created_at
                <= end).distinct().all())
        patients_with_vitals = {pid for (pid, ) in rows}
        for a in appts:
            if a.patient_id in patients_with_vitals:
                vitals_map[a.id] = True

    resp = []
    for a in appts:
        p: Patient = a.patient
        resp.append({
            "appointment_id": a.id,
            "time": a.slot_start.strftime("%H:%M"),
            "status": a.status,
            "visit_id": vis_map.get(a.id),
            "doctor_user_id": a.doctor_user_id,
            "booked_by": getattr(a, "booked_by", None),
            "patient": {
                "id": p.id,
                "uhid": p.uhid,
                "name": f"{p.first_name} {p.last_name or ''}".strip(),
                "phone": p.phone,
            },
            "has_vitals": bool(vitals_map.get(a.id, False)),
            "visit_purpose": a.purpose or "",
        })
    return resp
