# FILE: app/api/routes_ot_schedule_cases.py
from __future__ import annotations

from datetime import date, time, datetime
from typing import List, Optional
import re
from io import BytesIO
from typing import Literal
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user
from app.models.ipd import IpdBed, IpdAdmission, IpdRoom
from app.models.opd import Visit
from app.models.patient import Patient
from app.models.user import User as UserModel
from app.models.ot import (
    OtSchedule,
    OtCase,
    PreOpChecklist as PreOpChecklistModel,
    OtScheduleProcedure,
    OtProcedure,
    AnaesthesiaRecord,
)
from fastapi.responses import StreamingResponse
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
from app.models.user import User

from fastapi.responses import StreamingResponse
from app.services.ot_history_pdf import build_patient_ot_history_pdf
from app.services.ot_case_pdf import build_ot_case_pdf

router = APIRouter(prefix="/ot", tags=["OT - Schedule & Cases"])
logger = logging.getLogger(__name__)

# ============================================================
#  INTERNAL HELPERS
# ============================================================


def _need_any(user: User, codes: list[str]) -> None:
    """
    Enforce that user has at least ONE of the given permission codes.
    Admins bypass checks.
    """
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to perform this action.",
    )


# ---------------------------------------------------------------------
# OT bed helpers – mark occupied / vacant based on OT schedules
# ---------------------------------------------------------------------


def _mark_ot_bed_occupied(db: Session, ot_bed_id: int | None) -> None:
    if not ot_bed_id:
        return
    bed = db.query(IpdBed).filter(IpdBed.id == ot_bed_id).first()
    if not bed:
        return
    bed.state = "preoccupied"  # ✅ use allowed state
    bed.reserved_until = None
    db.add(bed)


def _release_ot_bed_if_free(db: Session, ot_bed_id: int | None) -> None:
    """
    Release bed back to vacant if there are no other active OT schedules
    (planned / in_progress) for this bed.
    """
    if not ot_bed_id:
        return

    active_count = (db.query(OtSchedule).filter(
        OtSchedule.ot_bed_id == ot_bed_id,
        OtSchedule.status.in_(["planned", "in_progress"]),
    ).count())
    if active_count > 0:
        return

    bed = db.query(IpdBed).filter(IpdBed.id == ot_bed_id).first()
    if not bed:
        return

    bed.state = "vacant"
    bed.reserved_until = None
    db.add(bed)


def _validate_time_order(start: time, end: Optional[time]) -> None:
    """
    Ensure end time is after start time when provided.
    """
    if end is not None and end <= start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="planned_end_time must be greater than planned_start_time",
        )


def _check_schedule_conflict(
    db: Session,
    ot_bed_id: int,
    date_: date,
    start: time,
    end: Optional[time],
    exclude_id: Optional[int] = None,
) -> None:
    """
    Check for OT booking overlap on the same OT bed and date.
    """
    if not ot_bed_id:
        return

    q = db.query(OtSchedule).filter(
        OtSchedule.ot_bed_id == ot_bed_id,
        OtSchedule.date == date_,
        OtSchedule.status != "cancelled",
    )

    if exclude_id:
        q = q.filter(OtSchedule.id != exclude_id)

    if end is None:
        q = q.filter(
            or_(
                and_(
                    OtSchedule.planned_end_time.isnot(None),
                    OtSchedule.planned_start_time <= start,
                    OtSchedule.planned_end_time > start,
                ),
                and_(
                    OtSchedule.planned_end_time.is_(None),
                    OtSchedule.planned_start_time == start,
                ),
            ))
    else:
        q = q.filter(
            or_(
                and_(
                    OtSchedule.planned_end_time.isnot(None),
                    OtSchedule.planned_start_time < end,
                    OtSchedule.planned_end_time > start,
                ),
                and_(
                    OtSchedule.planned_end_time.is_(None),
                    OtSchedule.planned_start_time >= start,
                    OtSchedule.planned_start_time < end,
                ),
            ))

    if q.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OT schedule conflict for this bed & time range",
        )


def _get_case_or_404(db: Session, case_id: int) -> OtCase:
    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Case not found",
        )
    return case


# ============================================================
#  OT SCHEDULE ROUTES
# ============================================================


@router.get("/schedule", response_model=List[OtScheduleOut])
def list_ot_schedules(
        date_: Optional[date] = Query(None, alias="date"),
        ot_bed_id: Optional[int] = Query(
            None, description="Filter by OT location bed"),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        status_: Optional[str] = Query(None, alias="status"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.schedule.view"])

    q = db.query(OtSchedule).options(
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),

        # ✅ OT location bed
        joinedload(OtSchedule.ot_bed).joinedload(IpdBed.room
                                                 ).joinedload(IpdRoom.ward),

        # Admission + patient's current bed (ward bed)
        joinedload(OtSchedule.admission
                   ).joinedload(IpdAdmission.current_bed
                                ).joinedload(IpdBed.room
                                             ).joinedload(IpdRoom.ward),
        joinedload(OtSchedule.case),
    )

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if ot_bed_id:
        q = q.filter(OtSchedule.ot_bed_id == ot_bed_id)
    if surgeon_user_id:
        q = q.filter(OtSchedule.surgeon_user_id == surgeon_user_id)
    if patient_id:
        q = q.filter(OtSchedule.patient_id == patient_id)
    if status_:
        q = q.filter(OtSchedule.status == status_)

    return q.order_by(OtSchedule.date.asc(),
                      OtSchedule.planned_start_time.asc()).all()


@router.get("/schedule/{schedule_id}", response_model=OtScheduleOut)
def get_ot_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.schedule.view"])

    schedule = (
        db.query(OtSchedule).options(
            joinedload(OtSchedule.patient),
            joinedload(OtSchedule.surgeon),
            joinedload(OtSchedule.anaesthetist),

            # ✅ OT location bed (FIXED)
            joinedload(OtSchedule.ot_bed
                       ).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            joinedload(OtSchedule.admission).joinedload(
                IpdAdmission.current_bed).joinedload(IpdBed.room).joinedload(
                    IpdRoom.ward),
            joinedload(OtSchedule.case),
        ).get(schedule_id))
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")
    return schedule


@router.post("/schedules", response_model=OtScheduleOut)
def create_ot_schedule(
        payload: OtScheduleCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.create"])

    if payload.planned_start_time:
        _validate_time_order(payload.planned_start_time,
                             payload.planned_end_time)

    missing_refs: list[str] = []

    if payload.patient_id and not db.get(Patient, payload.patient_id):
        missing_refs.append(f"patient_id={payload.patient_id}")

    if payload.ot_bed_id:
        bed = db.get(IpdBed, payload.ot_bed_id)
        if not bed:
            missing_refs.append(f"ot_bed_id={payload.ot_bed_id}")

    if payload.admission_id and not db.get(IpdAdmission, payload.admission_id):
        missing_refs.append(f"admission_id={payload.admission_id}")

    if payload.surgeon_user_id and not db.get(UserModel,
                                              payload.surgeon_user_id):
        missing_refs.append(f"surgeon_user_id={payload.surgeon_user_id}")

    if payload.anaesthetist_user_id and not db.get(
            UserModel, payload.anaesthetist_user_id):
        missing_refs.append(
            f"anaesthetist_user_id={payload.anaesthetist_user_id}")

    if missing_refs:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid FK reference(s): {', '.join(missing_refs)}",
        )

    if payload.ot_bed_id:
        _check_schedule_conflict(
            db=db,
            ot_bed_id=payload.ot_bed_id,
            date_=payload.date,
            start=payload.planned_start_time,
            end=payload.planned_end_time,
            exclude_id=None,
        )

    schedule = OtSchedule(
        date=payload.date,
        planned_start_time=payload.planned_start_time,
        planned_end_time=payload.planned_end_time,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        ot_bed_id=payload.ot_bed_id,
        surgeon_user_id=payload.surgeon_user_id,
        anaesthetist_user_id=payload.anaesthetist_user_id,
        procedure_name=payload.procedure_name,
        side=payload.side,
        priority=payload.priority or "Elective",
        notes=payload.notes,
        status="planned",
        primary_procedure_id=payload.primary_procedure_id,
    )

    db.add(schedule)
    db.flush()  # ✅ now schedule.id exists

    # ✅ create schedule-procedure links AFTER flush
    if payload.primary_procedure_id:
        db.add(
            OtScheduleProcedure(
                schedule_id=schedule.id,
                procedure_id=payload.primary_procedure_id,
                is_primary=True,
            ))

    for pid in (getattr(payload, "additional_procedure_ids", None) or []):
        if pid == payload.primary_procedure_id:
            continue
        db.add(
            OtScheduleProcedure(
                schedule_id=schedule.id,
                procedure_id=pid,
                is_primary=False,
            ))

    if payload.ot_bed_id:
        _mark_ot_bed_occupied(db, payload.ot_bed_id)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/schedules/{schedule_id}", response_model=OtScheduleOut)
def update_ot_schedule(
        schedule_id: int,
        payload: OtScheduleUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.update"])

    schedule = db.get(OtSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    data = payload.model_dump(exclude_unset=True)

    old_bed_id = schedule.ot_bed_id
    old_status = schedule.status

    new_date = data.get("date", schedule.date)
    new_start = data.get("planned_start_time", schedule.planned_start_time)
    new_end = data.get("planned_end_time", schedule.planned_end_time)
    new_bed_id = data.get("ot_bed_id", schedule.ot_bed_id)

    if new_start:
        _validate_time_order(new_start, new_end)

    if new_bed_id:
        _check_schedule_conflict(
            db=db,
            ot_bed_id=new_bed_id,
            date_=new_date,
            start=new_start,
            end=new_end,
            exclude_id=schedule.id,
        )

    for field in [
            "date",
            "planned_start_time",
            "planned_end_time",
            "patient_id",
            "admission_id",
            "ot_bed_id",
            "surgeon_user_id",
            "anaesthetist_user_id",
            "procedure_name",
            "side",
            "priority",
            "notes",
            "status",
    ]:
        if field in data:
            setattr(schedule, field, data[field])

    if "primary_procedure_id" in data or "additional_procedure_ids" in data:
        schedule.procedures.clear()
        db.flush()

        primary_id = data.get("primary_procedure_id",
                              schedule.primary_procedure_id)
        add_ids = data.get("additional_procedure_ids", [])

        schedule.primary_procedure_id = primary_id
        seen_ids = set()

        if primary_id:
            db.add(
                OtScheduleProcedure(
                    schedule_id=schedule.id,
                    procedure_id=primary_id,
                    is_primary=True,
                ))
            seen_ids.add(primary_id)

        for pid in add_ids or []:
            if pid in seen_ids:
                continue
            db.add(
                OtScheduleProcedure(
                    schedule_id=schedule.id,
                    procedure_id=pid,
                    is_primary=False,
                ))
            seen_ids.add(pid)

    # BED STATE LOGIC
    new_bed_final = schedule.ot_bed_id
    if "ot_bed_id" in data and new_bed_final != old_bed_id:
        if new_bed_final:
            _mark_ot_bed_occupied(db, new_bed_final)
        if old_bed_id:
            _release_ot_bed_if_free(db, old_bed_id)

    new_status = schedule.status
    if old_status != "cancelled" and new_status == "cancelled" and schedule.ot_bed_id:
        _release_ot_bed_if_free(db, schedule.ot_bed_id)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.post("/schedule/{schedule_id}/cancel", response_model=OtScheduleOut)
def cancel_ot_schedule(
        schedule_id: int,
        reason: Optional[str] = Query(
            None, description="Optional reason for cancellation"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.cancel"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")

    if schedule.case and schedule.case.actual_start_time:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel schedule – OT case already started",
        )

    schedule.status = "cancelled"
    if reason:
        schedule.notes = (schedule.notes or "") + (f"\n[CANCELLED]: {reason}"
                                                   if schedule.notes else
                                                   f"[CANCELLED]: {reason}")

    db.add(schedule)
    db.flush()

    if schedule.ot_bed_id:
        _release_ot_bed_if_free(db, schedule.ot_bed_id)

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/schedule/{schedule_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.delete"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")

    if schedule.case:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete schedule with an attached OT case",
        )

    ot_bed_id = schedule.ot_bed_id

    db.delete(schedule)
    db.flush()

    if ot_bed_id:
        _release_ot_bed_if_free(db, ot_bed_id)

    db.commit()
    return None


# ============================================================
#  OT CASE ROUTES
# ============================================================


@router.get("/cases", response_model=List[OtCaseOut])
def list_ot_cases(
        date_: Optional[date] = Query(
            None, alias="date", description="Filter by schedule date"),
        ot_bed_id: Optional[int] = Query(
            None, description="Filter by OT location bed"),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])

    q = (
        db.query(OtCase).join(OtSchedule).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            joinedload(OtCase.schedule).joinedload(
                OtSchedule.admission).joinedload(
                    IpdAdmission.current_bed).joinedload(
                        IpdBed.room).joinedload(IpdRoom.ward),

            # ✅ OT location bed (FIXED)
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.ot_bed
                                    ).joinedload(IpdBed.room
                                                 ).joinedload(IpdRoom.ward),
        ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if ot_bed_id:
        q = q.filter(OtSchedule.ot_bed_id == ot_bed_id)
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
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])

    case = (
        db.query(OtCase).options(
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            joinedload(OtCase.schedule).joinedload(
                OtSchedule.admission).joinedload(
                    IpdAdmission.current_bed).joinedload(
                        IpdBed.room).joinedload(IpdRoom.ward),

            # ✅ OT location bed (FIXED)
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.ot_bed
                                    ).joinedload(IpdBed.room
                                                 ).joinedload(IpdRoom.ward),
        ).get(case_id))

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

    # ✅ attach OP no
    data.op_no = op_no
    if data.schedule:
        data.schedule.op_no = op_no

    # ✅ flatten patient info
    p = None
    if case.schedule and case.schedule.patient:
        p = case.schedule.patient

    if p:
        data.patient_id = getattr(p, "id", None)
        fn = (getattr(p, "first_name", "") or "").strip()
        ln = (getattr(p, "last_name", "") or "").strip()
        data.patient_name = (f"{fn} {ln}").strip() or getattr(p, "name", None)
        data.uhid = getattr(p, "uhid", None)
        data.sex = getattr(p, "sex", None) or getattr(p, "gender", None)

        # ✅ age from DOB if available
        dob = getattr(p, "dob", None)
        if dob:
            today = date.today()
            data.age = today.year - dob.year - (
                (today.month, today.day) < (dob.month, dob.day))

    return data


@router.post("/cases",
             response_model=OtCaseOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_case(
        payload: OtCaseCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.create"])

    schedule = db.query(OtSchedule).get(payload.schedule_id)
    if not schedule:
        raise HTTPException(status_code=400, detail="Invalid OT Schedule")

    if schedule.case:
        raise HTTPException(status_code=400,
                            detail="Case already exists for this schedule")

    case = OtCase(
        preop_diagnosis=payload.preop_diagnosis,
        postop_diagnosis=payload.postop_diagnosis,
        final_procedure_name=payload.final_procedure_name,
        speciality_id=payload.speciality_id,
        actual_start_time=payload.actual_start_time,
        actual_end_time=payload.actual_end_time,
        outcome=payload.outcome,
        icu_required=payload.icu_required,
        immediate_postop_condition=payload.immediate_postop_condition,
    )

    db.add(case)
    db.flush()

    schedule.case_id = case.id
    if case.actual_start_time:
        schedule.status = "in_progress"
    db.add(schedule)

    db.commit()
    db.refresh(case)
    return case


@router.put("/cases/{case_id}", response_model=OtCaseOut)
def update_ot_case(
        case_id: int,
        payload: OtCaseUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.update"])

    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")

    data = payload.model_dump(exclude_unset=True)

    if "schedule_id" in data and data["schedule_id"] is not None:
        new_schedule_id = data["schedule_id"]
        current_schedule_id = case.schedule.id if case.schedule else None

        if new_schedule_id != current_schedule_id:
            new_schedule = db.query(OtSchedule).get(new_schedule_id)
            if not new_schedule:
                raise HTTPException(status_code=400,
                                    detail="Invalid new OT Schedule")
            if new_schedule.case and new_schedule.case.id != case.id:
                raise HTTPException(
                    status_code=400,
                    detail="Target schedule already has a case")

            if case.schedule:
                case.schedule.case_id = None

            new_schedule.case_id = case.id
            db.add(new_schedule)

    for field, value in data.items():
        if field == "schedule_id":
            continue
        setattr(case, field, value)

    if case.schedule:
        if case.actual_start_time and case.schedule.status == "planned":
            case.schedule.status = "in_progress"
            db.add(case.schedule)

        if case.actual_end_time and (case.outcome or "").lower() in (
                "completed", "done", "success", "successful", "converted"):
            case.schedule.status = "completed"
            db.add(case.schedule)
            if case.schedule.ot_bed_id:
                _release_ot_bed_if_free(db, case.schedule.ot_bed_id)

    db.add(case)
    db.commit()
    db.refresh(case)
    return case


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
#  OPEN / CLOSE CASE ENDPOINTS
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
    _need_any(user, ["ot.cases.create", "ot.schedule.update"])

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
            actual_start_time=payload.actual_start_time or now,
            icu_required=payload.icu_required or False,
            immediate_postop_condition=payload.immediate_postop_condition,
        )
        db.add(case)
        db.flush()

        schedule.case_id = case.id
        db.add(schedule)
    else:
        if not case.actual_start_time:
            case.actual_start_time = payload.actual_start_time or now
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            if field == "actual_start_time" and value is None:
                continue
            setattr(case, field, value)
        db.add(case)

    schedule.status = "in_progress"
    db.add(schedule)

    if schedule.ot_bed_id:
        _mark_ot_bed_occupied(db, schedule.ot_bed_id)

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
    case.actual_end_time = payload.actual_end_time or case.actual_end_time or now

    if case.actual_end_time and case.actual_start_time and case.actual_end_time <= case.actual_start_time:
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

        if schedule.ot_bed_id:
            _release_ot_bed_if_free(db, schedule.ot_bed_id)

    # ✅ Always close medically first
    db.commit()
    db.refresh(case)

    try:
        inv = create_ot_invoice_items_for_case(db=db,
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


def _build_preop_data_from_payload(payload: OtPreopChecklistIn) -> dict:
    return payload.model_dump(exclude={"completed"})


@router.get("/cases/{case_id}/preop-checklist",
            response_model=OtPreopChecklistOut)
def get_preop_checklist(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)

    checklist = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())
    if not checklist:
        raise HTTPException(status_code=404,
                            detail="Pre-op checklist not found for this case")

    data = checklist.data or {}
    return OtPreopChecklistOut(
        case_id=case_id,
        completed=checklist.completed,
        created_at=checklist.created_at,
        updated_at=checklist.completed_at or checklist.created_at,
        **data,
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
    _need_any(user, ["ot.cases.update"])
    _get_case_or_404(db, case_id)

    existing = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Pre-op checklist already exists. Use PUT to update.")

    data = _build_preop_data_from_payload(payload)
    now = datetime.utcnow() if payload.completed else None

    checklist = PreOpChecklistModel(
        case_id=case_id,
        nurse_user_id=user.id,
        data=data,
        completed=payload.completed,
        completed_at=now,
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
    _need_any(user, ["ot.cases.update"])
    _get_case_or_404(db, case_id)

    checklist = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())
    if not checklist:
        raise HTTPException(status_code=404,
                            detail="Pre-op checklist not found for this case")

    data = _build_preop_data_from_payload(payload)
    now = datetime.utcnow() if payload.completed else None

    checklist.data = data
    checklist.completed = payload.completed
    checklist.completed_at = now
    checklist.nurse_user_id = user.id

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
#  1) Single OT Case PDF
# ============================================================
@router.get("/cases/{case_id}/pdf")
def download_ot_case_pdf(
        case_id: int,
        disposition: Literal["inline", "attachment"] = Query("attachment"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (has_perm(user, "ot.cases.view") or has_perm(
            user, "ot.cases.update") or has_perm(user, "ot.schedules.view")):
        raise HTTPException(status_code=403,
                            detail="Not permitted to view OT case PDF.")

    case = (
        db.query(OtCase).options(
            # schedule + patient/admission/bed + doctors
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            joinedload(OtCase.schedule).joinedload(OtSchedule.admission),
            joinedload(OtCase.schedule).joinedload(OtSchedule.ot_bed),
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),

            # linked clinical records
            joinedload(OtCase.preanaesthesia),
            joinedload(OtCase.preop_checklist),
            joinedload(OtCase.safety_checklist),

            # anaesthesia header + child vitals/drugs
            joinedload(OtCase.anaesthesia_record
                       ).selectinload(AnaesthesiaRecord.vitals),
            joinedload(OtCase.anaesthesia_record).selectinload(
                AnaesthesiaRecord.drugs),
            joinedload(OtCase.nursing_record),
            joinedload(OtCase.counts_record),
            selectinload(OtCase.implant_records),
            selectinload(OtCase.blood_records),
            joinedload(OtCase.operation_note),
            joinedload(OtCase.pacu_record),
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

    pdf_bytes = build_ot_case_pdf(
        case=case,
        org_name="NUTRYAH HIMS",
        generated_by=getattr(user, "full_name", None)
        or getattr(user, "email", None),
    )

    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ============================================================
#  2) Patient OT History PDF (All cases)
# ============================================================
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
        joinedload(OtSchedule.ot_bed),
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


@router.get("/ot/cases/{case_id}/pdf")
def get_ot_case_pdf(
        case_id: int,
        disposition: str = Query("attachment",
                                 pattern="^(inline|attachment)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    case = db.query(OtCase).filter(OtCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    pdf_bytes = build_ot_case_pdf(
        case,
        org_name="NUTRYAH HIMS",
        generated_by=getattr(user, "full_name", None)
        or getattr(user, "email", None),
    )

    filename = f"OT_Case_{case_id}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)
