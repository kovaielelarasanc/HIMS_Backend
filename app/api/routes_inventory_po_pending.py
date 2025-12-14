# FILE: app/api/routes_inventory_purchase_orders.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, current_user as auth_current_user
from app.core.emailer import send_email
from app.models.user import User
from app.models.pharmacy_inventory import (
    PurchaseOrder,
    PurchaseOrderItem,
    POStatus,
    InventoryItem,
)

from app.schemas.pharmacy_inventory import (
    PurchaseOrderCreate,
    PurchaseOrderUpdate,
    PurchaseOrderOut,
)

# Optional PDF dependency (safe import)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:  # pragma: no cover
    A4 = None
    canvas = None


router = APIRouter(prefix="/inventory/purchase-orders", tags=["Inventory - Purchase Orders"])


# -------------------------
# Permissions helper
# -------------------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    for k in ("full_name", "name", "username", "email"):
        v = getattr(user, k, None)
        if v:
            return v
    return f"User #{getattr(user, 'id', 'unknown')}"


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


def _load_po(db: Session, po_id: int) -> PurchaseOrder:
    po = (
        db.query(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.location),
            selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item),
        )
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    return po


# -------------------------
# CRUD
# -------------------------
@router.post("", response_model=PurchaseOrderOut)
def create_purchase_order(
    payload: PurchaseOrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    with db.begin():
        po_number = _generate_po_number(db)
        po = PurchaseOrder(
            po_number=po_number,
            supplier_id=payload.supplier_id,
            location_id=payload.location_id,
            order_date=payload.order_date or date.today(),
            expected_date=payload.expected_date,
            notes=payload.notes or "",
            status=POStatus.DRAFT if hasattr(POStatus, "DRAFT") else "DRAFT",
            created_by_id=current_user.id,
        )
        db.add(po)
        db.flush()

        for line in payload.items:
            item = db.get(InventoryItem, line.item_id)
            if not item:
                raise HTTPException(status_code=400, detail=f"Item {line.item_id} not found")

            line_total = (_d(line.ordered_qty) * _d(line.unit_cost)).quantize(Decimal("0.01"))
            li = PurchaseOrderItem(
                po_id=po.id,
                item_id=line.item_id,
                ordered_qty=line.ordered_qty,
                received_qty=Decimal("0"),
                unit_cost=line.unit_cost,
                tax_percent=line.tax_percent,
                mrp=line.mrp,
                line_total=line_total,
            )
            db.add(li)

    return _load_po(db, po.id)


@router.get("", response_model=List[PurchaseOrderOut])
def list_purchase_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    status: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = (
        db.query(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.location),
            selectinload(PurchaseOrder.items),
        )
        .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc())
    )

    if status:
        q = q.filter(PurchaseOrder.status == status)
    if supplier_id:
        q = q.filter(PurchaseOrder.supplier_id == supplier_id)
    if from_date:
        q = q.filter(PurchaseOrder.order_date >= from_date)
    if to_date:
        q = q.filter(PurchaseOrder.order_date <= to_date)

    return q.limit(limit).all()


@router.get("/{po_id}", response_model=PurchaseOrderOut)
def get_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return _load_po(db, po_id)


@router.put("/{po_id}", response_model=PurchaseOrderOut)
def update_purchase_order(
    po_id: int,
    payload: PurchaseOrderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    with db.begin():
        po = db.get(PurchaseOrder, po_id)
        if not po:
            raise HTTPException(status_code=404, detail="PO not found")

        if str(po.status) not in ("DRAFT", "SENT"):
            raise HTTPException(status_code=400, detail="Only DRAFT/SENT POs can be edited")

        for k, v in payload.model_dump(exclude_unset=True, exclude={"items"}).items():
            setattr(po, k, v)

        if payload.items is not None:
            db.query(PurchaseOrderItem).filter(PurchaseOrderItem.po_id == po.id).delete()
            db.flush()

            for line in payload.items:
                item = db.get(InventoryItem, line.item_id)
                if not item:
                    raise HTTPException(status_code=400, detail=f"Item {line.item_id} not found")

                line_total = (_d(line.ordered_qty) * _d(line.unit_cost)).quantize(Decimal("0.01"))
                li = PurchaseOrderItem(
                    po_id=po.id,
                    item_id=line.item_id,
                    ordered_qty=line.ordered_qty,
                    received_qty=Decimal("0"),
                    unit_cost=line.unit_cost,
                    tax_percent=line.tax_percent,
                    mrp=line.mrp,
                    line_total=line_total,
                )
                db.add(li)

    return _load_po(db, po_id)


@router.post("/{po_id}/status", response_model=PurchaseOrderOut)
def change_purchase_order_status(
    po_id: int,
    status: str = Query(..., pattern="^(DRAFT|SENT|APPROVED|PARTIALLY_RECEIVED|COMPLETED|CANCELLED|CLOSED)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    with db.begin():
        po = db.get(PurchaseOrder, po_id)
        if not po:
            raise HTTPException(status_code=404, detail="PO not found")
        po.status = status

    return _load_po(db, po_id)


# -------------------------
# PDF + Email
# -------------------------
def _build_po_pdf(po: PurchaseOrder) -> bytes:
    if canvas is None or A4 is None:
        raise HTTPException(status_code=400, detail="reportlab not installed (required for PO PDF)")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Purchase Order: {po.po_number}")
    y -= 20
    c.setFont("Helvetica", 10)

    sup = getattr(po, "supplier", None)
    loc = getattr(po, "location", None)

    c.drawString(40, y, f"Supplier: {getattr(sup, 'name', '')}")
    y -= 15
    if getattr(sup, "address", None):
        c.drawString(40, y, f"Address: {sup.address}")
        y -= 15
    if getattr(sup, "gstin", None):
        c.drawString(40, y, f"GSTIN: {sup.gstin}")
        y -= 15

    y -= 10
    c.drawString(40, y, f"Location: {getattr(loc, 'name', '')}")
    y -= 15
    c.drawString(40, y, f"Order date: {po.order_date.isoformat() if po.order_date else ''}")
    y -= 15
    if po.expected_date:
        c.drawString(40, y, f"Expected date: {po.expected_date.isoformat()}")
        y -= 15

    y -= 20
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Code")
    c.drawString(120, y, "Item")
    c.drawString(320, y, "Qty")
    c.drawString(370, y, "Rate")
    c.drawString(430, y, "Tax%")
    c.drawString(480, y, "Total")
    y -= 15
    c.setFont("Helvetica", 9)

    grand_total = Decimal("0")
    for li in po.items or []:
        if y < 80:
            c.showPage()
            y = height - 40
        item = getattr(li, "item", None)
        c.drawString(40, y, str(getattr(item, "code", ""))[:12])
        c.drawString(120, y, str(getattr(item, "name", ""))[:30])
        c.drawRightString(350, y, f"{li.ordered_qty}")
        c.drawRightString(410, y, f"{li.unit_cost}")
        c.drawRightString(460, y, f"{li.tax_percent}")
        c.drawRightString(550, y, f"{li.line_total}")
        grand_total += _d(li.line_total)
        y -= 14

    y -= 20
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(550, y, f"Grand Total: {grand_total.quantize(Decimal('0.01'))}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


@router.get("/{po_id}/pdf")
def download_po_pdf(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = _load_po(db, po_id)
    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{po_id}/mark-sent", response_model=PurchaseOrderOut)
def mark_po_sent(
    po_id: int,
    email_to: str = Query(..., description="Supplier email address to send PO PDF to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    if not email_to:
        raise HTTPException(status_code=400, detail="email_to is required")

    po = _load_po(db, po_id)
    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"

    sender_name = user_display_name(current_user)
    body_text = (
        f"Dear {getattr(po.supplier, 'name', 'Supplier')},\n\n"
        f"Please find attached Purchase Order {po.po_number}.\n\n"
        "Regards,\n"
        f"{sender_name}"
    )

    try:
        send_email(
            email_to,
            f"Purchase Order {po.po_number}",
            body_text,
            attachments=[(filename, pdf_bytes, "application/pdf")],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send PO email: {e}")

    with db.begin():
        po.status = "SENT"
        po.email_sent_to = email_to
        po.email_sent_at = datetime.utcnow()
        po.email_sent_by_name = sender_name

    return _load_po(db, po_id)


# -------------------------
# ✅ Pending endpoints (ONLY HERE) — FIXED JSON (no ORM objects)
# -------------------------
class SupplierMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: Optional[str] = None
    name: Optional[str] = None


class LocationMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: Optional[str] = None
    name: Optional[str] = None


class ItemMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: Optional[str] = None
    name: Optional[str] = None
    generic_name: Optional[str] = None


class PendingPOOut(BaseModel):
    id: int
    po_number: str
    order_date: Optional[date] = None
    expected_date: Optional[date] = None
    status: str
    supplier: SupplierMini
    location: LocationMini
    pending_items_count: int


class PendingPOItemOut(BaseModel):
    po_item_id: int
    item_id: int
    item: ItemMini
    ordered_qty: Decimal
    received_qty: Decimal
    remaining_qty: Decimal
    unit_cost: Optional[Decimal] = None
    mrp: Optional[Decimal] = None
    tax_percent: Optional[Decimal] = None


class PendingPOItemsResponse(BaseModel):
    po_id: int
    po_number: str
    supplier_id: int
    location_id: int
    items: List[PendingPOItemOut]


@router.get("/pending", response_model=List[PendingPOOut])
def api_list_pending_pos(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    base = (
        db.query(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.location),
            selectinload(PurchaseOrder.items),
        )
        .filter(PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIALLY_RECEIVED, POStatus.APPROVED]))
        .order_by(PurchaseOrder.id.desc())
    )

    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.filter(PurchaseOrder.po_number.ilike(like))

    pos = base.limit(limit).all()

    out: List[PendingPOOut] = []
    for po in pos:
        pending_items = 0
        for it in po.items or []:
            if _d(it.ordered_qty) > _d(it.received_qty):
                pending_items += 1

        if pending_items > 0:
            out.append(
                PendingPOOut(
                    id=po.id,
                    po_number=po.po_number,
                    order_date=po.order_date,
                    expected_date=po.expected_date,
                    status=str(po.status),
                    supplier=SupplierMini.model_validate(po.supplier),
                    location=LocationMini.model_validate(po.location),
                    pending_items_count=pending_items,
                )
            )
    return out


@router.get("/{po_id}/pending-items", response_model=PendingPOItemsResponse)
def api_get_po_pending_items(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = (
        db.query(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.item))
        .filter(PurchaseOrder.id == po_id)
        .one_or_none()
    )
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    items: List[PendingPOItemOut] = []
    for it in po.items or []:
        ordered = _d(it.ordered_qty)
        received = _d(it.received_qty)
        remaining = ordered - received
        if remaining > 0:
            items.append(
                PendingPOItemOut(
                    po_item_id=it.id,
                    item_id=it.item_id,
                    item=ItemMini.model_validate(it.item),
                    ordered_qty=ordered,
                    received_qty=received,
                    remaining_qty=remaining,
                    unit_cost=it.unit_cost,
                    mrp=it.mrp,
                    tax_percent=it.tax_percent,
                )
            )

    return PendingPOItemsResponse(
        po_id=po.id,
        po_number=po.po_number,
        supplier_id=po.supplier_id,
        location_id=po.location_id,
        items=items,
    )
