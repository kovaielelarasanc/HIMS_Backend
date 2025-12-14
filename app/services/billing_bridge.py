from __future__ import annotations
from typing import Any
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.lis import LisOrder, LisOrderItem
from app.models.opd import LabTest


def create_or_get_invoice_for_lis_order(db: Session, order: LisOrder,
                                        created_by_user_id: int) -> int:
    """
    Creates a billing invoice for LIS order if not already billed.
    Returns invoice_id.

    NOTE:
    Replace the marked section with your real Billing models/functions.
    """
    if order.billing_invoice_id and order.billing_status == "billed":
        return int(order.billing_invoice_id)

    # ---- prepare line items from master price ----
    items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order.id).all()
    if not items:
        raise HTTPException(status_code=400,
                            detail="Cannot bill: no LIS items found")

    lines: list[dict[str, Any]] = []
    total = 0.0

    for it in items:
        test = db.query(LabTest).get(it.test_id)
        price = float(getattr(test, "price", 0) or 0)
        total += price
        lines.append({
            "name": it.test_name,
            "code": it.test_code,
            "qty": 1,
            "unit_price": price,
            "amount": price,
            "ref_type": "lis_order_item",
            "ref_id": it.id,
        })

    # ---- ✅ IMPORTANT: hook into your Billing module here ----
    # You likely already have: createInvoice(), addServiceItem(), finalizeInvoice() etc.
    #
    # Replace this stub with YOUR billing creation logic.
    #
    # Example expectation:
    #   invoice_id = billing_create_invoice(db, patient_id=order.patient_id, billing_type="lab", context_type=order.context_type, context_id=order.context_id, created_by=created_by_user_id)
    #   for line in lines: billing_add_item(db, invoice_id=invoice_id, ...)
    #
    # For now: raise with a clear message until you connect real billing.
    raise HTTPException(
        status_code=501,
        detail=
        "Billing bridge not wired. Connect to billing invoice creation logic here."
    )
