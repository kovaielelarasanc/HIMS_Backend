# FILE: app/api/routes_ipd_discharge.py
from __future__ import annotations

import io
from datetime import date, datetime
from typing import List, Optional, Dict, Any

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
from app.services.ipd_billing import (
    apply_ipd_bed_charges_to_invoice,
    ensure_invoice_for_context,
)

router = APIRouter(prefix="/ipd", tags=["IPD – Discharge"])


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
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


def _build_demographics_text(adm: IpdAdmission,
                             patient: Optional[Patient]) -> str:
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

        age = getattr(patient, "age_years", None) or getattr(
            patient, "age", None)
        sex = getattr(patient, "gender", None) or getattr(patient, "sex", None)
        if age is not None or sex:
            age_str = f"{age} yrs" if age is not None else ""
            if age_str and sex:
                lines.append(f"Age / Sex: {age_str} / {sex}")
            elif age_str:
                lines.append(f"Age: {age_str}")
            else:
                lines.append(f"Sex: {sex}")

        uhid = getattr(patient, "uhid", None) or getattr(
            patient, "patient_code", None)
        if uhid:
            lines.append(f"UHID: {uhid}")

    display_code = getattr(adm, "display_code", None) or f"IP-{adm.id:06d}"
    lines.append(f"IP No.: {display_code}")

    if adm.admitted_at:
        lines.append("Admission: " +
                     adm.admitted_at.strftime("%d-%m-%Y %H:%M"))

    if getattr(adm, "status", None):
        lines.append(f"Status: {adm.status}")

    return "\n".join(lines)


def _build_followup_text_from_opd(db: Session, adm: IpdAdmission) -> str:
    try:
        from app.models.opd import FollowUp  # type: ignore
    except Exception:
        return ""

    try:
        q = (db.query(FollowUp).filter(
            getattr(FollowUp, "patient_id") == adm.patient_id).order_by(
                getattr(FollowUp, "id").desc()).first())
    except Exception:
        return ""

    if not q:
        return ""

    try:
        dt = (getattr(q, "followup_date", None)
              or getattr(q, "scheduled_date", None)
              or getattr(q, "date", None))
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
    if not (obj.demographics or "").strip():
        obj.demographics = _build_demographics_text(adm, patient)

    if not (obj.follow_up or "").strip():
        auto_fu = _build_followup_text_from_opd(db, adm)
        if auto_fu:
            obj.follow_up = auto_fu

    if not (obj.prepared_by_name or "").strip():
        obj.prepared_by_name = _user_display_name(current_user)

    if not (obj.reviewed_by_name or "").strip():
        consultant_user = None
        try:
            if getattr(adm, "practitioner_user_id", None):
                consultant_user = db.query(User).get(adm.practitioner_user_id)
        except Exception:
            consultant_user = None

        obj.reviewed_by_name = _user_display_name(
            consultant_user) or _user_display_name(current_user)


def _close_open_bed_assignment(db: Session, admission_id: int,
                               stop_ts: datetime) -> None:
    last_assign = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id,
        IpdBedAssignment.to_ts.is_(None),
    ).order_by(IpdBedAssignment.id.desc()).first())
    if last_assign:
        last_assign.to_ts = stop_ts


def _free_current_bed_and_close_assignment(db: Session, adm: IpdAdmission,
                                           stop_ts: datetime) -> None:
    _close_open_bed_assignment(db, adm.id, stop_ts)

    if adm.current_bed_id:
        bed = db.query(IpdBed).get(adm.current_bed_id)
        if bed:
            bed.state = "vacant"

    adm.current_bed_id = None


def _mark_admission_status_and_release_bed(
    db: Session,
    adm: IpdAdmission,
    new_status: str,
    stop_ts: datetime,
) -> None:
    if new_status not in ("cancelled", "lama", "dama", "disappeared",
                          "discharged"):
        raise HTTPException(400, "Invalid status")

    adm.status = new_status

    # Keep discharge_at consistent (recommended)
    adm.discharge_at = stop_ts

    _free_current_bed_and_close_assignment(db, adm, stop_ts)


def _finalize_discharge_and_billing(
    db: Session,
    adm: IpdAdmission,
    user: User,
    stop_ts: datetime,
    finalize_invoice: bool = False,
) -> Dict[str, Any]:
    """
    Canonical discharge finalization:
    - mark admission discharged
    - release bed + close assignment at exact stop_ts
    - ensure invoice exists
    - create/update bed charge invoice items up to stop date
    - recalc + optionally finalize invoice
    - lock billing at admission level
    Idempotent if already discharged.
    """
    if adm.status == "discharged" or getattr(adm, "billing_locked", False):
        # idempotent response
        inv = ensure_invoice_for_context(
            db=db,
            patient_id=adm.patient_id,
            billing_type="ip_billing",
            context_type="ipd",
            context_id=adm.id,
        )
        return {
            "message": "Already discharged",
            "admission_id": adm.id,
            "invoice_id": inv.id,
            "invoice_status": inv.status,
            "billing_locked": getattr(adm, "billing_locked", False),
            "discharge_at": getattr(adm, "discharge_at", None),
        }

    # 1) discharge + bed release
    _mark_admission_status_and_release_bed(db, adm, "discharged", stop_ts)

    # 2) invoice
    inv = ensure_invoice_for_context(
        db=db,
        patient_id=adm.patient_id,
        billing_type="ip_billing",
        context_type="ipd",
        context_id=adm.id,
    )

    # 3) apply bed charges (creates/updates InvoiceItem rows)
    apply_ipd_bed_charges_to_invoice(
        db=db,
        admission_id=adm.id,
        upto_date=stop_ts.date(),
        user_id=user.id,
        tax_rate=0.0,
        invoice_id=inv.id,
        skip_if_already_billed=False,  # keep false so corrections re-sync
    )

    # 4) recalc totals
    inv.recalc()
    db.add(inv)

    # 5) finalize invoice (optional)
    if finalize_invoice:
        inv.status = inv.status or "draft"
        inv.finalized_at = datetime.utcnow()
        inv.finalized_by = user.id

    # 6) lock admission billing
    adm.billing_locked = True
    adm.billing_locked_at = datetime.utcnow()
    adm.billing_locked_by = user.id
    adm.discharge_at = stop_ts

    return {
        "message": "Discharged successfully",
        "admission_id": adm.id,
        "invoice_id": inv.id,
        "invoice_status": inv.status,
        "billing_locked": adm.billing_locked,
        "discharge_at": adm.discharge_at,
    }


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

    obj = (db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first())
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
    Create / Update IPD Discharge Summary.
    If payload.finalize = true:
      - sets discharge_datetime
      - calls canonical discharge finalizer (billing + lock)
      - locks summary
    """

    _need_any(user, ["ipd.doctor", "ipd.manage"])

    adm = _get_admission_or_404(db, admission_id)
    patient = db.query(Patient).get(adm.patient_id)

    obj = (db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first())
    if not obj:
        obj = IpdDischargeSummary(admission_id=admission_id)
        db.add(obj)

    # Update fields (except finalize flag)
    data = payload.model_dump(exclude={"finalize"}, exclude_unset=True)
    for field, value in data.items():
        setattr(obj, field, value if value is not None else "")

    _auto_fill_calculated_fields(db, obj, adm, patient, user)

    if payload.finalize and not obj.finalized:
        disc_ts = obj.discharge_datetime or datetime.utcnow()
        obj.discharge_datetime = disc_ts

        # canonical discharge + billing + lock
        _finalize_discharge_and_billing(
            db=db,
            adm=adm,
            user=user,
            stop_ts=disc_ts,
            finalize_invoice=False,
        )

        obj.finalized = True
        obj.finalized_by = user.id
        obj.finalized_at = datetime.utcnow()

    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------
# Dedicated Discharge Endpoint (Optional UI action)
# ---------------------------------------------------------
@router.patch("/admissions/{admission_id}/discharge")
def discharge_admission(
        admission_id: int,
        discharge_at: Optional[datetime] = None,
        finalize_invoice: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.manage"])

    adm = _get_admission_or_404(db, admission_id)

    stop_ts = discharge_at or datetime.utcnow()

    result = _finalize_discharge_and_billing(
        db=db,
        adm=adm,
        user=user,
        stop_ts=stop_ts,
        finalize_invoice=finalize_invoice,
    )

    db.commit()
    return result


# ---------------------------------------------------------
# Discharge PDF (allowed even after discharge)
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
        raise HTTPException(404, "Admission not found")

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

    return (db.query(IpdDischargeChecklist).filter(
        IpdDischargeChecklist.admission_id == admission_id).first())


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

    chk = (db.query(IpdDischargeChecklist).filter(
        IpdDischargeChecklist.admission_id == admission_id).first())
    if not chk:
        chk = IpdDischargeChecklist(admission_id=admission_id)
        db.add(chk)

    data = payload.model_dump(exclude_unset=True)

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
        _need_any(user, ["ipd.manage"])
        chk.submitted = True
        chk.submitted_at = datetime.utcnow()

    db.commit()
    db.refresh(chk)
    return chk


# ---------------------------------------------------------
# Discharge Queue
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
        ) for r in rows
    ]


# ---------------------------------------------------------
# Special status (LAMA / DAMA / disappeared)
# ---------------------------------------------------------
@router.patch("/admissions/{admission_id}/mark-status")
def mark_special_status(
        admission_id: int,
        status: str = Query(..., pattern="^(lama|dama|disappeared)$"),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.manage"])

    adm = _get_admission_or_404(db, admission_id)

    stop_ts = datetime.utcnow()
    _mark_admission_status_and_release_bed(db, adm, status, stop_ts)

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

    ds = (db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first())
    if not ds or not ds.finalized:
        raise HTTPException(status_code=400,
                            detail="Finalize discharge summary first")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

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

    rows = (db.query(IpdDischargeMedication).filter(
        IpdDischargeMedication.admission_id == admission_id).order_by(
            IpdDischargeMedication.id.asc()).all())
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

    obj = (db.query(IpdDischargeMedication).filter(
        IpdDischargeMedication.id == med_id,
        IpdDischargeMedication.admission_id == admission_id,
    ).first())
    if not obj:
        raise HTTPException(status_code=404,
                            detail="Discharge medication not found")

    db.delete(obj)
    db.commit()
    return None


@router.get(
    "/admissions/{admission_id}/followups",
    response_model=List[dict],
)
def list_followups_for_admission(
        admission_id: int,
        db: Session = Depends(get_db),
        user=Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "ipd.doctor", "ipd.manage"])
    adm = _get_admission_or_404(db, admission_id)

    try:
        from app.models.opd import OpdAppointment  # type: ignore
    except Exception:
        return []

    rows = (db.query(OpdAppointment).filter(
        OpdAppointment.patient_id == adm.patient_id).order_by(
            OpdAppointment.visit_date.desc()).limit(50).all())

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
