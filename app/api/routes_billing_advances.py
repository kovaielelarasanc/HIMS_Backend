from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.billing import Advance, Invoice
from app.models.patient import Patient
from app.schemas.billing_advances import (
    AdvanceCreate,
    AdvanceOut,
    ApplyAdvanceIn,
)
from app.services.billing_advance import (
    get_patient_advance_summary,
    apply_advance_to_invoice,
)

from app.models.user import User as UserModel
from app.models.billing import Advance, AdvanceAdjustment, Invoice
from app.schemas.billing_advances import AdvanceOut, AdvanceAdjustmentMiniOut
from sqlalchemy import desc
router = APIRouter(prefix="/billing/advances", tags=["Billing Advances"])


# ---------- CREATE ADVANCE ----------
@router.post("", response_model=AdvanceOut)
def create_advance(
        payload: AdvanceCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    patient = db.get(Patient, payload.patient_id)
    if not patient:
        raise HTTPException(404, "Patient not found")

    adv = Advance(
        patient_id=payload.patient_id,
        amount=payload.amount,
        balance_remaining=payload.amount,
        mode=payload.mode,
        reference_no=payload.reference_no,
        remarks=payload.remarks,
        context_type=payload.context_type,
        context_id=payload.context_id,
        created_by=user.id,
    )

    db.add(adv)
    db.commit()
    db.refresh(adv)
    return adv


# ---------- LIST ADVANCES BY PATIENT ----------


@router.get("/patient/{patient_id}", response_model=list[AdvanceOut])
def list_patient_advances(patient_id: int,
                          db: Session = Depends(get_db),
                          user: UserModel = Depends(current_user)):
    rows = (db.query(Advance).filter(
        Advance.patient_id == patient_id).order_by(desc(
            Advance.received_at)).all())

    if not rows:
        return []

    # Collect advance ids
    adv_ids = [a.id for a in rows]

    # Load adjustments + invoice mini
    adjs = (db.query(AdvanceAdjustment, Invoice).join(
        Invoice, Invoice.id == AdvanceAdjustment.invoice_id).filter(
            AdvanceAdjustment.advance_id.in_(adv_ids)).order_by(
                desc(AdvanceAdjustment.applied_at)).all())

    # Map advance_id -> list of adjustments
    used_map: dict[int, list[AdvanceAdjustmentMiniOut]] = {}
    for adj, inv in adjs:
        used_map.setdefault(adj.advance_id, []).append(
            AdvanceAdjustmentMiniOut(
                id=adj.id,
                invoice_id=adj.invoice_id,
                amount_applied=adj.amount_applied,
                applied_at=adj.applied_at,
                invoice_number=inv.invoice_number,
                invoice_uid=inv.invoice_uid,
                billing_type=inv.billing_type,
                status=inv.status,
                net_total=inv.net_total,
                balance_due=inv.balance_due,
            ))

    # Attach used list
    out: list[AdvanceOut] = []
    for a in rows:
        item = AdvanceOut.model_validate(a)
        item.used_invoices = used_map.get(a.id, [])
        out.append(item)

    return out


# ---------- PATIENT ADVANCE SUMMARY ----------
@router.get("/patient/{patient_id}/summary")
def patient_advance_summary(
        patient_id: int,
        db: Session = Depends(get_db),
):
    return get_patient_advance_summary(db, patient_id)


# ---------- APPLY ADVANCE TO INVOICE ----------
@router.post("/apply/{invoice_id}")
def apply_advance(
        invoice_id: int,
        payload: ApplyAdvanceIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    try:
        applied = apply_advance_to_invoice(
            db=db,
            invoice=invoice,
            amount=payload.amount,
            user_id=user.id,
        )
        db.commit()
        return {
            "invoice_id": invoice.id,
            "applied_amount": applied,
            "balance_due": float(invoice.balance_due),
        }
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
