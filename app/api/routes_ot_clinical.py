# FILE: app/api/routes_ot_clinical.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.ot import (
    OtCase,
    PreAnaesthesiaEvaluation,
    PreOpChecklist,
    SurgicalSafetyChecklist,
    AnaesthesiaRecord,
    AnaesthesiaVitalLog,
    AnaesthesiaDrugLog,
    OtNursingRecord,
    OtSpongeInstrumentCount,
    OtImplantRecord,
    OperationNote,
    OtBloodTransfusionRecord,
    PacuRecord,
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
    SurgicalSafetyChecklistCreate,
    SurgicalSafetyChecklistUpdate,
    SurgicalSafetyChecklistOut,
    # Anaesthesia record
    AnaesthesiaRecordCreate,
    AnaesthesiaRecordUpdate,
    AnaesthesiaRecordOut,
    AnaesthesiaVitalLogCreate,
    AnaesthesiaVitalLogUpdate,
    AnaesthesiaVitalLogOut,
    AnaesthesiaDrugLogCreate,
    AnaesthesiaDrugLogUpdate,
    AnaesthesiaDrugLogOut,
    # Nursing record
    OtNursingRecordCreate,
    OtNursingRecordUpdate,
    OtNursingRecordOut,
    # Sponge & instrument
    OtSpongeInstrumentCountCreate,
    OtSpongeInstrumentCountUpdate,
    OtSpongeInstrumentCountOut,
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
    PacuRecordCreate,
    PacuRecordUpdate,
    PacuRecordOut,
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

def _get_case_or_404(case_id: int, db: Session) -> OtCase:
    case = db.query(OtCase).get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="OT Case not found")
    return case


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
#  PRE-OPERATIVE CHECKLIST
# ============================================================


@router.get(
    "/cases/{case_id}/preop-checklist",
    response_model=PreOpChecklistOut,
)
def get_preop_checklist_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.preop_checklist
    if not record:
        raise HTTPException(status_code=404,
                            detail="Pre-op checklist not found")
    return record


@router.post(
    "/cases/{case_id}/preop-checklist",
    response_model=PreOpChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
def create_preop_checklist_for_case(
        case_id: int,
        payload: PreOpChecklistCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.create"])

    case = _get_case_or_404(case_id, db)
    if case.preop_checklist:
        raise HTTPException(
            status_code=400,
            detail="Pre-op checklist already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

    record = PreOpChecklist(
        case_id=case_id,
        nurse_user_id=payload.nurse_user_id,
        data=payload.data,
        completed=payload.completed,
        completed_at=payload.completed_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cases/{case_id}/preop-checklist",
    response_model=PreOpChecklistOut,
)
def update_preop_checklist_for_case(
        case_id: int,
        payload: PreOpChecklistUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.preop_checklist.update"])

    case = _get_case_or_404(case_id, db)
    record = case.preop_checklist
    if not record:
        raise HTTPException(status_code=404,
                            detail="Pre-op checklist not found")

    data = payload.model_dump(exclude_unset=True)
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


@router.get(
    "/cases/{case_id}/safety-checklist",
    response_model=SurgicalSafetyChecklistOut,
)
def get_safety_checklist_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.surgical_safety.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.safety_checklist
    if not record:
        raise HTTPException(status_code=404,
                            detail="Safety checklist not found")
    return record


@router.post(
    "/cases/{case_id}/safety-checklist",
    response_model=SurgicalSafetyChecklistOut,
    status_code=status.HTTP_201_CREATED,
)
def create_safety_checklist_for_case(
        case_id: int,
        payload: SurgicalSafetyChecklistCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.surgical_safety.create"])

    case = _get_case_or_404(case_id, db)
    if case.safety_checklist:
        raise HTTPException(
            status_code=400,
            detail="Safety checklist already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

    record = SurgicalSafetyChecklist(
        case_id=case_id,
        sign_in_data=payload.sign_in_data,
        sign_in_done_by_id=payload.sign_in_done_by_id,
        sign_in_time=payload.sign_in_time,
        time_out_data=payload.time_out_data,
        time_out_done_by_id=payload.time_out_done_by_id,
        time_out_time=payload.time_out_time,
        sign_out_data=payload.sign_out_data,
        sign_out_done_by_id=payload.sign_out_done_by_id,
        sign_out_time=payload.sign_out_time,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cases/{case_id}/safety-checklist",
    response_model=SurgicalSafetyChecklistOut,
)
def update_safety_checklist_for_case(
        case_id: int,
        payload: SurgicalSafetyChecklistUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.surgical_safety.update"])

    case = _get_case_or_404(case_id, db)
    record = case.safety_checklist
    if not record:
        raise HTTPException(status_code=404,
                            detail="Safety checklist not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  ANAESTHESIA RECORD + VITALS + DRUG LOGS
# ============================================================


@router.get(
    "/cases/{case_id}/anaesthesia-record",
    response_model=AnaesthesiaRecordOut,
)
def get_anaesthesia_record_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.anaesthesia_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")
    return record


@router.post(
    "/cases/{case_id}/anaesthesia-record",
    response_model=AnaesthesiaRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_record_for_case(
        case_id: int,
        payload: AnaesthesiaRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.create"])

    case = _get_case_or_404(case_id, db)
    if case.anaesthesia_record:
        raise HTTPException(
            status_code=400,
            detail="Anaesthesia record already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

    record = AnaesthesiaRecord(
        case_id=case_id,
        anaesthetist_user_id=payload.anaesthetist_user_id,
        preop_vitals=payload.preop_vitals,
        plan=payload.plan,
        airway_plan=payload.airway_plan,
        intraop_summary=payload.intraop_summary,
        complications=payload.complications,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cases/{case_id}/anaesthesia-record",
    response_model=AnaesthesiaRecordOut,
)
def update_anaesthesia_record_for_case(
        case_id: int,
        payload: AnaesthesiaRecordUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_record.update"])

    case = _get_case_or_404(case_id, db)
    record = case.anaesthesia_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ---- Anaesthesia Vitals ----


@router.get(
    "/anaesthesia-records/{record_id}/vitals",
    response_model=List[AnaesthesiaVitalLogOut],
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

    return record.vitals


@router.post(
    "/anaesthesia-records/{record_id}/vitals",
    response_model=AnaesthesiaVitalLogOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_vital(
        record_id: int,
        payload: AnaesthesiaVitalLogCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_vitals.create"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    if payload.record_id != record_id:
        raise HTTPException(
            status_code=400,
            detail="record_id in body does not match URL",
        )

    vital = AnaesthesiaVitalLog(
        record_id=record_id,
        time=payload.time,
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        pulse=payload.pulse,
        spo2=payload.spo2,
        rr=payload.rr,
        etco2=payload.etco2,
        temperature=payload.temperature,
        comments=payload.comments,
    )
    db.add(vital)
    db.commit()
    db.refresh(vital)
    return vital


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
    response_model=List[AnaesthesiaDrugLogOut],
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

    return record.drugs


@router.post(
    "/anaesthesia-records/{record_id}/drugs",
    response_model=AnaesthesiaDrugLogOut,
    status_code=status.HTTP_201_CREATED,
)
def create_anaesthesia_drug(
        record_id: int,
        payload: AnaesthesiaDrugLogCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.anaesthesia_drugs.create"])

    record = db.query(AnaesthesiaRecord).get(record_id)
    if not record:
        raise HTTPException(status_code=404,
                            detail="Anaesthesia record not found")

    if payload.record_id != record_id:
        raise HTTPException(
            status_code=400,
            detail="record_id in body does not match URL",
        )

    drug = AnaesthesiaDrugLog(
        record_id=record_id,
        time=payload.time,
        drug_name=payload.drug_name,
        dose=payload.dose,
        route=payload.route,
        remarks=payload.remarks,
    )
    db.add(drug)
    db.commit()
    db.refresh(drug)
    return drug


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

    case = _get_case_or_404(case_id, db)
    record = case.nursing_record
    if not record:
        raise HTTPException(status_code=404, detail="Nursing record not found")
    return record


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

    case = _get_case_or_404(case_id, db)
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
        primary_nurse_id=payload.primary_nurse_id,
        positioning=payload.positioning,
        skin_prep_details=payload.skin_prep_details,
        catheter_details=payload.catheter_details,
        drains_details=payload.drains_details,
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

    case = _get_case_or_404(case_id, db)
    record = case.nursing_record
    if not record:
        raise HTTPException(status_code=404, detail="Nursing record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  SPONGE & INSTRUMENT COUNT
# ============================================================


@router.get(
    "/cases/{case_id}/counts",
    response_model=OtSpongeInstrumentCountOut,
)
def get_counts_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.counts_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Sponge & instrument count not found")
    return record


@router.post(
    "/cases/{case_id}/counts",
    response_model=OtSpongeInstrumentCountOut,
    status_code=status.HTTP_201_CREATED,
)
def create_counts_for_case(
        case_id: int,
        payload: OtSpongeInstrumentCountCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.create"])

    case = _get_case_or_404(case_id, db)
    if case.counts_record:
        raise HTTPException(
            status_code=400,
            detail="Counts record already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

    record = OtSpongeInstrumentCount(
        case_id=case_id,
        initial_count_data=payload.initial_count_data,
        final_count_data=payload.final_count_data,
        discrepancy=payload.discrepancy,
        discrepancy_notes=payload.discrepancy_notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cases/{case_id}/counts",
    response_model=OtSpongeInstrumentCountOut,
)
def update_counts_for_case(
        case_id: int,
        payload: OtSpongeInstrumentCountUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.counts.update"])

    case = _get_case_or_404(case_id, db)
    record = case.counts_record
    if not record:
        raise HTTPException(status_code=404,
                            detail="Sponge & instrument count not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


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

    case = _get_case_or_404(case_id, db)
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

    case = _get_case_or_404(case_id, db)
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

    record = OperationNote(
        case_id=case_id,
        surgeon_user_id=payload.surgeon_user_id,
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

    case = _get_case_or_404(case_id, db)
    record = case.operation_note
    if not record:
        raise HTTPException(status_code=404, detail="Operation note not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# ============================================================
#  BLOOD / BLOOD COMPONENT TRANSFUSION
# ============================================================


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

    case = _get_case_or_404(case_id, db)
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
#  PACU / POST-ANAESTHESIA RECOVERY RECORD
# ============================================================


@router.get(
    "/cases/{case_id}/pacu",
    response_model=PacuRecordOut,
)
def get_pacu_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.view", "ot.cases.view"])

    case = _get_case_or_404(case_id, db)
    record = case.pacu_record
    if not record:
        raise HTTPException(status_code=404, detail="PACU record not found")
    return record


@router.post(
    "/cases/{case_id}/pacu",
    response_model=PacuRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pacu_for_case(
        case_id: int,
        payload: PacuRecordCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.create"])

    case = _get_case_or_404(case_id, db)
    if case.pacu_record:
        raise HTTPException(
            status_code=400,
            detail="PACU record already exists for this case",
        )

    if payload.case_id != case_id:
        raise HTTPException(
            status_code=400,
            detail="case_id in body does not match URL",
        )

    record = PacuRecord(
        case_id=case_id,
        nurse_user_id=payload.nurse_user_id,
        admission_time=payload.admission_time,
        discharge_time=payload.discharge_time,
        pain_scores=payload.pain_scores,
        vitals=payload.vitals,
        complications=payload.complications,
        disposition=payload.disposition,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.put(
    "/cases/{case_id}/pacu",
    response_model=PacuRecordOut,
)
def update_pacu_for_case(
        case_id: int,
        payload: PacuRecordUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.pacu.update"])

    case = _get_case_or_404(case_id, db)
    record = case.pacu_record
    if not record:
        raise HTTPException(status_code=404, detail="PACU record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("case_id", None)

    for field, value in data.items():
        setattr(record, field, value)

    db.add(record)
    db.commit()
    db.refresh(record)
    return record
