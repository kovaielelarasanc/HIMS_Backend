from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.billing import BillingCase, BillingInvoice
from app.schemas.billing_payments import MultiInvoicePaymentIn, PaymentOut, PaymentAllocationOut
from app.services.billing_payment_service import record_multi_invoice_payment
from app.services.billing_balances import invoice_due_map, case_advance_wallet

router = APIRouter(prefix="/billing/multi", tags=["Billing Payments"])


@router.get("/cases/{case_id}/financials")
def case_financials(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
) -> Dict[str, Any]:
    case = db.query(BillingCase).filter(BillingCase.id == int(case_id)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    invoices = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case.id).order_by(
            BillingInvoice.id.asc()).all())

    inv_ids = [int(i.id) for i in invoices]
    bal = invoice_due_map(db, inv_ids)
    wallet = case_advance_wallet(db, case.id)

    rows = []
    total_grand = Decimal("0")
    total_paid = Decimal("0")
    total_due = Decimal("0")

    for inv in invoices:
        b = bal.get(
            int(inv.id), {
                "grand_total": Decimal("0"),
                "paid": Decimal("0"),
                "due": Decimal("0"),
                "overpaid": Decimal("0"),
            })

        rows.append({
            "id":
            int(inv.id),
            "invoice_number":
            inv.invoice_number,
            "module":
            inv.module,
            "status":
            getattr(inv.status, "value", str(inv.status)),
            "payer_type":
            getattr(inv.payer_type, "value", str(inv.payer_type)),
            "grand_total":
            str(b["grand_total"]),
            "paid":
            str(b["paid"]),
            "due":
            str(b["due"]),
            "overpaid":
            str(b["overpaid"]),
        })

        total_grand += b["grand_total"]
        total_paid += b["paid"]
        total_due += b["due"]

    return {
        "case_id": int(case.id),
        "totals": {
            "grand_total": str(total_grand),
            "paid": str(total_paid),
            "due": str(total_due),
        },
        "advance_wallet": {
            k: str(v)
            for k, v in wallet.items()
        },
        "invoices": rows,
    }


@router.post("/cases/{case_id}/payments", response_model=PaymentOut)
def pay_multi(
        case_id: int,
        inp: Optional[MultiInvoicePaymentIn] = Body(default=None),

        # legacy query fallback (keep it, but still uses canonical multi service)
        amount: Optional[Decimal] = Query(default=None),
        mode: Optional[str] = Query(default=None),
        invoice_id: Optional[int] = Query(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # legacy fallback -> convert to canonical MultiInvoicePaymentIn
    if inp is None:
        if not amount or not invoice_id:
            raise HTTPException(
                status_code=400,
                detail="Provide body allocations OR amount+invoice_id")

        from app.models.billing import PayMode, PayerType
        inp = MultiInvoicePaymentIn(
            amount=Decimal(str(amount)),
            mode=PayMode(mode) if mode else PayMode.CASH,
            allocations=[{
                "invoice_id": int(invoice_id),
                "amount": Decimal(str(amount))
            }],
            payer_type=PayerType.PATIENT,
            payer_id=None,
            txn_ref=None,
            notes=None,
        )

    pay = record_multi_invoice_payment(db,
                                       case_id=int(case_id),
                                       inp=inp,
                                       user=user)

    allocs = []
    for a in (getattr(pay, "allocations", None) or []):
        allocs.append(
            PaymentAllocationOut(
                invoice_id=int(a.invoice_id) if a.invoice_id else 0,
                amount=Decimal(str(a.amount or 0)),
                payer_bucket=getattr(a.payer_bucket, "value",
                                     str(a.payer_bucket)),
            ))

    return PaymentOut(
        id=int(pay.id),
        receipt_number=pay.receipt_number,
        amount=Decimal(str(pay.amount or 0)),
        mode=getattr(pay.mode, "value", str(pay.mode)),
        status=getattr(pay.status, "value", str(pay.status)),
        allocations=allocs,
    )
