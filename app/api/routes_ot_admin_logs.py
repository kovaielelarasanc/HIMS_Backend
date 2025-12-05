# FILE: app/api/routes_ot_admin_logs.py
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.ot import (
    OtEquipmentDailyChecklist,
    OtCleaningLog,
    OtEnvironmentLog,
)
from app.schemas.ot import (
    OtEquipmentDailyChecklistCreate,
    OtEquipmentDailyChecklistUpdate,
    OtEquipmentDailyChecklistOut,
    OtCleaningLogCreate,
    OtCleaningLogUpdate,
    OtCleaningLogOut,
    OtEnvironmentLogCreate,
    OtEnvironmentLogUpdate,
    OtEnvironmentLogOut,
)

from app.models.user import User

router = APIRouter(prefix="/ot", tags=["OT - Admin & Logs"])


# ============================================================
#  OT EQUIPMENT DAILY CHECKLIST
# ============================================================
def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


@router.get(
    "/equipment-checklists",
    response_model=List[OtEquipmentDailyChecklistOut],
)
def list_equipment_checklists(
        theatre_id: Optional[int] = Query(None,
                                          description="Filter by theatre"),
        date_: Optional[date] = Query(
            None,
            alias="date",
            description="Filter by exact date (yyyy-mm-dd)",
        ),
        from_date: Optional[date] = Query(
            None, description="Filter from date (inclusive, yyyy-mm-dd)"),
        to_date: Optional[date] = Query(
            None, description="Filter to date (inclusive, yyyy-mm-dd)"),
        shift: Optional[str] = Query(
            None, description="Filter by shift (Morning/Evening/Night)"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.equipment_checklist.view"])

    q = db.query(OtEquipmentDailyChecklist)

    if theatre_id is not None:
        q = q.filter(OtEquipmentDailyChecklist.theatre_id == theatre_id)

    if date_:
        q = q.filter(OtEquipmentDailyChecklist.date == date_)
    else:
        if from_date:
            q = q.filter(OtEquipmentDailyChecklist.date >= from_date)
        if to_date:
            q = q.filter(OtEquipmentDailyChecklist.date <= to_date)

    if shift:
        q = q.filter(OtEquipmentDailyChecklist.shift == shift)

    q = q.order_by(
        OtEquipmentDailyChecklist.date.desc(),
        OtEquipmentDailyChecklist.shift.asc().nulls_last(),
        OtEquipmentDailyChecklist.id.desc(),
    )
    return q.all()


@router.get(
    "/equipment-checklists/{checklist_id}",
    response_model=OtEquipmentDailyChecklistOut,
)
def get_equipment_checklist(
        checklist_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.equipment_checklist.view"])

    record = db.query(OtEquipmentDailyChecklist).get(checklist_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Equipment checklist not found")
    return record


@router.post(
    "/equipment-checklists",
    response_model=OtEquipmentDailyChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
def create_equipment_checklist(
        payload: OtEquipmentDailyChecklistCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Create a daily equipment checklist for a theatre/date/shift.
    You can allow multiple per day per theatre by shift,
    or enforce uniqueness on (theatre_id, date, shift) at DB-level.
    """
    _need_any(user, ["ot.equipment_checklist.create"])

    record = OtEquipmentDailyChecklist(
        theatre_id=payload.theatre_id,
        date=payload.date,
        shift=payload.shift,
        checked_by_user_id=payload.checked_by_user_id,
        data=payload.data,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/equipment-checklists/{checklist_id}",
    response_model=OtEquipmentDailyChecklistOut,
)
def update_equipment_checklist(
        checklist_id: int,
        payload: OtEquipmentDailyChecklistUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.equipment_checklist.update"])

    record = db.query(OtEquipmentDailyChecklist).get(checklist_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Equipment checklist not found")

    data = payload.model_dump(exclude_unset=True)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.delete(
    "/equipment-checklists/{checklist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_equipment_checklist(
        checklist_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Hard delete – optional.  
    You can disable this in production and keep only updates.
    """
    _need_any(user, ["ot.equipment_checklist.delete"])

    record = db.query(OtEquipmentDailyChecklist).get(checklist_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Equipment checklist not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  OT CLEANING / STERILITY LOG
# ============================================================


@router.get(
    "/cleaning-logs",
    response_model=List[OtCleaningLogOut],
)
def list_cleaning_logs(
        theatre_id: Optional[int] = Query(None,
                                          description="Filter by theatre"),
        case_id: Optional[int] = Query(None, description="Filter by case"),
        date_: Optional[date] = Query(
            None,
            alias="date",
            description="Filter by exact date (yyyy-mm-dd)",
        ),
        from_date: Optional[date] = Query(
            None, description="Filter from date (inclusive)"),
        to_date: Optional[date] = Query(
            None, description="Filter to date (inclusive)"),
        session: Optional[str] = Query(
            None, description="pre-list / between-cases / end-of-day"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cleaning_log.view"])

    q = db.query(OtCleaningLog)

    if theatre_id is not None:
        q = q.filter(OtCleaningLog.theatre_id == theatre_id)

    if case_id is not None:
        q = q.filter(OtCleaningLog.case_id == case_id)

    if date_:
        q = q.filter(OtCleaningLog.date == date_)
    else:
        if from_date:
            q = q.filter(OtCleaningLog.date >= from_date)
        if to_date:
            q = q.filter(OtCleaningLog.date <= to_date)

    if session:
        q = q.filter(OtCleaningLog.session == session)

    q = q.order_by(
        OtCleaningLog.date.desc(),
        OtCleaningLog.session.asc().nulls_last(),
        OtCleaningLog.id.desc(),
    )
    return q.all()


@router.get(
    "/cleaning-logs/{log_id}",
    response_model=OtCleaningLogOut,
)
def get_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cleaning_log.view"])

    record = db.query(OtCleaningLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404, detail="Cleaning log not found")
    return record


@router.post(
    "/cleaning-logs",
    response_model=OtCleaningLogOut,
    status_code=status.HTTP_201_CREATED,
)
def create_cleaning_log(
        payload: OtCleaningLogCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cleaning_log.create"])

    record = OtCleaningLog(
        theatre_id=payload.theatre_id,
        date=payload.date,
        session=payload.session,
        case_id=payload.case_id,
        method=payload.method,
        done_by_user_id=payload.done_by_user_id,
        remarks=payload.remarks,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cleaning-logs/{log_id}",
    response_model=OtCleaningLogOut,
)
def update_cleaning_log(
        log_id: int,
        payload: OtCleaningLogUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cleaning_log.update"])

    record = db.query(OtCleaningLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    data = payload.model_dump(exclude_unset=True)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.delete(
    "/cleaning-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.cleaning_log.delete"])

    record = db.query(OtCleaningLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  OT ENVIRONMENT LOG (Temp / Humidity / Pressure)
# ============================================================


@router.get(
    "/environment-logs",
    response_model=List[OtEnvironmentLogOut],
)
def list_environment_logs(
        theatre_id: Optional[int] = Query(None,
                                          description="Filter by theatre"),
        date_: Optional[date] = Query(
            None,
            alias="date",
            description="Filter by exact date",
        ),
        from_date: Optional[date] = Query(
            None, description="Filter from date (inclusive)"),
        to_date: Optional[date] = Query(
            None, description="Filter to date (inclusive)"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.environment_log.view"])

    q = db.query(OtEnvironmentLog)

    if theatre_id is not None:
        q = q.filter(OtEnvironmentLog.theatre_id == theatre_id)

    if date_:
        q = q.filter(OtEnvironmentLog.date == date_)
    else:
        if from_date:
            q = q.filter(OtEnvironmentLog.date >= from_date)
        if to_date:
            q = q.filter(OtEnvironmentLog.date <= to_date)

    q = q.order_by(
        OtEnvironmentLog.date.desc(),
        OtEnvironmentLog.time.desc(),
        OtEnvironmentLog.id.desc(),
    )
    return q.all()


@router.get(
    "/environment-logs/{log_id}",
    response_model=OtEnvironmentLogOut,
)
def get_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.environment_log.view"])

    record = db.query(OtEnvironmentLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")
    return record


@router.post(
    "/environment-logs",
    response_model=OtEnvironmentLogOut,
    status_code=status.HTTP_201_CREATED,
)
def create_environment_log(
        payload: OtEnvironmentLogCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.environment_log.create"])

    record = OtEnvironmentLog(
        theatre_id=payload.theatre_id,
        date=payload.date,
        time=payload.time,
        temperature_c=payload.temperature_c,
        humidity_percent=payload.humidity_percent,
        pressure_diff_pa=payload.pressure_diff_pa,
        logged_by_user_id=payload.logged_by_user_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/environment-logs/{log_id}",
    response_model=OtEnvironmentLogOut,
)
def update_environment_log(
        log_id: int,
        payload: OtEnvironmentLogUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.environment_log.update"])

    record = db.query(OtEnvironmentLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")

    data = payload.model_dump(exclude_unset=True)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.delete(
    "/environment-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.environment_log.delete"])

    record = db.query(OtEnvironmentLog).get(log_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")

    db.delete(record)
    db.commit()
    return None
