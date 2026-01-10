from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select, and_, func
from sqlalchemy.orm import Session

from app.models.pharmacy_inventory import (
    InventoryItem,
    InventoryLocation,
    ItemLocationStock,
    ItemBatch,
    InvNumberSeries,
    StockTransaction,
)

from app.models.inv_indent_issue import InvIndent, InvIssue, InvIssueItem

# -------------------------
# Helpers
# -------------------------

def _today_key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _next_series(db: Session, key: str, on_date: date) -> Tuple[int, str]:
    """
    Generates a unique numeric id (int) and a friendly doc number string
    using existing table inv_number_series. NO new table.
    """
    dk = _today_key(on_date)

    row = db.execute(
        select(InvNumberSeries)
        .where(and_(InvNumberSeries.key == key, InvNumberSeries.date_key == dk))
        .with_for_update()
    ).scalar_one_or_none()

    if not row:
        row = InvNumberSeries(key=key, date_key=dk, next_seq=1)
        db.add(row)
        db.flush()

    seq = row.next_seq
    row.next_seq = seq + 1
    db.flush()

    # Build:
    # id: YYYYMMDD + 4-digit seq => int, unique daily
    doc_id = int(f"{dk}{seq:04d}")
    doc_no = f"{key}-{dk}-{seq:04d}"
    return doc_id, doc_no


def _lock_stock_row(db: Session, location_id: int, item_id: int) -> ItemLocationStock:
    stock = db.execute(
        select(ItemLocationStock)
        .where(and_(
            ItemLocationStock.location_id == location_id,
            ItemLocationStock.item_id == item_id
        ))
        .with_for_update()
    ).scalar_one_or_none()

    if not stock:
        # Create stock row if missing
        stock = ItemLocationStock(location_id=location_id, item_id=item_id, on_hand_qty=Decimal("0"), reserved_qty=Decimal("0"))
        db.add(stock)
        db.flush()
        # lock again
        stock = db.execute(
            select(ItemLocationStock)
            .where(and_(
                ItemLocationStock.location_id == location_id,
                ItemLocationStock.item_id == item_id
            ))
            .with_for_update()
        ).scalar_one()

    return stock


def _fefo_batches_for_item(db: Session, location_id: int, item_id: int) -> List[ItemBatch]:
    # FEFO: earliest expiry first, NULL expiry last
    return list(db.execute(
        select(ItemBatch)
        .where(and_(
            ItemBatch.location_id == location_id,
            ItemBatch.item_id == item_id,
            ItemBatch.is_active == True,
            ItemBatch.current_qty > 0,
        ))
        .order_by(
            ItemBatch.expiry_date.is_(None),  # False first => has expiry first
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
        .with_for_update()
    ).scalars().all())


def _get_or_create_reconcile_batch(db: Session, location_id: int, item_id: int) -> ItemBatch:
    """
    For rare case: reconciliation finds EXTRA stock (need +IN) but no batch info.
    We'll store it in a special existing-table batch: batch_no='RECONCILE'.
    """
    b = db.execute(
        select(ItemBatch)
        .where(and_(
            ItemBatch.location_id == location_id,
            ItemBatch.item_id == item_id,
            ItemBatch.batch_no == "RECONCILE",
            ItemBatch.expiry_key == 0
        ))
        .with_for_update()
    ).scalar_one_or_none()

    if b:
        return b

    b = ItemBatch(
        location_id=location_id,
        item_id=item_id,
        batch_no="RECONCILE",
        expiry_date=None,
        expiry_key=0,
        current_qty=Decimal("0"),
        reserved_qty=Decimal("0"),
        unit_cost=Decimal("0"),
        mrp=Decimal("0"),
        tax_percent=Decimal("0"),
        is_active=True,
        is_saleable=True,
        status="ACTIVE",  # matches your enum value string
    )
    db.add(b)
    db.flush()
    return b


# -------------------------
# Queries
# -------------------------

def list_eligible_items(
    db: Session,
    location_id: int,
    patient_id: Optional[int] = None,
    q: str = "",
    limit: int = 50,
):
    """
    Items shown in UI dropdown:
    ✅ ONLY items available at location (on_hand_qty > 0)
    ✅ If patient_id provided, further restrict to items issued via indents linked to that patient.
    """

    base = (
        select(
            InventoryItem.id.label("item_id"),
            InventoryItem.code,
            InventoryItem.name,
            InventoryItem.item_type,
            InventoryItem.unit,
            ItemLocationStock.on_hand_qty,
        )
        .join(ItemLocationStock, ItemLocationStock.item_id == InventoryItem.id)
        .where(and_(
            ItemLocationStock.location_id == location_id,
            ItemLocationStock.on_hand_qty > 0,
            InventoryItem.is_active == True,
        ))
    )

    if q:
        like = f"%{q.strip()}%"
        base = base.where(
            InventoryItem.name.ilike(like) | InventoryItem.code.ilike(like)
        )

    if patient_id:
        # Only items that were issued to this location through an indent tied to the patient
        issued_items_subq = (
            select(InvIssueItem.item_id)
            .join(InvIssue, InvIssue.id == InvIssueItem.issue_id)
            .join(InvIndent, InvIndent.id == InvIssue.indent_id)
            .where(and_(
                InvIssue.status == "POSTED",
                InvIssue.to_location_id == location_id,
                InvIndent.patient_id == patient_id,
            ))
            .distinct()
            .subquery()
        )
        base = base.where(InventoryItem.id.in_(select(issued_items_subq.c.item_id)))

    rows = db.execute(
        base.order_by(InventoryItem.name.asc()).limit(limit)
    ).mappings().all()

    return rows


# -------------------------
# Patient consumption (billable) - reduces OT/Ward stock
# -------------------------

@dataclass
class Allocation:
    batch_id: Optional[int]
    qty: Decimal


def post_patient_consumption(
    db: Session,
    *,
    user_id: int,
    location_id: int,
    patient_id: int,
    visit_id: Optional[int],
    doctor_id: Optional[int],
    notes: str,
    items: List[dict],
):
    # Validate location
    loc = db.get(InventoryLocation, location_id)
    if not loc or not loc.is_active:
        raise HTTPException(status_code=404, detail="Location not found")

    # Validate patient exists in your system (table name: patients)
    # If your Patient model path differs, replace this check accordingly.
    # We keep it soft to avoid breaking.
    # Example: patient = db.get(Patient, patient_id)
    # if not patient: raise HTTPException(404, "Patient not found")

    now = datetime.utcnow()
    doc_id, doc_no = _next_series(db, "CONS", on_date=now.date())

    out_lines = []

    for line in items:
        item_id = int(line["item_id"])
        req_qty: Decimal = Decimal(str(line["qty"]))
        batch_id = line.get("batch_id")

        if req_qty <= 0:
            raise HTTPException(status_code=400, detail="Qty must be > 0")

        item = db.get(InventoryItem, item_id)
        if not item or not item.is_active:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

        stock = _lock_stock_row(db, location_id, item_id)

        if stock.on_hand_qty < req_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for item {item.code}. Available={stock.on_hand_qty}, Requested={req_qty}",
            )

        allocations: List[Allocation] = []

        if batch_id:
            b = db.execute(
                select(ItemBatch)
                .where(and_(
                    ItemBatch.id == batch_id,
                    ItemBatch.location_id == location_id,
                    ItemBatch.item_id == item_id,
                ))
                .with_for_update()
            ).scalar_one_or_none()

            if not b:
                raise HTTPException(status_code=404, detail=f"Batch not found for item {item.code}")
            if b.current_qty < req_qty:
                raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

            # consume from this batch
            b.current_qty = b.current_qty - req_qty
            allocations.append(Allocation(batch_id=b.id, qty=req_qty))

            db.add(StockTransaction(
                location_id=location_id,
                item_id=item_id,
                batch_id=b.id,
                txn_time=now,
                txn_type="CONSUME_BILLABLE",
                ref_type="CONSUMPTION",
                ref_id=doc_id,
                ref_line_id=None,
                quantity_change=-req_qty,
                unit_cost=b.unit_cost or stock.last_unit_cost,
                mrp=b.mrp or stock.last_mrp,
                remark=(line.get("remark") or "") + (f" | {doc_no} | {notes}" if notes else f" | {doc_no}"),
                user_id=user_id,
                patient_id=patient_id,
                visit_id=visit_id,
                doctor_id=doctor_id,
            ))

        else:
            # Auto FEFO allocation across batches
            remaining = req_qty
            batches = _fefo_batches_for_item(db, location_id, item_id)

            if not batches:
                # No batches exist -> still allow consumption without batch
                allocations.append(Allocation(batch_id=None, qty=req_qty))
                db.add(StockTransaction(
                    location_id=location_id,
                    item_id=item_id,
                    batch_id=None,
                    txn_time=now,
                    txn_type="CONSUME_BILLABLE",
                    ref_type="CONSUMPTION",
                    ref_id=doc_id,
                    ref_line_id=None,
                    quantity_change=-req_qty,
                    unit_cost=stock.last_unit_cost,
                    mrp=stock.last_mrp,
                    remark=(line.get("remark") or "") + (f" | {doc_no} | {notes}" if notes else f" | {doc_no}"),
                    user_id=user_id,
                    patient_id=patient_id,
                    visit_id=visit_id,
                    doctor_id=doctor_id,
                ))
                remaining = Decimal("0")

            for b in batches:
                if remaining <= 0:
                    break
                take = min(Decimal(str(b.current_qty)), remaining)
                if take <= 0:
                    continue

                b.current_qty = b.current_qty - take
                allocations.append(Allocation(batch_id=b.id, qty=take))

                db.add(StockTransaction(
                    location_id=location_id,
                    item_id=item_id,
                    batch_id=b.id,
                    txn_time=now,
                    txn_type="CONSUME_BILLABLE",
                    ref_type="CONSUMPTION",
                    ref_id=doc_id,
                    ref_line_id=None,
                    quantity_change=-take,
                    unit_cost=b.unit_cost or stock.last_unit_cost,
                    mrp=b.mrp or stock.last_mrp,
                    remark=(line.get("remark") or "") + (f" | {doc_no} | {notes}" if notes else f" | {doc_no}"),
                    user_id=user_id,
                    patient_id=patient_id,
                    visit_id=visit_id,
                    doctor_id=doctor_id,
                ))
                remaining -= take

            if remaining > 0:
                raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

        # reduce location stock by total requested qty
        stock.on_hand_qty = stock.on_hand_qty - req_qty

        out_lines.append({
            "item_id": item_id,
            "requested_qty": req_qty,
            "allocations": [{"batch_id": a.batch_id, "qty": a.qty} for a in allocations],
        })

    db.flush()
    db.commit()

    return {
        "consumption_id": doc_id,
        "consumption_number": doc_no,
        "posted_at": now,
        "location_id": location_id,
        "patient_id": patient_id,
        "visit_id": visit_id,
        "doctor_id": doctor_id,
        "notes": notes or "",
        "items": out_lines,
    }


def list_patient_consumptions(
    db: Session,
    *,
    location_id: Optional[int],
    patient_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    limit: int,
    offset: int,
):
    """
    Nurse entry list: grouped by (ref_type='CONSUMPTION', ref_id=consumption_id)
    txn_type includes CONSUME_BILLABLE
    """
    conds = [
        StockTransaction.ref_type == "CONSUMPTION",
        StockTransaction.txn_type == "CONSUME_BILLABLE",
    ]
    if location_id:
        conds.append(StockTransaction.location_id == location_id)
    if patient_id:
        conds.append(StockTransaction.patient_id == patient_id)
    if date_from:
        conds.append(func.date(StockTransaction.txn_time) >= date_from)
    if date_to:
        conds.append(func.date(StockTransaction.txn_time) <= date_to)

    q = (
        select(
            StockTransaction.ref_id.label("consumption_id"),
            func.min(StockTransaction.txn_time).label("posted_at"),
            func.min(StockTransaction.location_id).label("location_id"),
            func.min(StockTransaction.patient_id).label("patient_id"),
            func.min(StockTransaction.visit_id).label("visit_id"),
            func.min(StockTransaction.doctor_id).label("doctor_id"),
            func.min(StockTransaction.user_id).label("user_id"),
            func.count().label("total_lines"),
            func.sum(func.abs(StockTransaction.quantity_change)).label("total_qty"),
        )
        .where(and_(*conds))
        .group_by(StockTransaction.ref_id)
        .order_by(func.min(StockTransaction.txn_time).desc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(q).mappings().all()

    out = []
    for r in rows:
        cid = int(r["consumption_id"])
        # reconstruct doc_no (same pattern as service)
        # If you want exact, you can store doc_no inside remark; we already do.
        out.append({
            "consumption_id": cid,
            "consumption_number": f"CONS-{str(cid)[:8]}-{str(cid)[8:]}",
            "posted_at": r["posted_at"],
            "location_id": r["location_id"],
            "patient_id": r["patient_id"],
            "visit_id": r["visit_id"],
            "doctor_id": r["doctor_id"],
            "user_id": r["user_id"],
            "total_lines": int(r["total_lines"]),
            "total_qty": Decimal(str(r["total_qty"] or 0)),
        })
    return out


# -------------------------
# Bulk reconcile (closing balance method)
# -------------------------

def post_bulk_reconcile(
    db: Session,
    *,
    user_id: int,
    location_id: int,
    on_date: date,
    notes: str,
    lines: List[dict],
):
    loc = db.get(InventoryLocation, location_id)
    if not loc or not loc.is_active:
        raise HTTPException(status_code=404, detail="Location not found")

    now = datetime.utcnow()
    rec_id, rec_no = _next_series(db, "RECON", on_date=on_date)

    out_lines = []

    for line in lines:
        item_id = int(line["item_id"])
        closing_qty = Decimal(str(line["closing_qty"]))
        batch_id = line.get("batch_id")
        remark = (line.get("remark") or "").strip()

        item = db.get(InventoryItem, item_id)
        if not item or not item.is_active:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

        stock = _lock_stock_row(db, location_id, item_id)
        before = Decimal(str(stock.on_hand_qty))

        diff = before - closing_qty  # positive => need reduce (auto consume), negative => need add (adjust in)

        allocations: List[Allocation] = []
        auto_consume = Decimal("0")
        adjust_in = Decimal("0")

        if diff == 0:
            out_lines.append({
                "item_id": item_id,
                "before_qty": before,
                "closing_qty": closing_qty,
                "auto_consumed_qty": auto_consume,
                "adjusted_in_qty": adjust_in,
                "allocations": [],
            })
            continue

        if diff > 0:
            # AUTO CONSUME BULK = reduce by diff
            remaining = diff
            auto_consume = diff

            if batch_id:
                b = db.execute(
                    select(ItemBatch)
                    .where(and_(
                        ItemBatch.id == batch_id,
                        ItemBatch.location_id == location_id,
                        ItemBatch.item_id == item_id,
                    ))
                    .with_for_update()
                ).scalar_one_or_none()
                if not b:
                    raise HTTPException(status_code=404, detail=f"Batch not found for item {item.code}")
                if b.current_qty < remaining:
                    raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

                b.current_qty = b.current_qty - remaining
                allocations.append(Allocation(batch_id=b.id, qty=remaining))

                db.add(StockTransaction(
                    location_id=location_id,
                    item_id=item_id,
                    batch_id=b.id,
                    txn_time=now,
                    txn_type="CONSUME_BULK",
                    ref_type="RECONCILE",
                    ref_id=rec_id,
                    quantity_change=-remaining,
                    unit_cost=b.unit_cost or stock.last_unit_cost,
                    mrp=b.mrp or stock.last_mrp,
                    remark=f"{rec_no} | {notes} | {remark}".strip(" |"),
                    user_id=user_id,
                    patient_id=None,
                    visit_id=None,
                    doctor_id=None,
                ))
            else:
                batches = _fefo_batches_for_item(db, location_id, item_id)
                if not batches:
                    allocations.append(Allocation(batch_id=None, qty=remaining))
                    db.add(StockTransaction(
                        location_id=location_id,
                        item_id=item_id,
                        batch_id=None,
                        txn_time=now,
                        txn_type="CONSUME_BULK",
                        ref_type="RECONCILE",
                        ref_id=rec_id,
                        quantity_change=-remaining,
                        unit_cost=stock.last_unit_cost,
                        mrp=stock.last_mrp,
                        remark=f"{rec_no} | {notes} | {remark}".strip(" |"),
                        user_id=user_id,
                    ))
                    remaining = Decimal("0")

                for b in batches:
                    if remaining <= 0:
                        break
                    take = min(Decimal(str(b.current_qty)), remaining)
                    if take <= 0:
                        continue
                    b.current_qty = b.current_qty - take
                    allocations.append(Allocation(batch_id=b.id, qty=take))

                    db.add(StockTransaction(
                        location_id=location_id,
                        item_id=item_id,
                        batch_id=b.id,
                        txn_time=now,
                        txn_type="CONSUME_BULK",
                        ref_type="RECONCILE",
                        ref_id=rec_id,
                        quantity_change=-take,
                        unit_cost=b.unit_cost or stock.last_unit_cost,
                        mrp=b.mrp or stock.last_mrp,
                        remark=f"{rec_no} | {notes} | {remark}".strip(" |"),
                        user_id=user_id,
                    ))
                    remaining -= take

                if remaining > 0:
                    raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

            stock.on_hand_qty = stock.on_hand_qty - diff

        else:
            # Found extra stock -> ADJUST IN (rare)
            add_qty = abs(diff)
            adjust_in = add_qty

            if batch_id:
                b = db.execute(
                    select(ItemBatch)
                    .where(and_(
                        ItemBatch.id == batch_id,
                        ItemBatch.location_id == location_id,
                        ItemBatch.item_id == item_id,
                    ))
                    .with_for_update()
                ).scalar_one_or_none()
                if not b:
                    raise HTTPException(status_code=404, detail=f"Batch not found for item {item.code}")
            else:
                b = _get_or_create_reconcile_batch(db, location_id, item_id)

            b.current_qty = b.current_qty + add_qty
            allocations.append(Allocation(batch_id=b.id, qty=add_qty))

            db.add(StockTransaction(
                location_id=location_id,
                item_id=item_id,
                batch_id=b.id,
                txn_time=now,
                txn_type="ADJUSTMENT_IN",
                ref_type="RECONCILE",
                ref_id=rec_id,
                quantity_change=add_qty,
                unit_cost=b.unit_cost or stock.last_unit_cost,
                mrp=b.mrp or stock.last_mrp,
                remark=f"{rec_no} | {notes} | {remark}".strip(" |"),
                user_id=user_id,
            ))

            stock.on_hand_qty = stock.on_hand_qty + add_qty

        out_lines.append({
            "item_id": item_id,
            "before_qty": before,
            "closing_qty": closing_qty,
            "auto_consumed_qty": auto_consume,
            "adjusted_in_qty": adjust_in,
            "allocations": [{"batch_id": a.batch_id, "qty": a.qty} for a in allocations],
        })

    db.flush()
    db.commit()

    return {
        "reconcile_id": rec_id,
        "reconcile_number": rec_no,
        "posted_at": now,
        "location_id": location_id,
        "on_date": on_date,
        "notes": notes or "",
        "lines": out_lines,
    }
