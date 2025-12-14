from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Tuple, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models.pharmacy_inventory import (
    PurchaseOrder, PurchaseOrderItem, POStatus, InvNumberSeries, InventoryItem
)



def D(v) -> Decimal:
    try:
        if v is None:
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money2(v: Decimal) -> Decimal:
    return D(v).quantize(Decimal("0.01"))


def qty4(v: Decimal) -> Decimal:
    return D(v).quantize(Decimal("0.0001"))


_ALLOWED_TRANSITIONS = {
    POStatus.DRAFT: {POStatus.APPROVED, POStatus.CANCELLED},
    POStatus.APPROVED: {POStatus.SENT, POStatus.CANCELLED},
    POStatus.SENT: {POStatus.PARTIALLY_RECEIVED, POStatus.COMPLETED, POStatus.CANCELLED},
    POStatus.PARTIALLY_RECEIVED: {POStatus.COMPLETED, POStatus.CLOSED},
    POStatus.COMPLETED: {POStatus.CLOSED},
    POStatus.CLOSED: set(),
    POStatus.CANCELLED: set(),
}


def generate_po_number(db: Session) -> str:
    """
    Pattern: POYYYYMMDDNNN (collision-safe)
    """
    today_str = date.today().strftime("%Y%m%d")
    date_key = int(today_str)

    row = db.execute(
        select(InvNumberSeries).where(
            InvNumberSeries.key == "PO",
            InvNumberSeries.date_key == date_key
        ).with_for_update()
    ).scalar_one_or_none()

    if not row:
        row = InvNumberSeries(key="PO", date_key=date_key, next_seq=1)
        db.add(row)
        db.flush()

    seq = row.next_seq
    row.next_seq = seq + 1
    db.flush()

    return f"PO{today_str}{seq:03d}"


def compute_po_totals(items: List[PurchaseOrderItem]) -> Tuple[Decimal, Decimal, Decimal]:
    sub_total = Decimal("0.00")
    tax_total = Decimal("0.00")

    for li in items:
        sub_total += D(li.line_sub_total)
        tax_total += D(li.line_tax_total)

    sub_total = money2(sub_total)
    tax_total = money2(tax_total)
    grand_total = money2(sub_total + tax_total)
    return sub_total, tax_total, grand_total


def compute_line(li: PurchaseOrderItem) -> None:
    qty = qty4(li.ordered_qty)
    rate = D(li.unit_cost)
    tax_pct = D(li.tax_percent)

    line_sub = money2(qty * rate)
    line_tax = money2(line_sub * (tax_pct / Decimal("100")))
    line_total = money2(line_sub + line_tax)

    li.line_sub_total = line_sub
    li.line_tax_total = line_tax
    li.line_total = line_total


def validate_items_exist(db: Session, item_ids: List[int]) -> None:
    if not item_ids:
        return
    found = db.query(InventoryItem.id).filter(InventoryItem.id.in_(item_ids)).all()
    found_ids = {x[0] for x in found}
    missing = [i for i in item_ids if i not in found_ids]
    if missing:
        raise HTTPException(status_code=400, detail=f"Items not found: {missing}")


def ensure_editable(po: PurchaseOrder) -> None:
    if po.status not in {POStatus.DRAFT}:
        raise HTTPException(status_code=400, detail="Only DRAFT PO can be edited")


def change_status(po: PurchaseOrder, target: POStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(po.status, set())
    if target not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status change {po.status} → {target}")


def summarize_receipt(po: PurchaseOrder) -> Tuple[Decimal, Decimal]:
    ordered = Decimal("0")
    received = Decimal("0")
    for li in po.items:
        ordered += D(li.ordered_qty)
        received += D(li.received_qty)
    return qty4(ordered), qty4(received)


def recalc_po_status_from_received(po: PurchaseOrder) -> None:
    total_ordered, total_received = summarize_receipt(po)

    if total_received <= 0:
        if po.status in {POStatus.PARTIALLY_RECEIVED, POStatus.COMPLETED}:
            po.status = POStatus.SENT
        return

    if total_received < total_ordered:
        po.status = POStatus.PARTIALLY_RECEIVED
    else:
        po.status = POStatus.COMPLETED
