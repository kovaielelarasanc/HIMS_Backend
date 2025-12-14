from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.api.deps import get_db, current_user as auth_current_user
from app.api.perm import has_perm
from app.core.emailer import send_email
from app.models.user import User
from app.models.pharmacy_inventory import (
    PurchaseOrder,
    PurchaseOrderItem,
    InventoryItem,
    POStatus,
)
from app.schemas.pharmacy_inventory import (
    PurchaseOrderCreate,
    PurchaseOrderUpdate,
    PurchaseOrderOut,
)

router = APIRouter(prefix="/inventory/purchase-orders", tags=["Inventory - Purchase Orders"])


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def _compute_po_line(li: PurchaseOrderItem) -> None:
    qty = _d(getattr(li, "ordered_qty", 0))
    rate = _d(getattr(li, "unit_cost", 0))
    tax_pct = _d(getattr(li, "tax_percent", 0))

    sub = (qty * rate).quantize(Decimal("0.01"))
    tax = (sub * tax_pct / Decimal("100")).quantize(Decimal("0.01"))
    total = (sub + tax).quantize(Decimal("0.01"))

    # write safely (depending on your model fields)
    if hasattr(li, "line_sub_total"):
        li.line_sub_total = sub
    if hasattr(li, "line_tax_total"):
        li.line_tax_total = tax
    if hasattr(li, "line_total"):
        li.line_total = total
    else:
        # fallback
        setattr(li, "line_total", total)


def _compute_po_totals(po: PurchaseOrder) -> None:
    sub = Decimal("0")
    tax = Decimal("0")
    total = Decimal("0")
    for li in po.items or []:
        qty = _d(li.ordered_qty)
        rate = _d(li.unit_cost)
        tax_pct = _d(getattr(li, "tax_percent", 0))

        line_sub = (qty * rate).quantize(Decimal("0.01"))
        line_tax = (line_sub * tax_pct / Decimal("100")).quantize(Decimal("0.01"))
        line_total = (line_sub + line_tax).quantize(Decimal("0.01"))

        sub += line_sub
        tax += line_tax
        total += line_total

    if hasattr(po, "sub_total"):
        po.sub_total = sub.quantize(Decimal("0.01"))
    if hasattr(po, "tax_total"):
        po.tax_total = tax.quantize(Decimal("0.01"))
    if hasattr(po, "grand_total"):
        po.grand_total = total.quantize(Decimal("0.01"))


def _generate_po_number(db: Session) -> str:
    today_str = date.today().strftime("%Y%m%d")
    prefix = f"PO{today_str}"
    seq = 1
    while True:
        candidate = f"{prefix}{seq:03d}"
        exists = db.query(PurchaseOrder.id).filter(PurchaseOrder.po_number == candidate).first()
        if not exists:
            return candidate
        seq += 1


def _po_q():
    return (
        selectinload(PurchaseOrder.supplier),
        selectinload(PurchaseOrder.location),
        selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item),
    )


@router.get("", response_model=List[PurchaseOrderOut])
def list_purchase_orders(
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    location_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    query = db.query(PurchaseOrder).options(*_po_q()).order_by(PurchaseOrder.id.desc())

    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(PurchaseOrder.po_number.ilike(like))
    if status and status != "ALL":
        query = query.filter(PurchaseOrder.status == status)
    if supplier_id:
        query = query.filter(PurchaseOrder.supplier_id == supplier_id)
    if location_id:
        query = query.filter(PurchaseOrder.location_id == location_id)
    if from_date:
        query = query.filter(PurchaseOrder.order_date >= from_date)
    if to_date:
        query = query.filter(PurchaseOrder.order_date <= to_date)

    return query.limit(limit).all()


# ✅ IMPORTANT: use /pending + typed int routes to avoid 422 collisions
@router.get("/pending")
def list_pending_pos(
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    allowed = [POStatus.SENT, POStatus.PARTIALLY_RECEIVED, POStatus.APPROVED]

    base = (
        db.query(PurchaseOrder)
        .options(selectinload(PurchaseOrder.supplier), selectinload(PurchaseOrder.location), selectinload(PurchaseOrder.items))
        .filter(PurchaseOrder.status.in_(allowed))
        .order_by(PurchaseOrder.id.desc())
    )

    if status and status != "ALL":
        base = base.filter(PurchaseOrder.status == status)

    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.filter(PurchaseOrder.po_number.ilike(like))

    pos = base.limit(limit).all()

    out = []
    for po in pos:
        pending_items = 0
        for it in po.items or []:
            if _d(it.ordered_qty) > _d(it.received_qty):
                pending_items += 1
        if pending_items > 0:
            out.append({
                "id": po.id,
                "po_number": po.po_number,
                "order_date": po.order_date,
                "expected_date": po.expected_date,
                "status": po.status,
                "supplier": po.supplier,
                "location": po.location,
                "supplier_id": po.supplier_id,
                "location_id": po.location_id,
                "pending_items_count": pending_items,
            })
    return out


@router.get("/{po_id:int}", response_model=PurchaseOrderOut)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = (
        db.query(PurchaseOrder)
        .options(*_po_q())
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    return po


@router.get("/{po_id:int}/pending-items")
def get_po_pending_items(
    po_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = (
        db.query(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item))
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    items = []
    for it in po.items or []:
        remaining = float(_d(it.ordered_qty) - _d(it.received_qty))
        if remaining > 0:
            items.append({
                "po_item_id": it.id,
                "item_id": it.item_id,
                "item": it.item,
                "ordered_qty": it.ordered_qty,
                "received_qty": it.received_qty,
                "remaining_qty": remaining,
                "unit_cost": it.unit_cost,
                "mrp": it.mrp,
                "tax_percent": it.tax_percent,
            })

    return {
        "po_id": po.id,
        "po_number": po.po_number,
        "supplier_id": po.supplier_id,
        "location_id": po.location_id,
        "items": items,
    }


@router.post("", response_model=PurchaseOrderOut)
def create_po(
    payload: PurchaseOrderCreate,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if not payload.items:
        raise HTTPException(status_code=400, detail="PO must have at least 1 item")

    po = PurchaseOrder(
        po_number=_generate_po_number(db),
        supplier_id=payload.supplier_id,
        location_id=payload.location_id,
        order_date=payload.order_date or date.today(),
        expected_date=payload.expected_date,
        notes=getattr(payload, "notes", "") or "",
        status=POStatus.DRAFT,
        created_by_id=me.id,
    )
    db.add(po)
    db.flush()

    for row in payload.items:
        item = db.get(InventoryItem, row.item_id)
        if not item:
            raise HTTPException(status_code=400, detail=f"Item {row.item_id} not found")

        li = PurchaseOrderItem(
            po_id=po.id,
            item_id=row.item_id,
            ordered_qty=row.ordered_qty,
            received_qty=Decimal("0"),
            unit_cost=row.unit_cost,
            tax_percent=getattr(row, "tax_percent", None),
            mrp=getattr(row, "mrp", None),
            remarks=getattr(row, "remarks", "") or "",
        )
        _compute_po_line(li)
        db.add(li)

    db.flush()
    _compute_po_totals(po)
    db.commit()

    po = (
        db.query(PurchaseOrder)
        .options(*_po_q())
        .filter(PurchaseOrder.id == po.id)
        .one()
    )
    return po


@router.put("/{po_id:int}", response_model=PurchaseOrderOut)
def update_po(
    po_id: int,
    payload: PurchaseOrderUpdate,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.items)).filter(PurchaseOrder.id == po_id).one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    if str(po.status) not in ("DRAFT", "SENT", "APPROVED"):
        raise HTTPException(status_code=400, detail="Only DRAFT/SENT/APPROVED POs can be edited")

    data = payload.model_dump(exclude_unset=True)
    items = data.pop("items", None)

    for k, v in data.items():
        setattr(po, k, v)

    if items is not None:
        db.query(PurchaseOrderItem).filter(PurchaseOrderItem.po_id == po.id).delete()
        db.flush()

        for row in items:
            li = PurchaseOrderItem(
                po_id=po.id,
                item_id=row["item_id"],
                ordered_qty=row.get("ordered_qty"),
                received_qty=Decimal("0"),
                unit_cost=row.get("unit_cost"),
                tax_percent=row.get("tax_percent"),
                mrp=row.get("mrp"),
                remarks=row.get("remarks") or "",
            )
            _compute_po_line(li)
            db.add(li)

    db.flush()
    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.items)).filter(PurchaseOrder.id == po.id).one()
    _compute_po_totals(po)
    db.commit()

    po = db.query(PurchaseOrder).options(*_po_q()).filter(PurchaseOrder.id == po.id).one()
    return po


# -------- PDF ----------
def _build_po_pdf(po: PurchaseOrder) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 40

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Purchase Order: {po.po_number}")
    y -= 20

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Supplier: {getattr(po.supplier, 'name', '-')}")
    y -= 14
    c.drawString(40, y, f"Location: {getattr(po.location, 'name', '-')}")
    y -= 14
    c.drawString(40, y, f"Order date: {po.order_date.isoformat() if po.order_date else '-'}")
    y -= 22

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Item")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(430, y, "Rate")
    c.drawRightString(490, y, "Tax%")
    c.drawRightString(560, y, "Total")
    y -= 12

    c.setFont("Helvetica", 9)
    grand = Decimal("0.00")
    for li in po.items or []:
        if y < 80:
            c.showPage()
            y = height - 40

        name = getattr(getattr(li, "item", None), "name", f"Item {li.item_id}")
        c.drawString(40, y, str(name)[:40])
        c.drawRightString(360, y, str(li.ordered_qty or 0))
        c.drawRightString(430, y, str(li.unit_cost or 0))
        c.drawRightString(490, y, str(getattr(li, "tax_percent", 0) or 0))
        c.drawRightString(560, y, str(getattr(li, "line_total", 0) or 0))
        grand += _d(getattr(li, "line_total", 0))
        y -= 12

    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(560, y, f"Grand Total: {grand}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


@router.get("/{po_id:int}/pdf")
def download_po_pdf(
    po_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.query(PurchaseOrder).options(*_po_q()).filter(PurchaseOrder.id == po_id).one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{po_id:int}/mark-sent", response_model=PurchaseOrderOut)
def mark_po_sent(
    po_id: int,
    email_to: str = Query(...),
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.query(PurchaseOrder).options(*_po_q()).filter(PurchaseOrder.id == po_id).one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"
    body_text = f"Dear {getattr(po.supplier, 'name', 'Supplier')},\n\nPlease find attached Purchase Order {po.po_number}.\n\nRegards,\n{getattr(me, 'full_name', '') or getattr(me, 'name', '') or 'User'}"

    send_email(
        email_to,
        f"Purchase Order {po.po_number}",
        body_text,
        attachments=[(filename, pdf_bytes, "application/pdf")],
    )

    po.status = POStatus.SENT
    po.email_sent_to = email_to
    po.email_sent_at = datetime.utcnow()

    db.commit()
    po = db.query(PurchaseOrder).options(*_po_q()).filter(PurchaseOrder.id == po_id).one()
    return po
