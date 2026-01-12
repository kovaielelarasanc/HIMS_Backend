# FILE: app/api/routes_ipd.py
from __future__ import annotations
from datetime import datetime, date, timedelta, time, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError
from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User as UserModel
from app.models.ipd import (
    IpdBed,
    IpdAdmission,
    IpdBedAssignment,
    # IpdTransfer,
    IpdNursingNote,
    IpdShiftHandover,
    IpdVital,
    IpdIntakeOutput,
    IpdRound,
    IpdProgressNote,
    IpdDischargeSummary,
    IpdDischargeChecklist,
    IpdOtCase,
    IpdAnaesthesiaRecord,
    IpdRoom,
    IpdBedRate,
    IpdWard,
    IpdAdmissionFeedback,
    # NEW models
    IpdPainAssessment,
    IpdFallRiskAssessment,
    IpdPressureUlcerAssessment,
    IpdNutritionAssessment,
    IpdOrder,
    IpdDischargeMedication,
    IpdFeedback,
    IpdMedication,
)
from app.models.patient import Patient
from app.models.user import User
from app.schemas.ipd import (
    AdmissionIn,
    AdmissionOut,
    AdmissionUpdateIn,
    AdmissionDetailOut,
    # TransferIn,
    # TransferOut,
    NursingNoteCreate,
    NursingNoteUpdate,
    NursingNoteOut,
    ShiftHandoverIn,
    ShiftHandoverOut,
    VitalCreate,
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
    OtCaseIn,
    OtCaseOut,
    AnaesthesiaIn,
    AnaesthesiaOut,
    BedChargePreviewOut,
    BedChargeDay,
    OtCaseForAdmissionIn,
    # NEW schemas
    PainAssessmentIn,
    PainAssessmentOut,
    FallRiskAssessmentIn,
    FallRiskAssessmentOut,
    PressureUlcerAssessmentIn,
    PressureUlcerAssessmentOut,
    NutritionAssessmentIn,
    NutritionAssessmentOut,
    OrderIn,
    OrderOut,
    DischargeMedicationIn,
    DischargeMedicationOut,
    IpdFeedbackIn,
    IpdFeedbackOut,
    IpdAssessmentOut,
    IpdAssessmentCreate,
    IpdMedicationOut,
    IpdMedicationCreate,
    IpdMedicationUpdate,
    IpdDischargeMedicationOut,
    IpdDischargeMedicationCreate,
    IpdAdmissionFeedbackOut,
    IpdAdmissionFeedbackCreate,
    VitalSnapshot)

# adjust if your model path differs
# we will create this
from app.services.ipd_billing import compute_ipd_room_charges_daily
from app.models.ipd import IpdBed, IpdBedAssignment, IpdAdmission
from app.services.id_gen import make_ip_admission_code
from app.services.billing_ipd_room import sync_ipd_room_charges
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

router = APIRouter()


def as_aware(dt: datetime | None, assume_tz=IST) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=assume_tz)  # ✅ assume incoming naive = IST
    return dt


def to_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return as_aware(dt).astimezone(timezone.utc)


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    # if DB returns naive, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _intake_total(payload: IOIn) -> int:
    split = (payload.intake_oral_ml or
             0) + (payload.intake_iv_ml or 0) + (payload.intake_blood_ml or 0)
    return split if split > 0 else (payload.intake_ml or 0)


def _urine_total(payload: IOIn) -> int:
    split = (payload.urine_foley_ml or 0) + (payload.urine_voided_ml or 0)
    return split if split > 0 else (payload.urine_ml or 0)


def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return adm


def _close_open_bed_assignment(
    db,
    admission_id: int,
    stop_ts: datetime,
) -> None:
    last_assign = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id,
        IpdBedAssignment.to_ts.is_(None),
    ).order_by(IpdBedAssignment.id.desc()).first())
    if last_assign:
        last_assign.to_ts = stop_ts


def _free_current_bed_and_close_assignment(
    db,
    adm: IpdAdmission,
    stop_ts: datetime,
) -> None:
    # close open assignment at exact stop time
    _close_open_bed_assignment(db, adm.id, stop_ts)

    # free bed
    if adm.current_bed_id:
        bed = db.query(IpdBed).get(adm.current_bed_id)
        if bed:
            bed.state = "vacant"

    # clear bed pointer
    adm.current_bed_id = None


def _mark_admission_status_and_release_bed(
    db,
    adm: IpdAdmission,
    new_status: str,
    stop_ts: datetime,
) -> None:
    """
    Single canonical method for:
    - cancel
    - lama
    - dama
    - disappeared
    - discharged
    """
    if new_status not in ("cancelled", "lama", "dama", "disappeared",
                          "discharged"):
        raise HTTPException(400, "Invalid status")

    adm.status = new_status
    # keep consistent discharge_at for all “exit” statuses (optional but recommended)
    if getattr(adm, "discharge_at", None) is None:
        adm.discharge_at = stop_ts

    _free_current_bed_and_close_assignment(db, adm, stop_ts)


def has_perm(user: UserModel, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


# ---------------- Utils ----------------
def _adm_display_code(db: Session, adm: IpdAdmission) -> str:
    admitted = adm.admitted_at or datetime.utcnow()

    return make_ip_admission_code(
        db,
        adm.id,
        on_date=admitted,  # ✅ pass datetime, NOT .date()
        id_width=6,
    )


def _admission_detail(db: Session, adm: IpdAdmission) -> AdmissionDetailOut:
    patient = db.query(Patient).get(adm.patient_id)
    bed = db.query(IpdBed).get(
        adm.current_bed_id) if adm.current_bed_id else None
    room = db.query(IpdRoom).get(bed.room_id) if bed else None
    ward = db.query(IpdWard).get(room.ward_id) if room else None
    doc = (db.query(UserModel).get(adm.practitioner_user_id)
           if adm.practitioner_user_id else None)

    patient_name = (
        f"{(patient.first_name or '').strip()} {(patient.last_name or '').strip()}"
        .strip() if patient else "")
    return AdmissionDetailOut(
        id=adm.id,
        display_code=_adm_display_code(db, adm),
        patient_id=adm.patient_id,
        patient_uhid=patient.uhid if patient else "",
        patient_name=patient_name or "-",
        department_id=adm.department_id,
        practitioner_user_id=adm.practitioner_user_id,
        practitioner_name=(doc.name if doc else None),
        admission_type=adm.admission_type,
        admitted_at=adm.admitted_at.astimezone(ZoneInfo("Asia/Kolkata")),
        expected_discharge_at=adm.expected_discharge_at,
        status=adm.status,
        current_bed_id=adm.current_bed_id,
        current_bed_code=(bed.code if bed else None),
        current_room_number=(room.number if room else None),
        current_ward_name=(ward.name if ward else None),
    )


# ---------------- Admissions ----------------
@router.post("/admissions", response_model=AdmissionOut)
def create_admission(
        payload: AdmissionIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")

    bed = db.query(IpdBed).get(payload.bed_id)
    if not bed:
        raise HTTPException(404, "Bed not found")
    if bed.state not in ("vacant", "reserved"):
        raise HTTPException(400, "Bed not available")

    admitted_at_utc = to_utc(payload.admitted_at)
    expected_discharge_utc = to_utc(
        payload.expected_discharge_at
    ) if payload.expected_discharge_at else None

    adm = IpdAdmission(
        patient_id=payload.patient_id,
        department_id=payload.department_id,
        practitioner_user_id=payload.practitioner_user_id,
        primary_nurse_user_id=payload.primary_nurse_user_id,
        admission_type=payload.admission_type,
        admitted_at=admitted_at_utc,  # ✅ store UTC (aware)
        expected_discharge_at=expected_discharge_utc,  # ✅ if you want consistent
        package_id=payload.package_id,
        payor_type=payload.payor_type,
        insurer_name=payload.insurer_name,
        policy_number=payload.policy_number,
        preliminary_diagnosis=payload.preliminary_diagnosis,
        history=payload.history,
        care_plan=payload.care_plan,
        current_bed_id=payload.bed_id,
        status="admitted",
        created_by=user.id,
    )
    db.add(adm)
    db.flush()
    ip_code = make_ip_admission_code(
        db,
        adm.id,
        on_date=to_ist(admitted_at_utc),  # ✅ datetime
        id_width=6,
    )

    # Save it ONLY if your model has a field for it (safe)
    for attr in ("admission_code", "admission_no", "ipd_no", "ip_uhid"):
        if hasattr(adm, attr) and not getattr(adm, attr, None):
            setattr(adm, attr, ip_code)
            break
    bed.state = "occupied"
    bed.reserved_until = None
    db.add(
        IpdBedAssignment(
            admission_id=adm.id,
            bed_id=bed.id,
            reason="admission",
            from_ts=admitted_at_utc,
            to_ts=None,
        ))
    try:
        sync_ipd_room_charges(db,
                              admission_id=adm.id,
                              upto_dt=admitted_at_utc,
                              user=user)
    except Exception:
        pass
    db.commit()
    db.refresh(adm)
    dto = AdmissionOut.model_validate(adm, from_attributes=True)
    return dto.model_copy(update={"display_code": _adm_display_code(db, adm)})


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

    rows = q.order_by(IpdAdmission.id.desc()).limit(min(limit, 500)).all()

    out: List[AdmissionOut] = []
    for adm in rows:
        dto = AdmissionOut.model_validate(adm, from_attributes=True)
        dto = dto.model_copy(
            update={"display_code": _adm_display_code(db, adm)})
        out.append(dto)

    return out


@router.get("/admissions/{admission_id}", response_model=AdmissionOut)
def get_admission(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    dto = AdmissionOut.model_validate(adm, from_attributes=True)
    return dto.model_copy(update={"display_code": _adm_display_code(db, adm)})


@router.put("/admissions/{admission_id}", response_model=AdmissionOut)
def update_admission(
        admission_id: int,
        payload: AdmissionUpdateIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if adm and adm.billing_locked:
        raise HTTPException(
            400, "Billing is locked for this admission (discharged).")

    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(adm, k, v)
    db.commit()
    db.refresh(adm)
    return adm


@router.patch("/admissions/{admission_id}/discharge")
def discharge_admission(
        admission_id: int,
        discharge_at: Optional[datetime] = None,
        finalize_invoice: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    # If already discharged, return idempotently
    if adm.status == "discharged":
        return {"message": "Already discharged", "admission_id": adm.id}

    # If billing already locked, stop (someone already finalized discharge)
    if getattr(adm, "billing_locked", False):
        raise HTTPException(
            400, "Billing is locked for this admission (already discharged).")

    stop_ts = discharge_at or datetime.utcnow()

    # 1) Mark discharge status + release bed + close open bed assignment EXACTLY at stop_ts
    _mark_admission_status_and_release_bed(db, adm, "discharged", stop_ts)

    # 2) Ensure invoice exists for this admission context (IPD)
    result = None
    try:
        result = sync_ipd_room_charges(db,
                                       admission_id=adm.id,
                                       upto_dt=stop_ts,
                                       user=user)
    except Exception:
        result = None

    # then lock billing, finalize invoice if needed (your new billing finalize route can do it)
    adm.billing_locked = True
    adm.discharge_at = stop_ts
    db.commit()

    return {
        "message": "Discharged successfully",
        "admission_id": adm.id,
        "billing": result,
        "billing_locked": adm.billing_locked,
        "discharge_at": adm.discharge_at,
    }


@router.patch("/admissions/{admission_id}/cancel")
def cancel_admission(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.manage"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if adm and adm.billing_locked:
        raise HTTPException(
            400, "Billing is locked for this admission (discharged).")

    if adm.status in ("discharged", "lama", "dama", "disappeared",
                      "cancelled"):
        return {"message": f"Already {adm.status}"}

    stop_ts = datetime.utcnow()
    _mark_admission_status_and_release_bed(db, adm, "cancelled", stop_ts)

    db.commit()
    return {"message": "Admission cancelled"}


# # ---------------- Transfers ----------------
# @router.post("/admissions/{admission_id}/transfer", response_model=TransferOut)
# def transfer_bed(
#         admission_id: int,
#         payload: TransferIn,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "ipd.manage"):
#         raise HTTPException(403, "Not permitted")
#     adm = db.query(IpdAdmission).get(admission_id)
#     if adm and adm.billing_locked:
#         raise HTTPException(
#             400, "Billing is locked for this admission (discharged).")

#     if not adm or adm.status != "admitted":
#         raise HTTPException(404, "Admission not found/active")

#     to_bed = db.query(IpdBed).get(payload.to_bed_id)
#     if not to_bed:
#         raise HTTPException(404, "Target bed not found")
#     if to_bed.state != "vacant":
#         raise HTTPException(400, "Target bed not vacant")

#     from_bed_id = adm.current_bed_id

#     now = datetime.utcnow()

#     # close current open assignment
#     last_assign = (db.query(IpdBedAssignment).filter(
#         IpdBedAssignment.admission_id == admission_id,
#         IpdBedAssignment.to_ts.is_(None),
#     ).order_by(IpdBedAssignment.id.desc()).first())
#     if last_assign:
#         last_assign.to_ts = now  # ✅ stop previous bed billing exactly here

#     if from_bed_id:
#         old = db.query(IpdBed).get(from_bed_id)
#         if old:
#             old.state = "vacant"

#     to_bed.state = "occupied"
#     adm.current_bed_id = to_bed.id

#     tr = IpdTransfer(
#         admission_id=admission_id,
#         from_bed_id=from_bed_id,
#         to_bed_id=to_bed.id,
#         reason=payload.reason or "",
#         requested_by=user.id,
#         approved_by=user.id,
#     )
#     db.add(tr)
#     db.add(
#         IpdBedAssignment(
#             admission_id=admission_id,
#             bed_id=to_bed.id,
#             reason="transfer",
#             from_ts=now,  # ✅ start new bed billing exactly here
#             to_ts=None,
#         ))
#     db.commit()
#     db.refresh(tr)
#     return tr

LOCK_AFTER_HOURS = 24


# ---------------- Helpers ----------------
def _ensure_admission(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.get(IpdAdmission, admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")
    return adm


def _ensure_editable(note: IpdNursingNote) -> None:
    """Disallow editing after LOCK_AFTER_HOURS or if note.is_locked."""
    cutoff = datetime.utcnow() - timedelta(hours=LOCK_AFTER_HOURS)
    if note.entry_time < cutoff or note.is_locked:
        raise HTTPException(
            status_code=400,
            detail=
            "This nursing note is locked and cannot be edited (older than 24 hours).",
        )


def _resolve_linked_vitals(
    db: Session,
    admission_id: int,
    linked_vital_id: int | None,
) -> int | None:
    """
    Decide which vitals row to attach:
    - If linked_vital_id is passed → verify it belongs to this admission.
    - Else → auto-link the latest vitals row (if exists).
    """
    if linked_vital_id is not None:
        v = db.get(IpdVital, linked_vital_id)
        if not v or v.admission_id != admission_id:
            raise HTTPException(
                status_code=400,
                detail="Invalid vitals reference for this admission.",
            )
        return v.id

    # Auto-link latest vitals
    latest = (db.query(IpdVital).filter(
        IpdVital.admission_id == admission_id).order_by(
            IpdVital.recorded_at.desc()).first())
    return latest.id if latest else None


# ---------------- Create ----------------
@router.post(
    "/admissions/{admission_id}/nursing-notes",
    response_model=NursingNoteOut,
)
def create_nursing_note(
        admission_id: int,
        payload: NursingNoteCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # Suggested permission code: "ipd.nursing.create"
    if not has_perm(user, "ipd.nursing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    adm = _ensure_admission(db, admission_id)

    linked_vital_id = _resolve_linked_vitals(
        db=db,
        admission_id=admission_id,
        linked_vital_id=payload.linked_vital_id,
    )

    note = IpdNursingNote(
        admission_id=admission_id,
        nurse_id=user.id,
        entry_time=payload.entry_time or datetime.utcnow(),
        # Core NABH narrative fields
        # patient_condition=payload.patient_condition or "",
        significant_events=payload.significant_events or "",
        nursing_interventions=payload.nursing_interventions or "",
        response_progress=payload.response_progress or "",
        handover_note=payload.handover_note or "",
        # Structured observation fields
        # wound_status=payload.wound_status or "",
        # oxygen_support=payload.oxygen_support or "",
        # urine_output=payload.urine_output or "",
        # drains_tubes=payload.drains_tubes or "",
        # pain_score=payload.pain_score or "",
        other_findings=payload.other_findings or "",
        # Shift / ICU flags
        shift=payload.shift,
        is_icu=payload.is_icu,
        note_type=payload.note_type or "routine",
        vital_signs_summary=payload.vital_signs_summary or "",
        todays_procedures=payload.todays_procedures or "",
        current_condition=payload.current_condition or "",
        recent_changes=payload.recent_changes or "",
        ongoing_treatment=payload.ongoing_treatment or "",
        watch_next_shift=payload.watch_next_shift or "",

        # Vitals linkage
        linked_vital_id=linked_vital_id,
    )

    db.add(note)
    db.commit()
    db.refresh(note)
    return note


# ---------------- List all for an admission ----------------
@router.get(
    "/admissions/{admission_id}/nursing-notes",
    response_model=list[NursingNoteOut],
)
def list_nursing_notes(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # Suggested permission code: "ipd.nursing.view"
    if not has_perm(user, "ipd.nursing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # Eager load nurse + vitals so Pydantic can build NurseMini + VitalSnapshot cleanly
    notes = (db.query(IpdNursingNote).options(
        joinedload(IpdNursingNote.nurse),
        joinedload(IpdNursingNote.vitals),
    ).filter(IpdNursingNote.admission_id == admission_id).order_by(
        IpdNursingNote.entry_time.desc()).all())
    return notes


# ---------------- Get single note ----------------
@router.get(
    "/admissions/{admission_id}/nursing-notes/{note_id}",
    response_model=NursingNoteOut,
)
def get_nursing_note(
        admission_id: int,
        note_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    note = (db.query(IpdNursingNote).options(
        joinedload(IpdNursingNote.nurse),
        joinedload(IpdNursingNote.vitals),
    ).filter(
        IpdNursingNote.id == note_id,
        IpdNursingNote.admission_id == admission_id,
    ).first())

    if not note:
        raise HTTPException(status_code=404, detail="Nursing note not found")

    return note


# ---------------- Update (no delete – NABH safe) ----------------
@router.put(
    "/admissions/{admission_id}/nursing-notes/{note_id}",
    response_model=NursingNoteOut,
)
def update_nursing_note(
        admission_id: int,
        note_id: int,
        payload: NursingNoteUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # Suggested permission code: "ipd.nursing.update"
    if not has_perm(user, "ipd.nursing.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    note = (db.query(IpdNursingNote).options(
        joinedload(IpdNursingNote.nurse),
        joinedload(IpdNursingNote.vitals),
    ).filter(
        IpdNursingNote.id == note_id,
        IpdNursingNote.admission_id == admission_id,
    ).first())

    if not note:
        raise HTTPException(status_code=404, detail="Nursing note not found")

    _ensure_editable(note)

    # Core narrative fields
    if payload.patient_condition is not None:
        note.patient_condition = payload.patient_condition
    if payload.significant_events is not None:
        note.significant_events = payload.significant_events
    if payload.nursing_interventions is not None:
        note.nursing_interventions = payload.nursing_interventions
    if payload.response_progress is not None:
        note.response_progress = payload.response_progress
    if payload.handover_note is not None:
        note.handover_note = payload.handover_note

    # Structured observation fields
    if payload.wound_status is not None:
        note.wound_status = payload.wound_status
    if payload.oxygen_support is not None:
        note.oxygen_support = payload.oxygen_support
    if payload.urine_output is not None:
        note.urine_output = payload.urine_output
    if payload.drains_tubes is not None:
        note.drains_tubes = payload.drains_tubes
    if payload.pain_score is not None:
        note.pain_score = payload.pain_score
    if payload.other_findings is not None:
        note.other_findings = payload.other_findings

    if payload.shift is not None:
        note.shift = payload.shift

    # Optional: allow correcting entry_time (within same day / shift if you want stricter rules)
    if payload.entry_time is not None:
        note.entry_time = payload.entry_time

    db.add(note)
    db.commit()
    db.refresh(note)
    return note


# ---------------- Shift Handover ----------------
@router.post(
    "/admissions/{admission_id}/shift-handover",
    response_model=ShiftHandoverOut,
)
@router.post(
    "/admissions/{admission_id}/shift-handovers",
    response_model=ShiftHandoverOut,
)
def create_shift_handover(
        admission_id: int,
        payload: ShiftHandoverIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if adm and adm.billing_locked:
        raise HTTPException(
            400, "Billing is locked for this admission (discharged).")

    if not adm:
        raise HTTPException(404, "Admission not found")
    sh = IpdShiftHandover(
        admission_id=admission_id,
        nurse_id=user.id,
        **payload.dict(),
    )
    db.add(sh)
    db.commit()
    db.refresh(sh)
    return sh


@router.get(
    "/admissions/{admission_id}/shift-handover",
    response_model=List[ShiftHandoverOut],
)
@router.get(
    "/admissions/{admission_id}/shift-handovers",
    response_model=List[ShiftHandoverOut],
)
def list_shift_handover(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdShiftHandover).filter(
        IpdShiftHandover.admission_id == admission_id).order_by(
            IpdShiftHandover.id.desc()).all())


# ---------------- Vitals ----------------
@router.post("/admissions/{admission_id}/vitals", response_model=VitalOut)
def record_vitals(
        admission_id: int,
        payload: VitalCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    v = IpdVital(
        admission_id=admission_id,
        recorded_by=user.id,
        recorded_at=payload.recorded_at or datetime.utcnow(),
        bp_systolic=payload.bp_systolic,
        bp_diastolic=payload.bp_diastolic,
        temp_c=payload.temp_c,
        rr=payload.rr,
        spo2=payload.spo2,
        pulse=payload.pulse,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


@router.get(
    "/admissions/{admission_id}/vitals",
    response_model=List[VitalOut],
)
def list_vitals(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdVital).filter(
        IpdVital.admission_id == admission_id).order_by(
            IpdVital.recorded_at.desc()).limit(200).all())


@router.get("/admissions/{admission_id}/vitals/latest",
            response_model=VitalSnapshot)
def get_latest_vitals(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.vitals.view"):
        raise HTTPException(403, "Not permitted")

    adm = db.get(IpdAdmission, admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    v = (db.query(IpdVital).filter(
        IpdVital.admission_id == admission_id).order_by(
            IpdVital.recorded_at.desc()).first())
    if not v:
        raise HTTPException(404, "No vitals recorded for this admission")

    return v


# ---------------- Intake/Output ----------------
@router.post(
    "/admissions/{admission_id}/intake-output",
    response_model=IOOut,
)
@router.post("/admissions/{admission_id}/io", response_model=IOOut)
def record_io(
        admission_id: int,
        payload: IOIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    intake_total = _intake_total(payload)
    urine_total = _urine_total(payload)

    io = IpdIntakeOutput(
        admission_id=admission_id,
        recorded_by=user.id,
        recorded_at=payload.recorded_at or datetime.utcnow(),

        # ✅ save split fields
        intake_oral_ml=payload.intake_oral_ml or 0,
        intake_iv_ml=payload.intake_iv_ml or 0,
        intake_blood_ml=payload.intake_blood_ml or 0,
        urine_foley_ml=payload.urine_foley_ml or 0,
        urine_voided_ml=payload.urine_voided_ml or 0,
        drains_ml=payload.drains_ml or 0,
        stools_count=payload.stools_count or 0,
        remarks=payload.remarks or "",

        # ✅ store totals for compatibility / reporting
        intake_ml=intake_total,
        urine_ml=urine_total,
    )

    db.add(io)
    db.commit()
    db.refresh(io)
    return io


@router.get(
    "/admissions/{admission_id}/intake-output",
    response_model=List[IOOut],
)
@router.get("/admissions/{admission_id}/io", response_model=List[IOOut])
def list_io(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdIntakeOutput).filter(
        IpdIntakeOutput.admission_id == admission_id).order_by(
            IpdIntakeOutput.recorded_at.desc()).limit(200).all())


# ---------------- Rounds & Progress ----------------
@router.post("/admissions/{admission_id}/rounds", response_model=RoundOut)
def create_round(
        admission_id: int,
        payload: RoundIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    r = IpdRound(
        admission_id=admission_id,
        by_user_id=user.id,
        notes=payload.notes or "",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.get(
    "/admissions/{admission_id}/rounds",
    response_model=List[RoundOut],
)
def list_rounds(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdRound).filter(
        IpdRound.admission_id == admission_id).order_by(
            IpdRound.id.desc()).all())


@router.post(
    "/admissions/{admission_id}/progress-notes",
    response_model=ProgressOut,
)
def create_progress(
        admission_id: int,
        payload: ProgressIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    pn = IpdProgressNote(
        admission_id=admission_id,
        by_user_id=user.id,
        observation=payload.observation or "",
        plan=payload.plan or "",
    )
    db.add(pn)
    db.commit()
    db.refresh(pn)
    return pn


@router.get(
    "/admissions/{admission_id}/progress-notes",
    response_model=List[ProgressOut],
)
def list_progress(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdProgressNote).filter(
        IpdProgressNote.admission_id == admission_id).order_by(
            IpdProgressNote.id.desc()).all())


# ---------------- OT & Anaesthesia ----------------
@router.post("/ot/cases", response_model=OtCaseOut)
def create_ot_case(
        payload: OtCaseIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
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
def list_ot_cases(
        admission_id: Optional[int] = None,
        status: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdOtCase)
    if admission_id:
        q = q.filter(IpdOtCase.admission_id == admission_id)
    if status:
        q = q.filter(IpdOtCase.status == status)
    return q.order_by(IpdOtCase.id.desc()).all()


@router.patch("/ot/cases/{case_id}/status", response_model=OtCaseOut)
def update_ot_status(
        case_id: int,
        status: str = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
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
def ot_time_log(
        case_id: int,
        actual_start: Optional[datetime] = None,
        actual_end: Optional[datetime] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
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


@router.post("/ot/anaesthesia", response_model=AnaesthesiaOut)
@router.post("/anaesthesia", response_model=AnaesthesiaOut)
def create_anaesthesia(
        payload: AnaesthesiaIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
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


@router.get(
    "/ot-cases/{case_id}/anaesthesia",
    response_model=List[AnaesthesiaOut],
)
def list_anaesthesia_for_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdAnaesthesiaRecord).filter(
        IpdAnaesthesiaRecord.ot_case_id == case_id).order_by(
            IpdAnaesthesiaRecord.id.desc()).all())


# ---------------- Bed Charge PREVIEW ----------------
def _resolve_rate(db: Session, room_type: str,
                  for_date: date) -> Optional[float]:
    r = (
        db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
            IpdBedRate.room_type == room_type).filter(
                IpdBedRate.effective_from <= for_date).filter(
                    (IpdBedRate.effective_to == None)  # noqa: E711
                    | (IpdBedRate.effective_to >= for_date)).order_by(
                        IpdBedRate.effective_from.desc()).first())
    return float(r.daily_rate) if r else None


@router.get(
    "/admissions/{admission_id}/bed-charges/preview",
    response_model=BedChargePreviewOut,
)
def preview_bed_charges_for_adm(
        admission_id: int,
        from_date: date = Query(...),
        to_date: date = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return _preview_bed_charges_core(admission_id, from_date, to_date, db,
                                     user)


@router.get(
    "/bed-charges/preview",
    response_model=BedChargePreviewOut,
)
def preview_bed_charges_alias(
        admission_id: int = Query(...),
        from_date: date = Query(...),
        to_date: date = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return _preview_bed_charges_core(admission_id, from_date, to_date, db,
                                     user)


def _preview_bed_charges_core(admission_id: int, from_date: date,
                              to_date: date, db: Session, user: User):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    data = compute_ipd_room_charges_daily(db, admission_id, from_date, to_date)
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
            BedChargeDay(
                date=cursor,
                bed_id=bed.id if bed else None,
                room_type=room_type,
                rate=rate_val,
                assignment_id=active.id,
            ))
        cursor += timedelta(days=1)

    total = round(sum(d.rate for d in days), 2)
    return BedChargePreviewOut(
        admission_id=admission_id,
        from_date=from_date,
        to_date=to_date,
        days=days,
        total_amount=total,
        missing_rate_days=missing,
    )


@router.get(
    "/admissions/{admission_id}/ot-cases",
    response_model=List[OtCaseOut],
)
def list_ot_cases_for_admission(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return (db.query(IpdOtCase).filter(
        IpdOtCase.admission_id == admission_id).order_by(
            IpdOtCase.id.desc()).all())


@router.post(
    "/admissions/{admission_id}/ot-cases",
    response_model=OtCaseOut,
)
def create_ot_case_for_admission(
        admission_id: int,
        payload: OtCaseForAdmissionIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
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


# =====================================================================
# NEW: Risk / Clinical Assessments
# =====================================================================
@router.post(
    "/admissions/{admission_id}/assessments/pain",
    response_model=PainAssessmentOut,
)
def create_pain_assessment(
        admission_id: int,
        payload: PainAssessmentIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdPainAssessment(
        admission_id=admission_id,
        recorded_at=payload.recorded_at or datetime.utcnow(),
        scale_type=payload.scale_type or "",
        score=payload.score,
        location=payload.location or "",
        character=payload.character or "",
        intervention=payload.intervention or "",
        post_intervention_score=payload.post_intervention_score,
        recorded_by=user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/assessments/pain",
    response_model=List[PainAssessmentOut],
)
def list_pain_assessments(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdPainAssessment).filter(
        IpdPainAssessment.admission_id == admission_id).order_by(
            IpdPainAssessment.recorded_at.desc()).all())


@router.post(
    "/admissions/{admission_id}/assessments/fall-risk",
    response_model=FallRiskAssessmentOut,
)
def create_fall_risk_assessment(
        admission_id: int,
        payload: FallRiskAssessmentIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdFallRiskAssessment(
        admission_id=admission_id,
        recorded_at=payload.recorded_at or datetime.utcnow(),
        tool=payload.tool or "",
        score=payload.score,
        risk_level=payload.risk_level or "",
        precautions=payload.precautions or "",
        recorded_by=user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/assessments/fall-risk",
    response_model=List[FallRiskAssessmentOut],
)
def list_fall_risk_assessments(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdFallRiskAssessment).filter(
        IpdFallRiskAssessment.admission_id == admission_id).order_by(
            IpdFallRiskAssessment.recorded_at.desc()).all())


@router.post(
    "/admissions/{admission_id}/assessments/pressure-ulcer",
    response_model=PressureUlcerAssessmentOut,
)
def create_pressure_ulcer_assessment(
        admission_id: int,
        payload: PressureUlcerAssessmentIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdPressureUlcerAssessment(
        admission_id=admission_id,
        recorded_at=payload.recorded_at or datetime.utcnow(),
        tool=payload.tool or "",
        score=payload.score,
        risk_level=payload.risk_level or "",
        existing_ulcer=payload.existing_ulcer,
        site=payload.site or "",
        stage=payload.stage or "",
        management_plan=payload.management_plan or "",
        recorded_by=user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/assessments/pressure-ulcer",
    response_model=List[PressureUlcerAssessmentOut],
)
def list_pressure_ulcer_assessments(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdPressureUlcerAssessment).filter(
        IpdPressureUlcerAssessment.admission_id == admission_id).order_by(
            IpdPressureUlcerAssessment.recorded_at.desc()).all())


@router.post(
    "/admissions/{admission_id}/assessments/nutrition",
    response_model=NutritionAssessmentOut,
)
def create_nutrition_assessment(
        admission_id: int,
        payload: NutritionAssessmentIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.nursing"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdNutritionAssessment(
        admission_id=admission_id,
        recorded_at=payload.recorded_at or datetime.utcnow(),
        bmi=payload.bmi,
        weight_kg=payload.weight_kg,
        height_cm=payload.height_cm,
        screening_tool=payload.screening_tool or "",
        score=payload.score,
        risk_level=payload.risk_level or "",
        dietician_referral=payload.dietician_referral,
        recorded_by=user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/assessments/nutrition",
    response_model=List[NutritionAssessmentOut],
)
def list_nutrition_assessments(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdNutritionAssessment).filter(
        IpdNutritionAssessment.admission_id == admission_id).order_by(
            IpdNutritionAssessment.recorded_at.desc()).all())


# =====================================================================
# NEW: Generic IPD Orders
# =====================================================================
@router.post(
    "/admissions/{admission_id}/orders",
    response_model=OrderOut,
)
def create_order(
        admission_id: int,
        payload: OrderIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdOrder(
        admission_id=admission_id,
        order_type=payload.order_type,
        linked_order_id=payload.linked_order_id,
        order_text=payload.order_text or "",
        order_status=payload.order_status,
        ordered_at=payload.ordered_at or datetime.utcnow(),
        ordered_by=user.id,
        performed_at=payload.performed_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/orders",
    response_model=List[OrderOut],
)
def list_orders(
        admission_id: int,
        order_type: Optional[str] = None,
        status: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdOrder).filter(IpdOrder.admission_id == admission_id)
    if order_type:
        q = q.filter(IpdOrder.order_type == order_type)
    if status:
        q = q.filter(IpdOrder.order_status == status)
    return q.order_by(IpdOrder.ordered_at.desc(), IpdOrder.id.desc()).all()


# =====================================================================
# NEW: Discharge Medications (structured)
# =====================================================================
@router.post(
    "/admissions/{admission_id}/discharge-medications",
    response_model=DischargeMedicationOut,
)
def create_discharge_medication(
        admission_id: int,
        payload: DischargeMedicationIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.doctor"):
        raise HTTPException(403, "Not permitted")
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdDischargeMedication(
        admission_id=admission_id,
        drug_name=payload.drug_name,
        dose=payload.dose,
        dose_unit=payload.dose_unit or "",
        route=payload.route or "",
        frequency=payload.frequency or "",
        duration_days=payload.duration_days,
        advice_text=payload.advice_text or "",
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/discharge-medications",
    response_model=List[DischargeMedicationOut],
)
def list_discharge_medications(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (db.query(IpdDischargeMedication).filter(
        IpdDischargeMedication.admission_id == admission_id).order_by(
            IpdDischargeMedication.id.asc()).all())


# =====================================================================
# NEW: IPD Feedback
# =====================================================================
@router.post(
    "/admissions/{admission_id}/feedback",
    response_model=IpdFeedbackOut,
)
def create_ipd_feedback(
        admission_id: int,
        payload: IpdFeedbackIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # Feedback can be collected by IPD manager / registration / nursing
    if not (has_perm(user, "ipd.manage") or has_perm(user, "ipd.nursing")
            or has_perm(user, "ipd.view")):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    rec = IpdFeedback(
        admission_id=admission_id,
        patient_id=adm.patient_id,
        rating_overall=payload.rating_overall,
        rating_nursing=payload.rating_nursing,
        rating_doctor=payload.rating_doctor,
        rating_cleanliness=payload.rating_cleanliness,
        comments=payload.comments or "",
        collected_at=datetime.utcnow(),
        collected_by=user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


@router.get(
    "/admissions/{admission_id}/feedback",
    response_model=List[IpdFeedbackOut],
)
def list_ipd_feedback_for_admission(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    return (db.query(IpdFeedback).filter(
        IpdFeedback.admission_id == admission_id).order_by(
            IpdFeedback.collected_at.desc()).all())


# ------------------------------
# Assessments
# ------------------------------
@router.get(
    "/admissions/{admission_id}/assessments",
    response_model=list[IpdAssessmentOut],
)
def list_ipd_assessments(
        admission_id: int,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.nursing", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)
    rows = (db.query(IpdAssessment).filter(
        IpdAssessment.admission_id == admission_id).order_by(
            IpdAssessment.assessed_at.desc()).all())
    return rows


@router.post(
    "/admissions/{admission_id}/assessments",
    response_model=IpdAssessmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ipd_assessment(
        admission_id: int,
        payload: IpdAssessmentCreate,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.nursing", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = IpdAssessment(
        admission_id=admission_id,
        assessment_type=payload.assessment_type or "nursing",
        assessed_at=payload.assessed_at or datetime.utcnow(),
        summary=payload.summary,
        plan=payload.plan,
        created_by_id=user.id if getattr(user, "id", None) else None,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# ------------------------------
# Medications (Drug chart)
# ------------------------------
@router.get(
    "/admissions/{admission_id}/medications",
    response_model=List[IpdMedicationOut],
)
def list_ipd_medications(
        admission_id: int,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    """
    List all IPD medications (drug chart master list) for a given admission.
    - Requires any of: ipd.view / ipd.doctor / ipd.manage
    - Returns [] if no medications.
    - Logs DB-level errors instead of crashing with raw 500 tracebacks.
    """
    # 1) Basic param sanity
    if admission_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid admission id",
        )

    # 2) Permission check
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])

    # 3) Guard against invalid admission
    _get_admission_or_404(db, admission_id)

    # 4) Safe DB access
    try:
        rows = (db.query(IpdMedication).filter(
            IpdMedication.admission_id == admission_id).order_by(
                IpdMedication.id.desc()).all())
        # Always return a list (never None)
        return rows or []
    except SQLAlchemyError as e:
        logger.exception("Failed to list medications for admission_id=%s",
                         admission_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load medications. Please try again.",
        ) from e


@router.post(
    "/admissions/{admission_id}/medications",
    response_model=IpdMedicationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ipd_medication(
        admission_id: int,
        payload: IpdMedicationCreate,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = IpdMedication(
        admission_id=admission_id,
        drug_name=payload.drug_name,
        route=payload.route or "oral",
        frequency=payload.frequency or "od",
        dose=payload.dose,
        start_date=payload.start_date,
        end_date=payload.end_date,
        instructions=payload.instructions,
        status=payload.status or "active",
        created_by_id=user.id if getattr(user, "id", None) else None,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put(
    "/admissions/{admission_id}/medications/{med_id}",
    response_model=IpdMedicationOut,
)
def update_ipd_medication(
        admission_id: int,
        med_id: int,
        payload: IpdMedicationUpdate,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = (db.query(IpdMedication).filter(
        IpdMedication.id == med_id,
        IpdMedication.admission_id == admission_id,
    ).first())
    if not obj:
        raise HTTPException(status_code=404, detail="Medication not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj


# ------------------------------
# Discharge Medications
# ------------------------------
@router.get(
    "/admissions/{admission_id}/discharge-meds",
    response_model=list[IpdDischargeMedicationOut],
)
def list_ipd_discharge_meds(
        admission_id: int,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    rows = (db.query(IpdDischargeMedication).filter(
        IpdDischargeMedication.admission_id == admission_id).order_by(
            IpdDischargeMedication.id.asc()).all())
    return rows


@router.post(
    "/admissions/{admission_id}/discharge-meds",
    response_model=IpdDischargeMedicationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ipd_discharge_med(
        admission_id: int,
        payload: IpdDischargeMedicationCreate,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = IpdDischargeMedication(
        admission_id=admission_id,
        drug_name=payload.drug_name,
        dose=payload.dose,
        dose_unit=payload.dose_unit or "",
        route=payload.route or "",
        frequency=payload.frequency or "",
        duration_days=payload.duration_days,
        advice_text=payload.advice_text or "",
        created_by_id=user.id if getattr(user, "id", None) else None,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# ------------------------------
# Admission Feedback (upsert)
# ------------------------------
@router.get(
    "/admissions/{admission_id}/feedback",
    response_model=IpdAdmissionFeedbackOut,
)
def get_ipd_feedback(
        admission_id: int,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = (db.query(IpdAdmissionFeedback).filter(
        IpdAdmissionFeedback.admission_id == admission_id).first())
    if not obj:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return obj


@router.post(
    "/admissions/{admission_id}/feedback",
    response_model=IpdAdmissionFeedbackOut,
)
def save_ipd_feedback(
        admission_id: int,
        payload: IpdAdmissionFeedbackCreate,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = (db.query(IpdAdmissionFeedback).filter(
        IpdAdmissionFeedback.admission_id == admission_id).first())
    if obj is None:
        obj = IpdAdmissionFeedback(
            admission_id=admission_id,
            created_by_id=user.id if getattr(user, "id", None) else None,
        )
        db.add(obj)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)

    db.commit()
    db.refresh(obj)
    return obj
