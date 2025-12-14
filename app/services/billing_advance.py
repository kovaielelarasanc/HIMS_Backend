# FILE: app/services/billing_advance.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.billing import Advance, AdvanceAdjustment, Invoice

# ---------- helpers ----------
TWOPLACES = Decimal("0.01")


def D(x) -> Decimal:
    """Safe Decimal conversion (never Decimal -= float)."""
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))  # IMPORTANT: str() avoids float binary issues


def money(x) -> Decimal:
    """Money rounding to 2 decimals."""
    return D(x).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# ---------- GET PATIENT ADVANCE SUMMARY ----------
def get_patient_advance_summary(db: Session, patient_id: int):
    total = db.query(func.coalesce(func.sum(Advance.amount), 0)).filter(
        Advance.patient_id == patient_id,
        Advance.is_voided.is_(False),
    ).scalar()

    remaining = db.query(func.coalesce(func.sum(Advance.balance_remaining),
                                       0)).filter(
                                           Advance.patient_id == patient_id,
                                           Advance.is_voided.is_(False),
                                       ).scalar()

    total_d = money(total)
    remaining_d = money(remaining)
    used_d = money(total_d - remaining_d)

    # If your API schema expects floats, keep floats here.
    return {
        "patient_id": patient_id,
        "total_advance": float(total_d),
        "used_advance": float(used_d),
        "available_advance": float(remaining_d),
    }


# ---------- APPLY ADVANCE TO INVOICE ----------
def apply_advance_to_invoice(
    db: Session,
    *,
    invoice: Invoice,
    amount,  # can be float/str/Decimal
    user_id: int,
) -> Decimal:
    inv_due = money(invoice.balance_due)

    if inv_due <= 0:
        raise ValueError("Invoice has no balance due")

    req_amt = money(amount)
    if req_amt <= 0:
        raise ValueError("Amount must be > 0")

    remaining_to_apply = money(min(req_amt, inv_due))

    advances = (db.query(Advance).filter(
        Advance.patient_id == invoice.patient_id,
        Advance.balance_remaining > 0,
        Advance.is_voided.is_(False),
    ).order_by(Advance.received_at.asc()).all())

    if not advances:
        raise ValueError("No available advance balance")

    # ✅ safe Decimal sum
    total_available = money(
        sum((money(a.balance_remaining) for a in advances), Decimal("0")))

    if remaining_to_apply > total_available:
        raise ValueError("Advance amount exceeds available balance")

    applied_total = Decimal("0.00")

    for adv in advances:
        if remaining_to_apply <= 0:
            break

        adv_rem = money(adv.balance_remaining)
        if adv_rem <= 0:
            continue

        use_amt = money(min(adv_rem, remaining_to_apply))
        if use_amt <= 0:
            continue

        adj = AdvanceAdjustment(
            advance_id=adv.id,
            invoice_id=invoice.id,
            amount_applied=use_amt,  # ✅ Decimal
            applied_at=datetime.utcnow(),
        )
        db.add(adj)

        # ✅ Decimal-safe updates
        adv.balance_remaining = money(adv_rem - use_amt)

        applied_total = money(applied_total + use_amt)
        remaining_to_apply = money(remaining_to_apply - use_amt)

    # Update invoice (Decimal-safe)
    invoice.advance_adjusted = money(
        D(invoice.advance_adjusted) + applied_total)
    invoice.balance_due = money(inv_due - applied_total)

    # optional: audit if you want
    # invoice.updated_by = user_id
    # invoice.updated_at = datetime.utcnow()

    db.add(invoice)
    for adv in advances:
        db.add(adv)

    # do NOT commit here if your route controls transaction; flush is enough
    db.flush()

    return applied_total
