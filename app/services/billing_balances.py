# FILE: app/services/billing_balances.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable

from sqlalchemy.sql import exists
from sqlalchemy import case as sa_case, func
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,  # ✅ ADD
    BillingPayment,
    BillingPaymentAllocation,
    BillingAdvance,
    BillingAdvanceApplication,
    ReceiptStatus,
    PaymentDirection,
    AdvanceType,
)

Q2 = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _q2(x: Decimal) -> Decimal:
    return _d(x).quantize(Q2, rounding=ROUND_HALF_UP)


def _line_amount_expr():
    """
    Robustly pick a line amount column depending on your model.
    Adjust order if your canonical column differs.
    """
    if hasattr(BillingInvoiceLine, "net_amount"):
        return BillingInvoiceLine.net_amount
    if hasattr(BillingInvoiceLine, "line_total"):
        return BillingInvoiceLine.line_total
    if hasattr(BillingInvoiceLine, "amount"):
        return BillingInvoiceLine.amount
    if hasattr(BillingInvoiceLine, "total"):
        return BillingInvoiceLine.total
    # fallback (raise early so you notice in dev)
    raise RuntimeError(
        "BillingInvoiceLine has no usable amount column (net_amount/line_total/amount/total)"
    )


def invoice_paid_map(db: Session,
                     invoice_ids: Iterable[int]) -> Dict[int, Decimal]:
    ids = [int(i) for i in set(invoice_ids or []) if i]
    if not ids:
        return {}

    # --- 1) Allocation-based paid (primary) ---
    alloc_rows = (db.query(
        BillingPaymentAllocation.invoice_id.label("invoice_id"),
        func.coalesce(
            func.sum(
                sa_case(
                    (BillingPayment.direction
                     == PaymentDirection.IN, BillingPaymentAllocation.amount),
                    else_=-BillingPaymentAllocation.amount,
                )),
            0,
        ).label("paid"),
    ).join(BillingPayment,
           BillingPaymentAllocation.payment_id == BillingPayment.id).filter(
               BillingPaymentAllocation.invoice_id.in_(ids)).filter(
                   BillingPaymentAllocation.status ==
                   ReceiptStatus.ACTIVE).filter(
                       BillingPayment.status == ReceiptStatus.ACTIVE).group_by(
                           BillingPaymentAllocation.invoice_id).all())

    paid: Dict[int, Decimal] = {
        int(r.invoice_id): _q2(_d(r.paid))
        for r in alloc_rows
    }

    # --- 2) Legacy paid: payments.invoice_id that have ZERO allocations ---
    legacy_rows = (db.query(
        BillingPayment.invoice_id.label("invoice_id"),
        func.coalesce(
            func.sum(
                sa_case(
                    (BillingPayment.direction
                     == PaymentDirection.IN, BillingPayment.amount),
                    else_=-BillingPayment.amount,
                )),
            0,
        ).label("paid"),
    ).filter(BillingPayment.invoice_id.in_(ids)).filter(
        BillingPayment.status == ReceiptStatus.ACTIVE).filter(~exists().where(
            BillingPaymentAllocation.payment_id == BillingPayment.id)).
                   group_by(BillingPayment.invoice_id).all())

    for r in legacy_rows:
        if r.invoice_id is None:
            continue
        inv_id = int(r.invoice_id)
        paid[inv_id] = _q2(paid.get(inv_id, Decimal("0")) + _d(r.paid))

    return paid


def invoice_grand_total_map(db: Session,
                            invoice_ids: Iterable[int]) -> Dict[int, Decimal]:
    """
    grand_total per invoice:
    - Prefer BillingInvoice.grand_total if present and > 0
    - Fallback to SUM(invoice_lines) if grand_total is 0/NULL (common when new invoice added)
    """
    ids = [int(i) for i in set(invoice_ids or []) if i]
    if not ids:
        return {}

    inv_rows = (db.query(BillingInvoice.id, BillingInvoice.grand_total).filter(
        BillingInvoice.id.in_(ids)).all())
    stored = {int(r.id): _q2(_d(r.grand_total)) for r in inv_rows}

    amt_col = _line_amount_expr()

    q = (db.query(
        BillingInvoiceLine.invoice_id.label("invoice_id"),
        func.coalesce(func.sum(amt_col), 0).label("sum_total"),
    ).filter(BillingInvoiceLine.invoice_id.in_(ids)))

    # Optional common filters (won't crash if column doesn't exist)
    if hasattr(BillingInvoiceLine, "status"):
        q = q.filter(getattr(BillingInvoiceLine, "status") != "VOID")
    if hasattr(BillingInvoiceLine, "is_void"):
        q = q.filter(getattr(BillingInvoiceLine,
                             "is_void") == False)  # noqa: E712
    if hasattr(BillingInvoiceLine, "is_deleted"):
        q = q.filter(getattr(BillingInvoiceLine,
                             "is_deleted") == False)  # noqa: E712

    line_rows = q.group_by(BillingInvoiceLine.invoice_id).all()
    line_sum = {int(r.invoice_id): _q2(_d(r.sum_total)) for r in line_rows}

    out: Dict[int, Decimal] = {}
    for inv_id in ids:
        g = stored.get(inv_id, Decimal("0"))
        if g is None or g <= 0:
            g = line_sum.get(inv_id, Decimal("0"))
        out[inv_id] = _q2(_d(g))
    return out


def invoice_due_map(
        db: Session,
        invoice_ids: Iterable[int]) -> Dict[int, Dict[str, Decimal]]:
    """
    Returns for each invoice:
      grand_total, paid, due, overpaid

    ✅ Now robust even if BillingInvoice.grand_total wasn't updated yet.
    """
    ids = [int(i) for i in set(invoice_ids or []) if i]
    if not ids:
        return {}

    grand_map = invoice_grand_total_map(db, ids)
    paid_map = invoice_paid_map(db, ids)

    out: Dict[int, Dict[str, Decimal]] = {}
    for inv_id in ids:
        grand = grand_map.get(inv_id, Decimal("0"))
        paid = paid_map.get(inv_id, Decimal("0"))
        due = _q2(grand - paid)

        if due < 0:
            out[inv_id] = {
                "grand_total": grand,
                "paid": paid,
                "due": Decimal("0.00"),
                "overpaid": _q2(-due),
            }
        else:
            out[inv_id] = {
                "grand_total": grand,
                "paid": paid,
                "due": due,
                "overpaid": Decimal("0.00"),
            }
    return out


def case_advance_wallet(db: Session,
                        billing_case_id: int) -> Dict[str, Decimal]:
    # keep your existing wallet logic unchanged
    case_id = int(billing_case_id)

    adv_credit = _d(
        db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == case_id).filter(
                BillingAdvance.entry_type.in_(
                    [AdvanceType.ADVANCE, AdvanceType.ADJUSTMENT])).scalar())
    adv_refund = _d(
        db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == case_id).filter(
                BillingAdvance.entry_type == AdvanceType.REFUND).scalar())

    used = _d(
        db.query(func.coalesce(func.sum(BillingAdvanceApplication.amount), 0)).
        join(BillingPayment,
             BillingAdvanceApplication.payment_id == BillingPayment.id).filter(
                 BillingAdvanceApplication.billing_case_id == case_id).filter(
                     BillingPayment.status == ReceiptStatus.ACTIVE).scalar())

    credited = _q2(adv_credit - adv_refund)
    used = _q2(used)
    bal = _q2(credited - used)
    if bal < 0:
        bal = Decimal("0.00")

    return {"credited": credited, "used": used, "balance": bal}
