from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, current_user as auth_current_user
from app.api.perm import has_perm
from app.models.user import User
from app.models.pharmacy_inventory import (
    GRN,
    GRNItem,
    InventoryItem,
    ItemBatch,
    PurchaseOrder,
    PurchaseOrderItem,
    POStatus,
)
from app.schemas.pharmacy_inventory import GRNCreate, GRNOut, GRNPostIn, GRNCancelIn
from app.services.inventory import create_stock_transaction, adjust_batch_qty
from app.services.supplier_ledger import sync_supplier_invoice_from_grn

router = APIRouter(prefix="/inventory/grn", tags=["Inventory - GRN"])


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def _grn_q():
    return (
        selectinload(GRN.supplier),
        selectinload(GRN.location),
        selectinload(GRN.purchase_order),
        selectinload(GRN.items).selectinload(GRNItem.item),
    )


def _generate_grn_number(db: Session) -> str:
    today_str = date.today().strftime("%Y%m%d")
    prefix = f"GRN{today_str}"
    seq = 1
    while True:
        candidate = f"{prefix}{seq:03d}"
        exists = db.query(GRN.id).filter(GRN.grn_number == candidate).first()
        if not exists:
            return candidate
        seq += 1


def recalc_grn_totals(grn: GRN) -> None:
    taxable = Decimal("0")
    disc = Decimal("0")
    cgst = sgst = igst = Decimal("0")
    total = Decimal("0")

    for it in (grn.items or []):
        qty = _d(it.quantity)
        rate = _d(it.unit_cost)
        gross = (qty * rate).quantize(Decimal("0.01"))

        disc_amt = _d(getattr(it, "discount_amount", 0))
        disc_pct = _d(getattr(it, "discount_percent", 0))
        if disc_amt <= 0 and disc_pct > 0:
            disc_amt = (gross * disc_pct / Decimal("100")).quantize(Decimal("0.01"))
        if disc_amt < 0:
            disc_amt = Decimal("0")

        tax_base = (gross - disc_amt).quantize(Decimal("0.01"))
        if tax_base < 0:
            tax_base = Decimal("0")

        igst_pct = _d(getattr(it, "igst_percent", 0))
        cgst_pct = _d(getattr(it, "cgst_percent", 0))
        sgst_pct = _d(getattr(it, "sgst_percent", 0))
        tax_pct = _d(getattr(it, "tax_percent", 0))

        igst_amt = (tax_base * igst_pct / Decimal("100")).quantize(Decimal("0.01"))
        cgst_amt = (tax_base * cgst_pct / Decimal("100")).quantize(Decimal("0.01"))
        sgst_amt = (tax_base * sgst_pct / Decimal("100")).quantize(Decimal("0.01"))

        if (igst_amt + cgst_amt + sgst_amt) == 0 and tax_pct > 0:
            t = (tax_base * tax_pct / Decimal("100")).quantize(Decimal("0.01"))
            cgst_amt = (t / 2).quantize(Decimal("0.01"))
            sgst_amt = (t - cgst_amt).quantize(Decimal("0.01"))

        line_total = (tax_base + igst_amt + cgst_amt + sgst_amt).quantize(Decimal("0.01"))

        if hasattr(it, "discount_amount"):
            it.discount_amount = disc_amt
        if hasattr(it, "taxable_amount"):
            it.taxable_amount = tax_base
        if hasattr(it, "igst_amount"):
            it.igst_amount = igst_amt
        if hasattr(it, "cgst_amount"):
            it.cgst_amount = cgst_amt
        if hasattr(it, "sgst_amount"):
            it.sgst_amount = sgst_amt

        it.line_total = line_total

        taxable += tax_base
        disc += disc_amt
        cgst += cgst_amt
        sgst += sgst_amt
        igst += igst_amt
        total += line_total

    grn.taxable_amount = taxable.quantize(Decimal("0.01"))
    grn.discount_amount = disc.quantize(Decimal("0.01"))
    grn.cgst_amount = cgst.quantize(Decimal("0.01"))
    grn.sgst_amount = sgst.quantize(Decimal("0.01"))
    grn.igst_amount = igst.quantize(Decimal("0.01"))

    extras = _d(grn.freight_amount) + _d(grn.other_charges) + _d(grn.round_off)
    grn.calculated_grn_amount = (total + extras).quantize(Decimal("0.01"))
    grn.amount_difference = (_d(grn.supplier_invoice_amount) - _d(grn.calculated_grn_amount)).quantize(Decimal("0.01"))


@router.post("", response_model=GRNOut)
def create_grn(payload: GRNCreate, db: Session = Depends(get_db), me: User = Depends(auth_current_user)):
    if not has_perm(me, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if not payload.items:
        raise HTTPException(status_code=400, detail="GRN must have at least 1 item")

    grn = GRN(
        grn_number=_generate_grn_number(db),
        po_id=payload.po_id,
        supplier_id=payload.supplier_id,
        location_id=payload.location_id,
        received_date=payload.received_date or date.today(),
        invoice_number=payload.invoice_number or "",
        invoice_date=payload.invoice_date,
        notes=payload.notes or "",
        status="DRAFT",
        created_by_id=me.id,
        supplier_invoice_amount=payload.supplier_invoice_amount,
        freight_amount=payload.freight_amount,
        other_charges=payload.other_charges,
        round_off=payload.round_off,
        difference_reason=payload.difference_reason or "",
    )
    db.add(grn)
    db.flush()

    for line in payload.items:
        item = db.get(InventoryItem, line.item_id)
        if not item:
            raise HTTPException(status_code=400, detail=f"Item {line.item_id} not found")

        it = GRNItem(
            grn_id=grn.id,
            po_item_id=line.po_item_id,
            item_id=line.item_id,
            batch_no=(line.batch_no or "").strip(),
            expiry_date=line.expiry_date,
            quantity=line.quantity,
            free_quantity=line.free_quantity,
            unit_cost=line.unit_cost,
            mrp=line.mrp,
            discount_percent=line.discount_percent,
            discount_amount=line.discount_amount,
            tax_percent=line.tax_percent,
            cgst_percent=line.cgst_percent,
            sgst_percent=line.sgst_percent,
            igst_percent=line.igst_percent,
            scheme=line.scheme or "",
            remarks=line.remarks or "",
        )
        db.add(it)

    db.flush()
    recalc_grn_totals(grn)
    db.commit()

    return db.query(GRN).options(*_grn_q()).filter(GRN.id == grn.id).one()


@router.get("", response_model=List[GRNOut])
def list_grns(
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    if not has_perm(me, "pharmacy.inventory.grn.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    query = db.query(GRN).options(selectinload(GRN.supplier), selectinload(GRN.location)).order_by(GRN.id.desc())

    if status and status != "ALL":
        query = query.filter(GRN.status == status)

    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter((GRN.grn_number.ilike(like)) | (GRN.invoice_number.ilike(like)))

    return query.limit(limit).all()


@router.get("/{grn_id:int}", response_model=GRNOut)
def get_grn(grn_id: int, db: Session = Depends(get_db), me: User = Depends(auth_current_user)):
    if not has_perm(me, "pharmacy.inventory.grn.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = db.query(GRN).options(*_grn_q()).filter(GRN.id == grn_id).one_or_none()
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    return grn


@router.put("/{grn_id:int}", response_model=GRNOut)
def update_grn(grn_id: int, payload: GRNCreate, db: Session = Depends(get_db), me: User = Depends(auth_current_user)):
    if not has_perm(me, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = db.query(GRN).options(selectinload(GRN.items)).filter(GRN.id == grn_id).one_or_none()
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if grn.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be edited")

    # header
    grn.po_id = payload.po_id
    grn.supplier_id = payload.supplier_id
    grn.location_id = payload.location_id
    grn.received_date = payload.received_date or grn.received_date
    grn.invoice_number = payload.invoice_number or ""
    grn.invoice_date = payload.invoice_date
    grn.notes = payload.notes or ""
    grn.supplier_invoice_amount = payload.supplier_invoice_amount
    grn.freight_amount = payload.freight_amount
    grn.other_charges = payload.other_charges
    grn.round_off = payload.round_off
    grn.difference_reason = payload.difference_reason or ""

    # replace items
    db.query(GRNItem).filter(GRNItem.grn_id == grn.id).delete()
    db.flush()

    for line in payload.items:
        item = db.get(InventoryItem, line.item_id)
        if not item:
            raise HTTPException(status_code=400, detail=f"Item {line.item_id} not found")

        it = GRNItem(
            grn_id=grn.id,
            po_item_id=line.po_item_id,
            item_id=line.item_id,
            batch_no=(line.batch_no or "").strip(),
            expiry_date=line.expiry_date,
            quantity=line.quantity,
            free_quantity=line.free_quantity,
            unit_cost=line.unit_cost,
            mrp=line.mrp,
            discount_percent=line.discount_percent,
            discount_amount=line.discount_amount,
            tax_percent=line.tax_percent,
            cgst_percent=line.cgst_percent,
            sgst_percent=line.sgst_percent,
            igst_percent=line.igst_percent,
            scheme=line.scheme or "",
            remarks=line.remarks or "",
        )
        db.add(it)

    db.flush()
    grn = db.query(GRN).options(selectinload(GRN.items)).filter(GRN.id == grn.id).one()
    recalc_grn_totals(grn)

    db.commit()
    return db.query(GRN).options(*_grn_q()).filter(GRN.id == grn.id).one()


@router.post("/from-po/{po_id:int}", response_model=GRNOut)
def create_grn_from_po(po_id: int, db: Session = Depends(get_db), me: User = Depends(auth_current_user)):
    if not has_perm(me, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = (
        db.query(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item))
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    grn = GRN(
        grn_number=_generate_grn_number(db),
        po_id=po.id,
        supplier_id=po.supplier_id,
        location_id=po.location_id,
        received_date=date.today(),
        invoice_number="",
        status="DRAFT",
        created_by_id=me.id,
        supplier_invoice_amount=Decimal("0"),
        freight_amount=Decimal("0"),
        other_charges=Decimal("0"),
        round_off=Decimal("0"),
        difference_reason="",
    )
    db.add(grn)
    db.flush()

    for li in po.items or []:
        pending = _d(li.ordered_qty) - _d(li.received_qty)
        if pending <= 0:
            continue

        it = GRNItem(
            grn_id=grn.id,
            po_item_id=li.id,
            item_id=li.item_id,
            batch_no="",  # user fills in UI
            expiry_date=None,
            quantity=pending,
            free_quantity=Decimal("0"),
            unit_cost=li.unit_cost,
            mrp=li.mrp,
            tax_percent=getattr(li, "tax_percent", 0),
            cgst_percent=Decimal("0"),
            sgst_percent=Decimal("0"),
            igst_percent=Decimal("0"),
            scheme="",
            remarks="",
        )
        db.add(it)

    db.flush()
    recalc_grn_totals(grn)
    db.commit()
    return db.query(GRN).options(*_grn_q()).filter(GRN.id == grn.id).one()


@router.post("/{grn_id:int}/post", response_model=GRNOut)
def post_grn(
    grn_id: int,
    body: GRNPostIn = Body(default_factory=GRNPostIn),
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = (
        db.query(GRN)
        .options(selectinload(GRN.items), selectinload(GRN.purchase_order).selectinload(PurchaseOrder.items))
        .filter(GRN.id == grn_id)
        .with_for_update()
        .one_or_none()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if grn.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be posted")
    if not grn.items:
        raise HTTPException(status_code=400, detail="Cannot post GRN with no items")
    if _d(grn.supplier_invoice_amount) <= 0:
        raise HTTPException(status_code=400, detail="Supplier invoice amount must be > 0 to post GRN")

    for li in grn.items:
        if not (li.batch_no or "").strip():
            raise HTTPException(status_code=400, detail="Batch number is required for all GRN items")
        if (_d(li.quantity) + _d(li.free_quantity)) <= 0:
            raise HTTPException(status_code=400, detail="Qty or Free must be > 0")

    recalc_grn_totals(grn)
    if _d(grn.amount_difference) != Decimal("0"):
        reason = (body.difference_reason or grn.difference_reason or "").strip()
        if not reason:
            raise HTTPException(status_code=400, detail="Invoice mismatch. Provide difference_reason to post.")
        grn.difference_reason = reason

    # ---- apply stock ----
    for li in grn.items:
        qty_in = _d(li.quantity) + _d(li.free_quantity)

        batch = (
            db.query(ItemBatch)
            .filter(
                ItemBatch.item_id == li.item_id,
                ItemBatch.location_id == grn.location_id,
                ItemBatch.batch_no == li.batch_no,
            )
            .one_or_none()
        )
        if not batch:
            batch = ItemBatch(
                item_id=li.item_id,
                location_id=grn.location_id,
                batch_no=li.batch_no,
                expiry_date=li.expiry_date,
                current_qty=Decimal("0"),
                unit_cost=li.unit_cost,
                mrp=li.mrp,
                is_active=True,
                is_saleable=True,
                status="ACTIVE",
            )
            db.add(batch)
            db.flush()

        adjust_batch_qty(batch=batch, delta=qty_in)

        create_stock_transaction(
            db,
            user=me,
            location_id=grn.location_id,
            item_id=li.item_id,
            batch_id=batch.id,
            qty_delta=qty_in,
            txn_type="GRN",
            ref_type="GRN",
            ref_id=grn.id,
            unit_cost=li.unit_cost,
            mrp=li.mrp,
            remark=f"GRN {grn.grn_number}",
        )

        # update PO received qty (only qty, not free)
        if grn.po_id and li.po_item_id:
            po_line = db.query(PurchaseOrderItem).filter(PurchaseOrderItem.id == li.po_item_id).one_or_none()
            if po_line:
                po_line.received_qty = _d(po_line.received_qty) + _d(li.quantity)

    # ---- update PO status ----
    if grn.purchase_order:
        po = grn.purchase_order
        any_received = False
        all_completed = True
        for li in po.items or []:
            if _d(li.received_qty) > 0:
                any_received = True
            if _d(li.received_qty) < _d(li.ordered_qty):
                all_completed = False

        if all_completed:
            po.status = POStatus.COMPLETED
        elif any_received:
            po.status = POStatus.PARTIALLY_RECEIVED
        else:
            # keep as is
            pass

    grn.status = "POSTED"
    grn.posted_by_id = me.id
    grn.posted_at = datetime.utcnow()

    # supplier ledger sync (safe)
    try:
        sync_supplier_invoice_from_grn(db, grn)
    except Exception:
        pass

    db.commit()
    return db.query(GRN).options(*_grn_q()).filter(GRN.id == grn.id).one()


@router.post("/{grn_id:int}/cancel", response_model=GRNOut)
def cancel_grn(
    grn_id: int,
    body: GRNCancelIn,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = db.query(GRN).options(*_grn_q()).filter(GRN.id == grn_id).one_or_none()
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    if grn.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be cancelled")

    reason = (body.cancel_reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="cancel_reason is required")

    grn.status = "CANCELLED"
    grn.cancel_reason = reason
    grn.cancelled_by_id = me.id
    grn.cancelled_at = datetime.utcnow()

    try:
        sync_supplier_invoice_from_grn(db, grn)
    except Exception:
        pass

    db.commit()
    return db.query(GRN).options(*_grn_q()).filter(GRN.id == grn.id).one()
