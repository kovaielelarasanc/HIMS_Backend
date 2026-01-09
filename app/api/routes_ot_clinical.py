# FILE: app/api/routes_ot_clinical.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date, time, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Literal
from sqlalchemy.orm.attributes import flag_modified

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import desc
from app.api.deps import get_db, current_user
from app.models.user import User
from sqlalchemy import select
from app.models.ot import (
    OtSchedule,
    OtCase,
    PreAnaesthesiaEvaluation,
    PreOpChecklist,
    SurgicalSafetyChecklist as SurgicalSafetyChecklistModel,
    AnaesthesiaRecord,
    AnaesthesiaVitalLog,
    AnaesthesiaDrugLog,
    OtNursingRecord,
    OtSpongeInstrumentCount,
    OtImplantRecord,
    OperationNote,
    OtBloodTransfusionRecord,
    PacuRecord,
    OtCleaningLog,
    OtEnvironmentLog,
    AnaesthesiaDeviceUse,
    OtCaseInstrumentCountLine,
)
from app.models.ot_master import OtDeviceMaster, OtInstrumentMaster
from app.models.ui_branding import UiBranding
from app.models.ipd import IpdBed, IpdAdmission, IpdRoom
from app.services.ui_branding import get_ui_branding
# Optional import - keep if you use it elsewhere in this file


from app.services.pdfs.ot_safety_checklist_pdf import build_ot_safety_checklist_pdf_bytes
from app.services.pdfs.ot_anaesthesia_record_pdf import (
    build_ot_anaesthesia_record_pdf_bytes,
    build_ot_preanaesthetic_record_pdf_bytes,
)
from app.services.pdfs.ot_pacu_record_pdf import build_ot_pacu_record_pdf_bytes

from app.schemas.ot import (
    # Pre-anaesthesia
    PreAnaesthesiaEvaluationCreate,
    PreAnaesthesiaEvaluationUpdate,
    PreAnaesthesiaEvaluationOut,
    # Pre-op checklist
    PreOpChecklistCreate,
    PreOpChecklistUpdate,
    PreOpChecklistOut,
    # Safety checklist
    OtSafetyChecklistIn,
    OtSafetyChecklistOut,
    OtSafetyPhaseSignIn,
    OtSafetyPhaseTimeOut,
    OtSafetyPhaseSignOut,
    # Anaesthesia record
    AnaesthesiaVitalLogUpdate,
    AnaesthesiaVitalLogOut,
    AnaesthesiaDrugLogUpdate,
    AnaesthesiaDrugLogOut,
    OtAnaesthesiaRecordIn,
    OtAnaesthesiaRecordOut,
    OtAnaesthesiaVitalIn,
    OtAnaesthesiaVitalOut,
    OtAnaesthesiaDrugIn,
    OtAnaesthesiaDrugOut,
    OtAnaesthesiaRecordDefaultsOut,
    # Nursing record
    OtNursingRecordCreate,
    OtNursingRecordUpdate,
    OtNursingRecordOut,
    # Sponge & instrument
    OtCountsIn,
    OtCountsOut,
    OtCountItemsUpsertIn,
    OtCountItemLineOut,
    # Implants
    OtImplantRecordCreate,
    OtImplantRecordUpdate,
    OtImplantRecordOut,
    # Operation note
    OperationNoteCreate,
    OperationNoteUpdate,
    OperationNoteOut,
    # Blood transfusion
    OtBloodTransfusionRecordCreate,
    OtBloodTransfusionRecordUpdate,
    OtBloodTransfusionRecordOut,
    # PACU
    PacuUiIn,
    PacuUiOut,
    # Logs
    OtCleaningLogCreate,
    OtCleaningLogUpdate,
    OtCleaningLogOut,
    OtEnvironmentLogCreate,
    OtEnvironmentLogUpdate,
    OtEnvironmentLogOut,
)

router = APIRouter(prefix="/ot", tags=["OT - Clinical Records"])

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# ============================================================
#  HELPERS
# ============================================================


def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


def _get_case_or_404(db: Session, case_id: int) -> OtCase:
    case = db.get(OtCase, case_id)
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Case not found",
        )
    return case


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _base_date_for_case(case: OtCase) -> date:
    if getattr(case, "schedule", None) and getattr(case.schedule, "date",
                                                   None):
        return case.schedule.date
    return _now_ist().date()


def _as_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC; convert aware to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_ist_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert stored dt (naive assumed UTC) to IST aware dt."""
    if not dt:
        return None
    return _as_utc(dt).astimezone(IST)


def _to_hhmm(dt: Optional[datetime]) -> Optional[str]:
    """Convert stored dt (naive assumed UTC) -> HH:MM in IST."""
    d = _to_ist_dt(dt)
    return d.strftime("%H:%M") if d else None


def _hhmm_to_utc_naive_for_case(case: OtCase,
                                t: Optional[str]) -> Optional[datetime]:
    """
    Parse UI 'HH:MM' (assumed IST) with case base date -> store as UTC naive datetime.
    """
    if not t:
        return None
    try:
        hour, minute = [int(x) for x in t.split(":", 1)]
    except (ValueError, TypeError):
        return None

    base_date = _base_date_for_case(case)
    dt_ist = datetime.combine(base_date,
                              time(hour=hour, minute=minute),
                              tzinfo=IST)
    dt_utc = dt_ist.astimezone(UTC)
    return dt_utc.replace(tzinfo=None)


# Backward compatible alias used by PACU endpoints
def _time_str_to_dt(value: Optional[str],
                    case: Optional[OtCase] = None) -> Optional[datetime]:
    """
    Parse 'HH:MM' in IST and store as UTC naive.
    If case is provided, uses case.schedule.date; else uses today's IST date.
    """
    if not value:
        return None
    dummy_case = case or OtCase()
    return _hhmm_to_utc_naive_for_case(dummy_case, value)


def _dt_to_time_str(value: Optional[datetime]) -> Optional[str]:
    """Convert stored dt (naive assumed UTC) into 'HH:MM' string in IST."""
    return _to_hhmm(value)


def _get_branding(db: Session) -> UiBranding:
    # ✅ adjust if you have tenant-wise branding
    branding = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not branding:
        branding = UiBranding(
            org_name="Hospital",
            org_tagline="",
            org_address="",
            org_phone="",
            org_email="",
            org_website="",
            org_gstin="",
            logo_path="",
        )
    return branding


# ============================================================
#  OT DEVICE MASTERS (AIRWAY / MONITOR)
#  ✅ Used by Anaesthesia UI for dynamic device selection
# ============================================================


@router.get("/device-masters", response_model=List[dict])
def list_ot_device_masters(
        category: Optional[str] = Query(None, description="AIRWAY or MONITOR"),
        q: Optional[str] = Query(None),
        active: Optional[bool] = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(
        user,
        ["ot.masters.view", "ot.anaesthesia_record.view", "ot.cases.view"])

    qry = db.query(OtDeviceMaster)

    if category:
        cat = category.strip().upper()
        if cat not in ("AIRWAY", "MONITOR"):
            raise HTTPException(
                status_code=400,
                detail="Invalid category (use AIRWAY or MONITOR)")
        qry = qry.filter(OtDeviceMaster.category == cat)

    if active is not None:
        qry = qry.filter(OtDeviceMaster.is_active == active)

    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(OtDeviceMaster.name.ilike(like))

    rows = qry.order_by(OtDeviceMaster.category.asc(),
                        OtDeviceMaster.name.asc()).all()

    return [{
        "id": r.id,
        "category": r.category,
        "code": r.code,
        "name": r.name,
        "cost": float(r.cost or 0),
        "description": r.description,
        "is_active": r.is_active,
    } for r in rows]


# ============================================================
#  DEVICE SYNC HELPERS (Anaesthesia record)
# ============================================================


def _validate_device_ids_by_category(db: Session, ids: list[int],
                                     category: str) -> list[int]:
    clean = sorted({int(x) for x in (ids or []) if x})
    if not clean:
        return []
    cat = category.strip().upper()

    rows = (db.query(OtDeviceMaster.id).filter(
        OtDeviceMaster.id.in_(clean)).filter(
            OtDeviceMaster.category == cat).all())
    found = {rid for (rid, ) in rows}
    missing = set(clean) - found
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {cat} device ids: {sorted(missing)}")
    return clean


def _get_device_ids_for_record(db: Session,
                               record_id: int) -> tuple[list[int], list[int]]:
    rows = (db.query(
        AnaesthesiaDeviceUse.device_id, OtDeviceMaster.category).join(
            OtDeviceMaster,
            OtDeviceMaster.id == AnaesthesiaDeviceUse.device_id).filter(
                AnaesthesiaDeviceUse.record_id == record_id).all())
    airway_ids: list[int] = []
    monitor_ids: list[int] = []
    for did, cat in rows:
        if cat == "AIRWAY":
            airway_ids.append(did)
        elif cat == "MONITOR":
            monitor_ids.append(did)
    airway_ids.sort()
    monitor_ids.sort()
    return airway_ids, monitor_ids


def _sync_anaesthesia_devices(db: Session, record: AnaesthesiaRecord,
                              device_ids: list[int]) -> None:
    """
    AnaesthesiaDeviceUse is the source of truth for devices used.
    device_ids must be the final selected list (AIRWAY + MONITOR).
    """
    clean = sorted({int(x) for x in (device_ids or []) if x})

    existing_rows = (db.query(AnaesthesiaDeviceUse).filter(
        AnaesthesiaDeviceUse.record_id == record.id).all())
    existing_ids = {r.device_id for r in existing_rows}

    if not clean:
        for r in existing_rows:
            db.delete(r)
        return

    valid_ids = {
        did
        for (did, ) in db.query(OtDeviceMaster.id).filter(
            OtDeviceMaster.id.in_(clean)).all()
    }
    invalid = set(clean) - valid_ids
    if invalid:
        raise HTTPException(status_code=400,
                            detail=f"Invalid device ids: {sorted(invalid)}")

    # delete removed
    for r in existing_rows:
        if r.device_id not in valid_ids:
            db.delete(r)

    # add new
    for did in clean:
        if did not in existing_ids:
            db.add(
                AnaesthesiaDeviceUse(record_id=record.id, device_id=did,
                                     qty=1))


def _device_names_by_ids(db: Session, ids: list[int]) -> list[str]:
    if not ids:
        return []
    rows = db.query(OtDeviceMaster).filter(OtDeviceMaster.id.in_(ids)).all()
    by_id = {
        int(r.id): (r.name or str(r.id)).strip()
        for r in rows if r.id is not None
    }
    return [by_id[i] for i in ids if by_id.get(i)]


# ============================================================
#  PRE-ANAESTHESIA EVALUATION
# ============================================================


@router.get("/cases/{case_id}/pre-anaesthesia",
            response_model=Optional[PreAnaesthesiaEvaluationOut])
def get_pre_anaesthesia_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.preanaesthesia
    return record or None


@router.post(
    "/cases/{case_id}/pre-anaesthesia",
    response_model=PreAnaesthesiaEvaluationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pre_anaesthesia_for_case(
        case_id: int,
        payload: PreAnaesthesiaEvaluationCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.create"])

    case = _get_case_or_404(db, case_id)
    if case.preanaesthesia:
        raise HTTPException(
            status_code=400,
            detail="Pre-anaesthesia record already exists for this case")

    if payload.case_id != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    record = PreAnaesthesiaEvaluation(
        case_id=case_id,
        anaesthetist_user_id=payload.anaesthetist_user_id,
        asa_grade=payload.asa_grade,
        comorbidities=payload.comorbidities,
        airway_assessment=payload.airway_assessment,
        allergies=payload.allergies,
        previous_anaesthesia_issues=payload.previous_anaesthesia_issues,
        plan=payload.plan,
        risk_explanation=payload.risk_explanation,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/cases/{case_id}/pre-anaesthesia",
            response_model=PreAnaesthesiaEvaluationOut)
def update_pre_anaesthesia_for_case(
        case_id: int,
        payload: PreAnaesthesiaEvaluationUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.update"])

    case = _get_case_or_404(db, case_id)
    record = case.preanaesthesia
    if not record:
        raise HTTPException(status_code=404,
                            detail="Pre-anaesthesia record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)
    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  PRE-OP CHECKLIST
# ============================================================


@router.get("/cases/{case_id}/pre-op-checklist",
            response_model=Optional[PreOpChecklistOut])
def get_preop_checklist_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = getattr(case, "preop_checklist", None)
    return record or None


@router.post(
    "/cases/{case_id}/pre-op-checklist",
    response_model=PreOpChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
def create_preop_checklist_for_case(
        case_id: int,
        payload: PreOpChecklistCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.create"])

    case = _get_case_or_404(db, case_id)
    existing = getattr(case, "preop_checklist", None)
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Pre-op checklist already exists for this case")

    if getattr(payload, "case_id", case_id) != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    record = PreOpChecklist(case_id=case_id)
    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)
    for k, v in data.items():
        if hasattr(record, k):
            setattr(record, k, v)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/cases/{case_id}/pre-op-checklist",
            response_model=PreOpChecklistOut)
def update_preop_checklist_for_case(
        case_id: int,
        payload: PreOpChecklistUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.update"])

    case = _get_case_or_404(db, case_id)
    record = getattr(case, "preop_checklist", None)
    if not record:
        create_payload = PreOpChecklistCreate(**payload.model_dump(
            exclude_unset=True),
                                              case_id=case_id)  # type: ignore
        return create_preop_checklist_for_case(case_id, create_payload, db,
                                               user)

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)
    for k, v in data.items():
        if hasattr(record, k):
            setattr(record, k, v)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  SURGICAL SAFETY CHECKLIST (WHO)
# ============================================================


@router.get("/cases/{case_id}/safety-checklist",
            response_model=Optional[OtSafetyChecklistOut])
def get_safety_checklist_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.safety.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.safety_checklist
    if not record:
        return None

    sign_in_data = record.sign_in_data or {}
    time_out_data = record.time_out_data or {}
    sign_out_data = record.sign_out_data or {}

    sign_in_done = bool(sign_in_data.get("done") or record.sign_in_done_by_id)
    time_out_done = bool(
        time_out_data.get("done") or record.time_out_done_by_id)
    sign_out_done = bool(
        sign_out_data.get("done") or record.sign_out_done_by_id)

    sign_in_phase = OtSafetyPhaseSignIn.model_validate({
        k: v
        for k, v in sign_in_data.items() if k != "done"
    })
    time_out_phase = OtSafetyPhaseTimeOut.model_validate({
        k: v
        for k, v in time_out_data.items() if k != "done"
    })
    sign_out_phase = OtSafetyPhaseSignOut.model_validate({
        k: v
        for k, v in sign_out_data.items() if k != "done"
    })

    updated_candidates = [
        record.sign_in_time, record.time_out_time, record.sign_out_time,
        record.created_at
    ]
    updated_at = max([d for d in updated_candidates if d is not None])

    return OtSafetyChecklistOut(
        case_id=case_id,
        sign_in_done=sign_in_done,
        sign_in_time=_to_hhmm(record.sign_in_time),
        time_out_done=time_out_done,
        time_out_time=_to_hhmm(record.time_out_time),
        sign_out_done=sign_out_done,
        sign_out_time=_to_hhmm(record.sign_out_time),
        sign_in=sign_in_phase,
        time_out=time_out_phase,
        sign_out=sign_out_phase,
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(updated_at),
    )


@router.post("/cases/{case_id}/safety-checklist",
             response_model=OtSafetyChecklistOut,
             status_code=status.HTTP_201_CREATED)
def create_safety_checklist_for_case(
        case_id: int,
        payload: OtSafetyChecklistIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.safety.manage"])

    case = _get_case_or_404(db, case_id)
    if case.safety_checklist:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Safety checklist already exists for this case")

    sign_in_time = _hhmm_to_utc_naive_for_case(case, payload.sign_in_time)
    time_out_time = _hhmm_to_utc_naive_for_case(case, payload.time_out_time)
    sign_out_time = _hhmm_to_utc_naive_for_case(case, payload.sign_out_time)

    sign_in_data = payload.sign_in.model_dump()
    sign_in_data["done"] = bool(payload.sign_in_done)

    time_out_data = payload.time_out.model_dump()
    time_out_data["done"] = bool(payload.time_out_done)

    sign_out_data = payload.sign_out.model_dump()
    sign_out_data["done"] = bool(payload.sign_out_done)

    record = SurgicalSafetyChecklistModel(
        case_id=case_id,
        sign_in_data=sign_in_data,
        sign_in_done_by_id=user.id if payload.sign_in_done else None,
        sign_in_time=sign_in_time,
        time_out_data=time_out_data,
        time_out_done_by_id=user.id if payload.time_out_done else None,
        time_out_time=time_out_time,
        sign_out_data=sign_out_data,
        sign_out_done_by_id=user.id if payload.sign_out_done else None,
        sign_out_time=sign_out_time,
    )

    db.add(record)
    db.commit()
    db.refresh(record)

    updated_candidates = [
        record.sign_in_time, record.time_out_time, record.sign_out_time,
        record.created_at
    ]
    updated_at = max([d for d in updated_candidates if d is not None])

    return OtSafetyChecklistOut(
        case_id=case_id,
        sign_in_done=payload.sign_in_done,
        sign_in_time=_to_hhmm(record.sign_in_time),
        time_out_done=payload.time_out_done,
        time_out_time=_to_hhmm(record.time_out_time),
        sign_out_done=payload.sign_out_done,
        sign_out_time=_to_hhmm(record.sign_out_time),
        sign_in=payload.sign_in,
        time_out=payload.time_out,
        sign_out=payload.sign_out,
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(updated_at),
    )


@router.put("/cases/{case_id}/safety-checklist",
            response_model=OtSafetyChecklistOut)
def update_safety_checklist_for_case(
        case_id: int,
        payload: OtSafetyChecklistIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.safety.manage"])

    case = _get_case_or_404(db, case_id)
    record = case.safety_checklist
    if not record:
        return create_safety_checklist_for_case(case_id=case_id,
                                                payload=payload,
                                                db=db,
                                                user=user)

    record.sign_in_time = _hhmm_to_utc_naive_for_case(case,
                                                      payload.sign_in_time)
    record.time_out_time = _hhmm_to_utc_naive_for_case(case,
                                                       payload.time_out_time)
    record.sign_out_time = _hhmm_to_utc_naive_for_case(case,
                                                       payload.sign_out_time)

    record.sign_in_data = payload.sign_in.model_dump()
    record.sign_in_data["done"] = bool(payload.sign_in_done)
    record.sign_in_done_by_id = user.id if payload.sign_in_done else None

    record.time_out_data = payload.time_out.model_dump()
    record.time_out_data["done"] = bool(payload.time_out_done)
    record.time_out_done_by_id = user.id if payload.time_out_done else None

    record.sign_out_data = payload.sign_out.model_dump()
    record.sign_out_data["done"] = bool(payload.sign_out_done)
    record.sign_out_done_by_id = user.id if payload.sign_out_done else None

    db.add(record)
    db.commit()
    db.refresh(record)

    updated_candidates = [
        record.sign_in_time, record.time_out_time, record.sign_out_time,
        record.created_at
    ]
    updated_at = max([d for d in updated_candidates if d is not None])

    return OtSafetyChecklistOut(
        case_id=case_id,
        sign_in_done=payload.sign_in_done,
        sign_in_time=_to_hhmm(record.sign_in_time),
        time_out_done=payload.time_out_done,
        time_out_time=_to_hhmm(record.time_out_time),
        sign_out_done=payload.sign_out_done,
        sign_out_time=_to_hhmm(record.sign_out_time),
        sign_in=payload.sign_in,
        time_out=payload.time_out,
        sign_out=payload.sign_out,
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(updated_at),
    )


def _empty_safety_pdf_payload() -> dict:
    return {"sign_in": {}, "time_out": {}, "sign_out": {}}


@router.get("/cases/{case_id}/safety-checklist/pdf")
def download_safety_checklist_pdf(
        case_id: int,
        download: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.safety.view", "ot.cases.view"])

    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.admission).joinedload(
                IpdAdmission.current_bed).joinedload(IpdBed.room).joinedload(
                    IpdRoom.ward),
        joinedload(OtCase.safety_checklist),
    ).filter(OtCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    record = case.safety_checklist
    data = _empty_safety_pdf_payload()

    if record:
        sign_in_data = {
            k: v
            for k, v in (record.sign_in_data or {}).items() if k != "done"
        }
        time_out_data = {
            k: v
            for k, v in (record.time_out_data or {}).items() if k != "done"
        }
        sign_out_data = {
            k: v
            for k, v in (record.sign_out_data or {}).items() if k != "done"
        }
        data = {
            "sign_in": sign_in_data,
            "time_out": time_out_data,
            "sign_out": sign_out_data
        }

    branding = _get_branding(db)

    pdf_bytes = build_ot_safety_checklist_pdf_bytes(
        branding=branding,
        case=case,
        safety_data=data,
    )

    filename = f"OT_Surgical_Safety_Checklist_Case_{case_id}.pdf"
    disp = "attachment" if download else "inline"

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disp}; filename="{filename}"'},
    )


# ============================================================
#  ANAESTHESIA RECORD + VITALS + DRUG LOGS
# ============================================================


def _safe_str(v) -> str:
    return "" if v is None else str(v).strip()


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def _calc_age_from_dob(dob) -> Optional[int]:
    try:
        if not dob:
            return None
        today = datetime.now(tz=IST).date()
        return today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day))
    except Exception:
        return None


def _case_for_pacu_pdf(db: Session, case_id: int) -> OtCase:

    def _opt_case(attr: str):
        return selectinload(getattr(OtCase, attr)) if hasattr(OtCase,
                                                              attr) else None

    opts = [
        _opt_case("patient"),
        _opt_case("procedure"),
        _opt_case("procedure_master"),
        _opt_case("theatre"),
        _opt_case("theatre_master"),
        _opt_case("ot_room"),
        _opt_case("or_room"),
        _opt_case("room"),
    ]
    opts = [o for o in opts if o is not None]

    # ✅ IMPORTANT: schedule is where your real data is (patient, theatre, procedure)
    if hasattr(OtCase, "schedule"):
        sch_rel = getattr(OtCase, "schedule")
        opts.append(selectinload(sch_rel))

        # Try nested loads dynamically
        try:
            sch_cls = sch_rel.property.mapper.class_

            def _opt_sched(child: str):
                return selectinload(sch_rel).selectinload(
                    getattr(sch_cls, child)) if hasattr(sch_cls,
                                                        child) else None

            nested = [
                _opt_sched("patient"),
                _opt_sched("admission"),
                _opt_sched("theater"),  # your code uses sched.theater
                _opt_sched("surgeon"),
                _opt_sched("anaesthetist"),
            ]
            opts.extend([o for o in nested if o is not None])
        except Exception:
            pass

    case = db.query(OtCase).options(*opts).filter(OtCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")
    return case


def _build_patient_fields_for_pdf(case: OtCase) -> dict:
    """
    Create a stable dict for PDF, even if you store patient snapshot on case.
    """
    p = getattr(case, "patient", None)

    def g(obj, name, default=""):
        return getattr(obj, name, default) if obj is not None else default

    # name fallback: case snapshot -> patient relation -> composed first/last
    full_name = (
        g(case, "patient_name", "") or g(case, "full_name", "")
        or g(p, "full_name", "") or g(p, "name", "")
        or (f"{g(p,'first_name','')} {g(p,'last_name','')}".strip())).strip()

    uhid = (g(case, "uhid", "") or g(case, "patient_uhid", "")
            or g(p, "uhid", "") or g(p, "patient_id", "") or g(p, "uid", ""))

    age = (g(case, "age", "") or g(case, "patient_age", "") or g(p, "age", "")
           or g(p, "age_years", ""))

    sex = (g(case, "sex", "") or g(case, "gender", "")
           or g(case, "patient_gender", "") or g(p, "sex", "")
           or g(p, "gender", ""))

    or_no = (g(case, "or_no", "") or g(case, "ot_room_name", "")
             or g(case, "theatre_name", "")
             or g(getattr(case, "ot_room", None), "name", "")
             or g(getattr(case, "or_room", None), "name", "")
             or g(getattr(case, "theatre", None), "name", "")
             or g(getattr(case, "theatre_master", None), "name", ""))

    return {
        "patient": p,  # ✅ allows PDF to read p.full_name / p.uhid etc.
        "name": full_name,
        "uhid": uhid,
        "age": age,
        "sex": sex,
        "case_no": g(case, "case_no", "") or str(getattr(case, "id", "")),
        "or_no": or_no,
    }


def _build_patient_fields_for_case(db: Session, case: OtCase) -> dict:
    sched = getattr(case, "schedule", None)
    patient = getattr(sched, "patient", None) if sched else None
    admission = getattr(sched, "admission", None) if sched else None

    prefix = _safe_str(getattr(patient, "prefix", None)) if patient else ""

    name = ""
    if patient:
        fn = _safe_str(getattr(patient, "first_name", None))
        ln = _safe_str(getattr(patient, "last_name", None))
        name = (f"{fn} {ln}").strip() or _safe_str(
            getattr(patient, "name", None)) or _safe_str(
                getattr(patient, "full_name", None))
    if not name:
        name = _safe_str(getattr(case, "patient_name", None))

    uhid = ""
    if patient:
        uhid = _safe_str(
            getattr(patient, "uhid", None)
            or getattr(patient, "uhid_number", None)
            or getattr(patient, "mrn", None))
    if not uhid:
        uhid = _safe_str(getattr(case, "uhid", None))

    sex = ""
    if patient:
        sex = _safe_str(
            getattr(patient, "sex", None) or getattr(patient, "gender", None))

    age = None
    if patient:
        age = _calc_age_from_dob(getattr(patient, "dob", None)) or getattr(
            patient, "age", None)
    age = age if isinstance(age, int) else None

    age_sex = ""
    if age is not None and sex:
        age_sex = f"{age} / {sex}"
    elif age is not None:
        age_sex = f"{age}"
    elif sex:
        age_sex = sex

    op_no = ""
    try:
        if patient and getattr(patient, "id", None):
            from app.models.opd import Visit
            latest_visit = (db.query(Visit).filter(
                Visit.patient_id == patient.id).order_by(
                    Visit.visit_at.desc()).first())
            if latest_visit:
                op_no = _safe_str(getattr(latest_visit, "op_no", None))
    except Exception:
        op_no = ""

    ip_no = ""
    if admission:
        ip_no = _safe_str(
            getattr(admission, "display_code", None)
            or getattr(admission, "ip_no", None)
            or getattr(admission, "admission_no", None))

    diagnosis = _safe_str(
        getattr(case, "preop_diagnosis", None)
        or getattr(case, "postop_diagnosis", None))

    proposed_operation = ""
    if sched:
        proposed_operation = _safe_str(
            getattr(sched, "procedure_name", None)
            or getattr(case, "final_procedure_name", None)
            or getattr(case, "procedure_name", None))

    ot_date = getattr(sched, "date", None) if sched else None
    date_str = ot_date.strftime("%d-%m-%Y") if ot_date else ""

    or_no = ""
    if sched:
        theater = getattr(sched, "theater", None)
        or_no = _safe_str(
            getattr(sched, "or_no", None) or getattr(theater, "name", None)
            or getattr(theater, "theater_no", None))

    case_no = _safe_str(
        getattr(case, "case_no", None) or getattr(sched, "case_no", None)
        or getattr(case, "id", None))

    blood_group = _safe_str(
        getattr(patient, "blood_group", None) if patient else "")
    weight = _safe_str(getattr(patient, "weight", None) if patient else "")
    height = _safe_str(getattr(patient, "height", None) if patient else "")

    return {
        "patient_prefix": prefix,
        "name": name,
        "uhid": uhid,
        "age_sex": age_sex,
        "op_no": op_no,
        "ip_no": ip_no,
        "diagnosis": diagnosis,
        "proposed_operation": proposed_operation,
        "date": date_str,
        "or_no": or_no,
        "case_no": case_no,
        "blood_group": blood_group,
        "weight": weight,
        "height": height,
    }


def _user_display(u) -> str:
    if not u:
        return ""
    return (_safe_str(getattr(u, "full_name", None))
            or _safe_str(getattr(u, "name", None))
            or _safe_str(getattr(u, "username", None))
            or (_safe_str(getattr(u, "first_name", None)) + " " +
                _safe_str(getattr(u, "last_name", None))).strip())


def _build_intra_fields_from_schedule(case: OtCase) -> Dict[str, str]:
    sched = getattr(case, "schedule", None)
    if not sched:
        return {}

    theater = getattr(sched, "theater", None)
    intra_date = ""
    if getattr(sched, "date", None):
        intra_date = sched.date.strftime("%d/%m/%Y")  # UI shows DD/MM/YYYY

    intra_or_no = _safe_str(getattr(sched, "or_no", None)) or _safe_str(
        getattr(theater, "name", None)) or _safe_str(
            getattr(theater, "theater_no", None))

    intra_anaes = _user_display(getattr(sched, "anaesthetist", None))
    intra_surgeon = _user_display(getattr(sched, "surgeon", None))

    intra_case_type = _safe_str(getattr(sched, "priority", None))
    intra_proc = _safe_str(getattr(
        sched, "procedure_name", None)) or _safe_str(
            getattr(case, "final_procedure_name", None))

    intra_anaes_type = _safe_str(getattr(
        sched, "anaesthesia_type", None)) or _safe_str(
            getattr(sched, "type_of_anaesthesia", None))

    return {
        "intra_date": intra_date,
        "intra_or_no": intra_or_no,
        "intra_anaesthesiologist": intra_anaes,
        "intra_surgeon": intra_surgeon,
        "intra_case_type": intra_case_type,
        "intra_surgical_procedure": intra_proc,
        "intra_anaesthesia_type": intra_anaes_type,
    }


def _merge_intra_auto_fields(case: OtCase, data: Dict[str,
                                                      Any]) -> Dict[str, Any]:
    data = dict(data or {})
    intra = _build_intra_fields_from_schedule(case)
    for k, v in intra.items():
        if _is_blank(data.get(k)) and not _is_blank(v):
            data[k] = v
    return data


def _merge_preop_auto_fields(db: Session, case: OtCase, data: dict) -> dict:
    data = dict(data or {})
    pf = _build_patient_fields_for_case(db, case)

    def _set_if_blank(key: str, value: str):
        if _is_blank(data.get(key)) and not _is_blank(value):
            data[key] = value

    _set_if_blank("patient_prefix", pf.get("patient_prefix") or "")
    _set_if_blank("diagnosis", pf.get("diagnosis") or "")
    _set_if_blank("proposed_operation", pf.get("proposed_operation") or "")
    _set_if_blank("blood_group", pf.get("blood_group") or "")
    _set_if_blank("weight", pf.get("weight") or "")
    _set_if_blank("height", pf.get("height") or "")

    return data


@router.get("/cases/{case_id}/anaesthesia-record/defaults",
            response_model=OtAnaesthesiaRecordDefaultsOut)
def get_anaesthesia_record_defaults(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.view", "ot.cases.view"])
    case = _get_case_or_404(db, case_id)
    intra = _build_intra_fields_from_schedule(case)
    return OtAnaesthesiaRecordDefaultsOut(
        **{
            k: intra.get(k, "")
            for k in OtAnaesthesiaRecordDefaultsOut.model_fields.keys()
        })


@router.get("/cases/{case_id}/anaesthesia-record",
            response_model=OtAnaesthesiaRecordOut | None)
def get_anaesthesia_record_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.view", "ot.cases.view"])
    case = _get_case_or_404(db, case_id)

    record = case.anaesthesia_record
    if not record:
        return None

    data = dict(record.preop_vitals or {})
    data = _merge_preop_auto_fields(db, case, data)
    data = _merge_intra_auto_fields(case, data)

    airway_ids, monitor_ids = _get_device_ids_for_record(db, record.id)

    out = {
        **data,  # ✅ show saved JSON at top-level for UI
        "id": record.id,
        "case_id": case_id,
        "anaesthetist_user_id": record.anaesthetist_user_id,
        "created_at": _to_ist_dt(record.created_at),
        "updated_at": _to_ist_dt(getattr(record, "updated_at", None)),
        "airway_device_ids": airway_ids,
        "monitor_device_ids": monitor_ids,
        "anaesthesia_type": record.plan or None,
        "notes": record.intraop_summary or None,
        "raw_json": data,
    }
    return OtAnaesthesiaRecordOut(**out)


def _build_record_json(payload: OtAnaesthesiaRecordIn) -> Dict[str, Any]:
    d = payload.model_dump()
    for k in (
            "id",
            "case_id",
            "record_id",
            "created_at",
            "updated_at",
            "anaesthetist_user_id",
            "raw_json",
            "anaesthesia_type",
            "notes",
            "airway_device_ids",
            "monitor_device_ids",
    ):
        d.pop(k, None)
    return d


@router.post("/cases/{case_id}/anaesthesia-record",
             response_model=OtAnaesthesiaRecordOut | None,
             status_code=status.HTTP_201_CREATED)
def create_anaesthesia_record_for_case(
        case_id: int,
        payload: OtAnaesthesiaRecordIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.create"])

    case = _get_case_or_404(db, case_id)
    if case.anaesthesia_record:
        raise HTTPException(
            status_code=400,
            detail="Anaesthesia record already exists for this case")

    preop = _build_record_json(payload)
    preop = _merge_preop_auto_fields(db, case, preop)
    preop = _merge_intra_auto_fields(case, preop)

    sched_anaes_id = getattr(getattr(case, "schedule", None),
                             "anaesthetist_user_id", None)
    anaes_user_id = sched_anaes_id or user.id

    record = AnaesthesiaRecord(
        case_id=case_id,
        anaesthetist_user_id=anaes_user_id,
        preop_vitals=preop,
        plan=payload.anaesthesia_type,
        intraop_summary=payload.notes,
    )
    db.add(record)
    db.flush()

    airway_ids = _validate_device_ids_by_category(
        db, payload.airway_device_ids or [], "AIRWAY")
    monitor_ids = _validate_device_ids_by_category(
        db, payload.monitor_device_ids or [], "MONITOR")
    _sync_anaesthesia_devices(db, record, airway_ids + monitor_ids)

    db.commit()
    db.refresh(record)
    return get_anaesthesia_record_for_case(case_id, db, user)  # type: ignore


@router.put("/cases/{case_id}/anaesthesia-record",
            response_model=OtAnaesthesiaRecordOut | None)
def update_anaesthesia_record_for_case(
        case_id: int,
        payload: OtAnaesthesiaRecordIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.update"])

    case = _get_case_or_404(db, case_id)
    record = case.anaesthesia_record
    if not record:
        return create_anaesthesia_record_for_case(case_id, payload, db, user)

    preop = _build_record_json(payload)
    preop = _merge_preop_auto_fields(db, case, preop)
    preop = _merge_intra_auto_fields(case, preop)

    record.preop_vitals = preop
    record.plan = payload.anaesthesia_type
    record.intraop_summary = payload.notes
    db.add(record)
    db.flush()

    airway_ids = _validate_device_ids_by_category(
        db, payload.airway_device_ids or [], "AIRWAY")
    monitor_ids = _validate_device_ids_by_category(
        db, payload.monitor_device_ids or [], "MONITOR")
    _sync_anaesthesia_devices(db, record, airway_ids + monitor_ids)

    db.commit()
    db.refresh(record)
    return get_anaesthesia_record_for_case(case_id, db, user)  # type: ignore


@router.get("/cases/{case_id}/anaesthesia-record/pdf")
def get_anaesthesia_record_pdf(
        case_id: int,
        section: str = Query("full", description="full | preop"),
        download: bool = Query(False),
        disposition: Optional[Literal["inline", "attachment"]] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.view", "ot.cases.view"])

    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(OtSchedule.patient),
        joinedload(OtCase.schedule).joinedload(OtSchedule.admission),
        joinedload(OtCase.schedule).joinedload(OtSchedule.theater),
        joinedload(OtCase.anaesthesia_record).joinedload(
            AnaesthesiaRecord.vitals),
        joinedload(OtCase.anaesthesia_record).joinedload(
            AnaesthesiaRecord.drugs),
    ).filter(OtCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    record = case.anaesthesia_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    branding = _get_branding(db)
    patient_fields = _build_patient_fields_for_case(db, case)

    record_dict = _merge_preop_auto_fields(db, case,
                                           dict(record.preop_vitals or {}))
    record_dict = _merge_intra_auto_fields(case, record_dict)

    # ✅ add created_at for "Created Date" on PDF strip
    record_dict["created_at"] = getattr(record, "created_at", None)
    record_dict["anaesthesia_type"] = getattr(record, "plan", None)
    record_dict["notes"] = getattr(record, "intraop_summary", None)

    airway_ids, monitor_ids = _get_device_ids_for_record(db, record.id)
    airway_names = _device_names_by_ids(db, airway_ids)
    monitor_names = _device_names_by_ids(db, monitor_ids)

    # vitals list for PDF
    def _to_float(x):
        try:
            return float(x) if x is not None else None
        except Exception:
            return None

    vitals: list[dict] = []
    for v in (getattr(record, "vitals", None) or []):
        sbp = getattr(v, "bp_systolic", None)
        dbp = getattr(v, "bp_diastolic", None)
        bp_str = None
        if sbp is not None and dbp is not None:
            bp_str = f"{sbp}/{dbp}"
        elif sbp is not None:
            bp_str = str(sbp)

        vitals.append({
            "time_dt":
            getattr(v, "time", None),
            "hr":
            getattr(v, "pulse", None),
            "bp":
            bp_str,
            "bp_systolic":
            sbp,
            "bp_diastolic":
            dbp,
            "spo2":
            getattr(v, "spo2", None),
            "rr":
            getattr(v, "rr", None),
            "temp_c":
            _to_float(getattr(v, "temperature", None)),
            "comments":
            getattr(v, "comments", None),
            "etco2":
            _to_float(getattr(v, "etco2", None)),
            "ventilation_mode":
            getattr(v, "ventilation_mode", None),
            "peak_airway_pressure":
            _to_float(getattr(v, "peak_airway_pressure", None)),
            "cvp_pcwp":
            _to_float(getattr(v, "cvp_pcwp", None)),
            "st_segment":
            getattr(v, "st_segment", None),
            "urine_output_ml":
            getattr(v, "urine_output_ml", None),
            "blood_loss_ml":
            getattr(v, "blood_loss_ml", None),
            "oxygen_fio2":
            getattr(v, "oxygen_fio2", None),
            "n2o":
            getattr(v, "n2o", None),
            "air":
            getattr(v, "air", None),
            "agent":
            getattr(v, "agent", None),
            "iv_fluids":
            getattr(v, "iv_fluids", None),
        })
    vitals.sort(key=lambda x: (x.get("time_dt") is None, x.get("time_dt")))

    # drugs list for PDF
    drugs: list[dict] = []
    for d in (getattr(record, "drugs", None) or []):
        drugs.append({
            "time_dt": getattr(d, "time", None),
            "drug_name": getattr(d, "drug_name", None) or "",
            "dose": getattr(d, "dose", None),
            "route": getattr(d, "route", None),
            "remarks": getattr(d, "remarks", None),
        })
    drugs.sort(key=lambda x: (x.get("time_dt") is None, x.get("time_dt")))

    section_norm = (section or "full").strip().lower().replace("_", "-")
    preop_aliases = {
        "preop",
        "pre-op",
        "preanaesthesia",
        "pre-anaesthesia",
        "preanesthesia",
        "pre-anesthesia",
        "preanesthetic",
        "pre-anesthetic",
        "pre-anaesthetic",
    }

    if section_norm in preop_aliases:
        pdf_bytes = build_ot_preanaesthetic_record_pdf_bytes(
            branding=branding,
            case=case,
            patient_fields=patient_fields,
            record=record_dict,
            airway_names=airway_names,
            monitor_names=monitor_names,
        )
        filename = f"OT_PreAnaesthetic_Record_Case_{case_id}.pdf"
    else:
        pdf_bytes = build_ot_anaesthesia_record_pdf_bytes(
            branding=branding,
            case=case,
            patient_fields=patient_fields,
            record=record_dict,
            airway_names=airway_names,
            monitor_names=monitor_names,
            vitals=vitals,
            drugs=drugs,
        )
        filename = f"OT_Anaesthesia_Record_Full_Case_{case_id}.pdf"

    disp = disposition or ("attachment" if download else "inline")
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disp}; filename="{filename}"'},
    )


# ---------------------------
# VITALS
# ---------------------------


@router.get("/anaesthesia-records/{record_id}/vitals",
            response_model=List[OtAnaesthesiaVitalOut])
def list_anaesthesia_vitals(
        record_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user,
              ["ot.anaesthesia_vitals.view", "ot.anaesthesia_record.view"])

    record = db.get(AnaesthesiaRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    out: list[OtAnaesthesiaVitalOut] = []
    for v in record.vitals:
        bp_str = f"{v.bp_systolic}/{v.bp_diastolic}" if (
            v.bp_systolic is not None and v.bp_diastolic is not None) else None

        out.append(
            OtAnaesthesiaVitalOut(
                id=v.id,
                record_id=v.record_id,
                time=_to_hhmm(v.time),
                hr=v.pulse,
                bp=bp_str,
                spo2=v.spo2,
                rr=v.rr,
                temp_c=float(v.temperature)
                if v.temperature is not None else None,
                etco2=float(v.etco2) if v.etco2 is not None else None,
                comments=v.comments,
                ventilation_mode=v.ventilation_mode,
                peak_airway_pressure=float(v.peak_airway_pressure)
                if v.peak_airway_pressure is not None else None,
                cvp_pcwp=float(v.cvp_pcwp) if v.cvp_pcwp is not None else None,
                st_segment=v.st_segment,
                urine_output_ml=v.urine_output_ml,
                blood_loss_ml=v.blood_loss_ml,
                oxygen_fio2=v.oxygen_fio2,
                n2o=v.n2o,
                air=v.air,
                agent=v.agent,
                iv_fluids=v.iv_fluids,
            ))
    return out


@router.post("/anaesthesia-records/{record_id}/vitals",
             response_model=OtAnaesthesiaVitalOut,
             status_code=status.HTTP_201_CREATED)
def create_anaesthesia_vital(
        record_id: int,
        payload: OtAnaesthesiaVitalIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.create"])

    record = db.get(AnaesthesiaRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")
    if not payload.time:
        raise HTTPException(status_code=400,
                            detail="Time is required in HH:MM format")

    case = record.case
    time_dt = _hhmm_to_utc_naive_for_case(case, payload.time)

    bp_systolic = bp_diastolic = None
    if payload.bp:
        try:
            parts = payload.bp.replace(" ", "").replace("\\", "/").split("/")
            if len(parts) >= 2:
                bp_systolic = int(parts[0] or 0) or None
                bp_diastolic = int(parts[1] or 0) or None
        except ValueError:
            bp_systolic = bp_diastolic = None

    vital = AnaesthesiaVitalLog(
        record_id=record_id,
        time=time_dt,
        bp_systolic=bp_systolic,
        bp_diastolic=bp_diastolic,
        pulse=payload.hr,
        spo2=payload.spo2,
        rr=payload.rr,
        etco2=payload.etco2,
        temperature=payload.temp_c,
        comments=payload.comments,
        ventilation_mode=payload.ventilation_mode,
        peak_airway_pressure=payload.peak_airway_pressure,
        cvp_pcwp=payload.cvp_pcwp,
        st_segment=payload.st_segment,
        urine_output_ml=payload.urine_output_ml,
        blood_loss_ml=payload.blood_loss_ml,
        oxygen_fio2=payload.oxygen_fio2,
        n2o=payload.n2o,
        air=payload.air,
        agent=payload.agent,
        iv_fluids=payload.iv_fluids,
    )

    db.add(vital)
    db.commit()
    db.refresh(vital)

    bp_str = f"{vital.bp_systolic}/{vital.bp_diastolic}" if (
        vital.bp_systolic is not None
        and vital.bp_diastolic is not None) else None

    return OtAnaesthesiaVitalOut(
        id=vital.id,
        record_id=vital.record_id,
        time=_to_hhmm(vital.time),
        hr=vital.pulse,
        bp=bp_str,
        spo2=vital.spo2,
        rr=vital.rr,
        temp_c=float(vital.temperature)
        if vital.temperature is not None else None,
        etco2=float(vital.etco2) if vital.etco2 is not None else None,
        comments=vital.comments,
        ventilation_mode=vital.ventilation_mode,
        peak_airway_pressure=float(vital.peak_airway_pressure)
        if vital.peak_airway_pressure is not None else None,
        cvp_pcwp=float(vital.cvp_pcwp) if vital.cvp_pcwp is not None else None,
        st_segment=vital.st_segment,
        urine_output_ml=vital.urine_output_ml,
        blood_loss_ml=vital.blood_loss_ml,
        oxygen_fio2=vital.oxygen_fio2,
        n2o=vital.n2o,
        air=vital.air,
        agent=vital.agent,
        iv_fluids=vital.iv_fluids,
    )


@router.put("/anaesthesia-vitals/{vital_id}",
            response_model=AnaesthesiaVitalLogOut)
def update_anaesthesia_vital(
        vital_id: int,
        payload: AnaesthesiaVitalLogUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.update"])

    vital = db.get(AnaesthesiaVitalLog, vital_id)
    if not vital:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia vital entry not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("record_id", None)

    if "time" in data and data["time"] is not None:
        v = data["time"]
        if isinstance(v, str):
            data["time"] = _hhmm_to_utc_naive_for_case(
                vital.record.case,
                v) if vital.record and vital.record.case else None
        elif isinstance(v, datetime):
            data["time"] = _as_utc(v).replace(tzinfo=None)

    for field, value in data.items():
        setattr(vital, field, value)

    db.add(vital)
    db.commit()
    db.refresh(vital)
    return vital


@router.delete("/anaesthesia-vitals/{vital_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_anaesthesia_vital(
        vital_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.delete"])

    vital = db.get(AnaesthesiaVitalLog, vital_id)
    if not vital:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia vital entry not found")

    db.delete(vital)
    db.commit()
    return None


# ---- Anaesthesia Drug Log ----


@router.get("/anaesthesia-records/{record_id}/drugs",
            response_model=List[OtAnaesthesiaDrugOut])
def list_anaesthesia_drugs(
        record_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user,
              ["ot.anaesthesia_drugs.view", "ot.anaesthesia_record.view"])

    record = db.get(AnaesthesiaRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    out: list[OtAnaesthesiaDrugOut] = []
    for d in record.drugs:
        out.append(
            OtAnaesthesiaDrugOut(
                id=d.id,
                record_id=d.record_id,
                time=_to_hhmm(d.time),
                drug_name=d.drug_name,
                dose=d.dose,
                route=d.route,
                remarks=d.remarks,
            ))
    return out


@router.post("/anaesthesia-records/{record_id}/drugs",
             response_model=OtAnaesthesiaDrugOut,
             status_code=status.HTTP_201_CREATED)
def create_anaesthesia_drug(
        record_id: int,
        payload: OtAnaesthesiaDrugIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.create"])

    record = db.get(AnaesthesiaRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")
    if not payload.time:
        raise HTTPException(status_code=400,
                            detail="Time is required in HH:MM format")

    case = record.case
    time_dt = _hhmm_to_utc_naive_for_case(case, payload.time)

    drug = AnaesthesiaDrugLog(
        record_id=record_id,
        time=time_dt,
        drug_name=payload.drug_name or "",
        dose=payload.dose,
        route=payload.route,
        remarks=payload.remarks,
    )
    db.add(drug)
    db.commit()
    db.refresh(drug)

    return OtAnaesthesiaDrugOut(
        id=drug.id,
        record_id=drug.record_id,
        time=_to_hhmm(drug.time),
        drug_name=drug.drug_name,
        dose=drug.dose,
        route=drug.route,
        remarks=drug.remarks,
    )


@router.put("/anaesthesia-drugs/{drug_id}",
            response_model=AnaesthesiaDrugLogOut)
def update_anaesthesia_drug(
        drug_id: int,
        payload: AnaesthesiaDrugLogUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.update"])

    drug = db.get(AnaesthesiaDrugLog, drug_id)
    if not drug:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia drug entry not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("record_id", None)

    if "time" in data and data["time"] is not None:
        v = data["time"]
        if isinstance(v, str):
            data["time"] = _hhmm_to_utc_naive_for_case(
                drug.record.case,
                v) if drug.record and drug.record.case else None
        elif isinstance(v, datetime):
            data["time"] = _as_utc(v).replace(tzinfo=None)

    for field, value in data.items():
        setattr(drug, field, value)

    db.add(drug)
    db.commit()
    db.refresh(drug)
    return drug


@router.delete("/anaesthesia-drugs/{drug_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_anaesthesia_drug(
        drug_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.delete"])

    drug = db.get(AnaesthesiaDrugLog, drug_id)
    if not drug:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia drug entry not found")

    db.delete(drug)
    db.commit()
    return None


# ============================================================
#  INTRA-OP NURSING RECORD
# ============================================================


@router.get("/cases/{case_id}/nursing-record",
            response_model=Optional[OtNursingRecordOut])
def get_nursing_record_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.nursing_record
    return record or None


@router.post("/cases/{case_id}/nursing-record",
             response_model=OtNursingRecordOut,
             status_code=status.HTTP_201_CREATED)
def create_nursing_record_for_case(
        case_id: int,
        payload: OtNursingRecordCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.create"])

    case = _get_case_or_404(db, case_id)
    if case.nursing_record:
        raise HTTPException(
            status_code=400,
            detail="Nursing record already exists for this case")

    if payload.case_id != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    record = OtNursingRecord(
        case_id=case_id,
        primary_nurse_id=payload.primary_nurse_id or user.id,
        scrub_nurse_name=payload.scrub_nurse_name,
        circulating_nurse_name=payload.circulating_nurse_name,
        positioning=payload.positioning,
        skin_prep=payload.skin_prep,
        catheterisation=payload.catheterisation,
        diathermy_plate_site=payload.diathermy_plate_site,
        counts_initial_done=bool(payload.counts_initial_done),
        counts_closure_done=bool(payload.counts_closure_done),
        antibiotics_time=payload.antibiotics_time,
        warming_measures=payload.warming_measures,
        notes=payload.notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/cases/{case_id}/nursing-record",
            response_model=OtNursingRecordOut)
def update_nursing_record_for_case(
        case_id: int,
        payload: OtNursingRecordUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.update"])

    case = _get_case_or_404(db, case_id)
    record = case.nursing_record
    if not record:
        create_payload = OtNursingRecordCreate(**payload.model_dump(
            exclude_unset=True),
                                               case_id=case_id)  # type: ignore
        return create_nursing_record_for_case(case_id, create_payload, db,
                                              user)

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    if "primary_nurse_id" in data and data["primary_nurse_id"] is None:
        data["primary_nurse_id"] = user.id

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  SPONGE & INSTRUMENT COUNT
# ============================================================


@router.get("/cases/{case_id}/counts", response_model=Optional[OtCountsOut])
def get_counts_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record: Optional[OtSpongeInstrumentCount] = case.counts_record
    if not record:
        return None

    initial = record.initial_count_data or {}
    final = record.final_count_data or {}

    return OtCountsOut(
        id=record.id,
        case_id=case_id,
        sponges_initial=initial.get("sponges_initial"),
        sponges_added=initial.get("sponges_added"),
        sponges_final=final.get("sponges_final"),
        instruments_initial=initial.get("instruments_initial"),
        instruments_final=final.get("instruments_final"),
        needles_initial=initial.get("needles_initial"),
        needles_final=final.get("needles_final"),
        discrepancy_text=record.discrepancy_notes,
        xray_done=bool(initial.get("xray_done") or False),
        resolved_by=initial.get("resolved_by"),
        notes=initial.get("notes"),
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(getattr(record, "updated_at", None)),
    )


@router.post("/cases/{case_id}/counts",
             response_model=OtCountsOut,
             status_code=status.HTTP_201_CREATED)
def create_counts_for_case(
        case_id: int,
        payload: OtCountsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.create"])

    case = _get_case_or_404(db, case_id)
    if case.counts_record:
        raise HTTPException(
            status_code=400,
            detail="Counts record already exists for this case")

    initial_count_data = {
        "sponges_initial": payload.sponges_initial,
        "sponges_added": payload.sponges_added,
        "instruments_initial": payload.instruments_initial,
        "needles_initial": payload.needles_initial,
        "xray_done": payload.xray_done,
        "resolved_by": payload.resolved_by,
        "notes": payload.notes,
    }
    final_count_data = {
        "sponges_final": payload.sponges_final,
        "instruments_final": payload.instruments_final,
        "needles_final": payload.needles_final,
    }

    record = OtSpongeInstrumentCount(
        case_id=case_id,
        initial_count_data=initial_count_data,
        final_count_data=final_count_data,
        discrepancy=bool(payload.discrepancy_text),
        discrepancy_notes=payload.discrepancy_text,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    initial = record.initial_count_data or {}
    final = record.final_count_data or {}

    return OtCountsOut(
        id=record.id,
        case_id=case_id,
        sponges_initial=initial.get("sponges_initial"),
        sponges_added=initial.get("sponges_added"),
        sponges_final=final.get("sponges_final"),
        instruments_initial=initial.get("instruments_initial"),
        instruments_final=final.get("instruments_final"),
        needles_initial=initial.get("needles_initial"),
        needles_final=final.get("needles_final"),
        discrepancy_text=record.discrepancy_notes,
        xray_done=bool(initial.get("xray_done") or False),
        resolved_by=initial.get("resolved_by"),
        notes=initial.get("notes"),
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(getattr(record, "updated_at", None)),
    )


@router.put("/cases/{case_id}/counts", response_model=OtCountsOut)
def update_counts_for_case(
        case_id: int,
        payload: OtCountsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.update"])

    case = _get_case_or_404(db, case_id)
    record: Optional[OtSpongeInstrumentCount] = case.counts_record
    if not record:
        return create_counts_for_case(case_id, payload, db, user)

    # ✅ COPY (avoid in-place mutation on JSON dict)
    initial = dict(record.initial_count_data or {})
    final = dict(record.final_count_data or {})

    initial.update({
        "sponges_initial": payload.sponges_initial,
        "sponges_added": payload.sponges_added,
        "instruments_initial": payload.instruments_initial,
        "needles_initial": payload.needles_initial,
        "xray_done": payload.xray_done,
        "resolved_by": payload.resolved_by,
        "notes": payload.notes,
    })
    final.update({
        "sponges_final": payload.sponges_final,
        "instruments_final": payload.instruments_final,
        "needles_final": payload.needles_final,
    })

    record.initial_count_data = initial
    record.final_count_data = final
    record.discrepancy = bool(payload.discrepancy_text)
    record.discrepancy_notes = payload.discrepancy_text

    # ✅ Force SQLAlchemy to persist JSON changes
    flag_modified(record, "initial_count_data")
    flag_modified(record, "final_count_data")

    db.add(record)
    db.commit()
    db.refresh(record)

    initial = record.initial_count_data or {}
    final = record.final_count_data or {}

    return OtCountsOut(
        id=record.id,
        case_id=case_id,
        sponges_initial=initial.get("sponges_initial"),
        sponges_added=initial.get("sponges_added"),
        sponges_final=final.get("sponges_final"),
        instruments_initial=initial.get("instruments_initial"),
        instruments_final=final.get("instruments_final"),
        needles_initial=initial.get("needles_initial"),
        needles_final=final.get("needles_final"),
        discrepancy_text=record.discrepancy_notes,
        xray_done=bool(initial.get("xray_done") or False),
        resolved_by=initial.get("resolved_by"),
        notes=initial.get("notes"),
        created_at=_to_ist_dt(record.created_at),
        updated_at=_to_ist_dt(getattr(record, "updated_at", None)),
    )


# ============================================================
#  EXTREME: INSTRUMENT COUNT LINES (per case)
# ============================================================


@router.get("/cases/{case_id}/counts/items",
            response_model=List[OtCountItemLineOut])
def list_case_count_items(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.view", "ot.cases.view"])

    _ = _get_case_or_404(db, case_id)

    rows = db.execute(
        select(OtCaseInstrumentCountLine).
        where(OtCaseInstrumentCountLine.case_id == case_id).order_by(
            OtCaseInstrumentCountLine.instrument_name.asc())).scalars().all()

    out: List[OtCountItemLineOut] = []
    for r in rows:
        expected_final = int(r.initial_qty or 0) + int(r.added_qty or 0)
        variance = int(r.final_qty or 0) - expected_final
        out.append(
            OtCountItemLineOut(
                id=r.id,
                case_id=r.case_id,
                instrument_id=r.instrument_id,
                instrument_code=r.instrument_code or "",
                instrument_name=r.instrument_name or "",
                uom=r.uom or "Nos",
                initial_qty=int(r.initial_qty or 0),
                added_qty=int(r.added_qty or 0),
                final_qty=int(r.final_qty or 0),
                expected_final=expected_final,
                variance=variance,
                has_discrepancy=(variance != 0),
                remarks=r.remarks or "",
                updated_at=_to_ist_dt(getattr(r, "updated_at", None)),
            ))
    return out


@router.put("/cases/{case_id}/counts/items",
            response_model=List[OtCountItemLineOut])
def upsert_case_count_items(
        case_id: int,
        payload: OtCountItemsUpsertIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.update", "ot.counts.create"])

    case = _get_case_or_404(db, case_id)

    # Ensure counts_record exists (so your summary can stay in sync)
    record: Optional[OtSpongeInstrumentCount] = case.counts_record
    if not record:
        record = OtSpongeInstrumentCount(
            case_id=case_id,
            initial_count_data={},
            final_count_data={},
            discrepancy=False,
            discrepancy_notes=None,
        )
        db.add(record)
        db.flush()

    # Upsert by (case_id, instrument_id)
    for line in payload.lines:
        instrument_id = line.instrument_id
        if instrument_id:
            m = db.get(OtInstrumentMaster, instrument_id)
            if not m or not bool(getattr(m, "is_active", True)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid instrument_id: {instrument_id}")

            existing = db.execute(
                select(OtCaseInstrumentCountLine).where(
                    OtCaseInstrumentCountLine.case_id == case_id).where(
                        OtCaseInstrumentCountLine.instrument_id ==
                        instrument_id)).scalars().first()

            if existing:
                existing.initial_qty = int(line.initial_qty or 0)
                existing.added_qty = int(line.added_qty or 0)
                existing.final_qty = int(line.final_qty or 0)
                existing.remarks = (line.remarks or "")[:500]
                existing.updated_by = user.id
                db.add(existing)
            else:
                row = OtCaseInstrumentCountLine(
                    case_id=case_id,
                    instrument_id=instrument_id,
                    instrument_code=(m.code or "")[:40],
                    instrument_name=(m.name or "")[:200],
                    uom=(m.uom or "Nos")[:30],
                    initial_qty=int(line.initial_qty or 0),
                    added_qty=int(line.added_qty or 0),
                    final_qty=int(line.final_qty or 0),
                    remarks=(line.remarks or "")[:500],
                    created_by=user.id,
                    updated_by=user.id,
                )
                db.add(row)

        # If you want to allow “free text instruments” later, handle instrument_id=None here.

    db.commit()

    # Sync summary instrument totals into your existing JSON fields
    # Sync summary instrument totals into your existing JSON fields
    rows = db.execute(
        select(OtCaseInstrumentCountLine).where(
            OtCaseInstrumentCountLine.case_id == case_id)).scalars().all()

    inst_initial = sum(int(r.initial_qty or 0) for r in rows)
    inst_final = sum(int(r.final_qty or 0) for r in rows)

    # ✅ COPY (avoid in-place mutation on JSON dict)
    initial = dict(record.initial_count_data or {})
    final = dict(record.final_count_data or {})

    initial["instruments_initial"] = inst_initial
    final["instruments_final"] = inst_final

    record.initial_count_data = initial
    record.final_count_data = final

    # Auto discrepancy flag if any line mismatch
    any_mismatch = any((int(r.final_qty or 0) -
                        (int(r.initial_qty or 0) + int(r.added_qty or 0))) != 0
                       for r in rows)
    record.discrepancy = bool(record.discrepancy_notes) or any_mismatch

    # ✅ Force SQLAlchemy to persist JSON changes
    flag_modified(record, "initial_count_data")
    flag_modified(record, "final_count_data")

    db.add(record)
    db.commit()
    db.refresh(record)

    # Return fresh list
    return list_case_count_items(case_id, db, user)


@router.delete("/cases/{case_id}/counts/items/{line_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_case_count_item(
        case_id: int,
        line_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.counts.update", "ot.counts.create"])

    _ = _get_case_or_404(db, case_id)

    row = db.get(OtCaseInstrumentCountLine, line_id)
    if not row or row.case_id != case_id:
        raise HTTPException(status_code=404, detail="Count line not found")

    db.delete(row)
    db.commit()
    return


# ============================================================
#  IMPLANT / PROSTHESIS RECORDS
# ============================================================


@router.get("/cases/{case_id}/implants",
            response_model=List[OtImplantRecordOut])
def list_implants_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.implants.view", "ot.cases.view"])
    case = _get_case_or_404(db, case_id)
    return case.implant_records


@router.post("/cases/{case_id}/implants",
             response_model=OtImplantRecordOut,
             status_code=status.HTTP_201_CREATED)
def create_implant_for_case(
        case_id: int,
        payload: OtImplantRecordCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.implants.create"])
    _get_case_or_404(db, case_id)

    if payload.case_id != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    record = OtImplantRecord(
        case_id=case_id,
        implant_name=payload.implant_name,
        size=payload.size,
        batch_no=payload.batch_no,
        lot_no=payload.lot_no,
        manufacturer=payload.manufacturer,
        expiry_date=payload.expiry_date,
        inventory_item_id=payload.inventory_item_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/implants/{implant_id}", response_model=OtImplantRecordOut)
def update_implant(
        implant_id: int,
        payload: OtImplantRecordUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.implants.update"])

    record = db.get(OtImplantRecord, implant_id)
    if not record:
        raise HTTPException(status_code=404, detail="Implant record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)
    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/implants/{implant_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_implant(
        implant_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.implants.delete"])

    record = db.get(OtImplantRecord, implant_id)
    if not record:
        raise HTTPException(status_code=404, detail="Implant record not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  OPERATION NOTE (SURGEON)
# ============================================================


@router.get("/cases/{case_id}/operation-note",
            response_model=Optional[OperationNoteOut])
def get_operation_note_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.operation_note
    return record or None


@router.post("/cases/{case_id}/operation-note",
             response_model=OperationNoteOut,
             status_code=status.HTTP_201_CREATED)
def create_operation_note_for_case(
        case_id: int,
        payload: OperationNoteCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.create"])

    case = _get_case_or_404(db, case_id)
    if case.operation_note:
        raise HTTPException(
            status_code=400,
            detail="Operation note already exists for this case")

    if payload.case_id != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    surgeon_id = payload.surgeon_user_id or user.id

    record = OperationNote(
        case_id=case_id,
        surgeon_user_id=surgeon_id,
        preop_diagnosis=payload.preop_diagnosis,
        postop_diagnosis=payload.postop_diagnosis,
        indication=payload.indication,
        findings=payload.findings,
        procedure_steps=payload.procedure_steps,
        blood_loss_ml=payload.blood_loss_ml,
        complications=payload.complications,
        drains_details=payload.drains_details,
        postop_instructions=payload.postop_instructions,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/cases/{case_id}/operation-note", response_model=OperationNoteOut)
def update_operation_note_for_case(
        case_id: int,
        payload: OperationNoteUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.update"])

    case = _get_case_or_404(db, case_id)
    record = case.operation_note
    if not record:
        create_payload = OperationNoteCreate(**payload.model_dump(
            exclude_unset=True),
                                             case_id=case_id)  # type: ignore
        return create_operation_note_for_case(case_id, create_payload, db,
                                              user)

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    if "surgeon_user_id" in data and data["surgeon_user_id"] is None:
        data["surgeon_user_id"] = user.id

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  BLOOD / BLOOD COMPONENT TRANSFUSION
# ============================================================


@router.get("/cases/{case_id}/blood-transfusions",
            response_model=List[OtBloodTransfusionRecordOut])
def list_blood_transfusions_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.view", "ot.cases.view"])
    case = _get_case_or_404(db, case_id)
    return case.blood_records


@router.post("/cases/{case_id}/blood-transfusions",
             response_model=OtBloodTransfusionRecordOut,
             status_code=status.HTTP_201_CREATED)
def create_blood_transfusion_for_case(
        case_id: int,
        payload: OtBloodTransfusionRecordCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.create"])
    _get_case_or_404(db, case_id)

    if payload.case_id != case_id:
        raise HTTPException(status_code=400,
                            detail="case_id in body does not match URL")

    record = OtBloodTransfusionRecord(
        case_id=case_id,
        component=payload.component,
        units=payload.units,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reaction=payload.reaction,
        notes=payload.notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put("/blood-transfusions/{record_id}",
            response_model=OtBloodTransfusionRecordOut)
def update_blood_transfusion(
        record_id: int,
        payload: OtBloodTransfusionRecordUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.update"])

    record = db.get(OtBloodTransfusionRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Blood transfusion record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)
    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/blood-transfusions/{record_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_blood_transfusion(
        record_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.delete"])

    record = db.get(OtBloodTransfusionRecord, record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Blood transfusion record not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  PACU / POST-ANAESTHESIA RECOVERY RECORD
# ============================================================


@router.get("/cases/{case_id}/pacu", response_model=Optional[PacuUiOut])
def get_pacu_record(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pacu.view", "ot.cases.view"])
    case = _get_case_or_404(db, case_id)

    rec: Optional[PacuRecord] = case.pacu_record
    return rec or None


@router.post("/cases/{case_id}/pacu",
             response_model=PacuUiOut,
             status_code=status.HTTP_201_CREATED)
def create_pacu_record(
        case_id: int,
        payload: PacuUiIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pacu.create"])

    case = _get_case_or_404(db, case_id)
    if case.pacu_record:
        raise HTTPException(status_code=400,
                            detail="PACU record already exists for this case")

    data = payload.model_dump(exclude_unset=True)

    rec = PacuRecord(
        case_id=case_id,
        nurse_user_id=user.id,
        **data,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.put("/cases/{case_id}/pacu", response_model=PacuUiOut)
def update_pacu_record(
        case_id: int,
        payload: PacuUiIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pacu.update"])

    case = _get_case_or_404(db, case_id)
    rec: Optional[PacuRecord] = case.pacu_record

    data = payload.model_dump(exclude_unset=True)

    if not rec:
        rec = PacuRecord(case_id=case_id, nurse_user_id=user.id, **data)
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec

    for k, v in data.items():
        setattr(rec, k, v)

    # keep nurse as last editor (optional)
    rec.nurse_user_id = user.id

    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _load_branding_for_pdf(db: Session, user: User):
    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)

    q = db.query(UiBranding)

    # tenant scope
    if tenant_id is not None and hasattr(UiBranding, "tenant_id"):
        q = q.filter(UiBranding.tenant_id == tenant_id)

    # active flag (optional)
    if hasattr(UiBranding, "is_active"):
        q = q.filter(UiBranding.is_active == True)  # noqa: E712

    return q.order_by(desc(UiBranding.id)).first()


@router.get("/cases/{case_id}/pacu/pdf")
def get_pacu_pdf(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.pacu.view", "ot.cases.view"])

    case = _case_for_pacu_pdf(db, case_id)

    pacu_record = getattr(case, "pacu_record", None) or getattr(
        case, "pacu", None)
    anaesthesia_record = getattr(case, "anaesthesia_record", None) or getattr(
        case, "anaesthesia", None)

    if not pacu_record:
        raise HTTPException(status_code=404,
                            detail="PACU record not found for this case")

    # ✅ Use your working patient builder (same as other OT PDFs)
    patient_fields = _build_patient_fields_for_case(db, case)

    # ✅ Branding fix (NO MORE branding=None)
    branding = _load_branding_for_pdf(db, user)

    print(
        "PACU PDF patient_fields:", {
            k: patient_fields.get(k)
            for k in [
                "name", "uhid", "age_sex", "case_no", "or_no", "date",
                "proposed_operation"
            ]
        })
    print(
        "PACU PDF branding:", {
            "org_name":
            getattr(branding, "org_name", None) if branding else None,
            "logo_path":
            getattr(branding, "logo_path", None) if branding else None,
        })

    pdf_bytes = build_ot_pacu_record_pdf_bytes(
        branding=branding,
        case=case,
        patient_fields=patient_fields,
        anaesthesia_record=anaesthesia_record,
        pacu_record=pacu_record,
    )

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="PACU_{case_id}.pdf"'
        },
    )


# ============================================================
#  OT ADMIN / CLEANING LOG
# ============================================================


@router.get("/cleaning-logs", response_model=List[OtCleaningLogOut])
def list_cleaning_logs(
        theatre_id: Optional[int] = Query(None),
        case_id: Optional[int] = Query(None),
        date: Optional[date] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
        session: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.view", "ot.logs.view", "ot.cases.view"])

    q = db.query(OtCleaningLog)

    if theatre_id is not None:
        q = q.filter(OtCleaningLog.theatre_id == theatre_id)
    if case_id is not None:
        q = q.filter(OtCleaningLog.case_id == case_id)
    if date is not None:
        q = q.filter(OtCleaningLog.date == date)
    if from_date is not None:
        q = q.filter(OtCleaningLog.date >= from_date)
    if to_date is not None:
        q = q.filter(OtCleaningLog.date <= to_date)
    if session is not None:
        q = q.filter(OtCleaningLog.session == session)

    q = q.order_by(
        OtCleaningLog.date.desc(),
        OtCleaningLog.created_at.desc(),
        OtCleaningLog.id.desc(),
    )
    return q.all()


@router.get("/cleaning-logs/{log_id}", response_model=OtCleaningLogOut)
def get_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.view", "ot.logs.view"])

    log = db.get(OtCleaningLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")
    return log


@router.post("/cleaning-logs",
             response_model=OtCleaningLogOut,
             status_code=status.HTTP_201_CREATED)
def create_cleaning_log(
        payload: OtCleaningLogCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.create", "ot.logs.manage"])

    log = OtCleaningLog(
        theatre_id=payload.theatre_id,
        date=payload.date,
        session=payload.session,
        case_id=payload.case_id,
        method=payload.method,
        done_by_user_id=payload.done_by_user_id,
        remarks=payload.remarks,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.put("/cleaning-logs/{log_id}", response_model=OtCleaningLogOut)
def update_cleaning_log(
        log_id: int,
        payload: OtCleaningLogUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.update", "ot.logs.manage"])

    log = db.get(OtCleaningLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(log, field, value)

    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.delete("/cleaning-logs/{log_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.delete", "ot.logs.manage"])

    log = db.get(OtCleaningLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    db.delete(log)
    db.commit()
    return None


# ============================================================
#  OT ADMIN / ENVIRONMENT LOG
# ============================================================


@router.get("/environment-logs", response_model=List[OtEnvironmentLogOut])
def list_environment_logs(
        theatre_id: Optional[int] = Query(None),
        date: Optional[date] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.view", "ot.logs.view"])

    q = db.query(OtEnvironmentLog)

    if theatre_id is not None:
        q = q.filter(OtEnvironmentLog.theatre_id == theatre_id)
    if date is not None:
        q = q.filter(OtEnvironmentLog.date == date)
    if from_date is not None:
        q = q.filter(OtEnvironmentLog.date >= from_date)
    if to_date is not None:
        q = q.filter(OtEnvironmentLog.date <= to_date)

    q = q.order_by(
        OtEnvironmentLog.date.desc(),
        OtEnvironmentLog.time.desc(),
        OtEnvironmentLog.created_at.desc(),
    )
    return q.all()


@router.get("/environment-logs/{log_id}", response_model=OtEnvironmentLogOut)
def get_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.view", "ot.logs.view"])

    log = db.get(OtEnvironmentLog, log_id)
    if not log:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")
    return log


@router.post("/environment-logs",
             response_model=OtEnvironmentLogOut,
             status_code=status.HTTP_201_CREATED)
def create_environment_log(
        payload: OtEnvironmentLogCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.create", "ot.logs.manage"])

    log = OtEnvironmentLog(
        theatre_id=payload.theatre_id,
        date=payload.date,
        time=payload.time,
        temperature_c=payload.temperature_c,
        humidity_percent=payload.humidity_percent,
        pressure_diff_pa=payload.pressure_diff_pa,
        logged_by_user_id=payload.logged_by_user_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.put("/environment-logs/{log_id}", response_model=OtEnvironmentLogOut)
def update_environment_log(
        log_id: int,
        payload: OtEnvironmentLogUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.update", "ot.logs.manage"])

    log = db.get(OtEnvironmentLog, log_id)
    if not log:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(log, field, value)

    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.delete("/environment-logs/{log_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.delete", "ot.logs.manage"])

    log = db.get(OtEnvironmentLog, log_id)
    if not log:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")

    db.delete(log)
    db.commit()
    return None
