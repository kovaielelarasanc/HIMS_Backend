# FILE: app/api/routes_ot_clinical.py
from __future__ import annotations

from typing import List, Optional
from datetime import datetime, date, time
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.ot import (
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
    OtCase,
)
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
    OtPreopChecklistIn,
    OtSafetyChecklistIn,
    OtSafetyChecklistOut,
    OtSafetyPhaseSignIn,
    OtSafetyPhaseTimeOut,
    OtSafetyPhaseSignOut,
    # Anaesthesia record
    AnaesthesiaVitalLogCreate,
    AnaesthesiaVitalLogUpdate,
    AnaesthesiaVitalLogOut,
    AnaesthesiaDrugLogCreate,
    AnaesthesiaDrugLogUpdate,
    AnaesthesiaDrugLogOut,
    OtAnaesthesiaRecordIn,
    OtAnaesthesiaRecordOut,
    OtAnaesthesiaVitalIn,
    OtAnaesthesiaVitalOut,
    OtAnaesthesiaDrugIn,
    OtAnaesthesiaDrugOut,
    # Nursing record
    OtNursingRecordCreate,
    OtNursingRecordUpdate,
    OtNursingRecordOut,
    # Sponge & instrument
    OtSpongeInstrumentCountCreate,
    OtSpongeInstrumentCountUpdate,
    OtSpongeInstrumentCountOut,
    OtCountsIn,
    OtCountsOut,
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
    OtCleaningLogCreate,
    OtCleaningLogUpdate,
    OtCleaningLogOut,
    OtEnvironmentLogCreate,
    OtEnvironmentLogUpdate,
    OtEnvironmentLogOut,
)
from app.models.user import User

router = APIRouter(prefix="/ot", tags=["OT - Clinical Records"])


# ============================================================
#  HELPER
# ============================================================
# ---------------- RBAC ----------------
def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


def _get_case_or_404(db: Session, case_id: int):
    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OT Case not found",
        )
    return case


def _time_str_to_dt(value: Optional[str]) -> Optional[datetime]:
    """
    Convert 'HH:MM' string from UI into a datetime for DB.
    Uses today's date for now.
    """
    if not value:
        return None
    try:
        h, m = map(int, value.split(':')[:2])
    except ValueError:
        return None
    today = datetime.utcnow().date()
    return datetime.combine(today, time(hour=h, minute=m))


def _dt_to_time_str(value: Optional[datetime]) -> Optional[str]:
    """Convert datetime from DB into 'HH:MM' string for UI."""
    if not value:
        return None
    return value.strftime("%H:%M")


# ============================================================
#  PRE-ANAESTHESIA EVALUATION
# ============================================================


@router.get(
    "/cases/{case_id}/pre-anaesthesia",
    response_model=PreAnaesthesiaEvaluationOut,
)
def get_pre_anaesthesia_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.preanaesthesia
    if not record:
        raise HTTPException(status_code=404,
                            detail="Pre-anaesthesia record not found")
    return record


@router.post(
    "/cases/{case_id}/pre-anaesthesia",
    response_model=PreAnaesthesiaEvaluationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pre_anaesthesia_for_case(
        case_id: int,
        payload: PreAnaesthesiaEvaluationCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.create"])

    case = _get_case_or_404(case_id, db)
    if case.preanaesthesia:
        raise HTTPException(
            status_code=400,
            detail="Pre-anaesthesia record already exists for this case",
        )

    if payload.case_id != case_id:
        # enforce consistency
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.put(
    "/cases/{case_id}/pre-anaesthesia",
    response_model=PreAnaesthesiaEvaluationOut,
)
def update_pre_anaesthesia_for_case(
        case_id: int,
        payload: PreAnaesthesiaEvaluationUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pre_anaesthesia.update"])

    case = _get_case_or_404(case_id, db)
    record = case.preanaesthesia
    if not record:
        raise HTTPException(status_code=404,
                            detail="Pre-anaesthesia record not found")

    data = payload.model_dump(exclude_unset=True)
    # Never allow case_id change via this endpoint
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  SURGICAL SAFETY CHECKLIST (WHO)
# ============================================================
def _build_anaes_json_from_payload(payload: OtAnaesthesiaRecordIn) -> dict:
    """
    Flatten OtAnaesthesiaRecordIn into a JSON dict that we store inside
    AnaesthesiaRecord.preop_vitals. This is flexible and backwards compatible.
    """
    return {
        # ---- PRE-OP ----
        "asa_grade": payload.asa_grade,
        "airway_assessment": payload.airway_assessment,
        "comorbidities": payload.comorbidities,
        "allergies": payload.allergies,
        "preop_pulse": payload.preop_pulse,
        "preop_bp": payload.preop_bp,
        "preop_rr": payload.preop_rr,
        "preop_temp_c": payload.preop_temp_c,
        "preop_cvs": payload.preop_cvs,
        "preop_rs": payload.preop_rs,
        "preop_cns": payload.preop_cns,
        "preop_pa": payload.preop_pa,
        "preop_veins": payload.preop_veins,
        "preop_spine": payload.preop_spine,
        "airway_teeth_status": payload.airway_teeth_status,
        "airway_denture": payload.airway_denture,
        "airway_neck_movements": payload.airway_neck_movements,
        "airway_mallampati_class": payload.airway_mallampati_class,
        "difficult_airway_anticipated": payload.difficult_airway_anticipated,
        "risk_factors": payload.risk_factors,
        "anaesthetic_plan_detail": payload.anaesthetic_plan_detail,
        "preop_instructions": payload.preop_instructions,

        # ---- INTRA-OP SETTINGS ----
        "preoxygenation": payload.preoxygenation,
        "cricoid_pressure": payload.cricoid_pressure,
        "induction_route": payload.induction_route,
        "intubation_done": payload.intubation_done,
        "intubation_route": payload.intubation_route,
        "intubation_state": payload.intubation_state,
        "intubation_technique": payload.intubation_technique,
        "tube_type": payload.tube_type,
        "tube_size": payload.tube_size,
        "tube_fixed_at": payload.tube_fixed_at,
        "cuff_used": payload.cuff_used,
        "cuff_medium": payload.cuff_medium,
        "bilateral_breath_sounds": payload.bilateral_breath_sounds,
        "added_sounds": payload.added_sounds,
        "laryngoscopy_grade": payload.laryngoscopy_grade,
        "airway_devices": payload.airway_devices,
        "ventilation_mode_baseline": payload.ventilation_mode_baseline,
        "ventilator_vt": payload.ventilator_vt,
        "ventilator_rate": payload.ventilator_rate,
        "ventilator_peep": payload.ventilator_peep,
        "breathing_system": payload.breathing_system,
        "monitors": payload.monitors,
        "lines": payload.lines,
        "tourniquet_used": payload.tourniquet_used,
        "patient_position": payload.patient_position,
        "eyes_taped": payload.eyes_taped,
        "eyes_covered_with_foil": payload.eyes_covered_with_foil,
        "pressure_points_padded": payload.pressure_points_padded,
        "iv_fluids_plan": payload.iv_fluids_plan,
        "blood_components_plan": payload.blood_components_plan,
        "regional_block_type": payload.regional_block_type,
        "regional_position": payload.regional_position,
        "regional_approach": payload.regional_approach,
        "regional_space_depth": payload.regional_space_depth,
        "regional_needle_type": payload.regional_needle_type,
        "regional_drug_dose": payload.regional_drug_dose,
        "regional_level": payload.regional_level,
        "regional_complications": payload.regional_complications,
        "block_adequacy": payload.block_adequacy,
        "sedation_needed": payload.sedation_needed,
        "conversion_to_ga": payload.conversion_to_ga,
    }


def _parse_time_str_for_case(case: OtCase,
                             t: Optional[str]) -> Optional[datetime]:
    """
    Convert 'HH:MM' string from UI into a datetime using case.schedule.date.
    Falls back to today's date if missing.
    """
    if not t:
        return None
    try:
        hour, minute = [int(x) for x in t.split(":", 1)]
    except (ValueError, TypeError):
        return None

    if case.schedule and case.schedule.date:
        base_date = case.schedule.date
    else:
        base_date = datetime.utcnow().date()

    return datetime.combine(base_date, time(hour=hour, minute=minute))


def _to_hhmm(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%H:%M")


@router.get(
    "/cases/{case_id}/safety-checklist",
    response_model=OtSafetyChecklistOut,
)
def get_safety_checklist_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.safety.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.safety_checklist
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Safety checklist not found",
        )

    sign_in_data = record.sign_in_data or {}
    time_out_data = record.time_out_data or {}
    sign_out_data = record.sign_out_data or {}

    # derive "done" flags
    sign_in_done = bool(sign_in_data.get("done") or record.sign_in_done_by_id)
    time_out_done = bool(
        time_out_data.get("done") or record.time_out_done_by_id)
    sign_out_done = bool(
        sign_out_data.get("done") or record.sign_out_done_by_id)

    # hydrate phase models from JSON (missing keys → defaults)
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
        record.sign_in_time,
        record.time_out_time,
        record.sign_out_time,
        record.created_at,
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
        created_at=record.created_at,
        updated_at=updated_at,
    )


@router.post(
    "/cases/{case_id}/safety-checklist",
    response_model=OtSafetyChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
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
            detail="Safety checklist already exists for this case",
        )

    sign_in_time = _parse_time_str_for_case(case, payload.sign_in_time)
    time_out_time = _parse_time_str_for_case(case, payload.time_out_time)
    sign_out_time = _parse_time_str_for_case(case, payload.sign_out_time)

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
        record.sign_in_time,
        record.time_out_time,
        record.sign_out_time,
        record.created_at,
    ]
    updated_at = max([d for d in updated_candidates if d is not None])

    return OtSafetyChecklistOut(
        case_id=case_id,
        sign_in_done=payload.sign_in_done,
        sign_in_time=payload.sign_in_time,
        time_out_done=payload.time_out_done,
        time_out_time=payload.time_out_time,
        sign_out_done=payload.sign_out_done,
        sign_out_time=payload.sign_out_time,
        sign_in=payload.sign_in,
        time_out=payload.time_out,
        sign_out=payload.sign_out,
        created_at=record.created_at,
        updated_at=updated_at,
    )


@router.put(
    "/cases/{case_id}/safety-checklist",
    response_model=OtSafetyChecklistOut,
)
def update_safety_checklist_for_case(
        case_id: int,
        payload: OtSafetyChecklistIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.safety.manage"])

    case = _get_case_or_404(db, case_id)
    record = case.safety_checklist

    # If no record yet, behave like create()
    if not record:
        return create_safety_checklist_for_case(
            case_id=case_id,
            payload=payload,
            db=db,
            user=user,
        )

    sign_in_time = _parse_time_str_for_case(case, payload.sign_in_time)
    time_out_time = _parse_time_str_for_case(case, payload.time_out_time)
    sign_out_time = _parse_time_str_for_case(case, payload.sign_out_time)

    record.sign_in_data = payload.sign_in.model_dump()
    record.sign_in_data["done"] = bool(payload.sign_in_done)
    record.sign_in_done_by_id = user.id if payload.sign_in_done else None
    record.sign_in_time = sign_in_time

    record.time_out_data = payload.time_out.model_dump()
    record.time_out_data["done"] = bool(payload.time_out_done)
    record.time_out_done_by_id = user.id if payload.time_out_done else None
    record.time_out_time = time_out_time

    record.sign_out_data = payload.sign_out.model_dump()
    record.sign_out_data["done"] = bool(payload.sign_out_done)
    record.sign_out_done_by_id = user.id if payload.sign_out_done else None
    record.sign_out_time = sign_out_time

    db.add(record)
    db.commit()
    db.refresh(record)

    updated_candidates = [
        record.sign_in_time,
        record.time_out_time,
        record.sign_out_time,
        record.created_at,
    ]
    updated_at = max([d for d in updated_candidates if d is not None])

    return OtSafetyChecklistOut(
        case_id=case_id,
        sign_in_done=payload.sign_in_done,
        sign_in_time=payload.sign_in_time,
        time_out_done=payload.time_out_done,
        time_out_time=payload.time_out_time,
        sign_out_done=payload.sign_out_done,
        sign_out_time=payload.sign_out_time,
        sign_in=payload.sign_in,
        time_out=payload.time_out,
        sign_out=payload.sign_out,
        created_at=record.created_at,
        updated_at=updated_at,
    )


# ============================================================
#  ANAESTHESIA RECORD + VITALS + DRUG LOGS
# ============================================================


@router.get(
    "/cases/{case_id}/anaesthesia-record",
    response_model=OtAnaesthesiaRecordOut,
)
def get_anaesthesia_record_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.anaesthesia_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    data = record.preop_vitals or {}

    return OtAnaesthesiaRecordOut(
        id=record.id,
        case_id=case_id,
        anaesthetist_user_id=record.anaesthetist_user_id,
        created_at=record.created_at,
        updated_at=None,

        # header
        anaesthesia_type=record.plan or None,
        notes=record.intraop_summary or None,

        # ---- PRE-OP ----
        asa_grade=data.get("asa_grade"),
        airway_assessment=data.get("airway_assessment"),
        comorbidities=data.get("comorbidities"),
        allergies=data.get("allergies"),
        preop_pulse=data.get("preop_pulse"),
        preop_bp=data.get("preop_bp"),
        preop_rr=data.get("preop_rr"),
        preop_temp_c=data.get("preop_temp_c"),
        preop_cvs=data.get("preop_cvs"),
        preop_rs=data.get("preop_rs"),
        preop_cns=data.get("preop_cns"),
        preop_pa=data.get("preop_pa"),
        preop_veins=data.get("preop_veins"),
        preop_spine=data.get("preop_spine"),
        airway_teeth_status=data.get("airway_teeth_status"),
        airway_denture=data.get("airway_denture"),
        airway_neck_movements=data.get("airway_neck_movements"),
        airway_mallampati_class=data.get("airway_mallampati_class"),
        difficult_airway_anticipated=data.get("difficult_airway_anticipated"),
        risk_factors=data.get("risk_factors"),
        anaesthetic_plan_detail=data.get("anaesthetic_plan_detail"),
        preop_instructions=data.get("preop_instructions"),

        # ---- INTRA-OP ----
        preoxygenation=data.get("preoxygenation"),
        cricoid_pressure=data.get("cricoid_pressure"),
        induction_route=data.get("induction_route"),
        intubation_done=data.get("intubation_done"),
        intubation_route=data.get("intubation_route"),
        intubation_state=data.get("intubation_state"),
        intubation_technique=data.get("intubation_technique"),
        tube_type=data.get("tube_type"),
        tube_size=data.get("tube_size"),
        tube_fixed_at=data.get("tube_fixed_at"),
        cuff_used=data.get("cuff_used"),
        cuff_medium=data.get("cuff_medium"),
        bilateral_breath_sounds=data.get("bilateral_breath_sounds"),
        added_sounds=data.get("added_sounds"),
        laryngoscopy_grade=data.get("laryngoscopy_grade"),
        airway_devices=data.get("airway_devices"),
        ventilation_mode_baseline=data.get("ventilation_mode_baseline"),
        ventilator_vt=data.get("ventilator_vt"),
        ventilator_rate=data.get("ventilator_rate"),
        ventilator_peep=data.get("ventilator_peep"),
        breathing_system=data.get("breathing_system"),
        monitors=data.get("monitors"),
        lines=data.get("lines"),
        tourniquet_used=data.get("tourniquet_used"),
        patient_position=data.get("patient_position"),
        eyes_taped=data.get("eyes_taped"),
        eyes_covered_with_foil=data.get("eyes_covered_with_foil"),
        pressure_points_padded=data.get("pressure_points_padded"),
        iv_fluids_plan=data.get("iv_fluids_plan"),
        blood_components_plan=data.get("blood_components_plan"),
        regional_block_type=data.get("regional_block_type"),
        regional_position=data.get("regional_position"),
        regional_approach=data.get("regional_approach"),
        regional_space_depth=data.get("regional_space_depth"),
        regional_needle_type=data.get("regional_needle_type"),
        regional_drug_dose=data.get("regional_drug_dose"),
        regional_level=data.get("regional_level"),
        regional_complications=data.get("regional_complications"),
        block_adequacy=data.get("block_adequacy"),
        sedation_needed=data.get("sedation_needed"),
        conversion_to_ga=data.get("conversion_to_ga"),
    )


@router.post(
    "/cases/{case_id}/anaesthesia-record",
    response_model=OtAnaesthesiaRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_record_for_case(
        case_id: int,
        payload: OtAnaesthesiaRecordIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.create"])

    case = _get_case_or_404(db, case_id)
    if case.anaesthesia_record:
        raise HTTPException(
            status_code=400,
            detail="Anaesthesia record already exists for this case",
        )

    preop_vitals = _build_anaes_json_from_payload(payload)

    record = AnaesthesiaRecord(
        case_id=case_id,
        anaesthetist_user_id=user.id,
        preop_vitals=preop_vitals,
        plan=payload.anaesthesia_type,
        intraop_summary=payload.notes,
        airway_plan=None,
        complications=None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return get_anaesthesia_record_for_case(case_id, db, user)


@router.put(
    "/cases/{case_id}/anaesthesia-record",
    response_model=OtAnaesthesiaRecordOut,
)
def update_anaesthesia_record_for_case(
        case_id: int,
        payload: OtAnaesthesiaRecordIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.update"])

    case = _get_case_or_404(db, case_id)
    record = case.anaesthesia_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    preop_vitals = _build_anaes_json_from_payload(payload)
    record.preop_vitals = preop_vitals
    record.plan = payload.anaesthesia_type
    record.intraop_summary = payload.notes

    db.add(record)
    db.commit()
    db.refresh(record)

    return get_anaesthesia_record_for_case(case_id, db, user)


# ---- Anaesthesia Vitals ----


@router.get(
    "/anaesthesia-records/{record_id}/vitals",
    response_model=List[OtAnaesthesiaVitalOut],
)
def list_anaesthesia_vitals(
        record_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user,
              ["ot.anaesthesia_vitals.view", "ot.anaesthesia_record.view"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    out: list[OtAnaesthesiaVitalOut] = []
    for v in record.vitals:
        time_str = _to_hhmm(v.time) if v.time else None

        if v.bp_systolic is not None and v.bp_diastolic is not None:
            bp_str = f"{v.bp_systolic}/{v.bp_diastolic}"
        else:
            bp_str = None

        out.append(
            OtAnaesthesiaVitalOut(
                id=v.id,
                record_id=v.record_id,
                time=time_str,
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
            ))
    return out


@router.post(
    "/anaesthesia-records/{record_id}/vitals",
    response_model=OtAnaesthesiaVitalOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_vital(
        record_id: int,
        payload: OtAnaesthesiaVitalIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.create"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    if not payload.time:
        raise HTTPException(
            status_code=400,
            detail="Time is required in HH:MM format",
        )

    case = record.case
    time_dt = _parse_time_str_for_case(case, payload.time)

    # BP parsing "120/80"
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
    )

    db.add(vital)
    db.commit()
    db.refresh(vital)

    bp_str = (f"{vital.bp_systolic}/{vital.bp_diastolic}"
              if vital.bp_systolic is not None
              and vital.bp_diastolic is not None else None)

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
    )


@router.put(
    "/anaesthesia-vitals/{vital_id}",
    response_model=AnaesthesiaVitalLogOut,
)
def update_anaesthesia_vital(
        vital_id: int,
        payload: AnaesthesiaVitalLogUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.update"])

    vital = db.query(AnaesthesiaVitalLog).get(vital_id)
    if not vital:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia vital entry not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("record_id", None)

    for field, value in data.items():
        setattr(vital, field, value)

    db.add(vital)
    db.commit()
    db.refresh(vital)
    return vital


@router.delete(
    "/anaesthesia-vitals/{vital_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_anaesthesia_vital(
        vital_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.delete"])

    vital = db.query(AnaesthesiaVitalLog).get(vital_id)
    if not vital:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia vital entry not found")

    db.delete(vital)
    db.commit()
    return None


# ---- Anaesthesia Drug Log ----


@router.get(
    "/anaesthesia-records/{record_id}/drugs",
    response_model=List[OtAnaesthesiaDrugOut],
)
def list_anaesthesia_drugs(
        record_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user,
              ["ot.anaesthesia_drugs.view", "ot.anaesthesia_record.view"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    out: list[OtAnaesthesiaDrugOut] = []
    for d in record.drugs:
        out.append(
            OtAnaesthesiaDrugOut(
                id=d.id,
                record_id=d.record_id,
                time=_to_hhmm(d.time) if d.time else None,
                drug_name=d.drug_name,
                dose=d.dose,
                route=d.route,
                remarks=d.remarks,
            ))
    return out


@router.post(
    "/anaesthesia-records/{record_id}/drugs",
    response_model=OtAnaesthesiaDrugOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_drug(
        record_id: int,
        payload: OtAnaesthesiaDrugIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.create"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    if not payload.time:
        raise HTTPException(
            status_code=400,
            detail="Time is required in HH:MM format",
        )

    case = record.case
    time_dt = _parse_time_str_for_case(case, payload.time)

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


@router.put(
    "/anaesthesia-drugs/{drug_id}",
    response_model=AnaesthesiaDrugLogOut,
)
def update_anaesthesia_drug(
        drug_id: int,
        payload: AnaesthesiaDrugLogUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.update"])

    drug = db.query(AnaesthesiaDrugLog).get(drug_id)
    if not drug:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia drug entry not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("record_id", None)

    for field, value in data.items():
        setattr(drug, field, value)

    db.add(drug)
    db.commit()
    db.refresh(drug)
    return drug


@router.delete(
    "/anaesthesia-drugs/{drug_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_anaesthesia_drug(
        drug_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.delete"])

    drug = db.query(AnaesthesiaDrugLog).get(drug_id)
    if not drug:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia drug entry not found")

    db.delete(drug)
    db.commit()
    return None


# ============================================================
#  INTRA-OP NURSING RECORD
# ============================================================

# FILE: app/api/routes_ot_clinical.py


@router.get(
    "/cases/{case_id}/nursing-record",
    response_model=OtNursingRecordOut,
)
def get_nursing_record_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.view", "ot.cases.view"])

    # ✅ IMPORTANT: pass (db, case_id) – new helper signature
    case = _get_case_or_404(db, case_id)

    record = case.nursing_record
    if not record:
        # Frontend NursingTab already handles 404 and shows empty form
        raise HTTPException(status_code=404, detail="Nursing record not found")

    return record


# FILE: app/api/routes_ot_clinical.py


@router.post(
    "/cases/{case_id}/nursing-record",
    response_model=OtNursingRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_nursing_record_for_case(
        case_id: int,
        payload: OtNursingRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.create"])

    case = _get_case_or_404(db, case_id)
    if case.nursing_record:
        raise HTTPException(
            status_code=400,
            detail="Nursing record already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.put(
    "/cases/{case_id}/nursing-record",
    response_model=OtNursingRecordOut,
)
def update_nursing_record_for_case(
        case_id: int,
        payload: OtNursingRecordUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.nursing_record.update"])

    case = _get_case_or_404(db, case_id)
    record = case.nursing_record
    if not record:
        raise HTTPException(status_code=404, detail="Nursing record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    # If primary_nurse_id omitted, keep existing; if explicitly null, set to current user.
    if "primary_nurse_id" in data and data["primary_nurse_id"] is None:
        data["primary_nurse_id"] = user.id

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  SPONGE & INSTRUMENT COUNT – flat UI API
# ============================================================

from app.schemas.ot import OtCountsIn, OtCountsOut
from app.models.ot import OtSpongeInstrumentCount


@router.get(
    "/cases/{case_id}/counts",
    response_model=OtCountsOut,
)
def get_counts_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record: OtSpongeInstrumentCount = case.counts_record
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sponge & instrument count not found",
        )

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
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


@router.post(
    "/cases/{case_id}/counts",
    response_model=OtCountsOut,
    status_code=status.HTTP_201_CREATED,
)
def create_counts_for_case(
        case_id: int,
        payload: OtCountsIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.create"])

    case = _get_case_or_404(db, case_id)
    if case.counts_record:
        raise HTTPException(
            status_code=400,
            detail="Counts record already exists for this case",
        )

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
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


@router.put(
    "/cases/{case_id}/counts",
    response_model=OtCountsOut,
)
def update_counts_for_case(
        case_id: int,
        payload: OtCountsIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.update"])

    case = _get_case_or_404(db, case_id)
    record: OtSpongeInstrumentCount = case.counts_record

    # If no record yet, behave like upsert → reuse POST logic
    if not record:
        return create_counts_for_case(case_id, payload, db, user)

    initial = record.initial_count_data or {}
    final = record.final_count_data or {}

    # update initial JSON
    initial.update({
        "sponges_initial": payload.sponges_initial,
        "sponges_added": payload.sponges_added,
        "instruments_initial": payload.instruments_initial,
        "needles_initial": payload.needles_initial,
        "xray_done": payload.xray_done,
        "resolved_by": payload.resolved_by,
        "notes": payload.notes,
    })

    # update final JSON
    final.update({
        "sponges_final": payload.sponges_final,
        "instruments_final": payload.instruments_final,
        "needles_final": payload.needles_final,
    })

    record.initial_count_data = initial
    record.final_count_data = final
    record.discrepancy = bool(payload.discrepancy_text)
    record.discrepancy_notes = payload.discrepancy_text

    db.add(record)
    db.commit()
    db.refresh(record)

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
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


# ============================================================
#  IMPLANT / PROSTHESIS RECORDS
# ============================================================


@router.get(
    "/cases/{case_id}/implants",
    response_model=List[OtImplantRecordOut],
)
def list_implants_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.implants.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    return case.implant_records


@router.post(
    "/cases/{case_id}/implants",
    response_model=OtImplantRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_implant_for_case(
        case_id: int,
        payload: OtImplantRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.implants.create"])

    _get_case_or_404(case_id, db)

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.put(
    "/implants/{implant_id}",
    response_model=OtImplantRecordOut,
)
def update_implant(
        implant_id: int,
        payload: OtImplantRecordUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.implants.update"])

    record = db.query(OtImplantRecord).get(implant_id)
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


@router.delete(
    "/implants/{implant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_implant(
        implant_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.implants.delete"])

    record = db.query(OtImplantRecord).get(implant_id)
    if not record:
        raise HTTPException(status_code=404, detail="Implant record not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  OPERATION NOTE (SURGEON)
# ============================================================


@router.get(
    "/cases/{case_id}/operation-note",
    response_model=OperationNoteOut,
)
def get_operation_note_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.operation_note
    if not record:
        raise HTTPException(status_code=404, detail="Operation note not found")
    return record


@router.post(
    "/cases/{case_id}/operation-note",
    response_model=OperationNoteOut,
    status_code=status.HTTP_201_CREATED,
)
def create_operation_note_for_case(
        case_id: int,
        payload: OperationNoteCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.create"])

    case = _get_case_or_404(db, case_id)
    if case.operation_note:
        raise HTTPException(
            status_code=400,
            detail="Operation note already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.put(
    "/cases/{case_id}/operation-note",
    response_model=OperationNoteOut,
)
def update_operation_note_for_case(
        case_id: int,
        payload: OperationNoteUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.operation_notes.update"])

    case = _get_case_or_404(db, case_id)
    record = case.operation_note
    if not record:
        raise HTTPException(status_code=404, detail="Operation note not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    # if surgeon_user_id explicitly null, reset to current user
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

# FILE: app/api/routes_ot_clinical.py


@router.get(
    "/cases/{case_id}/blood-transfusions",
    response_model=List[OtBloodTransfusionRecordOut],
)
def list_blood_transfusions_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)  # ✅ fixed
    return case.blood_records


@router.post(
    "/cases/{case_id}/blood-transfusions",
    response_model=OtBloodTransfusionRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_blood_transfusion_for_case(
        case_id: int,
        payload: OtBloodTransfusionRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.create"])

    _get_case_or_404(db, case_id)  # ✅ fixed

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.post(
    "/cases/{case_id}/blood-transfusions",
    response_model=OtBloodTransfusionRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_blood_transfusion_for_case(
        case_id: int,
        payload: OtBloodTransfusionRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.create"])

    _get_case_or_404(case_id, db)

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

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


@router.put(
    "/blood-transfusions/{record_id}",
    response_model=OtBloodTransfusionRecordOut,
)
def update_blood_transfusion(
        record_id: int,
        payload: OtBloodTransfusionRecordUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.update"])

    record = db.query(OtBloodTransfusionRecord).get(record_id)
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


@router.delete(
    "/blood-transfusions/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_blood_transfusion(
        record_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.blood_transfusion.delete"])

    record = db.query(OtBloodTransfusionRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Blood transfusion record not found")

    db.delete(record)
    db.commit()
    return None


# ============================================================
#  PACU / POST-ANAESTHESIA RECOVERY RECORD – UI FLAT
# ============================================================


@router.get(
    "/cases/{case_id}/pacu",
    response_model=PacuUiOut,
)
def get_pacu_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.view", "ot.cases.view"])

    case = _get_case_or_404(db, case_id)
    record = case.pacu_record
    if not record:
        raise HTTPException(status_code=404, detail="PACU record not found")

    pain_data = record.pain_scores or {}
    vitals = record.vitals or {}

    return PacuUiOut(
        id=record.id,
        case_id=record.case_id,
        nurse_user_id=record.nurse_user_id,
        arrival_time=_dt_to_time_str(record.admission_time),
        departure_time=_dt_to_time_str(record.discharge_time),
        pain_score=pain_data.get("score"),
        nausea_vomiting=vitals.get("nausea_vomiting"),
        airway_status=vitals.get("airway_status"),
        vitals_summary=vitals.get("vitals_summary"),
        complications=record.complications,
        discharge_criteria_met=bool(
            vitals.get("discharge_criteria_met") or False),
        notes=vitals.get("notes"),
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


@router.post(
    "/cases/{case_id}/pacu",
    response_model=PacuUiOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pacu_for_case(
        case_id: int,
        payload: PacuUiIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.create"])

    case = _get_case_or_404(db, case_id)
    if case.pacu_record:
        raise HTTPException(
            status_code=400,
            detail="PACU record already exists for this case",
        )

    pain_scores = ({
        "score": payload.pain_score
    } if payload.pain_score is not None else None)

    vitals = {
        "nausea_vomiting": payload.nausea_vomiting,
        "airway_status": payload.airway_status,
        "vitals_summary": payload.vitals_summary,
        "discharge_criteria_met": payload.discharge_criteria_met,
        "notes": payload.notes,
    }
    if all(v is None for v in vitals.values()):
        vitals = None

    record = PacuRecord(
        case_id=case_id,
        nurse_user_id=user.id,  # current user as PACU nurse
        admission_time=_time_str_to_dt(payload.arrival_time),
        discharge_time=_time_str_to_dt(payload.departure_time),
        pain_scores=pain_scores,
        vitals=vitals,
        complications=payload.complications,
        disposition=None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    pain_data = record.pain_scores or {}
    vitals = record.vitals or {}

    return PacuUiOut(
        id=record.id,
        case_id=record.case_id,
        nurse_user_id=record.nurse_user_id,
        arrival_time=_dt_to_time_str(record.admission_time),
        departure_time=_dt_to_time_str(record.discharge_time),
        pain_score=pain_data.get("score"),
        nausea_vomiting=vitals.get("nausea_vomiting"),
        airway_status=vitals.get("airway_status"),
        vitals_summary=vitals.get("vitals_summary"),
        complications=record.complications,
        discharge_criteria_met=bool(
            vitals.get("discharge_criteria_met") or False),
        notes=vitals.get("notes"),
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


@router.put(
    "/cases/{case_id}/pacu",
    response_model=PacuUiOut,
)
def update_pacu_for_case(
        case_id: int,
        payload: PacuUiIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.update"])

    case = _get_case_or_404(db, case_id)
    record = case.pacu_record

    # optional upsert: if no record, behave like create
    if not record:
        return create_pacu_for_case(case_id, payload, db, user)

    pain_scores = ({
        "score": payload.pain_score
    } if payload.pain_score is not None else None)

    vitals = {
        "nausea_vomiting": payload.nausea_vomiting,
        "airway_status": payload.airway_status,
        "vitals_summary": payload.vitals_summary,
        "discharge_criteria_met": payload.discharge_criteria_met,
        "notes": payload.notes,
    }
    if all(v is None for v in vitals.values()):
        vitals = None

    record.admission_time = _time_str_to_dt(payload.arrival_time)
    record.discharge_time = _time_str_to_dt(payload.departure_time)
    record.pain_scores = pain_scores
    record.vitals = vitals
    record.complications = payload.complications

    db.add(record)
    db.commit()
    db.refresh(record)

    pain_data = record.pain_scores or {}
    vitals = record.vitals or {}

    return PacuUiOut(
        id=record.id,
        case_id=record.case_id,
        nurse_user_id=record.nurse_user_id,
        arrival_time=_dt_to_time_str(record.admission_time),
        departure_time=_dt_to_time_str(record.discharge_time),
        pain_score=pain_data.get("score"),
        nausea_vomiting=vitals.get("nausea_vomiting"),
        airway_status=vitals.get("airway_status"),
        vitals_summary=vitals.get("vitals_summary"),
        complications=record.complications,
        discharge_criteria_met=bool(
            vitals.get("discharge_criteria_met") or False),
        notes=vitals.get("notes"),
        created_at=record.created_at,
        updated_at=getattr(record, "updated_at", None),
    )


# ============================================================
#  OT ADMIN / CLEANING / STERILITY LOG
# ============================================================
@router.get(
    "/cleaning-logs",
    response_model=List[OtCleaningLogOut],
)
def list_cleaning_logs(
        theatre_id: Optional[int] = Query(None),
        case_id: Optional[int] = Query(None),
        date: Optional[date] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
        session: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user=Depends(current_user),
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


@router.get(
    "/cleaning-logs/{log_id}",
    response_model=OtCleaningLogOut,
)
def get_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.view", "ot.logs.view"])

    log = db.query(OtCleaningLog).get(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")
    return log


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
    _need_any(user, ["ot.logs.cleaning.create", "ot.logs.manage"])

    log = OtCleaningLog(
        theatre_id=payload.theatre_id,
        date=payload.date,
        session=payload.session,
        case_id=payload.case_id,  # can be None or an OT case ID
        method=payload.method,
        done_by_user_id=payload.done_by_user_id,
        remarks=payload.remarks,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


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
    _need_any(user, ["ot.logs.cleaning.update", "ot.logs.manage"])

    log = db.query(OtCleaningLog).get(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(log, field, value)

    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.delete(
    "/cleaning-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_cleaning_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.logs.cleaning.delete", "ot.logs.manage"])

    log = db.query(OtCleaningLog).get(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Cleaning log not found")

    db.delete(log)
    db.commit()
    return None


# ============================================================
#  OT ADMIN / ENVIRONMENT LOG (TEMP / HUMIDITY / PRESSURE)
# ============================================================


@router.get(
    "/environment-logs",
    response_model=List[OtEnvironmentLogOut],
)
def list_environment_logs(
        theatre_id: Optional[int] = Query(None),
        date: Optional[date] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
        db: Session = Depends(get_db),
        user=Depends(current_user),
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


@router.get(
    "/environment-logs/{log_id}",
    response_model=OtEnvironmentLogOut,
)
def get_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.view", "ot.logs.view"])

    log = db.query(OtEnvironmentLog).get(log_id)
    if not log:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")
    return log


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
    _need_any(user, ["ot.logs.environment.update", "ot.logs.manage"])

    log = db.query(OtEnvironmentLog).get(log_id)
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


@router.delete(
    "/environment-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_environment_log(
        log_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.logs.environment.delete", "ot.logs.manage"])

    log = db.query(OtEnvironmentLog).get(log_id)
    if not log:
        raise HTTPException(status_code=404,
                            detail="Environment log not found")

    db.delete(log)
    db.commit()
    return None
