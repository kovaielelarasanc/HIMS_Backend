# FILE: app/api/routes_ot_schedule_cases.py
from __future__ import annotations

import logging
import re
from datetime import date, time, datetime, timedelta, timezone
from io import BytesIO
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_
from zoneinfo import ZoneInfo

from app.api.deps import get_db, current_user
from app.models.ipd import IpdAdmission, IpdBed, IpdRoom
from app.models.opd import Visit
from app.models.patient import Patient
from app.models.user import User
from app.models.ot import (
    OtSchedule,
    OtCase,
    PreOpChecklist as PreOpChecklistModel,
    OtScheduleProcedure,
    OtProcedure,
)
from app.models.ot_master import OtTheaterMaster as OtTheater
from app.models.ui_branding import UiBranding
from app.schemas.ot import (
    OtScheduleCreate,
    OtScheduleUpdate,
    OtScheduleOut,
    OtCaseCreate,
    OtCaseUpdate,
    OtCaseOut,
    OtPreopChecklistIn,
    OtPreopChecklistOut,
)
from app.services.billing_ot import create_ot_invoice_items_for_case
from app.services.ot_history_pdf import build_patient_ot_history_pdf
from app.services.ot_case_pdf import build_ot_case_pdf

router = APIRouter(prefix="/ot", tags=["OT - Schedule & Cases"])
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ============================================================
#  INTERNAL HELPERS
# ============================================================


def _need_any(user: User, codes: list[str]) -> None:
    """Enforce that user has at least ONE of the given permission codes. Admins bypass."""
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to perform this action.",
    )


def _as_aware(dt: datetime, assume_tz=IST) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=assume_tz)
    return dt


def _to_db_utc_naive(dt: datetime | None) -> datetime | None:
    """Store UTC as naive datetime in DB."""
    if dt is None:
        return None
    aware = _as_aware(dt, IST).astimezone(timezone.utc)
    return aware.replace(tzinfo=None)


def _time_to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def _min_to_time(m: int) -> time:
    m = max(0, m)
    h = (m // 60) % 24
    mm = m % 60
    return time(hour=h, minute=mm)


def _effective_end_time(start: time, end: time | None,
                        default_minutes: int) -> time:
    if end is not None:
        return end
    return _min_to_time(_time_to_min(start) + max(5, default_minutes))


def _get_user(db: Session, uid: int, label: str) -> User:
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, f"{label} user not found")
    # optional active check
    if hasattr(u, "is_active") and not getattr(u, "is_active"):
        raise HTTPException(400, f"{label} user is inactive")
    if hasattr(u, "active") and not getattr(u, "active"):
        raise HTTPException(400, f"{label} user is inactive")
    return u


def _get_theater(db: Session, theater_id: int) -> OtTheater:
    t = db.get(OtTheater, theater_id)
    if not t:
        raise HTTPException(404, "OT theater not found")
    if hasattr(t, "is_active") and not getattr(t, "is_active"):
        raise HTTPException(400, "OT theater is inactive")
    if hasattr(t, "active") and not getattr(t, "active"):
        raise HTTPException(400, "OT theater is inactive")
    return t


def _get_procedure(db: Session, pid: int) -> OtProcedure:
    p = db.get(OtProcedure, pid)
    if not p:
        raise HTTPException(404, "Procedure not found")
    if not getattr(p, "is_active", True):
        raise HTTPException(400, "Procedure is inactive")
    return p


def _ensure_patient_admission(db: Session, patient_id: int | None,
                              admission_id: int | None):
    if patient_id is not None and not db.get(Patient, patient_id):
        raise HTTPException(404, "Patient not found")
    if admission_id is not None and not db.get(IpdAdmission, admission_id):
        raise HTTPException(404, "Admission not found")


def _sync_procedure_links(
    db: Session,
    schedule: OtSchedule,
    primary_id: int | None,
    additional_ids: list[int] | None,
):
    schedule.procedures = []

    ids: List[int] = []
    if primary_id:
        ids.append(primary_id)
    if additional_ids:
        for x in additional_ids:
            if x and x not in ids:
                ids.append(x)

    for pid in ids:
        _get_procedure(db, pid)

    for pid in ids:
        schedule.procedures.append(
            OtScheduleProcedure(
                procedure_id=pid,
                is_primary=(primary_id is not None and pid == primary_id),
            ))


def _check_overlap(
    db: Session,
    *,
    theater_id: int,
    sched_date: date,
    start_t: time,
    end_t: time,
    ignore_schedule_id: int | None = None,
):
    """Theater-based overlap check (NO bed-based logic)."""
    start_m = _time_to_min(start_t)
    end_m = _time_to_min(end_t)

    q = db.query(OtSchedule).filter(
        OtSchedule.ot_theater_id == theater_id,
        OtSchedule.date == sched_date,
        OtSchedule.status.in_(["planned", "confirmed", "in_progress"]),
    )
    if ignore_schedule_id:
        q = q.filter(OtSchedule.id != ignore_schedule_id)

    rows = q.all()
    for r in rows:
        rs = _time_to_min(r.planned_start_time)
        re_ = _time_to_min(r.planned_end_time or r.planned_start_time)
        if r.planned_end_time is None:
            re_ = rs + 60  # fallback for old rows
        if start_m < re_ and end_m > rs:
            raise HTTPException(
                status_code=409,
                detail=
                f"Time overlap in the same theater (Schedule ID: {r.id})",
            )


def _load_schedule(db: Session, schedule_id: int) -> OtSchedule:
    s = (db.query(OtSchedule).options(
        joinedload(OtSchedule.theater),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.petitory),
        joinedload(OtSchedule.asst_doctor),
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.admission),
        joinedload(OtSchedule.primary_procedure),
        selectinload(OtSchedule.procedures).joinedload(
            OtScheduleProcedure.procedure),
    ).filter(OtSchedule.id == schedule_id).first())
    if not s:
        raise HTTPException(404, "OT schedule not found")
    return s


def _get_case_or_404(db: Session, case_id: int) -> OtCase:
    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")
    return case


def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (user.roles or []):
        for p in (getattr(r, "permissions", None) or []):
            if p.code == code:
                return True
    return False


def _safe_filename(x: str) -> str:
    x = x or "patient"
    x = re.sub(r"[^A-Za-z0-9._-]+", "_", x).strip("_")
    return x[:80] if x else "patient"


# ============================================================
#  OT SCHEDULE ROUTES
# ============================================================


@router.get("/schedules", response_model=List[OtScheduleOut])
def list_schedules(
        date_from: Optional[date] = Query(None),
        date_to: Optional[date] = Query(None),
        ot_theater_id: Optional[int] = Query(None),
        status_: Optional[str] = Query(None, alias="status"),
        q: Optional[str] = Query(None, description="Search procedure_name"),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedules.view", "ot.cases.view", "ot.masters.view"])

    qry = (db.query(OtSchedule).options(
        joinedload(OtSchedule.theater),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.petitory),
        joinedload(OtSchedule.asst_doctor),
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.admission),
        joinedload(OtSchedule.primary_procedure),
        selectinload(OtSchedule.procedures).joinedload(
            OtScheduleProcedure.procedure),
    ))

    if date_from:
        qry = qry.filter(OtSchedule.date >= date_from)
    if date_to:
        qry = qry.filter(OtSchedule.date <= date_to)
    if ot_theater_id:
        qry = qry.filter(OtSchedule.ot_theater_id == ot_theater_id)
    if status_:
        qry = qry.filter(OtSchedule.status == status_)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(OtSchedule.procedure_name.ilike(like)))

    rows = (qry.order_by(
        OtSchedule.date.desc(),
        OtSchedule.planned_start_time.asc()).limit(limit).all())
    return rows


@router.post("/schedules", response_model=OtScheduleOut, status_code=201)
def create_schedule(
        payload: OtScheduleCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(
        user,
        ["ot.schedules.create", "ot.schedules.manage", "ot.cases.create"])

    if not payload.ot_theater_id:
        raise HTTPException(422, "ot_theater_id is required")
    if not payload.surgeon_user_id:
        raise HTTPException(422, "surgeon_user_id is required")
    if not payload.anaesthetist_user_id:
        raise HTTPException(422, "anaesthetist_user_id is required")

    _ensure_patient_admission(db, payload.patient_id, payload.admission_id)
    _get_theater(db, payload.ot_theater_id)

    _get_user(db, payload.surgeon_user_id, "Surgeon")
    _get_user(db, payload.anaesthetist_user_id, "Anaesthetist")

    if payload.petitory_user_id:
        _get_user(db, payload.petitory_user_id, "Petitory")
    if payload.asst_doctor_user_id:
        _get_user(db, payload.asst_doctor_user_id, "Assistant doctor")

    default_min = 60
    if payload.primary_procedure_id:
        proc = _get_procedure(db, payload.primary_procedure_id)
        if proc.default_duration_min:
            default_min = int(proc.default_duration_min)
        if not (payload.procedure_name or "").strip():
            payload.procedure_name = proc.name

    end_t = _effective_end_time(payload.planned_start_time,
                                payload.planned_end_time, default_min)

    _check_overlap(
        db,
        theater_id=payload.ot_theater_id,
        sched_date=payload.date,
        start_t=payload.planned_start_time,
        end_t=end_t,
        ignore_schedule_id=None,
    )

    s = OtSchedule(
        date=payload.date,
        planned_start_time=payload.planned_start_time,
        planned_end_time=end_t,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        ot_theater_id=payload.ot_theater_id,
        surgeon_user_id=payload.surgeon_user_id,
        anaesthetist_user_id=payload.anaesthetist_user_id,
        petitory_user_id=payload.petitory_user_id,
        asst_doctor_user_id=payload.asst_doctor_user_id,
        procedure_name=(payload.procedure_name or "").strip(),
        side=payload.side,
        priority=payload.priority or "Elective",
        notes=payload.notes,
        status="planned",
        primary_procedure_id=payload.primary_procedure_id,
    )

    if s.procedure_name == "":
        raise HTTPException(422, "procedure_name is required")

    db.add(s)
    db.flush()

    _sync_procedure_links(db, s, payload.primary_procedure_id,
                          payload.additional_procedure_ids)

    db.commit()
    return _load_schedule(db, s.id)


@router.get("/schedules/{schedule_id}", response_model=OtScheduleOut)
def get_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedules.view", "ot.cases.view"])
    return _load_schedule(db, schedule_id)


@router.put("/schedules/{schedule_id}", response_model=OtScheduleOut)
def update_schedule(
        schedule_id: int,
        payload: OtScheduleUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(
        user,
        ["ot.schedules.update", "ot.schedules.manage", "ot.cases.update"])

    s = db.get(OtSchedule, schedule_id)
    if not s:
        raise HTTPException(404, "OT schedule not found")

    if payload.patient_id is not None or payload.admission_id is not None:
        _ensure_patient_admission(db, payload.patient_id, payload.admission_id)

    if payload.ot_theater_id is not None:
        _get_theater(db, payload.ot_theater_id)
        s.ot_theater_id = payload.ot_theater_id

    if payload.surgeon_user_id is not None:
        _get_user(db, payload.surgeon_user_id, "Surgeon")
        s.surgeon_user_id = payload.surgeon_user_id

    if payload.anaesthetist_user_id is not None:
        _get_user(db, payload.anaesthetist_user_id, "Anaesthetist")
        s.anaesthetist_user_id = payload.anaesthetist_user_id

    if payload.petitory_user_id is not None:
        if payload.petitory_user_id:
            _get_user(db, payload.petitory_user_id, "Petitory")
        s.petitory_user_id = payload.petitory_user_id

    if payload.asst_doctor_user_id is not None:
        if payload.asst_doctor_user_id:
            _get_user(db, payload.asst_doctor_user_id, "Assistant doctor")
        s.asst_doctor_user_id = payload.asst_doctor_user_id

    if payload.date is not None:
        s.date = payload.date
    if payload.planned_start_time is not None:
        s.planned_start_time = payload.planned_start_time
    if payload.planned_end_time is not None:
        s.planned_end_time = payload.planned_end_time

    if payload.patient_id is not None:
        s.patient_id = payload.patient_id
    if payload.admission_id is not None:
        s.admission_id = payload.admission_id

    if payload.procedure_name is not None:
        s.procedure_name = (payload.procedure_name or "").strip()
    if payload.side is not None:
        s.side = payload.side
    if payload.priority is not None:
        s.priority = payload.priority
    if payload.notes is not None:
        s.notes = payload.notes

    if payload.primary_procedure_id is not None:
        if payload.primary_procedure_id:
            _get_procedure(db, payload.primary_procedure_id)
        s.primary_procedure_id = payload.primary_procedure_id

    if payload.additional_procedure_ids is not None or payload.primary_procedure_id is not None:
        _sync_procedure_links(
            db,
            s,
            s.primary_procedure_id,
            payload.additional_procedure_ids or [],
        )

    default_min = 60
    if s.primary_procedure_id:
        proc = db.get(OtProcedure, s.primary_procedure_id)
        if proc and proc.default_duration_min:
            default_min = int(proc.default_duration_min)

    end_t = _effective_end_time(s.planned_start_time, s.planned_end_time,
                                default_min)
    s.planned_end_time = end_t

    if not s.ot_theater_id:
        raise HTTPException(422, "ot_theater_id is required")
    if not s.surgeon_user_id:
        raise HTTPException(422, "surgeon_user_id is required")
    if not s.anaesthetist_user_id:
        raise HTTPException(422, "anaesthetist_user_id is required")
    if not (s.procedure_name or "").strip():
        raise HTTPException(422, "procedure_name is required")

    _check_overlap(
        db,
        theater_id=s.ot_theater_id,
        sched_date=s.date,
        start_t=s.planned_start_time,
        end_t=s.planned_end_time,
        ignore_schedule_id=s.id,
    )

    db.commit()
    return _load_schedule(db, s.id)


@router.delete("/schedules/{schedule_id}")
def cancel_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(
        user,
        ["ot.schedules.delete", "ot.schedules.cancel", "ot.schedules.manage"])

    s = db.get(OtSchedule, schedule_id)
    if not s:
        raise HTTPException(404, "OT schedule not found")
    if s.status in ("completed", ):
        raise HTTPException(400, "Completed schedule cannot be cancelled")

    s.status = "cancelled"
    db.commit()
    return {"message": "Schedule cancelled", "id": s.id}


# ============================================================
#  OT CASE ROUTES
# ============================================================


@router.get("/cases", response_model=List[OtCaseOut])
def list_ot_cases(
        date_: Optional[date] = Query(
            None, alias="date", description="Filter by schedule date"),
        ot_theater_id: Optional[int] = Query(
            None, description="Filter by OT theater"),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])

    q = (
        db.query(OtCase).join(OtSchedule).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.theater),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            # keep only for IPD admission current bed display
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.admission
                                    ).joinedload(IpdAdmission.current_bed),
        ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if ot_theater_id:
        q = q.filter(OtSchedule.ot_theater_id == ot_theater_id)
    if surgeon_user_id:
        q = q.filter(OtSchedule.surgeon_user_id == surgeon_user_id)
    if patient_id:
        q = q.filter(OtSchedule.patient_id == patient_id)

    return q.order_by(OtSchedule.date.asc(),
                      OtSchedule.planned_start_time.asc()).all()


@router.get("/cases/{case_id}", response_model=OtCaseOut)
def get_ot_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])

    case = (
        db.query(OtCase).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            joinedload(OtCase.schedule).joinedload(OtSchedule.theater),
            # IPD current bed chain (optional for display)
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.admission).joinedload(
                           IpdAdmission.current_bed
                       ).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
        ).filter(OtCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    sched = case.schedule
    op_no = None
    if sched and sched.patient_id:
        latest_visit = (db.query(Visit).filter(
            Visit.patient_id == sched.patient_id).order_by(
                Visit.visit_at.desc()).first())
        if latest_visit:
            op_no = latest_visit.op_no

    data = OtCaseOut.model_validate(case, from_attributes=True)

    data.op_no = op_no
    if data.schedule:
        data.schedule.op_no = op_no

    p = sched.patient if (sched and sched.patient) else None
    if p:
        data.patient_id = getattr(p, "id", None)
        fn = (getattr(p, "first_name", "") or "").strip()
        ln = (getattr(p, "last_name", "") or "").strip()
        data.patient_name = (f"{fn} {ln}").strip() or getattr(p, "name", None)
        data.uhid = getattr(p, "uhid", None)
        data.sex = getattr(p, "sex", None) or getattr(p, "gender", None)

        dob = getattr(p, "dob", None)
        if dob:
            today = date.today()
            data.age = today.year - dob.year - (
                (today.month, today.day) < (dob.month, dob.day))

    return data


@router.post("/cases", response_model=OtCaseOut, status_code=201)
def create_case_from_schedule(
        payload: OtCaseCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user,
              ["ot.cases.create", "ot.cases.manage", "ot.schedules.update"])

    s = db.get(OtSchedule, payload.schedule_id)
    if not s:
        raise HTTPException(404, "OT schedule not found")

    if s.case_id:
        c = db.get(OtCase, s.case_id)
        if c:
            return c

    c = OtCase(
        preop_diagnosis=payload.preop_diagnosis,
        postop_diagnosis=payload.postop_diagnosis,
        final_procedure_name=payload.final_procedure_name,
        speciality_id=payload.speciality_id,
        actual_start_time=_to_db_utc_naive(payload.actual_start_time),
        actual_end_time=_to_db_utc_naive(payload.actual_end_time),
        outcome=payload.outcome,
        icu_required=payload.icu_required,
        immediate_postop_condition=payload.immediate_postop_condition,
    )

    if not c.speciality_id and s.primary_procedure_id:
        proc = db.get(OtProcedure, s.primary_procedure_id)
        if proc and proc.speciality_id:
            c.speciality_id = proc.speciality_id

    db.add(c)
    db.flush()

    s.case_id = c.id
    if c.actual_start_time:
        s.status = "in_progress"

    db.add(s)
    db.commit()

    c = db.query(OtCase).options(joinedload(
        OtCase.schedule)).filter(OtCase.id == c.id).first()
    return c


@router.put("/cases/{case_id}", response_model=OtCaseOut)
def update_case(
        case_id: int,
        payload: OtCaseUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.update", "ot.cases.manage"])

    c = db.get(OtCase, case_id)
    if not c:
        raise HTTPException(404, "OT case not found")

    # schedule link update (optional)
    if payload.schedule_id is not None:
        s = db.get(OtSchedule, payload.schedule_id)
        if not s:
            raise HTTPException(404, "Schedule not found")
        if s.case_id and s.case_id != c.id:
            raise HTTPException(
                409, "This schedule already linked to another case")
        s.case_id = c.id
        db.add(s)

    # fields
    for k in (
            "preop_diagnosis",
            "postop_diagnosis",
            "final_procedure_name",
            "speciality_id",
            "outcome",
            "icu_required",
            "immediate_postop_condition",
    ):
        v = getattr(payload, k)
        if v is not None:
            setattr(c, k, v)

    if payload.actual_start_time is not None:
        c.actual_start_time = _to_db_utc_naive(payload.actual_start_time)
    if payload.actual_end_time is not None:
        c.actual_end_time = _to_db_utc_naive(payload.actual_end_time)

    # auto status move (theater-based; no bed release)
    if c.schedule:
        if c.actual_start_time and c.schedule.status == "planned":
            c.schedule.status = "in_progress"
            db.add(c.schedule)

        if c.actual_end_time and (c.outcome or "").lower() in (
                "completed",
                "done",
                "success",
                "successful",
                "converted",
        ):
            c.schedule.status = "completed"
            db.add(c.schedule)

    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.delete("/cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.delete"])

    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")

    schedule = case.schedule

    db.delete(case)
    db.commit()

    if schedule and schedule.status in ("in_progress", "completed"):
        schedule.status = "planned"
        db.add(schedule)
        db.commit()

    return None


# ============================================================
#  OPEN / CLOSE CASE ENDPOINTS (NO OT BED)
# ============================================================


class OtCaseOpenPayload(BaseModel):
    preop_diagnosis: Optional[str] = None
    final_procedure_name: Optional[str] = None
    speciality_id: Optional[int] = None
    actual_start_time: Optional[datetime] = None
    icu_required: Optional[bool] = None
    immediate_postop_condition: Optional[str] = None


class OtCaseClosePayload(BaseModel):
    outcome: str = Field(..., max_length=50)
    actual_end_time: Optional[datetime] = None
    icu_required: Optional[bool] = None
    immediate_postop_condition: Optional[str] = None


@router.post("/schedule/{schedule_id}/open-case", response_model=OtCaseOut)
def open_ot_case_for_schedule(
        schedule_id: int,
        payload: OtCaseOpenPayload,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user,
              ["ot.cases.create", "ot.cases.manage", "ot.schedules.update"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")
    if schedule.status == "cancelled":
        raise HTTPException(status_code=400,
                            detail="Cannot open case for a cancelled schedule")

    case = schedule.case
    now = datetime.utcnow()

    if not case:
        case = OtCase(
            preop_diagnosis=payload.preop_diagnosis,
            final_procedure_name=payload.final_procedure_name,
            speciality_id=payload.speciality_id,
            actual_start_time=_to_db_utc_naive(payload.actual_start_time)
            or now,
            icu_required=payload.icu_required or False,
            immediate_postop_condition=payload.immediate_postop_condition,
        )
        db.add(case)
        db.flush()
        schedule.case_id = case.id
        db.add(schedule)
    else:
        if not case.actual_start_time:
            case.actual_start_time = _to_db_utc_naive(
                payload.actual_start_time) or now
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if field == "actual_start_time" and value is None:
                continue
            if field == "actual_start_time":
                setattr(case, field, _to_db_utc_naive(value))
            else:
                setattr(case, field, value)
        db.add(case)

    schedule.status = "in_progress"
    db.add(schedule)

    db.commit()
    db.refresh(case)
    return case


@router.post("/cases/{case_id}/close", response_model=OtCaseOut)
def close_ot_case(
        case_id: int,
        payload: OtCaseClosePayload,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.update", "ot.cases.close"])

    case: OtCase | None = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure), ).filter(
                    OtCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")

    schedule = case.schedule
    now = datetime.utcnow()

    if not case.actual_start_time:
        case.actual_start_time = now

    case.outcome = (payload.outcome or "").strip()
    case.actual_end_time = _to_db_utc_naive(
        payload.actual_end_time) or case.actual_end_time or now

    if (case.actual_end_time and case.actual_start_time
            and case.actual_end_time <= case.actual_start_time):
        raise HTTPException(
            status_code=400,
            detail="actual_end_time must be greater than actual_start_time")

    if payload.icu_required is not None:
        case.icu_required = payload.icu_required
    if payload.immediate_postop_condition is not None:
        case.immediate_postop_condition = payload.immediate_postop_condition

    db.add(case)

    if schedule:
        schedule.status = "completed"
        db.add(schedule)

    db.commit()
    db.refresh(case)

    # billing (best-effort)
    try:
        _ = create_ot_invoice_items_for_case(db=db,
                                             case_id=case.id,
                                             user_id=user.id)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("OT billing failed for case_id=%s: %s", case.id,
                         str(e))

    return case


# ============================================================
#  OT CASE SUB-RESOURCES – PRE-OP CHECKLIST
# ============================================================

from datetime import datetime
from fastapi import HTTPException


def _build_preop_data_from_payload(payload: OtPreopChecklistIn) -> dict:
    return payload.model_dump(exclude={"completed"})


def _empty_preop_payload() -> dict:
    # minimal skeleton (frontend hydrateForm will merge with DEFAULT_FORM)
    return {
        "checklist": {},
        "investigations": {},
        "vitals": {},
        "shave_completed": None,
        "nurse_signature": "",
    }


@router.get("/cases/{case_id}/preop-checklist",
            response_model=OtPreopChecklistOut)
def get_preop_checklist(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ align with frontend perms
    _need_any(user,
              ["ot.preop_checklist.view", "ot.cases.view", "ot.cases.update"])
    _get_case_or_404(db, case_id)

    checklist = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())

    # ✅ instead of 404: return empty payload (200)
    if not checklist:
        now = datetime.utcnow()
        empty = _empty_preop_payload()
        return OtPreopChecklistOut(
            case_id=case_id,
            completed=False,
            created_at=now,
            updated_at=now,
            **empty,
        )

    data = checklist.data or {}
    # ensure missing keys don't break response_model
    merged = {**_empty_preop_payload(), **data}

    return OtPreopChecklistOut(
        case_id=case_id,
        completed=checklist.completed,
        created_at=checklist.created_at,
        updated_at=checklist.completed_at or checklist.created_at,
        **merged,
    )


@router.post("/cases/{case_id}/preop-checklist",
             response_model=OtPreopChecklistOut,
             status_code=201)
def create_preop_checklist(
        case_id: int,
        payload: OtPreopChecklistIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.create", "ot.cases.update"])
    _get_case_or_404(db, case_id)

    existing = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())
    if existing:
        raise HTTPException(
            400, "Pre-op checklist already exists. Use PUT to update.")

    data = _build_preop_data_from_payload(payload)
    data = {**_empty_preop_payload(), **(data or {})}

    now_completed = datetime.utcnow() if payload.completed else None

    checklist = PreOpChecklistModel(
        case_id=case_id,
        nurse_user_id=user.id,
        data=data,
        completed=payload.completed,
        completed_at=now_completed,
    )

    db.add(checklist)
    db.commit()
    db.refresh(checklist)

    return OtPreopChecklistOut(
        case_id=case_id,
        created_at=checklist.created_at,
        updated_at=checklist.completed_at or checklist.created_at,
        completed=checklist.completed,
        **data,
    )


@router.put("/cases/{case_id}/preop-checklist",
            response_model=OtPreopChecklistOut)
def update_preop_checklist(
        case_id: int,
        payload: OtPreopChecklistIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # allow update OR create via PUT (upsert)
    _need_any(user, [
        "ot.preop_checklist.update", "ot.preop_checklist.create",
        "ot.cases.update"
    ])
    _get_case_or_404(db, case_id)

    checklist = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())

    data = _build_preop_data_from_payload(payload) or {}
    # optional: ensure base keys exist (safe for response_model)
    base = {
        "checklist": {},
        "investigations": {},
        "vitals": {},
        "shave_completed": None,
        "nurse_signature": "",
    }
    data = {**base, **data}

    now_completed = datetime.utcnow() if payload.completed else None

    # ✅ UPSERT: create if missing
    if not checklist:
        checklist = PreOpChecklistModel(
            case_id=case_id,
            nurse_user_id=user.id,
            data=data,
            completed=payload.completed,
            completed_at=now_completed,
        )
        db.add(checklist)
    else:
        checklist.data = data
        checklist.completed = payload.completed
        checklist.completed_at = now_completed
        checklist.nurse_user_id = user.id

    # if your model has updated_at column, set it
    if hasattr(checklist, "updated_at"):
        checklist.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(checklist)

    # ✅ updated_at for UI “Last saved”
    updated_at = (getattr(checklist, "updated_at", None)
                  or checklist.completed_at or datetime.utcnow())

    return OtPreopChecklistOut(
        case_id=case_id,
        created_at=checklist.created_at,
        updated_at=updated_at,
        completed=checklist.completed,
        **data,
    )


# ============================================================
#  PDF ENDPOINTS (THEATER-BASED; NO OT BED)
# ============================================================


@router.get("/cases/{case_id}/pdf")
def download_ot_case_pdf(
        case_id: int,
        disposition: Literal["inline", "attachment"] = Query("attachment"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # allow view/update/schedule-view
    if not (has_perm(user, "ot.cases.view") or has_perm(
            user, "ot.cases.update") or has_perm(user, "ot.schedules.view")):
        raise HTTPException(status_code=403,
                            detail="Not permitted to view OT case PDF.")

    case = (
        db.query(OtCase).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.theater),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            # IPD admission current bed (optional in PDF)
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.admission).joinedload(
                           IpdAdmission.current_bed
                       ).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            # linked clinical records
            joinedload(OtCase.preanaesthesia),
            joinedload(OtCase.preop_checklist),
            joinedload(OtCase.safety_checklist),
        ).filter(OtCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found.")

    schedule = case.schedule
    patient = schedule.patient if schedule else None

    uhid = getattr(patient, "uhid", None) or getattr(patient, "uhid_number",
                                                     None) or f"case_{case_id}"
    dt = getattr(schedule, "date", None)
    dt_part = dt.strftime("%Y%m%d") if dt else "date"
    filename = f"OT_Case_{_safe_filename(str(uhid))}_{dt_part}.pdf"

    branding = db.query(UiBranding).order_by(UiBranding.id.desc()).first()

    pdf_bytes = build_ot_case_pdf(
        case=case,
        org_name=(getattr(branding, "org_name", None) or "NUTRYAH HIMS"),
        generated_by=getattr(user, "full_name", None)
        or getattr(user, "email", None),
        branding=branding,
    )

    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.get("/patients/{patient_id}/history.pdf")
def download_patient_ot_history_pdf(
        patient_id: int,
        disposition: Literal["inline", "attachment"] = Query("attachment"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "ot.cases.view")
            or has_perm(user, "ot.schedules.view")):
        raise HTTPException(status_code=403,
                            detail="Not permitted to view OT history PDF.")

    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found.")

    schedules = (db.query(OtSchedule).options(
        joinedload(OtSchedule.case),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.theater),
    ).filter(OtSchedule.patient_id == patient_id).order_by(
        OtSchedule.date.desc(), OtSchedule.planned_start_time.desc()).all())

    uhid = getattr(patient, "uhid", None) or getattr(patient, "uhid_number",
                                                     None) or str(patient_id)
    filename = f"OT_History_{_safe_filename(str(uhid))}.pdf"

    pdf_bytes = build_patient_ot_history_pdf(
        patient=patient,
        schedules=schedules,
        org_name="NUTRYAH HIMS",
        generated_by=getattr(user, "full_name", None)
        or getattr(user, "email", None),
    )

    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)
