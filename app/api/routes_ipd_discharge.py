# FILE: app/api/routes_ipd_discharge.py
# FILE: app/api/routes_ipd_discharge.py
from __future__ import annotations

import io
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import current_user as auth_current_user, get_db
from app.models.ipd import (
    IpdAdmission,
    IpdDischargeSummary,
    IpdDischargeChecklist,
    IpdDischargeMedication,
    IpdBed,
    IpdBedAssignment,
)
from app.models.patient import Patient
from app.models.user import User
from app.schemas.ipd import (
    DischargeSummaryIn,
    DischargeSummaryOut,
    DischargeChecklistIn,
    DischargeChecklistOut,
    DueDischargeOut,
    DischargeMedicationIn,
    DischargeMedicationOut,
)
from app.services.pdf_discharge import generate_discharge_summary_pdf

# auto-billing helper (if present)
try:
    from app.services.billing_ipd import auto_finalize_ipd_on_discharge
except Exception:  # pragma: no cover
    auto_finalize_ipd_on_discharge = None  # type: ignore

router = APIRouter(prefix="/ipd", tags=["IPD – Discharge"])


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")
    return adm

def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in user.roles:
        for p in r.permissions:
            if p.code in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")

def _user_display_name(u: Optional[User]) -> str:
    if not u:
        return ""
    first = getattr(u, "first_name", "") or ""
    last = getattr(u, "last_name", "") or ""
    full = (first + " " + last).strip()
    if full:
        return full
    full2 = getattr(u, "full_name", "") or ""
    if full2:
        return full2
    username = getattr(u, "username", "") or getattr(u, "email", "") or ""
    if username:
        return username
    return f"User #{getattr(u, 'id', '')}"


def _build_demographics_text(adm: IpdAdmission, patient: Optional[Patient]) -> str:
    """
    Build a neat demographics block from IPD admission + patient table.
    """
    lines: List[str] = []

    if patient:
        name_parts = [
            getattr(patient, "prefix", "") or "",
            getattr(patient, "first_name", "") or "",
            getattr(patient, "last_name", "") or "",
        ]
        name = " ".join([p for p in name_parts if p]).strip()
        if name:
            lines.append(f"Name: {name}")

        # Age / sex
        age = getattr(patient, "age_years", None) or getattr(patient, "age", None)
        sex = getattr(patient, "gender", None) or getattr(patient, "sex", None)
        if age is not None or sex:
            age_str = f"{age} yrs" if age is not None else ""
            if age_str and sex:
                lines.append(f"Age / Sex: {age_str} / {sex}")
            elif age_str:
                lines.append(f"Age: {age_str}")
            else:
                lines.append(f"Sex: {sex}")

        uhid = getattr(patient, "uhid", None) or getattr(patient, "patient_code", None)
        if uhid:
            lines.append(f"UHID: {uhid}")

    # IP details
    display_code = getattr(adm, "display_code", None) or f"IP-{adm.id:06d}"
    lines.append(f"IP No.: {display_code}")

    if adm.admitted_at:
        lines.append("Admission: " + adm.admitted_at.strftime("%d-%m-%Y %H:%M"))

    if getattr(adm, "status", None):
        lines.append(f"Status: {adm.status}")

    return "\n".join(lines)


def _build_followup_text_from_opd(db: Session, adm: IpdAdmission) -> str:
    """
    Try to build follow-up instructions from OPD follow-up table.
    This is wrapped in try/except so it NEVER breaks the API if models differ.
    """
    try:
        from app.models.opd import FollowUp  # type: ignore
    except Exception:
        return ""

    try:
        q = (
            db.query(FollowUp)
            .filter(getattr(FollowUp, "patient_id") == adm.patient_id)
            .order_by(getattr(FollowUp, "id").desc())
            .first()
        )
    except Exception:
        return ""

    if not q:
        return ""

    try:
        dt = (
            getattr(q, "followup_date", None)
            or getattr(q, "scheduled_date", None)
            or getattr(q, "date", None)
        )
        reason = getattr(q, "reason", None) or getattr(q, "notes", None) or ""
        parts: List[str] = []
        if dt:
            parts.append("Next follow-up: " + dt.strftime("%d-%m-%Y"))
        if reason:
            parts.append(f"Reason: {reason}")
        return " | ".join(parts)
    except Exception:
        return ""


def _auto_fill_calculated_fields(
    db: Session,
    obj: IpdDischargeSummary,
    adm: IpdAdmission,
    patient: Optional[Patient],
    current_user: User,
) -> None:
    # 1) Demographics from admission + patient (if empty)
    if not (obj.demographics or "").strip():
        obj.demographics = _build_demographics_text(adm, patient)

    # 2) Follow-up from OPD follow-ups (if empty)
    if not (obj.follow_up or "").strip():
        auto_fu = _build_followup_text_from_opd(db, adm)
        if auto_fu:
            obj.follow_up = auto_fu

    # 3) Prepared by name from current user (if empty)
    if not (obj.prepared_by_name or "").strip():
        obj.prepared_by_name = _user_display_name(current_user)

    # 4) Reviewed by name from primary consultant (or current user)
    if not (obj.reviewed_by_name or "").strip():
        consultant_user = None
        try:
            if getattr(adm, "practitioner_user_id", None):
                consultant_user = db.query(User).get(adm.practitioner_user_id)
        except Exception:
            consultant_user = None

        obj.reviewed_by_name = _user_display_name(consultant_user) or _user_display_name(
            current_user
        )


# ---------------------------------------------------------
# Discharge Summary
# ---------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/discharge-summary",
    response_model=Optional[DischargeSummaryOut],
)
def get_discharge_summary(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )
    return obj


@router.post(
    "/admissions/{admission_id}/discharge-summary",
    response_model=DischargeSummaryOut,
    status_code=status.HTTP_200_OK,
)
def save_discharge_summary(
    admission_id: int,
    payload: DischargeSummaryIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    """
    Create / update discharge summary.
    If payload.finalize = true and not yet finalized:
      - Validate mandatory fields
      - Mark finalized
      - Optionally free bed + update admission status + auto billing
    """
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    adm = _get_admission_or_404(db, admission_id)
    patient = db.query(Patient).get(adm.patient_id)

    obj = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )

    if not obj:
        obj = IpdDischargeSummary(admission_id=admission_id)
        db.add(obj)

    data = payload.model_dump(exclude={"finalize"}, exclude_unset=True)

    for field, value in data.items():
        # Keep datetime fields as None if None; for text use empty string
        if field in {"patient_ack_datetime", "discharge_datetime"}:
            setattr(obj, field, value)
        else:
            setattr(obj, field, value if value is not None else "")

    # Auto-fill from other tables if empty
    _auto_fill_calculated_fields(db, obj, adm, patient, user)

    # Finalize logic (only forward, never un-finalize)
    if payload.finalize and not obj.finalized:
        if not (obj.final_diagnosis_primary or "").strip() or not (
            obj.hospital_course or ""
        ).strip():
            raise HTTPException(
                status_code=400,
                detail="Final diagnosis and hospital course are mandatory for finalization.",
            )

        obj.finalized = True
        obj.finalized_by = user.id
        obj.finalized_at = datetime.utcnow()

        if obj.discharge_datetime is None:
            obj.discharge_datetime = datetime.utcnow()

        # On finalization: mark admission discharged, free bed
        adm.status = "discharged"

        if adm.current_bed_id:
            bed = db.query(IpdBed).get(adm.current_bed_id)
            if bed:
                bed.state = "vacant"

            last_assign = (
                db.query(IpdBedAssignment)
                .filter(
                    IpdBedAssignment.admission_id == admission_id,
                    IpdBedAssignment.to_ts.is_(None),
                )
                .order_by(IpdBedAssignment.id.desc())
                .first()
            )
            if last_assign:
                last_assign.to_ts = datetime.utcnow()

        adm.current_bed_id = None

        # Auto-finalize billing if service is available
        if auto_finalize_ipd_on_discharge:
            try:
                auto_finalize_ipd_on_discharge(
                    db, admission_id=adm.id, user_id=user.id  # type: ignore[arg-type]
                )
            except Exception:
                # Don't block discharge if billing auto-finalize fails
                pass

    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------
# Discharge PDF
# ---------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/discharge-summary/pdf",
    response_class=StreamingResponse,
)
def download_discharge_summary_pdf(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")

    pdf_bytes = generate_discharge_summary_pdf(
        db,
        admission_id=admission_id,
        org_name="NUTRYAH DIGITAL HEALTH PRIVATE LIMITED",
        org_address="Address line 1, City",
        org_phone="Phone / Email",
    )

    display_code = getattr(adm, "display_code", None) or f"IP-{adm.id:06d}"
    filename = f"discharge_{display_code}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------
# Discharge Checklist
# ---------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/discharge-checklist",
    response_model=Optional[DischargeChecklistOut],
)
def get_discharge_checklist(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.nursing", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    return (
        db.query(IpdDischargeChecklist)
        .filter(IpdDischargeChecklist.admission_id == admission_id)
        .first()
    )


@router.post(
    "/admissions/{admission_id}/discharge-checklist",
    response_model=DischargeChecklistOut,
)
def save_discharge_checklist(
    admission_id: int,
    payload: DischargeChecklistIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.nursing", "ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    chk = (
        db.query(IpdDischargeChecklist)
        .filter(IpdDischargeChecklist.admission_id == admission_id)
        .first()
    )
    if not chk:
        chk = IpdDischargeChecklist(admission_id=admission_id)
        db.add(chk)

    data = payload.model_dump(exclude_unset=True)

    # Financial clearance
    if "financial_clearance" in data:
        chk.financial_clearance = bool(data["financial_clearance"])
        if chk.financial_clearance:
            chk.financial_cleared_by = user.id

    # Clinical clearance
    if "clinical_clearance" in data:
        chk.clinical_clearance = bool(data["clinical_clearance"])
        if chk.clinical_clearance:
            chk.clinical_cleared_by = user.id

    # Delay reason
    if "delay_reason" in data and data["delay_reason"] is not None:
        chk.delay_reason = data["delay_reason"]

    # Submit (only IPD managers)
    if payload.submit:
        _need_any(user, ["ipd.manage"])
        chk.submitted = True
        chk.submitted_at = datetime.utcnow()

    db.commit()
    db.refresh(chk)
    return chk


# ---------------------------------------------------------
# Discharge Queue (due discharges)
# ---------------------------------------------------------
@router.get("/due-discharges", response_model=List[DueDischargeOut])
def due_discharges(
    for_date: date = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.manage"])

    start = datetime.combine(for_date, datetime.min.time())
    end = datetime.combine(for_date, datetime.max.time())

    q = db.query(IpdAdmission).filter(
        IpdAdmission.status == "admitted",
        IpdAdmission.expected_discharge_at.isnot(None),
        IpdAdmission.expected_discharge_at >= start,
        IpdAdmission.expected_discharge_at <= end,
    )
    rows = q.all()

    return [
        DueDischargeOut(
            admission_id=r.id,
            patient_id=r.patient_id,
            expected_discharge_at=r.expected_discharge_at,
            status=r.status,
        )
        for r in rows
    ]


# ---------------------------------------------------------
# Special status (LAMA / DAMA / disappeared)
# ---------------------------------------------------------
@router.patch("/admissions/{admission_id}/mark-status")
def mark_special_status(
    admission_id: int,
    status: str = Query(..., regex="^(lama|dama|disappeared)$"),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.manage"])

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")

    adm.status = status

    if adm.current_bed_id:
        bed = db.query(IpdBed).get(adm.current_bed_id)
        if bed:
            bed.state = "vacant"

        last_assign = (
            db.query(IpdBedAssignment)
            .filter(
                IpdBedAssignment.admission_id == admission_id,
                IpdBedAssignment.to_ts.is_(None),
            )
            .order_by(IpdBedAssignment.id.desc())
            .first()
        )
        if last_assign:
            last_assign.to_ts = datetime.utcnow()

    adm.current_bed_id = None
    db.commit()
    return {"message": f"Admission marked {status}"}


# ---------------------------------------------------------
# ABHA linkage (stub)
# ---------------------------------------------------------
@router.post("/admissions/{admission_id}/push-to-abha")
def push_discharge_to_abha(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.manage"])

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")

    ds = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )
    if not ds or not ds.finalized:
        raise HTTPException(status_code=400, detail="Finalize discharge summary first")

    adm.abha_shared_at = datetime.utcnow()
    db.commit()
    return {
        "message": "Discharge summary pushed to ABHA (stubbed)",
        "shared_at": adm.abha_shared_at,
    }


# ---------------------------------------------------------
# Structured Discharge Medications
# ---------------------------------------------------------
@router.get(
    "/admissions/{admission_id}/discharge-medications",
    response_model=List[DischargeMedicationOut],
)
def list_discharge_medications(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.nursing", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    rows = (
        db.query(IpdDischargeMedication)
        .filter(IpdDischargeMedication.admission_id == admission_id)
        .order_by(IpdDischargeMedication.id.asc())
        .all()
    )
    return rows


@router.post(
    "/admissions/{admission_id}/discharge-medications",
    response_model=DischargeMedicationOut,
    status_code=status.HTTP_201_CREATED,
)
def add_discharge_medication(
    admission_id: int,
    payload: DischargeMedicationIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = IpdDischargeMedication(
        admission_id=admission_id,
        drug_name=payload.drug_name.strip(),
        dose=payload.dose,
        dose_unit=payload.dose_unit or "",
        route=payload.route or "",
        frequency=payload.frequency or "",
        duration_days=payload.duration_days,
        advice_text=payload.advice_text or "",
        created_by_id=user.id,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete(
    "/admissions/{admission_id}/discharge-medications/{med_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_discharge_medication(
    admission_id: int,
    med_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.doctor", "ipd.manage"])
    _get_admission_or_404(db, admission_id)

    obj = (
        db.query(IpdDischargeMedication)
        .filter(
            IpdDischargeMedication.id == med_id,
            IpdDischargeMedication.admission_id == admission_id,
        )
        .first()
    )
    if not obj:
        raise HTTPException(status_code=404, detail="Discharge medication not found")

    db.delete(obj)
    db.commit()
    return None

@router.get(
    "/admissions/{admission_id}/followups",
    response_model=List[dict],  # or a proper schema
)
def list_followups_for_admission(
    admission_id: int,
    db: Session = Depends(get_db),
    user = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])
    adm = _get_admission_or_404(db, admission_id)

    try:
        from app.models.opd import OpdAppointment  # type: ignore
    except Exception:
        return []

    rows = (
        db.query(OpdAppointment)
        .filter(OpdAppointment.patient_id == adm.patient_id)
        .order_by(OpdAppointment.visit_date.desc())
        .limit(50)
        .all()
    )

    out = []
    for r in rows:
        label_parts = []
        code = getattr(r, "appointment_code", None)
        if code:
            label_parts.append(code)
        dept = getattr(r, "department_name", None)
        if dept:
            label_parts.append(dept)
        dt = getattr(r, "visit_date", None)
        if dt:
            label_parts.append(dt.strftime("%d-%m-%Y"))
        label = " – ".join(label_parts) or f"OPD #{r.id}"
        out.append({"value": str(r.id), "label": label})

    return out