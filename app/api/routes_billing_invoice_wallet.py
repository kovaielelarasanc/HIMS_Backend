from __future__ import annotations

from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User as UserModel

from app.models.billing_wallet import PatientWalletAllocation
from app.schemas.billing_wallet import ApplyWalletToInvoiceIn
from app.services.billing_wallet import get_wallet_totals

# ✅ Your invoice model
from app.models.billing import Invoice  # <-- matches your provided file

router = APIRouter(prefix="/billing/invoices", tags=["Billing Wallet Apply"])


def recalc_invoice_balance(inv: Invoice) -> None:
    net_total = Decimal(inv.net_total or 0)
    prev = Decimal(inv.previous_balance_snapshot or 0)
    paid = Decimal(inv.amount_paid or 0)
    adv = Decimal(inv.advance_adjusted or 0)
    inv.balance_due = (net_total + prev) - paid - adv


@router.post("/{invoice_id}/apply-wallet")
def apply_wallet_to_invoice(
        invoice_id: int,
        body: ApplyWalletToInvoiceIn,
        db: Session = Depends(get_db),
        user: UserModel = Depends(current_user),
):
    amt = Decimal(body.amount)
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    # lock invoice row to prevent double apply in concurrency
    inv = (db.query(Invoice).filter(
        Invoice.id == invoice_id).with_for_update().first())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if inv.status in ("cancelled", "finalized"):
        raise HTTPException(status_code=400,
                            detail=f"{inv.status} invoice cannot be modified")

    patient_id = int(inv.patient_id)

    totals = get_wallet_totals(db, patient_id)
    available = Decimal(totals["available_balance"] or 0)

    if available <= 0:
        raise HTTPException(status_code=400,
                            detail="No available advance/deposit balance")

    if amt > available:
        raise HTTPException(
            status_code=400,
            detail=f"Apply amount exceeds available balance ({available})")

    bal = Decimal(inv.balance_due or 0)
    if bal <= 0:
        raise HTTPException(status_code=400,
                            detail="Invoice has no balance due")

    if amt > bal:
        raise HTTPException(
            status_code=400,
            detail=f"Apply amount exceeds invoice balance due ({bal})")

    try:
        alloc = PatientWalletAllocation(
            patient_id=patient_id,
            invoice_id=inv.id,
            amount=amt,
            notes=body.notes,
            allocated_by=getattr(user, "id", None),
        )
        db.add(alloc)

        inv.advance_adjusted = Decimal(inv.advance_adjusted or 0) + amt
        recalc_invoice_balance(inv)

        db.add(inv)
        db.commit()
        db.refresh(inv)

        return {
            "ok": True,
            "invoice_id": inv.id,
            "patient_id": patient_id,
            "applied_amount": float(amt),
            "available_before": float(available),
            "new_advance_adjusted": float(inv.advance_adjusted or 0),
            "new_balance_due": float(inv.balance_due or 0),
        }
    except Exception:
        db.rollback()
        raise
