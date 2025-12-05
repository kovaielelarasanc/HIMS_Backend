# FILE: app/api/routes_ot_schedule_cases.py
from __future__ import annotations

from datetime import date, time, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_

from app.api.deps import get_db, current_user
from app.models.ot import OtSchedule, OtTheatre, OtCase
from app.schemas.ot import (
    OtScheduleCreate,
    OtScheduleUpdate,
    OtScheduleOut,
    OtCaseCreate,
    OtCaseUpdate,
    OtCaseOut,
)
from app.models.user import User

router = APIRouter(prefix="/ot", tags=["OT - Schedule & Cases"])

# ============================================================
#  INTERNAL HELPERS
# ============================================================


# ---------------- RBAC ----------------
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
    theatre_id: int,
    date_: date,
    start: time,
    end: Optional[time],
    exclude_id: Optional[int] = None,
) -> None:
    """
    Check for OT booking overlap in same theatre/date.
    Conflicts if:
      - Same theatre
      - Same date
      - Status != cancelled
      - Time ranges overlap.
    """
    q = db.query(OtSchedule).filter(
        OtSchedule.theatre_id == theatre_id,
        OtSchedule.date == date_,
        OtSchedule.status != "cancelled",
    )

    if exclude_id:
        q = q.filter(OtSchedule.id != exclude_id)

    if end is None:
        # No end time: treat as point booking.
        q = q.filter(
            or_(
                # existing has end and our start lies within it
                and_(
                    OtSchedule.planned_end_time.isnot(None),
                    OtSchedule.planned_start_time <= start,
                    OtSchedule.planned_end_time > start,
                ),
                # both have no end and same start
                and_(
                    OtSchedule.planned_end_time.is_(None),
                    OtSchedule.planned_start_time == start,
                ),
            ))
    else:
        # Our block [start, end) overlaps existing block.
        q = q.filter(
            or_(
                # existing also a block
                and_(
                    OtSchedule.planned_end_time.isnot(None),
                    OtSchedule.planned_start_time < end,
                    OtSchedule.planned_end_time > start,
                ),
                # existing is point booking inside our block
                and_(
                    OtSchedule.planned_end_time.is_(None),
                    OtSchedule.planned_start_time >= start,
                    OtSchedule.planned_start_time < end,
                ),
            ))

    if q.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OT schedule conflict in this theatre & time range",
        )


def _get_case_or_404(db: Session, case_id: int) -> OtCase:
    """
    Common helper to load an OT case or raise 404.
    """
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
        date_: Optional[date] = Query(
            None,
            alias="date",
            description="Filter by date (yyyy-mm-dd)",
        ),
        theatre_id: Optional[int] = Query(None),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        status_: Optional[str] = Query(None, alias="status"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.schedule.view"])

    q = (db.query(OtSchedule).options(
        joinedload(OtSchedule.theatre),
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.case),
    ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if theatre_id:
        q = q.filter(OtSchedule.theatre_id == theatre_id)
    if surgeon_user_id:
        q = q.filter(OtSchedule.surgeon_user_id == surgeon_user_id)
    if patient_id:
        q = q.filter(OtSchedule.patient_id == patient_id)
    if status_:
        q = q.filter(OtSchedule.status == status_)

    q = q.order_by(
        OtSchedule.date.asc(),
        OtSchedule.planned_start_time.asc(),
    )
    return q.all()


@router.get("/schedule/{schedule_id}", response_model=OtScheduleOut)
def get_ot_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.schedule.view"])

    schedule = (db.query(OtSchedule).options(
        joinedload(OtSchedule.theatre),
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.case),
    ).get(schedule_id))
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")
    return schedule


@router.post(
    "/schedule",
    response_model=OtScheduleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_schedule(
        payload: OtScheduleCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.create"])

    theatre = db.query(OtTheatre).get(payload.theatre_id)
    if not theatre or not theatre.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or inactive OT Theatre",
        )

    _validate_time_order(payload.planned_start_time, payload.planned_end_time)
    _check_schedule_conflict(
        db=db,
        theatre_id=payload.theatre_id,
        date_=payload.date,
        start=payload.planned_start_time,
        end=payload.planned_end_time,
    )

    schedule = OtSchedule(
        theatre_id=payload.theatre_id,
        date=payload.date,
        planned_start_time=payload.planned_start_time,
        planned_end_time=payload.planned_end_time,
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        surgeon_user_id=payload.surgeon_user_id,
        anaesthetist_user_id=payload.anaesthetist_user_id,
        procedure_name=payload.procedure_name,
        side=payload.side,
        priority=payload.priority,
        status="planned",
        notes=payload.notes,
    )

    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/schedule/{schedule_id}", response_model=OtScheduleOut)
def update_ot_schedule(
        schedule_id: int,
        payload: OtScheduleUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.schedule.update"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Schedule not found",
        )

    data = payload.model_dump(exclude_unset=True)

    new_theatre_id = data.get("theatre_id", schedule.theatre_id)
    new_date = data.get("date", schedule.date)
    new_start = data.get("planned_start_time", schedule.planned_start_time)
    new_end = data.get("planned_end_time", schedule.planned_end_time)

    _validate_time_order(new_start, new_end)
    _check_schedule_conflict(
        db=db,
        theatre_id=new_theatre_id,
        date_=new_date,
        start=new_start,
        end=new_end,
        exclude_id=schedule.id,
    )

    # If theatre changed, verify active
    if "theatre_id" in data:
        theatre = db.query(OtTheatre).get(new_theatre_id)
        if not theatre or not theatre.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or inactive OT Theatre",
            )

    for field, value in data.items():
        setattr(schedule, field, value)

    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.post("/schedule/{schedule_id}/cancel", response_model=OtScheduleOut)
def cancel_ot_schedule(
        schedule_id: int,
        reason: Optional[str] = Query(
            None,
            description="Optional reason for cancellation",
        ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Cancel instead of delete (NABH / medico-legal safety).
    Not allowed if case has already started.
    """
    _need_any(user, ["ot.schedule.cancel"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Schedule not found",
        )

    if schedule.case and schedule.case.actual_start_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel schedule – OT case already started",
        )

    schedule.status = "cancelled"
    if reason:
        if schedule.notes:
            schedule.notes += f"\n[CANCELLED]: {reason}"
        else:
            schedule.notes = f"[CANCELLED]: {reason}"

    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete(
    "/schedule/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ot_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Hard delete – strongly discouraged in production.
    Use cancel instead.
    """
    _need_any(user, ["ot.schedule.delete"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Schedule not found",
        )

    if schedule.case:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete schedule with an attached OT case",
        )

    db.delete(schedule)
    db.commit()
    return None


# ============================================================
#  OT CASE ROUTES
# ============================================================


@router.get("/cases", response_model=List[OtCaseOut])
def list_ot_cases(
        date_: Optional[date] = Query(
            None,
            alias="date",
            description="Filter by OT date (Schedule date)",
        ),
        theatre_id: Optional[int] = Query(None),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    List OT cases, optionally filtered via schedule attributes.
    """
    _need_any(user, ["ot.cases.view"])

    q = (db.query(OtCase).join(OtSchedule).options(
        joinedload(OtCase.schedule).joinedload(OtSchedule.theatre),
        joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
        joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
        joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
    ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if theatre_id:
        q = q.filter(OtSchedule.theatre_id == theatre_id)
    if surgeon_user_id:
        q = q.filter(OtSchedule.surgeon_user_id == surgeon_user_id)
    if patient_id:
        q = q.filter(OtSchedule.patient_id == patient_id)

    q = q.order_by(
        OtSchedule.date.asc(),
        OtSchedule.planned_start_time.asc(),
    )
    return q.all()


@router.get("/cases/{case_id}", response_model=OtCaseOut)
def get_ot_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(OtSchedule.theatre),
        joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
        joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
        joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
    ).get(case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Case not found",
        )
    return case


@router.post(
    "/cases",
    response_model=OtCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_case(
        payload: OtCaseCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Generic case create. In practice, prefer /schedule/{id}/open-case.
    """
    _need_any(user, ["ot.cases.create"])

    schedule = db.query(OtSchedule).get(payload.schedule_id)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OT Schedule",
        )

    if schedule.case:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Case already exists for this schedule",
        )

    case = OtCase(
        schedule_id=payload.schedule_id,
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
    # Mark schedule as in_progress if case has started
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

    if "schedule_id" in data:
        new_schedule_id = data["schedule_id"]
        if new_schedule_id != case.schedule_id:
            schedule = db.query(OtSchedule).get(new_schedule_id)
            if not schedule:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid new OT Schedule",
                )
            if schedule.case and schedule.case.id != case.id:
                raise HTTPException(
                    status_code=400,
                    detail="Target schedule already has a case",
                )

    for field, value in data.items():
        setattr(case, field, value)

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
    """
    Hard delete – again, clinically be careful.
    """
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
    """
    Payload when surgeon/OT staff 'open' a case from schedule.
    """
    preop_diagnosis: Optional[str] = None
    final_procedure_name: Optional[str] = None
    speciality_id: Optional[int] = None
    actual_start_time: Optional[datetime] = None
    icu_required: Optional[bool] = None
    immediate_postop_condition: Optional[str] = None


class OtCaseClosePayload(BaseModel):
    """
    Payload for 'closing' an OT case.
    """
    outcome: str = Field(..., max_length=50)
    actual_end_time: Optional[datetime] = None
    icu_required: Optional[bool] = None
    immediate_postop_condition: Optional[str] = None


@router.post(
    "/schedule/{schedule_id}/open-case",
    response_model=OtCaseOut,
)
def open_ot_case_for_schedule(
        schedule_id: int,
        payload: OtCaseOpenPayload,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Open an OT case for a scheduled surgery.
    - Creates OtCase if not exists.
    - Sets actual_start_time.
    - Sets schedule.status = 'in_progress'.
    """
    _need_any(user, ["ot.cases.create", "ot.schedule.update"])

    schedule = db.query(OtSchedule).get(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="OT Schedule not found")

    if schedule.status == "cancelled":
        raise HTTPException(
            status_code=400,
            detail="Cannot open case for a cancelled schedule",
        )

    case = schedule.case
    now = datetime.utcnow()

    if not case:
        case = OtCase(
            schedule_id=schedule.id,
            preop_diagnosis=payload.preop_diagnosis,
            final_procedure_name=payload.final_procedure_name,
            speciality_id=payload.speciality_id,
            actual_start_time=payload.actual_start_time or now,
            icu_required=payload.icu_required or False,
            immediate_postop_condition=payload.immediate_postop_condition,
        )
        db.add(case)
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

    db.commit()
    # reload with schedule + nested
    db.refresh(case)
    return case


@router.post(
    "/cases/{case_id}/close",
    response_model=OtCaseOut,
)
def close_ot_case(
        case_id: int,
        payload: OtCaseClosePayload,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Close an OT case:
    - Sets outcome & actual_end_time.
    - Updates schedule.status appropriately.
    """
    _need_any(user,
              ["ot.cases.close", "ot.cases.update", "ot.schedule.update"])

    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")

    if not case.actual_start_time:
        raise HTTPException(
            status_code=400,
            detail=
            "Cannot close a case that has not been started (no actual_start_time)",
        )

    now = datetime.utcnow()

    case.outcome = payload.outcome
    case.actual_end_time = payload.actual_end_time or now

    if payload.icu_required is not None:
        case.icu_required = payload.icu_required
    if payload.immediate_postop_condition is not None:
        case.immediate_postop_condition = payload.immediate_postop_condition

    schedule = case.schedule
    if schedule:
        outcome_lower = (payload.outcome or "").lower()
        if outcome_lower in ("completed", "converted"):
            schedule.status = "completed"
        elif outcome_lower in ("abandoned", "cancelled"):
            schedule.status = "cancelled"
        else:
            if schedule.status != "completed":
                schedule.status = "completed"
        db.add(schedule)

    db.add(case)
    db.commit()
    db.refresh(case)
    return case


# ============================================================
#  OT CASE SUB-RESOURCES (STUB IMPLEMENTATIONS)
#  These make your OtCaseDetailPage load without 404/405,
#  and can be wired to real DB models later.
# ============================================================

# --------- Pydantic DTOs for sub-resources ----------


class OtPreopChecklistOut(BaseModel):
    case_id: int
    completed: bool = False
    items: list[dict] = []  # you can structure later


class OtSafetyChecklistOut(BaseModel):
    case_id: int
    sign_in: Optional[datetime] = None
    time_out: Optional[datetime] = None
    sign_out: Optional[datetime] = None


class OtAnaesthesiaRecordOut(BaseModel):
    case_id: int
    events: list[dict] = []
    vitals: list[dict] = []


class OtNursingRecordOut(BaseModel):
    case_id: int
    entries: list[dict] = []


class OtCountsOut(BaseModel):
    case_id: int
    initial: list[dict] = []
    final: list[dict] = []
    discrepancies: list[dict] = []


class OtTransfusionsOut(BaseModel):
    case_id: int
    events: list[dict] = []


class OtOperationNoteOut(BaseModel):
    case_id: int
    note: str = ""


class OtPacuRecordOut(BaseModel):
    case_id: int
    observations: list[dict] = []
    discharge_status: Optional[str] = None


class OtCleaningLogOut(BaseModel):
    id: int
    case_id: Optional[int] = None
    theatre_id: Optional[int] = None
    cleaned_at: Optional[datetime] = None
    cleaned_by: Optional[str] = None
    remarks: Optional[str] = None


# ---------- Pre-op checklist ----------


@router.get(
    "/cases/{case_id}/preop-checklist",
    response_model=OtPreopChecklistOut,
)
def get_preop_checklist(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    # TODO: plug into real table later
    return OtPreopChecklistOut(case_id=case_id)


# ---------- Safety checklist ----------
@router.post(
    "/cases/{case_id}/preop-checklist",
    response_model=OtPreopChecklistOut,
)
def create_preop_checklist(
        case_id: int,
        payload: OtPreopChecklistIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.update"])
    _get_case_or_404(db, case_id)

    # TODO: save to DB
    # For now just echo:
    return OtPreopChecklistOut(
        case_id=case_id,
        completed=payload.completed,
        items=[payload.model_dump()],
    )


@router.put(
    "/cases/{case_id}/preop-checklist",
    response_model=OtPreopChecklistOut,
)
def update_preop_checklist(
        case_id: int,
        payload: OtPreopChecklistIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.update"])
    _get_case_or_404(db, case_id)

    # TODO: update in DB
    # For now just echo updated payload
    return OtPreopChecklistOut(
        case_id=case_id,
        completed=payload.completed,
        items=[payload.model_dump()],
    )


@router.get(
    "/cases/{case_id}/safety-checklist",
    response_model=OtSafetyChecklistOut,
)
def get_safety_checklist(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtSafetyChecklistOut(case_id=case_id)


# ---------- Anaesthesia record ----------


@router.get(
    "/cases/{case_id}/anaesthesia-record",
    response_model=OtAnaesthesiaRecordOut,
)
def get_anaesthesia_record(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtAnaesthesiaRecordOut(case_id=case_id)


# ---------- Intra-op nursing ----------


@router.get(
    "/cases/{case_id}/nursing",
    response_model=OtNursingRecordOut,
)
def get_nursing_record(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Only GET is implemented here so your UI can load without 405.
    Create/update endpoints can be added separately.
    """
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtNursingRecordOut(case_id=case_id)


# ---------- Instrument / sponge counts ----------


@router.get(
    "/cases/{case_id}/counts",
    response_model=OtCountsOut,
)
def get_counts(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtCountsOut(case_id=case_id)


# ---------- Blood transfusions ----------


@router.get(
    "/cases/{case_id}/transfusions",
    response_model=OtTransfusionsOut,
)
def get_transfusions(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Only GET is implemented so that your page load doesn't 405.
    """
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtTransfusionsOut(case_id=case_id)


# ---------- Operation note ----------


@router.get(
    "/cases/{case_id}/operation-note",
    response_model=OtOperationNoteOut,
)
def get_operation_note(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtOperationNoteOut(case_id=case_id)


# ---------- PACU / Recovery ----------


@router.get(
    "/cases/{case_id}/pacu",
    response_model=OtPacuRecordOut,
)
def get_pacu_record(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    _get_case_or_404(db, case_id)
    return OtPacuRecordOut(case_id=case_id)


# ---------- Cleaning logs (outside case sub-path) ----------


@router.get(
    "/cleaning-logs",
    response_model=List[OtCleaningLogOut],
)
def list_cleaning_logs(
        case_id: Optional[int] = Query(
            None,
            description="Optional filter by OT case id",
        ),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Stub list endpoint to satisfy UI calls like:
    GET /api/ot/cleaning-logs?case_id=1

    Currently returns an empty list; you can later
    wire this to a real OtCleaningLog model.
    """
    _need_any(user, ["ot.cases.view"])

    # If you want, you can at least validate that the case exists
    if case_id is not None:
        _get_case_or_404(db, case_id)

    # No DB model yet → return empty list
    return []
