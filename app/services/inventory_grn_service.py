from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.pharmacy_inventory import (
    GRN, GRNItem, GRNStatus,
    ItemBatch,
    InventoryItem,
    StockTransaction,
    PurchaseOrder, PurchaseOrderItem, POStatus,
)
from app.models.accounts_supplier import SupplierInvoice, SupplierInvoiceStatus
from app.services.inventory_number_series import next_document_number


def d(x: Any) -> Decimal:
    if x is None or x == "":
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _calc_line_amounts(li: GRNItem) -> Dict[str, Decimal]:
    qty = d(li.quantity)
    rate = d(li.unit_cost)

    gross = qty * rate

    disc_amt = d(li.discount_amount)
    disc_pct = d(li.discount_percent)
    disc = disc_amt if disc_amt > 0 else (gross * disc_pct / Decimal("100") if disc_pct > 0 else Decimal("0"))

    taxable = gross - disc
    if taxable < 0:
        taxable = Decimal("0")

    # tax split wins; otherwise fallback tax_percent
    cgst_p = d(li.cgst_percent)
    sgst_p = d(li.sgst_percent)
    igst_p = d(li.igst_percent)
    tax_p = d(li.tax_percent)

    split = cgst_p + sgst_p + igst_p
    if split > 0:
        cgst_amt = taxable * cgst_p / Decimal("100")
        sgst_amt = taxable * sgst_p / Decimal("100")
        igst_amt = taxable * igst_p / Decimal("100")
    else:
        # fallback: store into igst_amount for simplicity
        cgst_amt = Decimal("0")
        sgst_amt = Decimal("0")
        igst_amt = taxable * tax_p / Decimal("100")

    total = taxable + cgst_amt + sgst_amt + igst_amt

    return {
        "gross": gross,
        "discount": disc,
        "taxable": taxable,
        "cgst": cgst_amt,
        "sgst": sgst_amt,
        "igst": igst_amt,
        "total": total,
    }


def _get_or_create_batch(
    db: Session,
    *,
    item_id: int,
    location_id: int,
    batch_no: str,
    expiry_date: Optional[date],
) -> ItemBatch:
    q = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.batch_no == batch_no,
            ItemBatch.expiry_date == expiry_date,
            ItemBatch.is_active == True,  # noqa
        )
        .with_for_update()
    )
    b = q.one_or_none()
    if b:
        return b

    b = ItemBatch(
        item_id=item_id,
        location_id=location_id,
        batch_no=batch_no,
        expiry_date=expiry_date,
        current_qty=Decimal("0"),
        unit_cost=Decimal("0"),
        mrp=Decimal("0"),
        tax_percent=Decimal("0"),
        is_active=True,
        is_saleable=True,
        status="ACTIVE",
    )
    db.add(b)
    db.flush()
    return b


def _update_po_status(db: Session, po: PurchaseOrder) -> None:
    if not po:
        return
    if po.status in (POStatus.CANCELLED, POStatus.CLOSED):
        return

    items = po.items or []
    if not items:
        return

    any_received = False
    all_received = True

    for li in items:
        ordered = d(li.ordered_qty)
        received = d(li.received_qty)
        if received > Decimal("0"):
            any_received = True
        if received + Decimal("0.0000") < ordered:
            all_received = False

    if all_received:
        po.status = POStatus.COMPLETED
    elif any_received:
        po.status = POStatus.PARTIALLY_RECEIVED
    else:
        # keep current (SENT/APPROVED)
        if po.status == POStatus.DRAFT:
            po.status = POStatus.SENT


def _upsert_supplier_invoice(db: Session, grn: GRN) -> None:
    """
    Auto-create supplier ledger invoice on GRN POST.
    """
    inv = db.query(SupplierInvoice).filter(SupplierInvoice.grn_id == grn.id).with_for_update().one_or_none()

    # choose invoice amount (supplier says net > 0 else use calculated)
    invoice_amt = d(grn.supplier_invoice_amount)
    if invoice_amt <= 0:
        invoice_amt = d(grn.calculated_grn_amount)

    paid = d(inv.paid_amount) if inv else Decimal("0")
    outstanding = invoice_amt - paid
    if outstanding < 0:
        outstanding = Decimal("0")

    status = SupplierInvoiceStatus.UNPAID.value
    if invoice_amt == 0:
        status = SupplierInvoiceStatus.PAID.value
    elif paid > 0 and outstanding > 0:
        status = SupplierInvoiceStatus.PARTIAL.value
    elif paid >= invoice_amt and invoice_amt > 0:
        status = SupplierInvoiceStatus.PAID.value

    # basic due date logic (later you can add supplier credit days)
    due = None
    if grn.invoice_date:
        due = grn.invoice_date + timedelta(days=30)

    is_overdue = bool(due and due < date.today() and status in (SupplierInvoiceStatus.UNPAID.value, SupplierInvoiceStatus.PARTIAL.value))

    if not inv:
        inv = SupplierInvoice(
            grn_id=grn.id,
            supplier_id=grn.supplier_id,
            location_id=grn.location_id,
            grn_number=grn.grn_number,
            invoice_number=grn.invoice_number or "",
            invoice_date=grn.invoice_date,
            due_date=due,
            currency="INR",
            invoice_amount=invoice_amt,
            paid_amount=Decimal("0"),
            outstanding_amount=invoice_amt,
            status=status,
            is_overdue=is_overdue,
            last_payment_date=None,
            notes=f"Auto-created from GRN {grn.grn_number}",
        )
        db.add(inv)
    else:
        inv.supplier_id = grn.supplier_id
        inv.location_id = grn.location_id
        inv.grn_number = grn.grn_number
        inv.invoice_number = grn.invoice_number or ""
        inv.invoice_date = grn.invoice_date
        inv.due_date = due
        inv.invoice_amount = invoice_amt
        inv.outstanding_amount = outstanding
        inv.status = status
        inv.is_overdue = is_overdue

    db.flush()


def create_grn_draft(db: Session, created_by_id: Optional[int], payload: Dict[str, Any]) -> GRN:
    supplier_id = int(payload.get("supplier_id") or 0)
    location_id = int(payload.get("location_id") or 0)
    if not supplier_id:
        raise HTTPException(status_code=400, detail="supplier_id is required")
    if not location_id:
        raise HTTPException(status_code=400, detail="location_id is required")

    rcv_date = payload.get("received_date") or date.today()
    if isinstance(rcv_date, str):
        rcv_date = date.fromisoformat(rcv_date)

    grn_number = next_document_number(db, key="GRN", prefix="GRN", doc_date=rcv_date, pad=4)

    grn = GRN(
        grn_number=grn_number,
        po_id=payload.get("po_id"),
        supplier_id=supplier_id,
        location_id=location_id,
        received_date=rcv_date,
        invoice_number=(payload.get("invoice_number") or "").strip(),
        invoice_date=payload.get("invoice_date"),
        supplier_invoice_amount=d(payload.get("supplier_invoice_amount")),
        freight_amount=d(payload.get("freight_amount")),
        other_charges=d(payload.get("other_charges")),
        round_off=d(payload.get("round_off")),
        difference_reason=(payload.get("difference_reason") or "").strip(),
        notes=(payload.get("notes") or "").strip(),
        status=GRNStatus.DRAFT.value,
        created_by_id=created_by_id,
    )
    db.add(grn)
    db.flush()

    items = payload.get("items") or []
    for li in items:
        db.add(
            GRNItem(
                grn_id=grn.id,
                po_item_id=li.get("po_item_id"),
                item_id=int(li["item_id"]),
                batch_no=(li.get("batch_no") or "").strip(),
                expiry_date=li.get("expiry_date"),
                quantity=d(li.get("quantity")),
                free_quantity=d(li.get("free_quantity")),
                unit_cost=d(li.get("unit_cost")),
                mrp=d(li.get("mrp")),
                discount_percent=d(li.get("discount_percent")),
                discount_amount=d(li.get("discount_amount")),
                tax_percent=d(li.get("tax_percent")),
                cgst_percent=d(li.get("cgst_percent")),
                sgst_percent=d(li.get("sgst_percent")),
                igst_percent=d(li.get("igst_percent")),
                scheme=(li.get("scheme") or "").strip(),
                remarks=(li.get("remarks") or "").strip(),
            )
        )

    db.flush()
    return grn


def update_grn_draft(db: Session, grn_id: int, payload: Dict[str, Any]) -> GRN:
    grn = db.query(GRN).filter(GRN.id == grn_id).with_for_update().one_or_none()
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if str(grn.status) != GRNStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be edited")

    # header
    for k in [
        "received_date", "invoice_number", "invoice_date",
        "supplier_invoice_amount", "freight_amount", "other_charges", "round_off",
        "notes", "difference_reason",
    ]:
        if k in payload and payload[k] is not None:
            v = payload[k]
            if k in ("supplier_invoice_amount", "freight_amount", "other_charges", "round_off"):
                v = d(v)
            if k == "invoice_number":
                v = (v or "").strip()
            setattr(grn, k, v)

    # replace items if provided
    if "items" in payload and payload["items"] is not None:
        db.query(GRNItem).filter(GRNItem.grn_id == grn.id).delete()
        db.flush()

        for li in payload["items"]:
            item_id = int(li["item_id"])
            it = db.get(InventoryItem, item_id)
            if not it:
                raise HTTPException(status_code=400, detail=f"Invalid item_id: {item_id}")

            db.add(
                GRNItem(
                    grn_id=grn.id,
                    po_item_id=li.get("po_item_id"),
                    item_id=item_id,
                    batch_no=(li.get("batch_no") or "").strip(),
                    expiry_date=li.get("expiry_date"),
                    quantity=d(li.get("quantity")),
                    free_quantity=d(li.get("free_quantity")),
                    unit_cost=d(li.get("unit_cost")),
                    mrp=d(li.get("mrp")),
                    discount_percent=d(li.get("discount_percent")),
                    discount_amount=d(li.get("discount_amount")),
                    tax_percent=d(li.get("tax_percent")),
                    cgst_percent=d(li.get("cgst_percent")),
                    sgst_percent=d(li.get("sgst_percent")),
                    igst_percent=d(li.get("igst_percent")),
                    scheme=(li.get("scheme") or "").strip(),
                    remarks=(li.get("remarks") or "").strip(),
                )
            )

    db.flush()
    return grn


def create_grn_from_po(db: Session, po_id: int, created_by_id: Optional[int]) -> GRN:
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).with_for_update().one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status in (POStatus.CANCELLED, POStatus.CLOSED):
        raise HTTPException(status_code=400, detail="Cannot create GRN from CANCELLED/CLOSED PO")

    rcv_date = date.today()
    grn_number = next_document_number(db, key="GRN", prefix="GRN", doc_date=rcv_date, pad=4)

    grn = GRN(
        grn_number=grn_number,
        po_id=po.id,
        supplier_id=po.supplier_id,
        location_id=po.location_id,
        received_date=rcv_date,
        invoice_number="",
        invoice_date=None,
        supplier_invoice_amount=Decimal("0"),
        freight_amount=Decimal("0"),
        other_charges=Decimal("0"),
        round_off=Decimal("0"),
        notes=f"Auto-created from PO {po.po_number}",
        status=GRNStatus.DRAFT.value,
        created_by_id=created_by_id,
    )
    db.add(grn)
    db.flush()

    created = 0
    for li in po.items or []:
        remaining = d(li.ordered_qty) - d(li.received_qty)
        if remaining <= Decimal("0.0000"):
            continue

        db.add(
            GRNItem(
                grn_id=grn.id,
                po_item_id=li.id,
                item_id=li.item_id,
                batch_no="",
                expiry_date=None,
                quantity=remaining,          # autofill remaining
                free_quantity=Decimal("0"),
                unit_cost=d(li.unit_cost),
                mrp=d(li.mrp),
                tax_percent=d(li.tax_percent),
                cgst_percent=Decimal("0"),
                sgst_percent=Decimal("0"),
                igst_percent=Decimal("0"),
                discount_percent=Decimal("0"),
                discount_amount=Decimal("0"),
                scheme="",
                remarks="",
            )
        )
        created += 1

    if created == 0:
        raise HTTPException(status_code=400, detail="No pending quantities in this PO")

    db.flush()
    return grn


def post_grn(db: Session, grn_id: int, posted_by_user_id: Optional[int], difference_reason: str = "") -> GRN:
    grn = (
        db.query(GRN)
        .filter(GRN.id == grn_id)
        .with_for_update()
        .one_or_none()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")

    if str(grn.status) != GRNStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be posted")

    lines = db.query(GRNItem).filter(GRNItem.grn_id == grn.id).with_for_update().all()
    if not lines:
        raise HTTPException(status_code=400, detail="GRN has no line items")

    # posting validations
    if not (grn.invoice_number or "").strip():
        raise HTTPException(status_code=400, detail="Invoice number is required to POST GRN")

    # ensure lines have batch
    for li in lines:
        if not li.item_id:
            raise HTTPException(status_code=400, detail="Invalid item in GRN lines")
        if d(li.quantity) <= 0 and d(li.free_quantity) <= 0:
            raise HTTPException(status_code=400, detail="Each line must have quantity or free_quantity > 0")
        if not (li.batch_no or "").strip():
            raise HTTPException(status_code=400, detail="Batch No is required for all lines on POST")

    # calculate totals
    sub_total = Decimal("0")
    disc_total = Decimal("0")
    taxable_total = Decimal("0")
    cgst_total = Decimal("0")
    sgst_total = Decimal("0")
    igst_total = Decimal("0")
    calc_total = Decimal("0")

    for li in lines:
        am = _calc_line_amounts(li)
        sub_total += am["gross"]
        disc_total += am["discount"]
        taxable_total += am["taxable"]
        cgst_total += am["cgst"]
        sgst_total += am["sgst"]
        igst_total += am["igst"]
        calc_total += am["total"]

        # write computed back into line
        li.taxable_amount = am["taxable"]
        li.cgst_amount = am["cgst"]
        li.sgst_amount = am["sgst"]
        li.igst_amount = am["igst"]
        li.line_total = am["total"]

    extras = d(grn.freight_amount) + d(grn.other_charges) + d(grn.round_off)
    calc_total = calc_total + extras

    invoice_amt = d(grn.supplier_invoice_amount)
    diff = invoice_amt - calc_total

    # mismatch rule
    if invoice_amt > 0 and abs(diff) >= Decimal("0.01"):
        final_reason = (difference_reason or grn.difference_reason or "").strip()
        if not final_reason:
            raise HTTPException(status_code=400, detail="Difference reason required (invoice vs calculated mismatch)")
        grn.difference_reason = final_reason
    else:
        grn.difference_reason = (grn.difference_reason or "").strip()

    # write header totals
    grn.discount_amount = disc_total
    grn.taxable_amount = taxable_total
    grn.cgst_amount = cgst_total
    grn.sgst_amount = sgst_total
    grn.igst_amount = igst_total
    grn.calculated_grn_amount = calc_total
    grn.amount_difference = diff

    # STOCK: create/update batches + transactions + PO received
    for li in lines:
        batch = _get_or_create_batch(
            db,
            item_id=li.item_id,
            location_id=grn.location_id,
            batch_no=(li.batch_no or "").strip(),
            expiry_date=li.expiry_date,
        )

        qty_in = d(li.quantity) + d(li.free_quantity)
        batch.current_qty = d(batch.current_qty) + qty_in

        # update batch rates to latest
        batch.unit_cost = d(li.unit_cost)
        batch.mrp = d(li.mrp)
        # tax percent (fallback)
        t = d(li.tax_percent)
        if t <= 0:
            t = d(li.cgst_percent) + d(li.sgst_percent) + d(li.igst_percent)
        batch.tax_percent = t

        li.batch_id = batch.id

        db.add(
            StockTransaction(
                location_id=grn.location_id,
                item_id=li.item_id,
                batch_id=batch.id,
                txn_time=datetime.utcnow(),
                txn_type="GRN",
                ref_type="GRN",
                ref_id=grn.id,
                quantity_change=qty_in,
                unit_cost=d(li.unit_cost),
                mrp=d(li.mrp),
                remark=f"GRN {grn.grn_number} / Inv {grn.invoice_number}",
                user_id=posted_by_user_id,
            )
        )

        # PO received update (count purchased qty only; free doesn't reduce pending)
        if li.po_item_id:
            poi = db.query(PurchaseOrderItem).filter(PurchaseOrderItem.id == li.po_item_id).with_for_update().one_or_none()
            if poi:
                poi.received_qty = d(poi.received_qty) + d(li.quantity)

    # PO status update
    if grn.po_id:
        po = db.query(PurchaseOrder).filter(PurchaseOrder.id == grn.po_id).with_for_update().one_or_none()
        if po:
            _update_po_status(db, po)

    # ledger invoice create/update
    _upsert_supplier_invoice(db, grn)

    # finalize
    grn.status = GRNStatus.POSTED.value
    grn.posted_by_id = posted_by_user_id
    grn.posted_at = datetime.utcnow()

    db.flush()
    return grn


def cancel_grn(db: Session, grn_id: int, cancelled_by_user_id: Optional[int], reason: str) -> GRN:
    grn = db.query(GRN).filter(GRN.id == grn_id).with_for_update().one_or_none()
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")

    if str(grn.status) != GRNStatus.DRAFT.value:
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be cancelled (posted reversal not implemented)")

    grn.status = GRNStatus.CANCELLED.value
    grn.cancel_reason = (reason or "").strip()
    grn.cancelled_by_id = cancelled_by_user_id
    grn.cancelled_at = datetime.utcnow()

    db.flush()
    return grn
