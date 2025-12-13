from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import case

from app.models.accounts_supplier import (
    SupplierInvoice,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from app.models.pharmacy_inventory import GRN  # your GRN model


D0 = Decimal("0.00")
Q2 = Decimal("0.01")

DEBUG_SUPP_SYNC = True

def dbg(*args):
    print("[SUPP-INVOICE]", *args)

def _d(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else 0)).quantize(Q2)
    except Exception:
        return D0

def _has(model, name: str) -> bool:
    return hasattr(model, name)


def compute_invoice_status(inv: SupplierInvoice) -> None:
    inv.invoice_amount = _d(getattr(inv, "invoice_amount", 0))
    inv.paid_amount = _d(getattr(inv, "paid_amount", 0))

    if (getattr(inv, "status", "") or "").upper() == "CANCELLED":
        inv.outstanding_amount = D0
        inv.is_overdue = False
        return

    outstanding = _d(inv.invoice_amount - inv.paid_amount)
    inv.outstanding_amount = max(D0, outstanding)

    if inv.outstanding_amount <= D0:
        inv.status = "PAID"
        inv.outstanding_amount = D0
    elif inv.paid_amount > D0:
        inv.status = "PARTIAL"
    else:
        inv.status = "UNPAID"

    due = getattr(inv, "due_date", None)
    inv.is_overdue = bool(due and inv.outstanding_amount > D0 and due < date.today())


def sync_supplier_invoice_from_grn(db, grn, user_id=None):
    """
    SIMPLE, TENANT-FREE
    - One SupplierInvoice per GRN
    - Called ONLY when GRN is POSTED
    """

    print("[SUPP-INVOICE] START grn_id=", grn.id, "grn_number=", grn.grn_number)

    # ✅ HARD VALIDATION (fail fast)
    if not grn.supplier_id:
        raise ValueError("GRN.supplier_id is missing")
    if not grn.location_id:
        raise ValueError("GRN.location_id is missing")

    # ✅ idempotent fetch
    inv = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.grn_id == grn.id)
        .first()
    )

    if not inv:
        # ✅ SET ALL REQUIRED FIELDS FIRST
        inv = SupplierInvoice(
            grn_id=grn.id,
            supplier_id=grn.supplier_id,
            location_id=grn.location_id,
            grn_number=grn.grn_number,
            invoice_number=grn.invoice_number or "",
            invoice_date=grn.invoice_date,
            invoice_amount=_d(grn.supplier_invoice_amount),
            paid_amount=D0,
            outstanding_amount=_d(grn.supplier_invoice_amount),
            status="UNPAID",
            notes=grn.notes or "",
        )

        db.add(inv)
        db.flush()  # ✅ SAFE now
        print("[SUPP-INVOICE] CREATED invoice_id=", inv.id)

    else:
        # ✅ UPDATE PATH
        inv.supplier_id = grn.supplier_id
        inv.location_id = grn.location_id
        inv.grn_number = grn.grn_number
        inv.invoice_number = grn.invoice_number or ""
        inv.invoice_date = grn.invoice_date
        inv.invoice_amount = _d(grn.supplier_invoice_amount)
        inv.outstanding_amount = _d(inv.invoice_amount - _d(inv.paid_amount))
        inv.notes = grn.notes or ""

        print("[SUPP-INVOICE] UPDATED invoice_id=", inv.id)

    # ✅ due date (optional)
    if inv.invoice_date and not inv.due_date:
        inv.due_date = inv.invoice_date + timedelta(days=30)

    # ✅ status compute
    if inv.outstanding_amount <= D0:
        inv.status = "PAID"
        inv.outstanding_amount = D0
    elif inv.paid_amount > D0:
        inv.status = "PARTIAL"
    else:
        inv.status = "UNPAID"

    print(
        "[SUPP-INVOICE] DONE",
        "invoice_id=", inv.id,
        "supplier_id=", inv.supplier_id,
        "amount=", inv.invoice_amount,
        "status=", inv.status,
    )

    return inv



def _validate_invoice_allocatable(inv: SupplierInvoice, grn: Optional[GRN]) -> None:
    if (getattr(inv, "status", "") or "").upper() == "CANCELLED":
        raise ValueError(f"Invoice {getattr(inv, 'id', '?')} is CANCELLED; cannot allocate payment.")

    if grn and (getattr(grn, "status", "") or "").upper() == "CANCELLED":
        raise ValueError(f"GRN {getattr(grn, 'grn_number', None) or grn.id} is CANCELLED; cannot allocate payment.")

    if grn and (getattr(grn, "status", "") or "").upper() != "POSTED":
        raise ValueError(f"GRN {getattr(grn, 'grn_number', None) or grn.id} is not POSTED; cannot allocate payment.")


def auto_allocate_oldest_first(
    db: Session,
    supplier_id: int,
    amount: Decimal,
) -> List[Tuple[int, Decimal]]:
    remaining = _d(amount)
    if remaining <= D0:
        return []

    invs = (
        db.query(SupplierInvoice)
        .filter(
            SupplierInvoice.supplier_id == supplier_id,
            SupplierInvoice.status.in_(["UNPAID", "PARTIAL"]),
        )
        .order_by(
            case((SupplierInvoice.invoice_date.is_(None), 1), else_=0),
            SupplierInvoice.invoice_date.asc(),
            SupplierInvoice.id.asc(),
        )
        .all()
    )

    out: List[Tuple[int, Decimal]] = []
    for inv in invs:
        compute_invoice_status(inv)
        if inv.outstanding_amount <= D0:
            continue
        take = min(remaining, _d(inv.outstanding_amount))
        if take > D0:
            out.append((inv.id, take))
            remaining = _d(remaining - take)
        if remaining <= D0:
            break

    return out


def allocate_payment_to_invoices(
    db: Session,
    payment: SupplierPayment,
    allocations: List[Tuple[int, Decimal]],
) -> None:
    payment.amount = _d(getattr(payment, "amount", 0))
    if payment.amount <= D0:
        raise ValueError("Payment amount must be > 0")

    if not allocations:
        payment.allocated_amount = D0
        payment.advance_amount = payment.amount
        return

    invoice_ids = [int(i) for i, _ in allocations]

    q = db.query(SupplierInvoice)
    if hasattr(SupplierInvoice, "grn"):
        q = q.options(joinedload(SupplierInvoice.grn))

    invoices = q.filter(SupplierInvoice.id.in_(invoice_ids)).with_for_update().all()
    inv_map = {inv.id: inv for inv in invoices}

    missing = [i for i in invoice_ids if i not in inv_map]
    if missing:
        raise ValueError(f"Invoice not found: {missing}")

    remaining = _d(payment.amount)
    allocated_total = D0
    seen = set()

    for inv_id, amt in allocations:
        inv_id = int(inv_id)
        if inv_id in seen:
            raise ValueError(f"Duplicate invoice in allocations: {inv_id}")
        seen.add(inv_id)

        amt = _d(amt)
        if amt <= D0:
            continue

        inv = inv_map[inv_id]

        if int(inv.supplier_id) != int(payment.supplier_id):
            raise ValueError(f"Invoice {inv.id} does not belong to selected supplier.")

        grn = getattr(inv, "grn", None)
        if grn is None and hasattr(inv, "grn_id") and getattr(inv, "grn_id", None):
            grn = db.get(GRN, inv.grn_id)

        _validate_invoice_allocatable(inv, grn)

        compute_invoice_status(inv)
        outstanding = _d(inv.outstanding_amount)
        if outstanding <= D0:
            raise ValueError(f"Invoice {inv.invoice_number or inv.grn_number} already PAID.")

        if amt > remaining:
            amt = remaining

        if amt > outstanding:
            raise ValueError(
                f"Overpayment not allowed for invoice {inv.invoice_number or inv.grn_number}. "
                f"Outstanding={outstanding}, tried={amt}"
            )

        exists = (
            db.query(SupplierPaymentAllocation.id)
            .filter(
                SupplierPaymentAllocation.payment_id == payment.id,
                SupplierPaymentAllocation.invoice_id == inv.id,
            )
            .first()
        )
        if exists:
            raise ValueError(f"Duplicate allocation detected for invoice {inv.id} in this payment.")

        db.add(SupplierPaymentAllocation(payment_id=payment.id, invoice_id=inv.id, amount=amt))

        inv.paid_amount = _d(inv.paid_amount + amt)
        inv.last_payment_date = getattr(payment, "payment_date", None)
        compute_invoice_status(inv)

        allocated_total = _d(allocated_total + amt)
        remaining = _d(remaining - amt)
        if remaining <= D0:
            break

    payment.allocated_amount = _d(allocated_total)
    payment.advance_amount = max(D0, _d(payment.amount - allocated_total))