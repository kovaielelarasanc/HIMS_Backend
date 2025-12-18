from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any, List, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from pydantic import BaseModel, Field, field_validator

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


# ---------------- VITALS ----------------
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

    @field_validator("appointment_id")
    @classmethod
    def _noop(cls, v):  # keep for symmetry
        return v

    @field_validator("patient_id")
    @classmethod
    def _noop2(cls, v):
        return v

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, v):
        return (v or "").strip() or None

    @field_validator("appointment_id", mode="before")
    @classmethod
    def at_least_one_id(cls, v, info):
        # pydantic v2: we validate at model level via after validator
        return v

    @field_validator("patient_id", mode="before")
    @classmethod
    def at_least_one_id_2(cls, v, info):
        return v

    def validate_ids(self):
        if not self.appointment_id and not self.patient_id:
            raise ValueError("Either appointment_id or patient_id is required")


@router.post("/vitals")
def record_vitals(
        payload: VitalsCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not has_perm(user, "vitals.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    try:
        payload.validate_ids()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    patient_id = payload.patient_id
    appt = None

    if not patient_id and payload.appointment_id:
        appt = db.get(Appointment, payload.appointment_id)
        if not appt:
            raise HTTPException(status_code=404,
                                detail="Appointment not found")
        patient_id = appt.patient_id

    if not patient_id:
        raise HTTPException(status_code=400,
                            detail="patient_id could not be resolved")

    vit_kwargs: Dict[str, Any] = dict(
        patient_id=patient_id,
        height_cm=payload.height_cm,
        weight_kg=payload.weight_kg,
        temp_c=payload.temp_c,
        pulse=payload.pulse,
        rr=payload.resp_rate,
        spo2=int(round(payload.spo2)) if payload.spo2 is not None else None,
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


def _vitals_out(v: Vitals) -> Dict[str, Any]:
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
        if not patient_id and appointment_id:
            appt = db.get(Appointment, appointment_id)
            if not appt:
                raise HTTPException(status_code=404,
                                    detail="Appointment not found")
            patient_id = appt.patient_id
        q = q.filter(Vitals.patient_id == patient_id)

    if for_date:
        q = q.filter(func.date(Vitals.created_at) == for_date)

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
            appt = db.get(Appointment, appointment_id)
            if not appt:
                raise HTTPException(status_code=404,
                                    detail="Appointment not found")
            patient_id = appt.patient_id
        q = q.filter(Vitals.patient_id == patient_id)

    if for_date:
        q = q.filter(func.date(Vitals.created_at) == for_date)

    rows = q.order_by(Vitals.created_at.desc()).limit(limit).all()
    return [_vitals_out(v) for v in rows]


# ---------------- QUEUE ----------------
# ---------------- QUEUE ----------------
@router.get("/queue")
def get_queue(
        doctor_user_id:
    Optional[int] = Query(
        None,
        description=
        "Optional doctor filter. If omitted, may return all doctors (based on permission).",
    ),
        department_id: Optional[int] = Query(
            None,
            description=
            "Optional department filter (works with all-doctors list too).",
        ),
        for_date: date = Query(default_factory=date.today),
        my_only: bool = Query(
            False,
            description=
            "If true, returns ONLY current logged-in doctor's appointments",
        ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # Allow triage/desk/doctor/admin based on common perms
    if not (has_perm(user, "appointments.view") or has_perm(
            user, "visits.view") or has_perm(user, "vitals.create")
            or user.is_admin or user.is_doctor):
        raise HTTPException(status_code=403, detail="Not permitted")

    # ---------------- Decide doctor scope ----------------
    doctor_filter: Optional[int] = None

    if my_only:
        if not user.is_doctor:
            raise HTTPException(status_code=403,
                                detail="My appointments is only for doctors")
        doctor_filter = user.id

    else:
        if doctor_user_id is not None:
            # Explicit doctor filter
            doctor_filter = doctor_user_id

            # Doctors cannot view other doctors unless they have appointments.view
            if user.is_doctor and doctor_filter != user.id and not has_perm(
                    user, "appointments.view"):
                raise HTTPException(
                    status_code=403,
                    detail="Not permitted to view other doctor's queue")

        else:
            # No doctor specified (list-first behavior)
            if user.is_doctor and not has_perm(user, "appointments.view"):
                # doctor user without broad permission -> default to self (secure)
                doctor_filter = user.id
            else:
                # staff/admin/doctor-with-perm -> all doctors
                doctor_filter = None

    # ---------------- Build query ----------------
    opts = [joinedload(Appointment.patient)]

    # join doctor/department only if relationships exist (safe)
    if hasattr(Appointment, "doctor"):
        opts.append(joinedload(Appointment.doctor))
    if hasattr(Appointment, "department"):
        opts.append(joinedload(Appointment.department))

    q = db.query(Appointment).options(*opts).filter(
        Appointment.date == for_date)

    if doctor_filter is not None:
        q = q.filter(Appointment.doctor_user_id == doctor_filter)

    if department_id is not None:
        q = q.filter(Appointment.department_id == department_id)

    # Better ordering for all-doctors mode
    q = q.order_by(
        Appointment.doctor_user_id.asc(),
        Appointment.queue_no.asc(),
        Appointment.id.asc(),
    )

    appts: List[Appointment] = q.all()
    appt_ids = [a.id for a in appts]

    # ---------------- Visit map ----------------
    vis_map: Dict[int, int] = {}
    if appt_ids:
        vis_map = {
            v.appointment_id: v.id
            for v in db.query(Visit).filter(Visit.appointment_id.in_(
                appt_ids)).all()
        }

    # ---------------- Vitals flags + last_at ----------------
    vitals_by_appt: Set[int] = set()
    vitals_last_at_by_appt: Dict[int, datetime] = {}

    vitals_by_patient: Set[int] = set()
    vitals_last_at_by_patient: Dict[int, datetime] = {}

    if appt_ids and hasattr(Vitals, "appointment_id"):
        rows = (db.query(Vitals.appointment_id,
                         func.max(Vitals.created_at)).filter(
                             Vitals.appointment_id.in_(appt_ids)).group_by(
                                 Vitals.appointment_id).all())
        for aid, last_at in rows:
            if aid:
                vitals_by_appt.add(aid)
                vitals_last_at_by_appt[aid] = last_at
    else:
        patient_ids = [a.patient_id for a in appts if a.patient_id]
        if patient_ids:
            rows = (db.query(Vitals.patient_id,
                             func.max(Vitals.created_at)).filter(
                                 Vitals.patient_id.in_(patient_ids),
                                 func.date(Vitals.created_at) == for_date,
                             ).group_by(Vitals.patient_id).all())
            for pid, last_at in rows:
                if pid:
                    vitals_by_patient.add(pid)
                    vitals_last_at_by_patient[pid] = last_at

    # ---------------- Response ----------------
    resp = []
    for a in appts:
        p: Patient = a.patient
        slot_start = getattr(a, "slot_start", None)

        # doctor/dept names (supports list-first UI)
        doc_name = ""
        dep_name = ""

        if hasattr(a, "doctor") and getattr(a, "doctor", None):
            doc_name = getattr(a.doctor, "name", "") or getattr(
                a.doctor, "full_name", "") or ""
        if hasattr(a, "department") and getattr(a, "department", None):
            dep_name = getattr(a.department, "name", "") or ""

        # vitals flags
        if hasattr(Vitals, "appointment_id"):
            has_vitals = a.id in vitals_by_appt
            vit_last_at = vitals_last_at_by_appt.get(a.id)
        else:
            has_vitals = a.patient_id in vitals_by_patient
            vit_last_at = vitals_last_at_by_patient.get(a.patient_id)

        resp.append({
            "appointment_id": a.id,
            "queue_no": getattr(a, "queue_no", None),
            "appointment_type": getattr(a, "appointment_type", None),
            "time": slot_start.strftime("%H:%M") if slot_start else "—",
            "status": a.status,
            "visit_id": vis_map.get(a.id),
            "doctor_user_id": a.doctor_user_id,
            "department_id": a.department_id,

            # ✅ for UI
            "doctor_name": doc_name,
            "department_name": dep_name,
            "doctor": {
                "id": a.doctor_user_id,
                "name": doc_name
            },
            "department": {
                "id": a.department_id,
                "name": dep_name
            },
            "booked_by": getattr(a, "booked_by", None),
            "patient": {
                "id": p.id,
                "uhid": p.uhid,
                "name": f"{p.first_name} {p.last_name or ''}".strip(),
                "phone": p.phone,
            },
            "has_vitals": bool(has_vitals),
            "vitals_last_at": vit_last_at,
            "visit_purpose": a.purpose or "",
        })

    return resp
