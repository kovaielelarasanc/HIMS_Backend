# FILE: app/services/inventory_indent_service.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Tuple, Dict

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import or_, func, text  

from app.utils.timezone import now_ist, today_ist

from app.models.inv_indent_issue import (
    InvIndent,
    InvIndentItem,
    InvIssue,
    InvIssueItem,
    IndentStatus,
    IssueStatus,
    IndentPriority,
)

from app.models.pharmacy_inventory import (
    InventoryLocation,
    InventoryItem,
    ItemBatch,
    ItemLocationStock,
    StockTransaction,
    InvNumberSeries,
    BatchStatus,
)


class IndentError(RuntimeError):
    pass


def D(v, default="0") -> Decimal:
    try:
        if v is None:
            return Decimal(default)
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def now_db() -> datetime:
    """Always store naive datetime in MySQL DATETIME columns."""
    dt = now_ist()
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def today_db() -> date:
    return today_ist()


def _expiry_key(expiry_date: Optional[date]) -> int:
    return int(expiry_date.strftime("%Y%m%d")) if expiry_date else 0


def _org_code() -> str:
    try:
        from app.core.config import settings
        for k in ("ORG_CODE", "HOSPITAL_CODE", "TENANT_CODE", "ORG_PREFIX"):
            val = getattr(settings, k, None)
            if val:
                return str(val).strip().upper()
    except Exception:
        pass
    return "NH"


def _date_key(d: Optional[date] = None) -> int:
    dd = d or today_db()
    return int(dd.strftime("%Y%m%d"))


def next_inv_doc_number(db: Session, key: str, prefix: str) -> str:
    today = today_db()
    dk = _date_key(today)

    row = (
        db.query(InvNumberSeries)
        .filter(InvNumberSeries.key == key, InvNumberSeries.date_key == dk)
        .with_for_update()
        .first()
    )

    if not row:
        row = InvNumberSeries(key=key, date_key=dk, next_seq=1)
        db.add(row)
        db.flush()
        row = (
            db.query(InvNumberSeries)
            .filter(InvNumberSeries.key == key, InvNumberSeries.date_key == dk)
            .with_for_update()
            .one()
        )

    seq = int(row.next_seq or 1)
    row.next_seq = seq + 1
    db.flush()

    ddmmyyyy = today.strftime("%d%m%Y")
    return f"{prefix}{ddmmyyyy}{seq:06d}"


def lock_or_create_fast_stock(db: Session, item_id: int, location_id: int) -> ItemLocationStock:
    st = (
        db.query(ItemLocationStock)
        .filter(ItemLocationStock.item_id == item_id, ItemLocationStock.location_id == location_id)
        .with_for_update()
        .first()
    )
    if st:
        return st

    st = ItemLocationStock(item_id=item_id, location_id=location_id, on_hand_qty=D(0), reserved_qty=D(0))
    db.add(st)
    db.flush()

    return (
        db.query(ItemLocationStock)
        .filter(ItemLocationStock.id == st.id)
        .with_for_update()
        .one()
    )


def ensure_dest_batch(db: Session, *, src_batch: ItemBatch, dest_location_id: int) -> ItemBatch:
    ek = _expiry_key(src_batch.expiry_date)
    b = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == src_batch.item_id,
            ItemBatch.location_id == dest_location_id,
            ItemBatch.batch_no == src_batch.batch_no,
            ItemBatch.expiry_key == ek,
        )
        .with_for_update()
        .first()
    )
    if b:
        return b

    b = ItemBatch(
        item_id=src_batch.item_id,
        location_id=dest_location_id,
        batch_no=src_batch.batch_no,
        mfg_date=src_batch.mfg_date,
        expiry_date=src_batch.expiry_date,
        expiry_key=ek,
        current_qty=D(0),
        reserved_qty=D(0),
        unit_cost=src_batch.unit_cost,
        mrp=src_batch.mrp,
        tax_percent=src_batch.tax_percent,
        is_active=True,
        is_saleable=True,
        status=BatchStatus.ACTIVE,
    )
    db.add(b)
    db.flush()
    return (
        db.query(ItemBatch)
        .filter(ItemBatch.id == b.id)
        .with_for_update()
        .one()
    )


def pick_batches_fefo(
    db: Session,
    *,
    item_id: int,
    location_id: int,
    qty_needed: Decimal,
    forced_batch_id: Optional[int] = None,
) -> List[Tuple[ItemBatch, Decimal]]:
    if qty_needed <= 0:
        return []

    today = today_db()

    base_q = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == BatchStatus.ACTIVE,
            or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
        )
        .order_by(
            ItemBatch.expiry_date.is_(None),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
        .with_for_update()
    )

    if forced_batch_id:
        b = base_q.filter(ItemBatch.id == forced_batch_id).first()
        if not b:
            raise IndentError("Selected batch not available / expired / not saleable.")
        available = D(b.current_qty) - D(b.reserved_qty)
        if available < qty_needed:
            raise IndentError(f"Insufficient stock in selected batch. Available {available}, need {qty_needed}.")
        return [(b, qty_needed)]

    batches = base_q.all()
    remaining = D(qty_needed)
    picks: List[Tuple[ItemBatch, Decimal]] = []

    for b in batches:
        available = D(b.current_qty) - D(b.reserved_qty)
        if available <= 0:
            continue
        take = available if available <= remaining else remaining
        picks.append((b, take))
        remaining -= take
        if remaining <= 0:
            break

    if remaining > 0:
        raise IndentError(f"Insufficient stock for item_id={item_id}. Short {remaining}.")
    return picks


# ============================================================
# INDENT
# ============================================================
def create_indent(db: Session, payload, user_id: Optional[int]) -> InvIndent:
    if payload.from_location_id == payload.to_location_id:
        raise IndentError("from_location_id and to_location_id cannot be same.")

    frm = db.query(InventoryLocation).filter(InventoryLocation.id == payload.from_location_id).first()
    to = db.query(InventoryLocation).filter(InventoryLocation.id == payload.to_location_id).first()
    if not frm or not frm.is_active:
        raise IndentError("Invalid from_location_id.")
    if not to or not to.is_active:
        raise IndentError("Invalid to_location_id.")

    if not payload.items:
        raise IndentError("Indent must have at least 1 item.")

    org = _org_code()
    indent_no = next_inv_doc_number(db, key="INDENT", prefix=f"{org}IND")

    pr = (payload.priority or "ROUTINE").upper().strip()
    priority = IndentPriority.STAT if pr == "STAT" else IndentPriority.ROUTINE

    ind = InvIndent(
        indent_number=indent_no,
        indent_date=today_db(),
        priority=priority,
        from_location_id=payload.from_location_id,
        to_location_id=payload.to_location_id,
        patient_id=payload.patient_id,
        visit_id=payload.visit_id,
        ipd_admission_id=payload.ipd_admission_id,
        encounter_type=payload.encounter_type,
        encounter_id=payload.encounter_id,
        status=IndentStatus.DRAFT,
        notes=payload.notes or "",
        created_by_id=user_id,
    )
    db.add(ind)
    db.flush()

    for it in payload.items:
        inv_item = db.query(InventoryItem).filter(InventoryItem.id == it.item_id).first()
        if not inv_item or not inv_item.is_active:
            raise IndentError(f"Invalid item_id={it.item_id}")

        rq = D(it.requested_qty)
        if rq <= 0:
            raise IndentError("requested_qty must be > 0")

        db.add(
            InvIndentItem(
                indent_id=ind.id,
                item_id=it.item_id,
                requested_qty=rq,
                approved_qty=D(0),
                issued_qty=D(0),
                is_stat=bool(it.is_stat),
                remarks=it.remarks or "",
            )
        )

    db.flush()
    return ind


def update_indent(db: Session, indent_id: int, payload, user_id: Optional[int]) -> InvIndent:
    ind = db.query(InvIndent).filter(InvIndent.id == indent_id).with_for_update().first()
    if not ind:
        raise IndentError("Indent not found.")
    if ind.status != IndentStatus.DRAFT:
        raise IndentError("Only DRAFT indent can be edited.")

    if payload.priority is not None:
        pr = (payload.priority or "ROUTINE").upper().strip()
        ind.priority = IndentPriority.STAT if pr == "STAT" else IndentPriority.ROUTINE
    if payload.notes is not None:
        ind.notes = payload.notes or ""

    db.flush()
    return ind


def submit_indent(db: Session, indent_id: int, user_id: Optional[int]) -> InvIndent:
    ind = (
        db.query(InvIndent)
        .options(selectinload(InvIndent.items))
        .filter(InvIndent.id == indent_id)
        .with_for_update()
        .first()
    )
    if not ind:
        raise IndentError("Indent not found.")
    if ind.status != IndentStatus.DRAFT:
        raise IndentError("Only DRAFT indent can be submitted.")
    if not ind.items:
        raise IndentError("Indent has no items.")
    if all(D(x.requested_qty) <= 0 for x in ind.items):
        raise IndentError("Indent items must have requested_qty > 0.")

    ind.status = IndentStatus.SUBMITTED
    ind.submitted_by_id = user_id
    ind.submitted_at = now_db()
    db.flush()
    return ind


def approve_indent(db: Session, indent_id: int, payload, user_id: Optional[int]) -> InvIndent:
    ind = (
        db.query(InvIndent)
        .options(selectinload(InvIndent.items))
        .filter(InvIndent.id == indent_id)
        .with_for_update()
        .first()
    )
    if not ind:
        raise IndentError("Indent not found.")
    if ind.status != IndentStatus.SUBMITTED:
        raise IndentError("Only SUBMITTED indent can be approved.")
    if not ind.items:
        raise IndentError("Indent has no items.")

    approved_map: Dict[int, Decimal] = {}
    if payload.items:
        for x in payload.items:
            approved_map[int(x.indent_item_id)] = D(x.approved_qty)

    for it in ind.items:
        req = D(it.requested_qty)
        ap = approved_map.get(it.id, req)
        if ap < 0:
            raise IndentError("approved_qty cannot be negative.")
        if ap > req:
            raise IndentError(f"approved_qty cannot exceed requested_qty for indent_item_id={it.id}")
        it.approved_qty = ap

    if payload.notes:
        ind.notes = (ind.notes or "") + ("\n" if ind.notes else "") + payload.notes

    ind.status = IndentStatus.APPROVED
    ind.approved_by_id = user_id
    ind.approved_at = now_db()
    db.flush()
    return ind


def cancel_indent(db: Session, indent_id: int, reason: str, user_id: Optional[int]) -> InvIndent:
    ind = db.query(InvIndent).filter(InvIndent.id == indent_id).with_for_update().first()
    if not ind:
        raise IndentError("Indent not found.")
    if ind.status in (IndentStatus.ISSUED, IndentStatus.PARTIALLY_ISSUED, IndentStatus.CLOSED):
        raise IndentError("Cannot cancel indent after issue.")
    if ind.status == IndentStatus.CANCELLED:
        return ind

    ind.status = IndentStatus.CANCELLED
    ind.cancel_reason = reason or ""
    ind.cancelled_by_id = user_id
    ind.cancelled_at = now_db()
    db.flush()
    return ind


# ============================================================
# ISSUE
# ============================================================
def create_issue_from_indent(db: Session, indent_id: int, payload, user_id: Optional[int]) -> InvIssue:
    ind = (
        db.query(InvIndent)
        .options(selectinload(InvIndent.items))
        .filter(InvIndent.id == indent_id)
        .with_for_update()
        .first()
    )
    if not ind:
        raise IndentError("Indent not found.")
    if ind.status not in (IndentStatus.APPROVED, IndentStatus.PARTIALLY_ISSUED):
        raise IndentError("Indent must be APPROVED/PARTIALLY_ISSUED before creating issue.")

    org = _org_code()
    issue_no = next_inv_doc_number(db, key="ISSUE", prefix=f"{org}ISS")

    issue = InvIssue(
        issue_number=issue_no,
        issue_date=today_db(),
        indent_id=ind.id,
        from_location_id=ind.from_location_id,
        to_location_id=ind.to_location_id,
        status=IssueStatus.DRAFT,
        notes=payload.notes or "",
        created_by_id=user_id,
    )
    db.add(issue)
    db.flush()

    if payload.items is None:
        for it in ind.items:
            remaining = D(it.approved_qty) - D(it.issued_qty)
            if remaining <= 0:
                continue
            db.add(
                InvIssueItem(
                    issue_id=issue.id,
                    indent_item_id=it.id,
                    item_id=it.item_id,
                    batch_id=None,
                    issued_qty=remaining,
                    remarks="",
                )
            )
    else:
        indent_map = {x.id: x for x in ind.items}
        for li in payload.items:
            qty = D(li.issued_qty)
            if qty <= 0:
                raise IndentError("issued_qty must be > 0")

            if li.indent_item_id:
                src = indent_map.get(int(li.indent_item_id))
                if not src:
                    raise IndentError(f"Invalid indent_item_id={li.indent_item_id}")
                remaining = D(src.approved_qty) - D(src.issued_qty)
                if qty > remaining:
                    raise IndentError(f"Issue qty exceeds remaining approved. Remaining {remaining}")
                item_id = src.item_id
            else:
                item_id = li.item_id

            db.add(
                InvIssueItem(
                    issue_id=issue.id,
                    indent_item_id=li.indent_item_id,
                    item_id=item_id,
                    batch_id=li.batch_id,
                    issued_qty=qty,
                    remarks=li.remarks or "",
                )
            )

    db.flush()
    return issue


def update_issue_item(db: Session, issue_item_id: int, payload, user_id: Optional[int]) -> InvIssueItem:
    li = (
        db.query(InvIssueItem)
        .filter(InvIssueItem.id == issue_item_id)
        .with_for_update()
        .first()
    )
    if not li:
        raise IndentError("Issue item not found.")

    issue = db.query(InvIssue).filter(InvIssue.id == li.issue_id).with_for_update().first()
    if not issue:
        raise IndentError("Issue not found.")
    if issue.status != IssueStatus.DRAFT:
        raise IndentError("Only DRAFT issue items can be edited.")

    # ---- issued_qty validation ----
    if payload.issued_qty is not None:
        q = D(payload.issued_qty)
        if q <= 0:
            raise IndentError("issued_qty must be > 0")

        if li.indent_item_id:
            src = (
                db.query(InvIndentItem)
                .filter(InvIndentItem.id == li.indent_item_id)
                .with_for_update()
                .first()
            )
            if src:
                already_posted = D(src.issued_qty)
                approved = D(src.approved_qty)
                remaining = approved - already_posted

                other_sum = (
                    db.query(func.coalesce(func.sum(InvIssueItem.issued_qty), 0))
                    .filter(
                        InvIssueItem.issue_id == li.issue_id,
                        InvIssueItem.indent_item_id == li.indent_item_id,
                        InvIssueItem.id != li.id,
                    )
                    .scalar()
                )
                remaining_effective = remaining - D(other_sum)

                if q > remaining_effective:
                    raise IndentError(f"Issued qty exceeds remaining approved. Remaining {remaining_effective}")

        li.issued_qty = q

    # ---- batch_id validation + allow CLEAR ----
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    if "batch_id" in fields_set:
        if payload.batch_id is None:
            # allow user selecting Auto FEFO (clear)
            li.batch_id = None
        else:
            bid = int(payload.batch_id)
            today = today_db()

            b = (
                db.query(ItemBatch)
                .filter(ItemBatch.id == bid)
                .with_for_update()
                .first()
            )
            if not b:
                raise IndentError("Selected batch not found.")

            if int(b.item_id) != int(li.item_id):
                raise IndentError("Selected batch does not match this item.")

            if int(b.location_id) != int(issue.from_location_id):
                raise IndentError("Selected batch is not from the Issue FROM location.")

            if not b.is_active or not b.is_saleable or b.status != BatchStatus.ACTIVE:
                raise IndentError("Selected batch is not active/saleable.")

            if b.expiry_date and b.expiry_date < today:
                raise IndentError("Selected batch is expired.")

            need = D(li.issued_qty)
            available = D(b.current_qty) - D(b.reserved_qty)
            if available < need:
                raise IndentError(f"Insufficient stock in selected batch. Available {available}, need {need}.")

            li.batch_id = bid

    if payload.remarks is not None:
        li.remarks = payload.remarks or ""

    db.flush()
    return li


def _recalc_indent_status(ind: InvIndent) -> None:
    any_partial = False
    all_done = True
    for it in ind.items or []:
        ap = D(it.approved_qty)
        iss = D(it.issued_qty)
        if iss < ap:
            all_done = False
            if iss > 0:
                any_partial = True

    if all_done:
        ind.status = IndentStatus.ISSUED
    elif any_partial:
        ind.status = IndentStatus.PARTIALLY_ISSUED
    else:
        ind.status = IndentStatus.APPROVED



def post_issue(db: Session, issue_id: int, user_id: Optional[int]) -> InvIssue:
    issue = (
        db.query(InvIssue)
        .options(selectinload(InvIssue.items))
        .filter(InvIssue.id == issue_id)
        .with_for_update()
        .first()
    )
    if not issue:
        raise IndentError("Issue not found.")
    if issue.status != IssueStatus.DRAFT:
        raise IndentError("Only DRAFT issue can be posted.")
    if not issue.items:
        raise IndentError("Issue has no items.")

    indent: Optional[InvIndent] = None
    if issue.indent_id:
        indent = (
            db.query(InvIndent)
            .options(selectinload(InvIndent.items))
            .filter(InvIndent.id == issue.indent_id)
            .with_for_update()
            .first()
        )

    for li in issue.items:
        qty = D(li.issued_qty)
        if qty <= 0:
            continue

        src_fast = lock_or_create_fast_stock(db, li.item_id, issue.from_location_id)
        dst_fast = lock_or_create_fast_stock(db, li.item_id, issue.to_location_id)

        picks = pick_batches_fefo(
            db,
            item_id=li.item_id,
            location_id=issue.from_location_id,
            qty_needed=qty,
            forced_batch_id=li.batch_id,
        )

        if not picks:
            raise IndentError(f"No stock batches available for item_id={li.item_id}")

        first_out_txn_id: Optional[int] = None
        first_batch_id: Optional[int] = None

        for b, take in picks:
            if first_batch_id is None:
                first_batch_id = int(b.id)

            # OUT from source
            b.current_qty = D(b.current_qty) - take
            src_fast.on_hand_qty = D(src_fast.on_hand_qty) - take

            out_tx = StockTransaction(
                location_id=issue.from_location_id,
                item_id=li.item_id,
                batch_id=b.id,
                txn_time=now_db(),
                txn_type="ISSUE_OUT",
                ref_type="ISSUE",
                ref_id=issue.id,
                ref_line_id=li.id,
                quantity_change=-take,
                unit_cost=b.unit_cost,
                mrp=b.mrp,
                remark=f"Issue OUT to location_id={issue.to_location_id}",
                user_id=user_id,
            )
            db.add(out_tx)
            db.flush()
            if first_out_txn_id is None:
                first_out_txn_id = out_tx.id

            # IN to destination
            dst_batch = ensure_dest_batch(db, src_batch=b, dest_location_id=issue.to_location_id)
            dst_batch.current_qty = D(dst_batch.current_qty) + take
            dst_fast.on_hand_qty = D(dst_fast.on_hand_qty) + take

            in_tx = StockTransaction(
                location_id=issue.to_location_id,
                item_id=li.item_id,
                batch_id=dst_batch.id,
                txn_time=now_db(),
                txn_type="ISSUE_IN",
                ref_type="ISSUE",
                ref_id=issue.id,
                ref_line_id=li.id,
                quantity_change=take,
                unit_cost=dst_batch.unit_cost,
                mrp=dst_batch.mrp,
                remark=f"Issue IN from location_id={issue.from_location_id}",
                user_id=user_id,
            )
            db.add(in_tx)

      
        li.batch_id = first_batch_id
        li.stock_txn_id = first_out_txn_id

        if indent and li.indent_item_id:
            src_indent_item = next((x for x in indent.items if x.id == li.indent_item_id), None)
            if src_indent_item:
                src_indent_item.issued_qty = D(src_indent_item.issued_qty) + qty

    issue.status = IssueStatus.POSTED
    issue.posted_by_id = user_id
    issue.posted_at = now_db()

    if indent:
        _recalc_indent_status(indent)

    db.flush()
    return issue



def cancel_issue(db: Session, issue_id: int, reason: str, user_id: Optional[int]) -> InvIssue:
    issue = (
        db.query(InvIssue)
        .options(selectinload(InvIssue.items))
        .filter(InvIssue.id == issue_id)
        .with_for_update()
        .first()
    )
    if not issue:
        raise IndentError("Issue not found.")
    if issue.status == IssueStatus.CANCELLED:
        return issue

    if issue.status == IssueStatus.DRAFT:
        issue.status = IssueStatus.CANCELLED
        issue.cancel_reason = reason or ""
        issue.cancelled_by_id = user_id
        issue.cancelled_at = now_db()
        db.flush()
        return issue

    if issue.status != IssueStatus.POSTED:
        raise IndentError("Only DRAFT/POSTED issue can be cancelled.")

    indent: Optional[InvIndent] = None
    if issue.indent_id:
        indent = (
            db.query(InvIndent)
            .options(selectinload(InvIndent.items))
            .filter(InvIndent.id == issue.indent_id)
            .with_for_update()
            .first()
        )

    txns = (
        db.query(StockTransaction)
        .filter(StockTransaction.ref_type == "ISSUE", StockTransaction.ref_id == issue.id)
        .order_by(StockTransaction.id.asc())
        .with_for_update()
        .all()
    )

    in_txns = [t for t in txns if t.txn_type == "ISSUE_IN" and D(t.quantity_change) > 0]
    out_txns = [t for t in txns if t.txn_type == "ISSUE_OUT" and D(t.quantity_change) < 0]

    for t in in_txns:
        qty = D(t.quantity_change)
        dst_batch = db.query(ItemBatch).filter(ItemBatch.id == t.batch_id).with_for_update().first()
        if not dst_batch:
            raise IndentError("Cancel failed: destination batch missing.")
        if D(dst_batch.current_qty) < qty:
            raise IndentError("Cannot cancel: destination stock already consumed (not enough qty to reverse).")

        dst_fast = lock_or_create_fast_stock(db, t.item_id, t.location_id)
        dst_batch.current_qty = D(dst_batch.current_qty) - qty
        dst_fast.on_hand_qty = D(dst_fast.on_hand_qty) - qty

        db.add(
            StockTransaction(
                location_id=t.location_id,
                item_id=t.item_id,
                batch_id=t.batch_id,
                txn_time=now_db(),
                txn_type="ISSUE_IN_REV",
                ref_type="ISSUE_CANCEL",
                ref_id=issue.id,
                ref_line_id=t.ref_line_id,
                quantity_change=-qty,
                unit_cost=t.unit_cost,
                mrp=t.mrp,
                remark="Cancel Issue: reverse IN",
                user_id=user_id,
            )
        )

    for t in out_txns:
        qty = abs(D(t.quantity_change))
        src_batch = db.query(ItemBatch).filter(ItemBatch.id == t.batch_id).with_for_update().first()
        if not src_batch:
            raise IndentError("Cancel failed: source batch missing.")

        src_fast = lock_or_create_fast_stock(db, t.item_id, t.location_id)
        src_batch.current_qty = D(src_batch.current_qty) + qty
        src_fast.on_hand_qty = D(src_fast.on_hand_qty) + qty

        db.add(
            StockTransaction(
                location_id=t.location_id,
                item_id=t.item_id,
                batch_id=t.batch_id,
                txn_time=now_db(),
                txn_type="ISSUE_OUT_REV",
                ref_type="ISSUE_CANCEL",
                ref_id=issue.id,
                ref_line_id=t.ref_line_id,
                quantity_change=qty,
                unit_cost=t.unit_cost,
                mrp=t.mrp,
                remark="Cancel Issue: reverse OUT",
                user_id=user_id,
            )
        )

    if indent:
        for li in issue.items or []:
            if li.indent_item_id:
                it = next((x for x in indent.items if x.id == li.indent_item_id), None)
                if it:
                    it.issued_qty = max(D(0), D(it.issued_qty) - D(li.issued_qty))
        _recalc_indent_status(indent)

    issue.status = IssueStatus.CANCELLED
    issue.cancel_reason = reason or ""
    issue.cancelled_by_id = user_id
    issue.cancelled_at = now_db()

    db.flush()
    return issue
