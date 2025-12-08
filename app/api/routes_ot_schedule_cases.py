# FILE: app/api/routes_ot_schedule_cases.py
from __future__ import annotations

from datetime import date, time, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_
from app.models.ipd import IpdBed, IpdAdmission, IpdRoom, IpdWard  # 👈 add
from app.models.opd import Visit
from app.models.patient import Patient  # 👈 add
from app.models.user import User as UserModel  # 👈 alias to avoid clash
from sqlalchemy.exc import IntegrityError
from app.api.deps import get_db, current_user
from app.models.ot import (
    OtSchedule,
    OtCase,
    PreOpChecklist as PreOpChecklistModel,
    OtScheduleProcedure,
    OtProcedure,
)
from app.schemas.ot import (
    OtScheduleCreate,
    OtScheduleUpdate,
    OtScheduleOut,
    OtCaseCreate,
    OtCaseUpdate,
    OtCaseOut,
    OtPreopChecklistIn,
    OtPreopInvestigations,
    OtPreopVitals,
    OtPreopChecklistOut,
    OtCaseCloseBody,
)
from app.services.billing_ot import create_ot_invoice_items_for_case
from app.models.user import User

router = APIRouter(prefix="/ot", tags=["OT - Schedule & Cases"])

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
    bed_id: int,
    date_: date,
    start: time,
    end: Optional[time],
    exclude_id: Optional[int] = None,
) -> None:
    """
    Check for OT booking overlap **on the same IPD bed (OT bed)** and date.
    Conflicts if:
      - Same bed
      - Same date
      - Status != cancelled
      - Time ranges overlap.
    """
    if not bed_id:
        # If no bed assigned yet, skip conflict validation
        return

    q = db.query(OtSchedule).filter(
        OtSchedule.bed_id == bed_id,
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
            detail="OT schedule conflict for this bed & time range",
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
        bed_id: Optional[int] = Query(
            None,
            description="Filter by IPD bed (OT location via Ward/Room/Bed)",
        ),
        surgeon_user_id: Optional[int] = Query(None),
        patient_id: Optional[int] = Query(None),
        status_: Optional[str] = Query(None, alias="status"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.schedule.view"])

    q = (
        db.query(OtSchedule).options(
            joinedload(OtSchedule.patient),
            joinedload(OtSchedule.surgeon),
            joinedload(OtSchedule.anaesthetist),
            # OT bed (location)
            joinedload(OtSchedule.bed).joinedload(IpdBed.room
                                                  ).joinedload(IpdRoom.ward),
            # Admission + ward bed (if patient admitted)
            joinedload(OtSchedule.admission
                       ).joinedload(IpdAdmission.current_bed
                                    ).joinedload(IpdBed.room
                                                 ).joinedload(IpdRoom.ward),
            joinedload(OtSchedule.case),
        ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if bed_id:
        q = q.filter(OtSchedule.bed_id == bed_id)
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
        joinedload(OtSchedule.patient),
        joinedload(OtSchedule.surgeon),
        joinedload(OtSchedule.anaesthetist),
        joinedload(OtSchedule.bed).joinedload(IpdBed.room).joinedload(
            IpdRoom.ward),
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

    # ---------- FK validations (tell exactly which ID is wrong) ----------
    missing_refs: list[str] = []

    print("DEBUG OT SCHEDULE PAYLOAD:", payload.model_dump())

    if payload.patient_id:
        if not db.get(Patient, payload.patient_id):
            missing_refs.append(f"patient_id={payload.patient_id}")

    if payload.bed_id:
        bed = db.get(IpdBed, payload.bed_id)
        print(
            "DEBUG OT SCHEDULE BED CHECK: payload.bed_id =",
            payload.bed_id,
            "| bed found in DB:",
            (bed.id if bed else None),
        )
        if not bed:
            missing_refs.append(f"bed_id={payload.bed_id}")

    if payload.admission_id:
        if not db.get(IpdAdmission, payload.admission_id):
            missing_refs.append(f"admission_id={payload.admission_id}")

    if payload.surgeon_user_id:
        if not db.get(UserModel, payload.surgeon_user_id):
            missing_refs.append(f"surgeon_user_id={payload.surgeon_user_id}")

    if payload.anaesthetist_user_id:
        if not db.get(UserModel, payload.anaesthetist_user_id):
            missing_refs.append(
                f"anaesthetist_user_id={payload.anaesthetist_user_id}")

    if missing_refs:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid FK reference(s): {', '.join(missing_refs)}",
        )

    # ---------- Procedure master validation ----------
    if payload.primary_procedure_id:
        proc = db.get(OtProcedure, payload.primary_procedure_id)
        if not proc:
            raise HTTPException(
                status_code=400,
                detail=
                f"Invalid primary_procedure_id={payload.primary_procedure_id}",
            )

    # ---------- Conflict check ----------
    if payload.bed_id:
        _check_schedule_conflict(
            db=db,
            bed_id=payload.bed_id,
            date_=payload.date,
            start=payload.planned_start_time,
            end=payload.planned_end_time,
            exclude_id=None,
        )

    # ---------- Create schedule ----------
    try:
        schedule = OtSchedule(
            date=payload.date,
            planned_start_time=payload.planned_start_time,
            planned_end_time=payload.planned_end_time,
            patient_id=payload.patient_id,
            admission_id=payload.admission_id,
            bed_id=payload.bed_id,
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
        db.flush()

        # ... add OtScheduleProcedure links ...

        db.commit()
        db.refresh(schedule)
        return schedule

    except IntegrityError as e:
        db.rollback()
        print("DEBUG INTEGRITY ERROR:", str(e.orig))
        raise HTTPException(
            status_code=400,
            detail=
            ("Database constraint error (check patient/bed/admission/procedure IDs): "
             f"{str(e.orig)}"),
        )


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

    # Pre-calculate potential new values for conflict check
    new_date = data.get("date", schedule.date)
    new_start = data.get("planned_start_time", schedule.planned_start_time)
    new_end = data.get("planned_end_time", schedule.planned_end_time)
    new_bed_id = data.get("bed_id", schedule.bed_id)

    if new_start:
        _validate_time_order(new_start, new_end)

    # conflict check for updated bed/date/time
    if new_bed_id:
        _check_schedule_conflict(
            db=db,
            bed_id=new_bed_id,
            date_=new_date,
            start=new_start,
            end=new_end,
            exclude_id=schedule.id,
        )

    # handle simple fields
    for field in [
            "date",
            "planned_start_time",
            "planned_end_time",
            "patient_id",
            "admission_id",
            "bed_id",
            "surgeon_user_id",
            "anaesthetist_user_id",
            "procedure_name",
            "side",
            "priority",
            "notes",
    ]:
        if field in data:
            setattr(schedule, field, data[field])

    # primary_procedure_id + additional_procedure_ids
    if "primary_procedure_id" in data or "additional_procedure_ids" in data:
        # reset all links and rebuild
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
        bed_id: Optional[int] = Query(
            None,
            description="Filter by IPD bed (OT location via Ward/Room/Bed)",
        ),
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
        joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
        joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
        joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.admission).joinedload(
                IpdAdmission.current_bed).joinedload(IpdBed.room).joinedload(
                    IpdRoom.ward),
        joinedload(OtCase.schedule).joinedload(OtSchedule.bed).joinedload(
            IpdBed.room).joinedload(IpdRoom.ward),
    ))

    if date_:
        q = q.filter(OtSchedule.date == date_)
    if bed_id:
        q = q.filter(OtSchedule.bed_id == bed_id)
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
        user=Depends(current_user),
):
    _need_any(user, ["ot.cases.view"])
    case = (
        db.query(OtCase).options(
            # schedule + patient
            joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
            # surgeon + anaesthetist
            joinedload(OtCase.schedule).joinedload(OtSchedule.surgeon),
            joinedload(OtCase.schedule).joinedload(OtSchedule.anaesthetist),
            # admission + its current bed
            joinedload(OtCase.schedule
                       ).joinedload(OtSchedule.admission).joinedload(
                           IpdAdmission.current_bed
                       ).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            # OT bed (location)
            joinedload(OtCase.schedule).joinedload(OtSchedule.bed).joinedload(
                IpdBed.room).joinedload(IpdRoom.ward),
        ).get(case_id))

    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    sched = case.schedule
    op_no = None

    # 🔹 resolve latest OP visit for this patient (if any)
    if sched and sched.patient_id:
        latest_visit = (db.query(Visit).filter(
            Visit.patient_id == sched.patient_id).order_by(
                Visit.visit_at.desc()).first())
        if latest_visit:
            op_no = latest_visit.op_no  # property we added above

    # Let Pydantic build the base object from ORM…
    data = OtCaseOut.model_validate(case, from_attributes=True)

    # …then inject op_no into nested schedule for the UI
    if data.schedule:
        data.schedule.op_no = op_no

    return data


# FILE: app/api/routes_ot_schedule_cases.py


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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OT Schedule",
        )

    if schedule.case:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Case already exists for this schedule",
        )

    # ❌ DON'T PASS schedule_id INTO OtCase(...)
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
    db.flush()  # get case.id

    # 🔗 link via schedule.case_id
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

    # 🔁 re-link to another schedule if requested
    if "schedule_id" in data and data["schedule_id"] is not None:
        new_schedule_id = data["schedule_id"]
        current_schedule_id = case.schedule.id if case.schedule else None

        if new_schedule_id != current_schedule_id:
            new_schedule = db.query(OtSchedule).get(new_schedule_id)
            if not new_schedule:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid new OT Schedule",
                )
            if new_schedule.case and new_schedule.case.id != case.id:
                raise HTTPException(
                    status_code=400,
                    detail="Target schedule already has a case",
                )

            # unlink old schedule
            if case.schedule:
                case.schedule.case_id = None

            # link new schedule
            new_schedule.case_id = case.id
            db.add(new_schedule)

    # apply other fields
    for field, value in data.items():
        if field == "schedule_id":
            continue
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
        raise HTTPException(
            status_code=400,
            detail="Cannot open case for a cancelled schedule",
        )

    case = schedule.case
    now = datetime.utcnow()

    if not case:
        # ❌ no schedule_id here
        case = OtCase(
            preop_diagnosis=payload.preop_diagnosis,
            final_procedure_name=payload.final_procedure_name,
            speciality_id=payload.speciality_id,
            actual_start_time=payload.actual_start_time or now,
            icu_required=payload.icu_required or False,
            immediate_postop_condition=payload.immediate_postop_condition,
        )
        db.add(case)
        db.flush()  # get case.id

        # 🔗 link both sides
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

    db.commit()
    db.refresh(case)
    return case



# ============================================================
#  OT CASE SUB-RESOURCES – PRE-OP CHECKLIST
# ============================================================


def _build_preop_data_from_payload(payload: OtPreopChecklistIn) -> dict:
    """
    Convert OtPreopChecklistIn into a JSON-friendly dict
    that we store in PreOpChecklist.data.
    We keep `completed` as a separate DB column, so we exclude it from data.
    """
    return payload.model_dump(exclude={"completed"})


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

    checklist = (db.query(PreOpChecklistModel).filter(
        PreOpChecklistModel.case_id == case_id).first())

    if not checklist:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pre-op checklist not found for this case",
        )

    data = checklist.data or {}

    return OtPreopChecklistOut(
        case_id=case_id,
        completed=checklist.completed,
        created_at=checklist.created_at,
        updated_at=checklist.completed_at or checklist.created_at,
        **data,  # 🔥 includes checklist, investigations, vitals, flags, etc.
    )


@router.post(
    "/cases/{case_id}/preop-checklist",
    response_model=OtPreopChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pre-op checklist already exists. Use PUT to update.",
        )

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


@router.put(
    "/cases/{case_id}/preop-checklist",
    response_model=OtPreopChecklistOut,
)
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pre-op checklist not found for this case",
        )

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
