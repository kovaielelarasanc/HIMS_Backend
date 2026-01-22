# FILE: app/services/billing_payment_service.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingPayment,
    BillingPaymentAllocation,
    ReceiptStatus,
    PaymentDirection,
    PaymentKind,
    NumberDocType,
    NumberResetPeriod,
)
from app.schemas.billing_payments import MultiInvoicePaymentIn
from app.services.billing_balances import invoice_due_map
from app.services.billing_numbers import next_number

Q2 = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _q2(x: Decimal) -> Decimal:
    return _d(x).quantize(Q2, rounding=ROUND_HALF_UP)


def _status_value(x) -> str:
    if x is None:
        return ""
    if hasattr(x, "value"):
        return str(x.value).upper()
    s = str(x)
    if "." in s:
        s = s.split(".")[-1]
    return s.upper()


PAYABLE_STATUSES = {"APPROVED", "POSTED"}  # adjust if you allow DRAFT payments


def record_multi_invoice_payment(
    db: Session,
    *,
    case_id: int,
    inp: MultiInvoicePaymentIn,
    user: User,
) -> BillingPayment:
    case = db.query(BillingCase).filter(BillingCase.id == int(case_id)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    allocs = [{
        "invoice_id": int(a.invoice_id),
        "amount": _q2(_d(a.amount))
    } for a in inp.allocations]
    if not allocs:
        raise HTTPException(status_code=400,
                            detail="Provide at least one allocation")
    if any(a["amount"] <= 0 for a in allocs):
        raise HTTPException(status_code=400,
                            detail="Allocation amount must be > 0")

    inv_ids = sorted({a["invoice_id"] for a in allocs})

    invoices: List[BillingInvoice] = (db.query(BillingInvoice).filter(
        BillingInvoice.id.in_(inv_ids)).filter(
            BillingInvoice.billing_case_id == case.id).with_for_update().all())
    if len(invoices) != len(inv_ids):
        raise HTTPException(
            status_code=400,
            detail="One or more invoices do not belong to this case")

    inv_by_id = {int(i.id): i for i in invoices}

    for inv in invoices:
        st = _status_value(inv.status)
        if st == "VOID":
            raise HTTPException(status_code=409,
                                detail=f"Invoice {inv.invoice_number} is VOID")
        if PAYABLE_STATUSES and st not in PAYABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=
                f"Invoice {inv.invoice_number} is not payable until {sorted(PAYABLE_STATUSES)} (status={st})",
            )

    alloc_sum = _q2(sum((a["amount"] for a in allocs), Decimal("0")))
    amount = _q2(_d(inp.amount)) if inp.amount is not None else alloc_sum

    if amount <= 0:
        raise HTTPException(status_code=400,
                            detail="Payment amount must be > 0")
    if amount != alloc_sum:
        raise HTTPException(
            status_code=400,
            detail=
            f"Amount mismatch: amount={amount} but allocations sum={alloc_sum}",
        )

    # ✅ due map now robust for newly created invoices (grand_total fallback to sum(lines))
    due_map = invoice_due_map(db, inv_ids)

    for a in allocs:
        inv_id = a["invoice_id"]
        inv = inv_by_id[inv_id]
        b = due_map.get(inv_id, {})
        due = _q2(b.get("due", Decimal("0")))
        paid = _q2(b.get("paid", Decimal("0")))
        grand = _q2(b.get("grand_total", Decimal("0")))

        if due <= 0:
            raise HTTPException(
                status_code=409,
                detail=
                f"Invoice {inv.invoice_number} already settled (grand={grand} paid={paid} due={due})",
            )

        if a["amount"] > due:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Allocation exceeds due for invoice {inv.invoice_number}. "
                    f"grand={grand} paid={paid} due={due} got={a['amount']}"),
            )

    receipt_no = next_number(
        db,
        doc_type=NumberDocType.RECEIPT,
        prefix="RCPT-",
        reset_period=NumberResetPeriod.YEAR,
        padding=6,
    )

    pay = BillingPayment(
        billing_case_id=case.id,
        invoice_id=None,
        payer_type=inp.payer_type,
        payer_id=inp.payer_id,
        mode=inp.mode,
        amount=amount,
        txn_ref=(inp.txn_ref or None),
        notes=(inp.notes or None),
        receipt_number=receipt_no,
        kind=PaymentKind.RECEIPT,
        direction=PaymentDirection.IN,
        status=ReceiptStatus.ACTIVE,
        received_by=getattr(user, "id", None),
    )
    db.add(pay)
    db.flush()

    for a in allocs:
        inv = inv_by_id[a["invoice_id"]]
        db.add(
            BillingPaymentAllocation(
                tenant_id=None,
                billing_case_id=case.id,
                payment_id=pay.id,
                invoice_id=inv.id,
                payer_bucket=inv.payer_type,
                amount=a["amount"],
                status=ReceiptStatus.ACTIVE,
                allocated_by=getattr(user, "id", None),
            ))

    db.commit()
    db.refresh(pay)
    return pay
