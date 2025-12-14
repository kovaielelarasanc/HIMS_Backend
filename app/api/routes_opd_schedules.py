# app/api/routes_opd_schedule.py
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.opd import OpdSchedule, Appointment
from app.schemas.opd import OpdScheduleCreate, OpdScheduleUpdate, OpdScheduleOut

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def can_manage_all(user: User) -> bool:
    return has_perm(user, "schedules.manage")


def can_self_manage(user: User) -> bool:
    return has_perm(user, "appointments.create")


def ensure_can_create_for(user: User, doctor_user_id: int):
    if can_manage_all(user):
        return
    if user.id == doctor_user_id and can_self_manage(user):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


def ensure_can_mutate_schedule(user: User, sch: OpdSchedule):
    if can_manage_all(user):
        return
    if user.id == sch.doctor_user_id and can_self_manage(user):
        return
    raise HTTPException(status_code=403, detail="Not permitted")




@router.get("/schedules", response_model=List[OpdScheduleOut])
def list_schedules(
        doctor_user_id: Optional[int] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (can_manage_all(user) or has_perm(user, "appointments.view")):
        raise HTTPException(status_code=403, detail="Not permitted")
    q = db.query(OpdSchedule)
    if doctor_user_id:
        q = q.filter(OpdSchedule.doctor_user_id == doctor_user_id)
    return q.order_by(OpdSchedule.doctor_user_id, OpdSchedule.weekday).all()


@router.get("/schedules/{schedule_id}", response_model=OpdScheduleOut)
def get_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (can_manage_all(user) or has_perm(user, "appointments.view")):
        raise HTTPException(status_code=403, detail="Not permitted")
    sch = db.get(OpdSchedule, schedule_id)
    if not sch:
        raise HTTPException(status_code=404, detail="Not found")
    return sch


@router.post("/schedules", response_model=OpdScheduleOut)
def create_schedule(
        payload: OpdScheduleCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    ensure_can_create_for(user, payload.doctor_user_id)
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=400,
                            detail="End time must be after start time")

    # One schedule per doctor per weekday (extra safety in addition to DB constraint)
    exists = (db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == payload.doctor_user_id,
        OpdSchedule.weekday == payload.weekday,
    ).first())
    if exists:
        raise HTTPException(
            status_code=400,
            detail="Schedule already exists for this doctor on this weekday",
        )

    sch = OpdSchedule(
        doctor_user_id=payload.doctor_user_id,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        slot_minutes=payload.slot_minutes or 15,
        location=payload.location or "",
        is_active=payload.is_active if payload.is_active is not None else True,
    )
    db.add(sch)
    db.commit()
    db.refresh(sch)
    return sch


@router.put("/schedules/{schedule_id}", response_model=OpdScheduleOut)
def update_schedule(
        schedule_id: int,
        payload: OpdScheduleUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    sch = db.get(OpdSchedule, schedule_id)
    if not sch:
        raise HTTPException(status_code=404, detail="Not found")

    ensure_can_mutate_schedule(user, sch)

    data = payload.dict(exclude_unset=True)
    if "start_time" in data or "end_time" in data:
        start = data.get("start_time", sch.start_time)
        end = data.get("end_time", sch.end_time)
        if end <= start:
            raise HTTPException(
                status_code=400,
                detail="End time must be after start time",
            )

    # prevent moving to an already-occupied weekday for that doctor
    if "weekday" in data:
        new_weekday = data["weekday"]
        if new_weekday != sch.weekday:
            dup = (db.query(OpdSchedule).filter(
                OpdSchedule.doctor_user_id == sch.doctor_user_id,
                OpdSchedule.weekday == new_weekday,
                OpdSchedule.id != sch.id,
            ).first())
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail=
                    "Schedule already exists for this doctor on the target weekday",
                )

    for k, v in data.items():
        setattr(sch, k, v)

    db.commit()
    db.refresh(sch)
    return sch


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
        schedule_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    sch = db.get(OpdSchedule, schedule_id)
    if not sch:
        raise HTTPException(status_code=404, detail="Not found")
    ensure_can_mutate_schedule(user, sch)
    db.delete(sch)
    db.commit()
    return {"message": "Deleted"}


# ---------- free slots (string list) ----------
@router.get("/slots/free", response_model=List[str])
def get_free_slots(
        doctor_user_id: int,
        date: date,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if not (can_manage_all(user) or has_perm(user, "appointments.view")):
        raise HTTPException(status_code=403, detail="Not permitted")

    weekday = date.weekday()
    schedules = (db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == doctor_user_id,
        OpdSchedule.weekday == weekday,
        OpdSchedule.is_active.is_(True),
    ).all())
    if not schedules:
        return []

    busy = {
        row[0].strftime("%H:%M")
        for row in db.query(Appointment.slot_start).filter(
            Appointment.doctor_user_id == doctor_user_id,
            Appointment.date == date,
            Appointment.status.in_(["booked", "checked_in", "in_progress"]),
        ).all()
    }

    free: List[str] = []
    for sch in schedules:
        step = timedelta(minutes=sch.slot_minutes or 15)
        cur = datetime.combine(date, sch.start_time)
        end = datetime.combine(date, sch.end_time)
        while cur + step <= end:
            t = cur.strftime("%H:%M")
            if t not in busy:
                free.append(t)
            cur += step
    return sorted(set(free))
