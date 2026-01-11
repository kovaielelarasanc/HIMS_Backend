from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional, Tuple, Dict

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
from app.models.inv_patient_consumption import (
    InvPatientConsumption,
    InvPatientConsumptionLine,
    InvPatientConsumptionAllocation,
)

from app.services.billing_patient_consumption_sync import sync_consumption_to_billing


# -------------------------
# Helpers
# -------------------------

def _today_key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _next_series(db: Session, key: str, on_date: date) -> Tuple[int, str]:
    """
    Uses existing inv_number_series table (NO new table).
    Returns (doc_id, doc_no). We will NOT store doc_id in stock_txn (to avoid int overflow).
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

    doc_id = int(f"{dk}{seq:04d}")   # only for display/debug
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
        stock = ItemLocationStock(
            location_id=location_id,
            item_id=item_id,
            on_hand_qty=Decimal("0"),
            reserved_qty=Decimal("0")
        )
        db.add(stock)
        db.flush()
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
    return list(db.execute(
        select(ItemBatch)
        .where(and_(
            ItemBatch.location_id == location_id,
            ItemBatch.item_id == item_id,
            ItemBatch.is_active == True,
            ItemBatch.current_qty > 0,
        ))
        .order_by(
            ItemBatch.expiry_date.is_(None),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
        .with_for_update()
    ).scalars().all())


def _resolve_encounter(
    *,
    encounter_type: Optional[str],
    encounter_id: Optional[int],
    visit_id: Optional[int],
) -> Tuple[Optional[str], Optional[int]]:
    # Preferred
    if encounter_type and encounter_id:
        return (encounter_type, int(encounter_id))

    # Backward: visit_id => OP encounter
    if visit_id:
        return ("OP", int(visit_id))

    return (None, None)


# -------------------------
# Queries
# -------------------------

def list_eligible_items(
    db: Session,
    *,
    location_id: int,
    patient_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    encounter_id: Optional[int] = None,
    q: str = "",
    limit: int = 50,
):
    """
    Dropdown items:
    ✅ ONLY items available at location (on_hand_qty > 0)
    ✅ If patient_id provided -> items issued via indents linked to that patient
    ✅ If encounter provided -> further restrict issued items to that encounter
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
        issued_q = (
            select(InvIssueItem.item_id)
            .join(InvIssue, InvIssue.id == InvIssueItem.issue_id)
            .join(InvIndent, InvIndent.id == InvIssue.indent_id)
            .where(and_(
                InvIssue.status == "POSTED",
                InvIssue.to_location_id == location_id,
                InvIndent.patient_id == patient_id,
            ))
        )

        if encounter_type and encounter_id:
            issued_q = issued_q.where(and_(
                InvIndent.encounter_type == encounter_type,
                InvIndent.encounter_id == int(encounter_id),
            ))

        issued_items_subq = issued_q.distinct().subquery()
        base = base.where(InventoryItem.id.in_(select(issued_items_subq.c.item_id)))

    rows = db.execute(
        base.order_by(InventoryItem.name.asc()).limit(limit)
    ).mappings().all()

    return rows


# -------------------------
# Patient Consumption (Billable) — header/lines + stock_txn + billing sync
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
    encounter_type: Optional[str],
    encounter_id: Optional[int],
    visit_id: Optional[int],
    doctor_id: Optional[int],
    notes: str,
    items: List[dict],
):
    loc = db.get(InventoryLocation, location_id)
    if not loc or not loc.is_active:
        raise HTTPException(status_code=404, detail="Location not found")

    enc_type, enc_id = _resolve_encounter(
        encounter_type=encounter_type,
        encounter_id=encounter_id,
        visit_id=visit_id,
    )

    if not enc_type or not enc_id:
        raise HTTPException(
            status_code=400,
            detail="Encounter is required for billing (send encounter_type + encounter_id OR visit_id for OP).",
        )

    now = datetime.utcnow()
    _, doc_no = _next_series(db, "CONS", on_date=now.date())

    # ✅ create consumption header
    cons = InvPatientConsumption(
        consumption_number=doc_no,
        posted_at=now,
        location_id=location_id,
        patient_id=patient_id,
        encounter_type=enc_type,
        encounter_id=enc_id,
        visit_id=visit_id,
        doctor_id=doctor_id,
        notes=notes or "",
        created_by_id=user_id,
    )
    db.add(cons)
    db.flush()  # get cons.id

    out_lines = []
    billing_lines_payload: List[dict] = []

    for line in items:
        item_id = int(line["item_id"])
        req_qty = Decimal(str(line["qty"]))
        batch_id = line.get("batch_id")
        remark = (line.get("remark") or "").strip()

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

        # create line
        cons_line = InvPatientConsumptionLine(
            consumption_id=cons.id,
            item_id=item_id,
            requested_qty=req_qty,
            remark=remark,
        )
        db.add(cons_line)
        db.flush()  # get line.id

        allocations: List[Allocation] = []

        if batch_id:
            b = db.execute(
                select(ItemBatch)
                .where(and_(
                    ItemBatch.id == int(batch_id),
                    ItemBatch.location_id == location_id,
                    ItemBatch.item_id == item_id,
                ))
                .with_for_update()
            ).scalar_one_or_none()

            if not b:
                raise HTTPException(status_code=404, detail=f"Batch not found for item {item.code}")
            if b.current_qty < req_qty:
                raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

            b.current_qty = b.current_qty - req_qty
            allocations.append(Allocation(batch_id=b.id, qty=req_qty))

        else:
            remaining = req_qty
            batches = _fefo_batches_for_item(db, location_id, item_id)
            if not batches:
                # allow no batch case
                allocations.append(Allocation(batch_id=None, qty=req_qty))
                remaining = Decimal("0")

            for b in batches:
                if remaining <= 0:
                    break
                take = min(Decimal(str(b.current_qty)), remaining)
                if take <= 0:
                    continue
                b.current_qty = b.current_qty - take
                allocations.append(Allocation(batch_id=b.id, qty=take))
                remaining -= take

            if remaining > 0:
                raise HTTPException(status_code=400, detail=f"Batch stock insufficient for item {item.code}")

        # save allocations + stock_txn
        for a in allocations:
            db.add(
                InvPatientConsumptionAllocation(
                    line_id=cons_line.id,
                    batch_id=a.batch_id,
                    qty=a.qty,
                )
            )

            db.add(
                StockTransaction(
                    location_id=location_id,
                    item_id=item_id,
                    batch_id=a.batch_id,
                    txn_time=now,
                    txn_type="CONSUME_BILLABLE",
                    ref_type="CONSUMPTION",
                    ref_id=cons.id,              # ✅ safe int (no overflow)
                    ref_line_id=cons_line.id,    # ✅ line traceability
                    quantity_change=-a.qty,
                    unit_cost=(Decimal("0")),
                    mrp=(Decimal("0")),
                    remark=f"{doc_no} | {notes or ''} | {remark}".strip(" |"),
                    user_id=user_id,
                    patient_id=patient_id,
                    visit_id=visit_id,
                    doctor_id=doctor_id,
                )
            )

        # reduce location stock
        stock.on_hand_qty = stock.on_hand_qty - req_qty

        out_lines.append({
            "item_id": item_id,
            "requested_qty": req_qty,
            "allocations": [{"batch_id": x.batch_id, "qty": x.qty} for x in allocations],
        })

        # prepare billing sync payload (use first batch_id if any)
        billing_lines_payload.append({
            "line_id": cons_line.id,
            "item_id": item_id,
            "qty": req_qty,
            "batch_id": allocations[0].batch_id if allocations else None,
        })

    # ✅ billing sync (same DB transaction)
    case_id, invoice_ids = sync_consumption_to_billing(
        db,
        consumption_id=cons.id,
        patient_id=patient_id,
        encounter_type=enc_type,
        encounter_id=enc_id,
        doctor_id=doctor_id,
        created_by=user_id,
        lines=billing_lines_payload,
        tariff_plan_id=None,
    )

    cons.billing_case_id = case_id
    cons.billing_invoice_ids_json = invoice_ids

    db.flush()
    db.commit()

    return {
        "consumption_id": cons.id,
        "consumption_number": cons.consumption_number,
        "posted_at": cons.posted_at,
        "location_id": cons.location_id,
        "patient_id": cons.patient_id,
        "encounter_type": cons.encounter_type,
        "encounter_id": cons.encounter_id,
        "visit_id": cons.visit_id,
        "doctor_id": cons.doctor_id,
        "notes": cons.notes or "",
        "items": out_lines,
        "billing_case_id": case_id,
        "billing_invoice_ids": invoice_ids or [],
    }


def list_patient_consumptions(
    db: Session,
    *,
    location_id: Optional[int],
    patient_id: Optional[int],
    encounter_type: Optional[str],
    encounter_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    limit: int,
    offset: int,
):
    conds = [InvPatientConsumption.is_cancelled == False]

    if location_id:
        conds.append(InvPatientConsumption.location_id == location_id)
    if patient_id:
        conds.append(InvPatientConsumption.patient_id == patient_id)
    if encounter_type and encounter_id:
        conds.append(InvPatientConsumption.encounter_type == encounter_type)
        conds.append(InvPatientConsumption.encounter_id == int(encounter_id))
    if date_from:
        conds.append(func.date(InvPatientConsumption.posted_at) >= date_from)
    if date_to:
        conds.append(func.date(InvPatientConsumption.posted_at) <= date_to)

    q = (
        select(
            InvPatientConsumption.id.label("consumption_id"),
            InvPatientConsumption.consumption_number,
            InvPatientConsumption.posted_at,
            InvPatientConsumption.location_id,
            InvPatientConsumption.patient_id,
            InvPatientConsumption.encounter_type,
            InvPatientConsumption.encounter_id,
            InvPatientConsumption.visit_id,
            InvPatientConsumption.doctor_id,
            InvPatientConsumption.created_by_id.label("user_id"),
            func.count(InvPatientConsumptionLine.id).label("total_lines"),
            func.coalesce(func.sum(InvPatientConsumptionLine.requested_qty), 0).label("total_qty"),
        )
        .join(InvPatientConsumptionLine, InvPatientConsumptionLine.consumption_id == InvPatientConsumption.id)
        .where(and_(*conds))
        .group_by(InvPatientConsumption.id)
        .order_by(InvPatientConsumption.posted_at.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = db.execute(q).mappings().all()

    out = []
    for r in rows:
        out.append({
            "consumption_id": int(r["consumption_id"]),
            "consumption_number": r["consumption_number"],
            "posted_at": r["posted_at"],
            "location_id": r["location_id"],
            "patient_id": r["patient_id"],
            "encounter_type": r["encounter_type"],
            "encounter_id": r["encounter_id"],
            "visit_id": r["visit_id"],
            "doctor_id": r["doctor_id"],
            "user_id": r["user_id"],
            "total_lines": int(r["total_lines"] or 0),
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
