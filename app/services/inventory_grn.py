# FILE: app/services/inventory_grn.py
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.pharmacy_inventory import (
    GRN, GRNItem, GRNStatus,
    ItemBatch, StockTransaction,
    PurchaseOrder, PurchaseOrderItem, POStatus,
    InventoryItem, InvNumberSeries
)
from app.models.accounts_supplier import SupplierInvoice, SupplierInvoiceStatus

# ----------------- helpers -----------------
def D(x) -> Decimal:
    try:
        if x is None or x == "":
            return Decimal("0")
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid decimal value: {x}")

def nonneg(x: Decimal, name: str):
    if x < 0:
        raise HTTPException(status_code=400, detail=f"{name} cannot be negative")

def _parse_payment_terms_days(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    d = int(m.group(1))
    return d if d >= 0 else None

def _next_number(db: Session, key: str, on_date: date | None = None) -> str:
    on_date = on_date or date.today()
    date_key = int(on_date.strftime("%Y%m%d"))

    row = (
        db.query(InvNumberSeries)
        .filter(InvNumberSeries.key == key, InvNumberSeries.date_key == date_key)
        .with_for_update()
        .one_or_none()
    )
    if not row:
        row = InvNumberSeries(key=key, date_key=date_key, next_seq=1)
        db.add(row)
        db.flush()

    seq = row.next_seq
    row.next_seq = seq + 1
    db.flush()

    return f"{key}-{on_date.strftime('%y%m%d')}-{seq:04d}"

def _compute_line_amounts(ln: GRNItem) -> dict:
    qty = D(ln.quantity)
    rate = D(ln.unit_cost)
    disc_pct = D(ln.discount_percent)
    disc_amt = D(ln.discount_amount)

    nonneg(qty, "quantity")
    nonneg(rate, "unit_cost")
    nonneg(disc_pct, "discount_percent")
    nonneg(disc_amt, "discount_amount")

    gross = qty * rate
    disc = disc_amt if disc_amt > 0 else (gross * disc_pct / Decimal("100") if disc_pct > 0 else Decimal("0"))
    base = gross - disc
    if base < 0:
        base = Decimal("0")

    cgst_p = D(ln.cgst_percent)
    sgst_p = D(ln.sgst_percent)
    igst_p = D(ln.igst_percent)
    tax_p = D(ln.tax_percent)

    split = cgst_p + sgst_p + igst_p

    if split > 0:
        cgst = base * cgst_p / Decimal("100")
        sgst = base * sgst_p / Decimal("100")
        igst = base * igst_p / Decimal("100")
    elif tax_p > 0:
        # fallback split 50/50 CGST/SGST (common default)
        half = tax_p / Decimal("2")
        cgst = base * half / Decimal("100")
        sgst = base * half / Decimal("100")
        igst = Decimal("0")
    else:
        cgst = sgst = igst = Decimal("0")

    total = base + cgst + sgst + igst
    return {
        "gross": gross,
        "discount": disc,
        "taxable": base,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "line_total": total,
    }

def _update_po_status_from_receipts(po: PurchaseOrder):
    if po.status in (POStatus.CANCELLED, POStatus.CLOSED):
        return

    items = po.items or []
    if not items:
        return

    all_done = True
    any_received = False

    for it in items:
        ordered = Decimal(it.ordered_qty or 0)
        received = Decimal(it.received_qty or 0)
        if received > 0:
            any_received = True
        if ordered > 0 and received < ordered:
            all_done = False

    if all_done:
        po.status = POStatus.COMPLETED
    elif any_received:
        po.status = POStatus.PARTIALLY_RECEIVED
    else:
        # keep as SENT/APPROVED
        if po.status == POStatus.DRAFT:
            po.status = POStatus.APPROVED

def _create_or_update_supplier_invoice(db: Session, grn: GRN, po: PurchaseOrder | None):
    invoice_amount = D(grn.supplier_invoice_amount)

    # If no invoice amount, skip ledger entry (optional)
    if invoice_amount <= 0:
        return

    if not (grn.invoice_number or "").strip():
        raise HTTPException(status_code=400, detail="Invoice number is required for ledger when invoice amount > 0")

    # prevent duplicate invoice no per supplier
    dup = (
        db.query(SupplierInvoice)
        .filter(
            SupplierInvoice.supplier_id == grn.supplier_id,
            SupplierInvoice.invoice_number == grn.invoice_number,
            SupplierInvoice.grn_id != grn.id,
        )
        .first()
    )
    if dup:
        raise HTTPException(status_code=409, detail="Supplier invoice number already exists for this supplier")

    inv = db.query(SupplierInvoice).filter(SupplierInvoice.grn_id == grn.id).one_or_none()

    base_date = grn.invoice_date or grn.received_date
    due_days = _parse_payment_terms_days(po.payment_terms) if (po and po.payment_terms) else None
    due_date = (base_date + timedelta(days=due_days)) if (base_date and due_days is not None) else None

    paid = D(inv.paid_amount) if inv else Decimal("0")
    outstanding = invoice_amount - paid
    if outstanding < 0:
        outstanding = Decimal("0")

    status = SupplierInvoiceStatus.UNPAID.value
    if paid > 0 and outstanding > 0:
        status = SupplierInvoiceStatus.PARTIAL.value
    elif outstanding == 0 and invoice_amount > 0:
        status = SupplierInvoiceStatus.PAID.value

    is_overdue = bool(due_date and due_date < date.today() and outstanding > 0)

    if not inv:
        inv = SupplierInvoice(
            grn_id=grn.id,
            supplier_id=grn.supplier_id,
            location_id=grn.location_id,
            grn_number=grn.grn_number,
            invoice_number=grn.invoice_number,
            invoice_date=grn.invoice_date,
            due_date=due_date,
            currency=getattr(po, "currency", "INR") if po else "INR",
            invoice_amount=invoice_amount,
            paid_amount=paid,
            outstanding_amount=outstanding,
            status=status,
            is_overdue=is_overdue,
            notes=grn.notes or "",
        )
        db.add(inv)
    else:
        inv.grn_number = grn.grn_number
        inv.location_id = grn.location_id
        inv.invoice_date = grn.invoice_date
        inv.due_date = due_date
        inv.invoice_amount = invoice_amount
        inv.outstanding_amount = outstanding
        inv.status = status
        inv.is_overdue = is_overdue
        inv.notes = grn.notes or ""

# ----------------- public API -----------------
def create_grn_draft(db: Session, user_id: int | None, payload: dict) -> GRN:
    received_date = payload.get("received_date") or date.today()
    grn_no = _next_number(db, "GRN", received_date)

    grn = GRN(
        grn_number=grn_no,
        po_id=payload.get("po_id"),
        supplier_id=payload["supplier_id"],
        location_id=payload["location_id"],
        received_date=received_date,
        invoice_number=payload.get("invoice_number") or "",
        invoice_date=payload.get("invoice_date"),
        supplier_invoice_amount=D(payload.get("supplier_invoice_amount", "0")),
        freight_amount=D(payload.get("freight_amount", "0")),
        other_charges=D(payload.get("other_charges", "0")),
        round_off=D(payload.get("round_off", "0")),
        notes=payload.get("notes") or "",
        difference_reason=payload.get("difference_reason") or "",
        status=GRNStatus.DRAFT.value,
        created_by_id=user_id,
    )
    db.add(grn)
    db.flush()

    items = payload.get("items") or []
    for it in items:
        ln = GRNItem(
            grn_id=grn.id,
            po_item_id=it.get("po_item_id"),
            item_id=it["item_id"],
            batch_no=it.get("batch_no") or "",
            expiry_date=it.get("expiry_date"),
            quantity=D(it.get("quantity", "0")),
            free_quantity=D(it.get("free_quantity", "0")),
            unit_cost=D(it.get("unit_cost", "0")),
            mrp=D(it.get("mrp", "0")),
            discount_percent=D(it.get("discount_percent", "0")),
            discount_amount=D(it.get("discount_amount", "0")),
            cgst_percent=D(it.get("cgst_percent", "0")),
            sgst_percent=D(it.get("sgst_percent", "0")),
            igst_percent=D(it.get("igst_percent", "0")),
            tax_percent=D(it.get("tax_percent", "0")),
            scheme=it.get("scheme") or "",
            remarks=it.get("remarks") or "",
        )
        db.add(ln)

    db.flush()
    return grn

def update_grn_draft(db: Session, grn_id: int, payload: dict) -> GRN:
    grn = (
        db.query(GRN)
        .options(selectinload(GRN.items))
        .filter(GRN.id == grn_id)
        .one_or_none()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if grn.status != GRNStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be updated")

    grn.po_id = payload.get("po_id")
    grn.supplier_id = payload["supplier_id"]
    grn.location_id = payload["location_id"]
    grn.received_date = payload.get("received_date") or grn.received_date
    grn.invoice_number = payload.get("invoice_number") or ""
    grn.invoice_date = payload.get("invoice_date")
    grn.supplier_invoice_amount = D(payload.get("supplier_invoice_amount", "0"))
    grn.freight_amount = D(payload.get("freight_amount", "0"))
    grn.other_charges = D(payload.get("other_charges", "0"))
    grn.round_off = D(payload.get("round_off", "0"))
    grn.notes = payload.get("notes") or ""
    grn.difference_reason = payload.get("difference_reason") or ""

    # replace items (simple + safe)
    grn.items.clear()
    db.flush()

    items = payload.get("items") or []
    for it in items:
        db.add(GRNItem(
            grn_id=grn.id,
            po_item_id=it.get("po_item_id"),
            item_id=it["item_id"],
            batch_no=it.get("batch_no") or "",
            expiry_date=it.get("expiry_date"),
            quantity=D(it.get("quantity", "0")),
            free_quantity=D(it.get("free_quantity", "0")),
            unit_cost=D(it.get("unit_cost", "0")),
            mrp=D(it.get("mrp", "0")),
            discount_percent=D(it.get("discount_percent", "0")),
            discount_amount=D(it.get("discount_amount", "0")),
            cgst_percent=D(it.get("cgst_percent", "0")),
            sgst_percent=D(it.get("sgst_percent", "0")),
            igst_percent=D(it.get("igst_percent", "0")),
            tax_percent=D(it.get("tax_percent", "0")),
            scheme=it.get("scheme") or "",
            remarks=it.get("remarks") or "",
        ))

    db.flush()
    return grn

def post_grn(db: Session, grn_id: int, user_id: int | None, difference_reason: str = "") -> GRN:
    grn = (
        db.query(GRN)
        .options(selectinload(GRN.items))
        .filter(GRN.id == grn_id)
        .with_for_update()
        .one_or_none()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if grn.status != GRNStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be posted")
    if not grn.items:
        raise HTTPException(status_code=400, detail="GRN has no items")

    if difference_reason:
        grn.difference_reason = difference_reason

    # Validate lines for POST
    for ln in grn.items:
        if not (ln.batch_no or "").strip():
            raise HTTPException(status_code=400, detail="Batch No is required for all lines before POST")
        if D(ln.quantity) <= 0 and D(ln.free_quantity) <= 0:
            raise HTTPException(status_code=400, detail="Qty or Free must be > 0 in all lines")

    # Recompute totals (server side)
    taxable = cgst = sgst = igst = disc_total = Decimal("0")
    for ln in grn.items:
        calc = _compute_line_amounts(ln)
        ln.taxable_amount = calc["taxable"]
        ln.cgst_amount = calc["cgst"]
        ln.sgst_amount = calc["sgst"]
        ln.igst_amount = calc["igst"]
        ln.line_total = calc["line_total"]
        taxable += calc["taxable"]
        cgst += calc["cgst"]
        sgst += calc["sgst"]
        igst += calc["igst"]
        disc_total += calc["discount"]

    extras = D(grn.freight_amount) + D(grn.other_charges) + D(grn.round_off)
    calculated = taxable + cgst + sgst + igst + extras
    invoice = D(grn.supplier_invoice_amount)
    diff = invoice - calculated

    grn.taxable_amount = taxable
    grn.cgst_amount = cgst
    grn.sgst_amount = sgst
    grn.igst_amount = igst
    grn.discount_amount = disc_total
    grn.calculated_grn_amount = calculated
    grn.amount_difference = diff

    if invoice > 0 and abs(diff) >= Decimal("0.01") and not (grn.difference_reason or "").strip():
        raise HTTPException(status_code=400, detail="Difference reason required (invoice mismatch)")

    # Lock PO (optional) and update received qty
    po = None
    if grn.po_id:
        po = (
            db.query(PurchaseOrder)
            .options(selectinload(PurchaseOrder.items))
            .filter(PurchaseOrder.id == grn.po_id)
            .with_for_update()
            .one_or_none()
        )
        if not po:
            raise HTTPException(status_code=400, detail="Linked PO not found")
        if po.status == POStatus.CANCELLED:
            raise HTTPException(status_code=400, detail="Cannot post GRN for a cancelled PO")

    # Update stock + batches + txns
    for ln in grn.items:
        qty_in = D(ln.quantity) + D(ln.free_quantity)

        batch = (
            db.query(ItemBatch)
            .filter(
                ItemBatch.item_id == ln.item_id,
                ItemBatch.location_id == grn.location_id,
                ItemBatch.batch_no == ln.batch_no,
                ItemBatch.expiry_date == ln.expiry_date,
            )
            .with_for_update()
            .one_or_none()
        )
        if not batch:
            batch = ItemBatch(
                item_id=ln.item_id,
                location_id=grn.location_id,
                batch_no=ln.batch_no,
                expiry_date=ln.expiry_date,
                current_qty=Decimal("0"),
                unit_cost=D(ln.unit_cost),
                mrp=D(ln.mrp),
                tax_percent=D(ln.tax_percent),
            )
            db.add(batch)
            db.flush()

        batch.current_qty = D(batch.current_qty) + qty_in
        batch.unit_cost = D(ln.unit_cost)
        batch.mrp = D(ln.mrp)
        # set tax_percent as best available
        split = D(ln.cgst_percent) + D(ln.sgst_percent) + D(ln.igst_percent)
        batch.tax_percent = split if split > 0 else D(ln.tax_percent)

        ln.batch_id = batch.id

        db.add(StockTransaction(
            location_id=grn.location_id,
            item_id=ln.item_id,
            batch_id=batch.id,
            txn_time=datetime.utcnow(),
            txn_type="GRN",
            ref_type="GRN",
            ref_id=grn.id,
            quantity_change=qty_in,
            unit_cost=D(ln.unit_cost),
            mrp=D(ln.mrp),
            remark=f"GRN {grn.grn_number} ({grn.invoice_number})",
            user_id=user_id,
        ))

        # Optional auto-update item master purchase defaults (dynamic system)
        item = db.query(InventoryItem).filter(InventoryItem.id == ln.item_id).one_or_none()
        if item:
            item.default_price = D(ln.unit_cost)
            item.default_mrp = D(ln.mrp)
            # keep tax sensible
            tp = (D(ln.cgst_percent) + D(ln.sgst_percent) + D(ln.igst_percent))
            item.default_tax_percent = tp if tp > 0 else D(ln.tax_percent)

        # PO received qty (count only billed qty, not free)
        if po:
            po_item = None
            if ln.po_item_id:
                po_item = next((x for x in po.items if x.id == ln.po_item_id), None)
            if not po_item:
                po_item = next((x for x in po.items if x.item_id == ln.item_id), None)

            if po_item:
                po_item.received_qty = D(po_item.received_qty) + D(ln.quantity)

    if po:
        _update_po_status_from_receipts(po)

    # Ledger invoice auto-create/update
    _create_or_update_supplier_invoice(db, grn, po)

    grn.status = GRNStatus.POSTED.value
    grn.posted_by_id = user_id
    grn.posted_at = datetime.utcnow()

    db.flush()
    return grn

def create_grn_from_po(db: Session, po_id: int, user_id: int | None) -> GRN:
    po = (
        db.query(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item))
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status in (POStatus.CANCELLED, POStatus.CLOSED):
        raise HTTPException(status_code=400, detail="Cannot create GRN from cancelled/closed PO")

    pending = []
    for li in po.items or []:
        ordered = Decimal(li.ordered_qty or 0)
        received = Decimal(li.received_qty or 0)
        remain = ordered - received
        if remain > 0:
            pending.append(li)

    if not pending:
        raise HTTPException(status_code=400, detail="No pending items in this PO")

    payload = {
        "po_id": po.id,
        "supplier_id": po.supplier_id,
        "location_id": po.location_id,
        "received_date": date.today(),
        "invoice_number": "",
        "invoice_date": None,
        "supplier_invoice_amount": "0",
        "freight_amount": "0",
        "other_charges": "0",
        "round_off": "0",
        "notes": "",
        "difference_reason": "",
        "items": [
            {
                "po_item_id": li.id,
                "item_id": li.item_id,
                "batch_no": "",  # DRAFT ok; enforce on POST
                "expiry_date": None,
                "quantity": str(ordered - received),
                "free_quantity": "0",
                "unit_cost": str(li.unit_cost or 0),
                "mrp": str(li.mrp or 0),
                "tax_percent": str(li.tax_percent or 0),
                "discount_percent": "0",
                "discount_amount": "0",
                "cgst_percent": "0",
                "sgst_percent": "0",
                "igst_percent": "0",
                "scheme": "",
                "remarks": "",
            }
            for li in pending
        ],
    }
    return create_grn_draft(db, user_id, payload)
