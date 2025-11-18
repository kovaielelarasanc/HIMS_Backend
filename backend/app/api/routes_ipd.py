from __future__ import annotations
from datetime import datetime, date, timedelta, time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User as UserModel
from app.models.ipd import (IpdBed, IpdAdmission, IpdBedAssignment,
                            IpdTransfer, IpdNursingNote, IpdShiftHandover,
                            IpdVital, IpdIntakeOutput, IpdRound,
                            IpdProgressNote, IpdDischargeSummary,
                            IpdDischargeChecklist, IpdReferral, IpdOtCase,
                            IpdAnaesthesiaRecord, IpdRoom, IpdBedRate, IpdWard)
from app.models.patient import Patient
from app.models.user import User
from app.schemas.ipd import (
    AdmissionIn,
    AdmissionOut,
    AdmissionUpdateIn,
    AdmissionDetailOut,
    TransferIn,
    TransferOut,
    NursingNoteIn,
    NursingNoteOut,
    ShiftHandoverIn,
    ShiftHandoverOut,
    VitalIn,
    VitalOut,
    IOIn,
    IOOut,
    RoundIn,
    RoundOut,
    ProgressIn,
    ProgressOut,
    DischargeSummaryIn,
    DischargeSummaryOut,
    DischargeChecklistIn,
    DischargeChecklistOut,
    DueDischargeOut,
    ReferralIn,
    ReferralOut,
    OtCaseIn,
    OtCaseOut,
    AnaesthesiaIn,
    AnaesthesiaOut,
    BedChargePreviewOut,
    BedChargeDay,
    OtCaseForAdmissionIn,
)
from app.services.ipd_billing import auto_finalize_ipd_on_discharge

router = APIRouter()


def has_perm(user: UserModel, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


# ---------------- Utils ----------------


def _adm_display_code(adm_id: int) -> str:
    # friendly, non-PII admission code
    return f"IP-{adm_id:06d}"


def _admission_detail(db: Session, adm: IpdAdmission) -> AdmissionDetailOut:
    patient = db.query(Patient).get(adm.patient_id)
    bed = db.query(IpdBed).get(
        adm.current_bed_id) if adm.current_bed_id else None
    room = db.query(IpdRoom).get(bed.room_id) if bed else None
    ward = db.query(IpdWard).get(room.ward_id) if room else None
    doc = db.query(UserModel).get(
        adm.practitioner_user_id) if adm.practitioner_user_id else None

    patient_name = f"{(patient.first_name or '').strip()} {(patient.last_name or '').strip()}".strip(
    ) if patient else ""
    return AdmissionDetailOut(
        id=adm.id,
        display_code=_adm_display_code(adm.id),
        patient_id=adm.patient_id,
        patient_uhid=patient.uhid if patient else "",
        patient_name=patient_name or "-",
        department_id=adm.department_id,
        practitioner_user_id=adm.practitioner_user_id,
        practitioner_name=(doc.name if doc else None),
        admission_type=adm.admission_type,
        admitted_at=adm.admitted_at,
        expected_discharge_at=adm.expected_discharge_at,
        status=adm.status,
        current_bed_id=adm.current_bed_id,
        current_bed_code=(bed.code if bed else None),
        current_room_number=(room.number if room else None),
        current_ward_name=(ward.name if ward else None),
    )


# ---------------- Admissions ----------------


@router.post("/admissions", response_model=AdmissionOut)
def create_admission(payload: AdmissionIn,
                     db: Session = Depends(get_db),
                     user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")

    bed = db.query(IpdBed).get(payload.bed_id)
    if not bed:
        raise HTTPException(404, "Bed not found")
    if bed.state not in ("vacant", "reserved"):
        raise HTTPException(400, "Bed not available")

    adm = IpdAdmission(patient_id=payload.patient_id,
                       department_id=payload.department_id,
                       practitioner_user_id=payload.practitioner_user_id,
                       primary_nurse_user_id=payload.primary_nurse_user_id,
                       admission_type=payload.admission_type,
                       expected_discharge_at=payload.expected_discharge_at,
                       package_id=payload.package_id,
                       payor_type=payload.payor_type,
                       insurer_name=payload.insurer_name,
                       policy_number=payload.policy_number,
                       preliminary_diagnosis=payload.preliminary_diagnosis,
                       history=payload.history,
                       care_plan=payload.care_plan,
                       current_bed_id=payload.bed_id,
                       status="admitted",
                       created_by=user.id)
    db.add(adm)
    db.flush()

    # occupy bed + create assignment
    bed.state = "occupied"
    bed.reserved_until = None
    db.add(
        IpdBedAssignment(admission_id=adm.id,
                         bed_id=bed.id,
                         reason="admission"))
    db.commit()
    db.refresh(adm)
    return adm


@router.get("/admissions", response_model=List[AdmissionOut])
def list_admissions(
        status: Optional[str] = None,
        patient_id: Optional[int] = None,
        practitioner_user_id: Optional[int] = None,
        department_id: Optional[int] = None,
        limit: int = 300,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdAdmission)
    if status:
        q = q.filter(IpdAdmission.status == status)
    if patient_id:
        q = q.filter(IpdAdmission.patient_id == patient_id)
    if practitioner_user_id:
        q = q.filter(IpdAdmission.practitioner_user_id == practitioner_user_id)
    if department_id:
        q = q.filter(IpdAdmission.department_id == department_id)
    return q.order_by(IpdAdmission.id.desc()).limit(min(limit, 500)).all()


@router.get("/admissions/{admission_id}", response_model=AdmissionOut)
def get_admission(admission_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return adm


@router.put("/admissions/{admission_id}", response_model=AdmissionOut)
def update_admission(admission_id: int,
                     payload: AdmissionUpdateIn,
                     db: Session = Depends(get_db),
                     user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    # Only basic fields; bed changes require /transfer
    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(adm, k, v)
    db.commit()
    db.refresh(adm)
    return adm


@router.patch("/admissions/{admission_id}/cancel")
def cancel_admission(admission_id: int,
                     db: Session = Depends(get_db),
                     user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    if adm.status in ("discharged", "lama", "dama", "disappeared",
                      "cancelled"):
        return {"message": f"Already {adm.status}"}

    # release bed & close current assignment
    if adm.current_bed_id:
        bed = db.query(IpdBed).get(adm.current_bed_id)
        if bed:
            bed.state = "vacant"
        last_assign = db.query(IpdBedAssignment).filter(
            IpdBedAssignment.admission_id == admission_id,
            IpdBedAssignment.to_ts.is_(None)).order_by(
                IpdBedAssignment.id.desc()).first()
        if last_assign:
            last_assign.to_ts = datetime.utcnow()

    adm.status = "cancelled"
    adm.current_bed_id = None
    db.commit()
    return {"message": "Admission cancelled"}


# ---------------- Transfers ----------------


@router.post("/admissions/{admission_id}/transfer", response_model=TransferOut)
def transfer_bed(admission_id: int,
                 payload: TransferIn,
                 db: Session = Depends(get_db),
                 user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm or adm.status != "admitted":
        raise HTTPException(404, "Admission not found/active")

    to_bed = db.query(IpdBed).get(payload.to_bed_id)
    if not to_bed:
        raise HTTPException(404, "Target bed not found")
    if to_bed.state != "vacant":
        raise HTTPException(400, "Target bed not vacant")

    from_bed_id = adm.current_bed_id

    # close old assignment
    last_assign = db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id,
        IpdBedAssignment.to_ts.is_(None)).order_by(
            IpdBedAssignment.id.desc()).first()
    if last_assign:
        last_assign.to_ts = datetime.utcnow()

    # free old bed
    if from_bed_id:
        old = db.query(IpdBed).get(from_bed_id)
        if old:
            old.state = "vacant"

    # occupy new bed
    to_bed.state = "occupied"
    adm.current_bed_id = to_bed.id

    tr = IpdTransfer(admission_id=admission_id,
                     from_bed_id=from_bed_id,
                     to_bed_id=to_bed.id,
                     reason=payload.reason or "",
                     requested_by=user.id,
                     approved_by=user.id)
    db.add(tr)
    db.add(
        IpdBedAssignment(admission_id=admission_id,
                         bed_id=to_bed.id,
                         reason="transfer"))
    db.commit()
    db.refresh(tr)
    return tr


# ---------------- Nursing Notes ----------------


@router.post("/admissions/{admission_id}/nursing-notes",
             response_model=NursingNoteOut)
def create_nursing_note(admission_id: int,
                        payload: NursingNoteIn,
                        db: Session = Depends(get_db),
                        user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    n = IpdNursingNote(
        admission_id=admission_id,
        entry_time=payload.entry_time or datetime.utcnow(),
        nurse_id=user.id,
        patient_condition=payload.patient_condition or "",
        clinical_finding=payload.clinical_finding or "",
        significant_events=payload.significant_events or "",
        response_progress=payload.response_progress or "",
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


@router.get("/admissions/{admission_id}/nursing-notes",
            response_model=List[NursingNoteOut])
def list_nursing_notes(admission_id: int,
                       db: Session = Depends(get_db),
                       user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdNursingNote).filter(
        IpdNursingNote.admission_id == admission_id).order_by(
            IpdNursingNote.entry_time.desc()).all()


# ---------------- Shift Handover (with plural alias) ----------------


@router.post("/admissions/{admission_id}/shift-handover",
             response_model=ShiftHandoverOut)
@router.post("/admissions/{admission_id}/shift-handovers",
             response_model=ShiftHandoverOut)
def create_shift_handover(admission_id: int,
                          payload: ShiftHandoverIn,
                          db: Session = Depends(get_db),
                          user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    sh = IpdShiftHandover(admission_id=admission_id,
                          nurse_id=user.id,
                          **payload.dict())
    db.add(sh)
    db.commit()
    db.refresh(sh)
    return sh


@router.get("/admissions/{admission_id}/shift-handover",
            response_model=List[ShiftHandoverOut])
@router.get("/admissions/{admission_id}/shift-handovers",
            response_model=List[ShiftHandoverOut])
def list_shift_handover(admission_id: int,
                        db: Session = Depends(get_db),
                        user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdShiftHandover).filter(
        IpdShiftHandover.admission_id == admission_id).order_by(
            IpdShiftHandover.id.desc()).all()


# ---------------- Vitals ----------------


@router.post("/admissions/{admission_id}/vitals", response_model=VitalOut)
def record_vitals(admission_id: int,
                  payload: VitalIn,
                  db: Session = Depends(get_db),
                  user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    v = IpdVital(admission_id=admission_id,
                 recorded_by=user.id,
                 recorded_at=payload.recorded_at or datetime.utcnow(),
                 bp_systolic=payload.bp_systolic,
                 bp_diastolic=payload.bp_diastolic,
                 temp_c=payload.temp_c,
                 rr=payload.rr,
                 spo2=payload.spo2,
                 pulse=payload.pulse)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


@router.get("/admissions/{admission_id}/vitals", response_model=List[VitalOut])
def list_vitals(admission_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdVital).filter(
        IpdVital.admission_id == admission_id).order_by(
            IpdVital.recorded_at.desc()).limit(200).all()


# ---------------- Intake/Output (+ /io alias) ----------------


@router.post("/admissions/{admission_id}/intake-output", response_model=IOOut)
@router.post("/admissions/{admission_id}/io", response_model=IOOut)
def record_io(admission_id: int,
              payload: IOIn,
              db: Session = Depends(get_db),
              user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    io = IpdIntakeOutput(admission_id=admission_id,
                         recorded_by=user.id,
                         recorded_at=payload.recorded_at or datetime.utcnow(),
                         intake_ml=payload.intake_ml,
                         urine_ml=payload.urine_ml,
                         drains_ml=payload.drains_ml,
                         stools_count=payload.stools_count,
                         remarks=payload.remarks or "")
    db.add(io)
    db.commit()
    db.refresh(io)
    return io


@router.get("/admissions/{admission_id}/intake-output",
            response_model=List[IOOut])
@router.get("/admissions/{admission_id}/io", response_model=List[IOOut])
def list_io(admission_id: int,
            db: Session = Depends(get_db),
            user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdIntakeOutput).filter(
        IpdIntakeOutput.admission_id == admission_id).order_by(
            IpdIntakeOutput.recorded_at.desc()).limit(200).all()


# ---------------- Rounds & Progress ----------------


@router.post("/admissions/{admission_id}/rounds", response_model=RoundOut)
def create_round(admission_id: int,
                 payload: RoundIn,
                 db: Session = Depends(get_db),
                 user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    r = IpdRound(admission_id=admission_id,
                 by_user_id=user.id,
                 notes=payload.notes or "")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.get("/admissions/{admission_id}/rounds", response_model=List[RoundOut])
def list_rounds(admission_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdRound).filter(
        IpdRound.admission_id == admission_id).order_by(
            IpdRound.id.desc()).all()


@router.post("/admissions/{admission_id}/progress-notes",
             response_model=ProgressOut)
def create_progress(admission_id: int,
                    payload: ProgressIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    pn = IpdProgressNote(admission_id=admission_id,
                         by_user_id=user.id,
                         observation=payload.observation or "",
                         plan=payload.plan or "")
    db.add(pn)
    db.commit()
    db.refresh(pn)
    return pn


@router.get("/admissions/{admission_id}/progress-notes",
            response_model=List[ProgressOut])
def list_progress(admission_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdProgressNote).filter(
        IpdProgressNote.admission_id == admission_id).order_by(
            IpdProgressNote.id.desc()).all()


# ---------------- Discharge Summary & Checklist ----------------


@router.get("/admissions/{admission_id}/discharge-summary",
            response_model=Optional[DischargeSummaryOut])
def get_discharge_summary(admission_id: int,
                          db: Session = Depends(get_db),
                          user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first()


@router.post("/admissions/{admission_id}/discharge-summary",
             response_model=DischargeSummaryOut)
def upsert_discharge_summary(
        admission_id: int,
        payload: DischargeSummaryIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    ds = db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first()
    if not ds:
        ds = IpdDischargeSummary(admission_id=admission_id)
        db.add(ds)

    # Update editable fields (exclude finalize flag itself)
    data = payload.dict(exclude_unset=True)
    data.pop("finalize", None)
    for k, v in data.items():
        setattr(ds, k, v if v is not None else "")

    # Finalize (idempotent)
    if payload.finalize and not ds.finalized:
        ds.finalized = True
        ds.finalized_by = user.id
        ds.finalized_at = datetime.utcnow()

        # Free bed & close active assignment
        if adm.current_bed_id:
            bed = db.query(IpdBed).get(adm.current_bed_id)
            if bed:
                bed.state = "vacant"

        last_assign = db.query(IpdBedAssignment).filter(
            IpdBedAssignment.admission_id == admission_id,
            IpdBedAssignment.to_ts.is_(None)).order_by(
                IpdBedAssignment.id.desc()).first()
        if last_assign:
            last_assign.to_ts = datetime.utcnow()

        # Mark discharged
        adm.status = "discharged"
        adm.current_bed_id = None
        # (Optional) if you later add adm.discharge_at column, set it here:
        # adm.discharge_at = datetime.utcnow()

        # Let billing consolidate
        db.flush()
        auto_finalize_ipd_on_discharge(db,
                                       admission_id=adm.id,
                                       user_id=user.id)

    db.commit()
    db.refresh(ds)
    return ds


@router.get("/admissions/{admission_id}/discharge-checklist",
            response_model=Optional[DischargeChecklistOut])
def get_discharge_checklist(admission_id: int,
                            db: Session = Depends(get_db),
                            user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdDischargeChecklist).filter(
        IpdDischargeChecklist.admission_id == admission_id).first()


@router.post("/admissions/{admission_id}/discharge-checklist",
             response_model=DischargeChecklistOut)
def upsert_discharge_checklist(admission_id: int,
                               payload: DischargeChecklistIn,
                               db: Session = Depends(get_db),
                               user: User = Depends(auth_current_user)):
    if not (has_perm(user, "ipd.nursing") or has_perm(user, "ipd.doctor")
            or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")

    chk = db.query(IpdDischargeChecklist).filter(
        IpdDischargeChecklist.admission_id == admission_id).first()
    if not chk:
        chk = IpdDischargeChecklist(admission_id=admission_id)
        db.add(chk)

    data = payload.dict(exclude_unset=True)
    if "financial_clearance" in data:
        chk.financial_clearance = bool(data["financial_clearance"])
        if chk.financial_clearance:
            chk.financial_cleared_by = user.id
    if "clinical_clearance" in data:
        chk.clinical_clearance = bool(data["clinical_clearance"])
        if chk.clinical_clearance:
            chk.clinical_cleared_by = user.id
    if "delay_reason" in data and data["delay_reason"] is not None:
        chk.delay_reason = data["delay_reason"]
    if payload.submit:
        if not has_perm(user, "ipd.manage"):
            raise HTTPException(403, "Only IPD managers can submit checklist")
        chk.submitted = True
        chk.submitted_at = datetime.utcnow()

    db.commit()
    db.refresh(chk)
    return chk


# ---------------- Discharge Queue ----------------


@router.get("/due-discharges", response_model=List[DueDischargeOut])
def due_discharges(for_date: date = Query(...),
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    start = datetime.combine(for_date, datetime.min.time())
    end = datetime.combine(for_date, datetime.max.time())
    q = db.query(IpdAdmission).filter(
        IpdAdmission.status == "admitted",
        IpdAdmission.expected_discharge_at.isnot(None),
        IpdAdmission.expected_discharge_at >= start,
        IpdAdmission.expected_discharge_at <= end)
    rows = q.all()
    return [
        DueDischargeOut(admission_id=r.id,
                        patient_id=r.patient_id,
                        expected_discharge_at=r.expected_discharge_at,
                        status=r.status) for r in rows
    ]


@router.patch("/admissions/{admission_id}/mark-status")
def mark_special_status(admission_id: int,
                        status: str = Query(...,
                                            regex="^(lama|dama|disappeared)$"),
                        db: Session = Depends(get_db),
                        user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    adm.status = status
    if adm.current_bed_id:
        bed = db.query(IpdBed).get(adm.current_bed_id)
        if bed:
            bed.state = "vacant"
        last_assign = db.query(IpdBedAssignment).filter(
            IpdBedAssignment.admission_id == admission_id,
            IpdBedAssignment.to_ts.is_(None)).first()
        if last_assign:
            last_assign.to_ts = datetime.utcnow()
    adm.current_bed_id = None
    db.commit()
    return {"message": f"Admission marked {status}"}


# ---------------- ABHA linkage (stub) ----------------


@router.post("/admissions/{admission_id}/push-to-abha")
def push_discharge_to_abha(admission_id: int,
                           db: Session = Depends(get_db),
                           user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    ds = db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first()
    if not ds or not ds.finalized:
        raise HTTPException(400, "Finalize discharge summary first")
    adm.abha_shared_at = datetime.utcnow()
    db.commit()
    return {
        "message": "Discharge summary pushed to ABHA (stubbed)",
        "shared_at": adm.abha_shared_at
    }


# ---------------- Referrals ----------------


@router.post("/admissions/{admission_id}/referrals",
             response_model=ReferralOut)
def create_referral(admission_id: int,
                    payload: ReferralIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(auth_current_user)):
    if not (has_perm(user, "ipd.nursing") or has_perm(user, "ipd.doctor")
            or has_perm(user, "ipd.manage")):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    ref = IpdReferral(admission_id=admission_id, **payload.dict())
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref


@router.get("/admissions/{admission_id}/referrals",
            response_model=List[ReferralOut])
def list_referrals(admission_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdReferral).filter(
        IpdReferral.admission_id == admission_id).order_by(
            IpdReferral.id.desc()).all()


# ---------------- OT & Anaesthesia ----------------


@router.post("/ot/cases", response_model=OtCaseOut)
def create_ot_case(payload: OtCaseIn,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(payload.admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    c = IpdOtCase(**payload.dict())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/ot/cases", response_model=List[OtCaseOut])
def list_ot_cases(admission_id: Optional[int] = None,
                  status: Optional[str] = None,
                  db: Session = Depends(get_db),
                  user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdOtCase)
    if admission_id:
        q = q.filter(IpdOtCase.admission_id == admission_id)
    if status:
        q = q.filter(IpdOtCase.status == status)
    return q.order_by(IpdOtCase.id.desc()).all()


@router.patch("/ot/cases/{case_id}/status", response_model=OtCaseOut)
def update_ot_status(case_id: int,
                     status: str = Query(...),
                     db: Session = Depends(get_db),
                     user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    c = db.query(IpdOtCase).get(case_id)
    if not c:
        raise HTTPException(404, "OT case not found")
    c.status = status
    db.commit()
    db.refresh(c)
    return c


@router.patch("/ot/cases/{case_id}/time-log", response_model=OtCaseOut)
def ot_time_log(case_id: int,
                actual_start: Optional[datetime] = None,
                actual_end: Optional[datetime] = None,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    c = db.query(IpdOtCase).get(case_id)
    if not c:
        raise HTTPException(404, "OT case not found")
    if actual_start:
        c.actual_start = actual_start
    if actual_end:
        c.actual_end = actual_end
    db.commit()
    db.refresh(c)
    return c


# --- Anaesthesia: main + aliases for your UI ---


@router.post("/ot/anaesthesia", response_model=AnaesthesiaOut)
@router.post("/anaesthesia", response_model=AnaesthesiaOut
             )  # alias to satisfy POST /api/ipd/anaesthesia
def create_anaesthesia(payload: AnaesthesiaIn,
                       db: Session = Depends(get_db),
                       user: User = Depends(auth_current_user)):
    if not (has_perm(user, "ipd.manage") or has_perm(user, "ipd.doctor")):
        raise HTTPException(403, "Not permitted")
    case = db.query(IpdOtCase).get(payload.ot_case_id)
    if not case:
        raise HTTPException(404, "OT case not found")
    rec = IpdAnaesthesiaRecord(**payload.dict())
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get("/ot-cases/{case_id}/anaesthesia",
            response_model=List[AnaesthesiaOut])
def list_anaesthesia_for_case(case_id: int,
                              db: Session = Depends(get_db),
                              user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdAnaesthesiaRecord).filter(
        IpdAnaesthesiaRecord.ot_case_id == case_id).order_by(
            IpdAnaesthesiaRecord.id.desc()).all()


# ---------------- Bed Charge PREVIEW ----------------
# Rule: 1 bed-day per calendar day; pick assignment active at end-of-day.


def _resolve_rate(db: Session, room_type: str,
                  for_date: date) -> Optional[float]:
    r = (
        db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
            IpdBedRate.room_type == room_type).filter(
                IpdBedRate.effective_from <= for_date).filter(
                    (IpdBedRate.effective_to == None) |
                    (IpdBedRate.effective_to >= for_date))  # noqa: E711
        .order_by(IpdBedRate.effective_from.desc()).first())
    return float(r.daily_rate) if r else None


@router.get("/admissions/{admission_id}/bed-charges/preview",
            response_model=BedChargePreviewOut)
def preview_bed_charges_for_adm(admission_id: int,
                                from_date: date = Query(...),
                                to_date: date = Query(...),
                                db: Session = Depends(get_db),
                                user: User = Depends(auth_current_user)):
    return _preview_bed_charges_core(admission_id, from_date, to_date, db,
                                     user)


@router.get("/bed-charges/preview", response_model=BedChargePreviewOut
            )  # alias: ?admission_id=…&from_date=…&to_date=…
def preview_bed_charges_alias(admission_id: int = Query(...),
                              from_date: date = Query(...),
                              to_date: date = Query(...),
                              db: Session = Depends(get_db),
                              user: User = Depends(auth_current_user)):
    return _preview_bed_charges_core(admission_id, from_date, to_date, db,
                                     user)


def _preview_bed_charges_core(admission_id: int, from_date: date,
                              to_date: date, db: Session,
                              user: User) -> BedChargePreviewOut:
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    if to_date < from_date:
        raise HTTPException(400, "to_date must be >= from_date")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    assigns = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id).order_by(
            IpdBedAssignment.from_ts.asc()).all())

    days: List[BedChargeDay] = []
    missing = 0
    cursor = from_date
    while cursor <= to_date:
        eod = datetime.combine(cursor, time.max)
        active = None
        for a in assigns:
            start_ok = a.from_ts <= eod
            end_ok = (a.to_ts is None) or (a.to_ts >= datetime.combine(
                cursor, time.min))
            if start_ok and end_ok:
                active = a
        if not active:
            cursor += timedelta(days=1)
            continue

        bed = db.query(IpdBed).get(active.bed_id)
        room = db.query(IpdRoom).get(bed.room_id) if bed else None
        room_type = room.type if room else "General"
        rate = _resolve_rate(db, room_type, cursor)

        if rate is None:
            missing += 1
            rate_val = 0.0
        else:
            rate_val = float(rate)

        days.append(
            BedChargeDay(date=cursor,
                         bed_id=bed.id if bed else None,
                         room_type=room_type,
                         rate=rate_val,
                         assignment_id=active.id))
        cursor += timedelta(days=1)

    total = round(sum(d.rate for d in days), 2)
    return BedChargePreviewOut(admission_id=admission_id,
                               from_date=from_date,
                               to_date=to_date,
                               days=days,
                               total_amount=total,
                               missing_rate_days=missing)


@router.get("/admissions/{admission_id}/ot-cases",
            response_model=List[OtCaseOut])
def list_ot_cases_for_admission(admission_id: int,
                                db: Session = Depends(get_db),
                                user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    # ensure admission exists
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return db.query(IpdOtCase)\
             .filter(IpdOtCase.admission_id == admission_id)\
             .order_by(IpdOtCase.id.desc())\
             .all()


@router.post("/admissions/{admission_id}/ot-cases", response_model=OtCaseOut)
def create_ot_case_for_admission(admission_id: int,
                                 payload: OtCaseForAdmissionIn,
                                 db: Session = Depends(get_db),
                                 user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    c = IpdOtCase(
        admission_id=admission_id,
        surgery_name=payload.surgery_name,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        status=payload.status or "planned",
        surgeon_id=payload.surgeon_id,
        anaesthetist_id=payload.anaesthetist_id,
        staff_tags=payload.staff_tags or "",
        preop_notes=payload.preop_notes or "",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c
