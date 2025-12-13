# FILE: app/api/routes_inventory.py
from __future__ import annotations
from fastapi import Body
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO, BytesIO
from typing import List, Optional
import qrcode
from fastapi.responses import StreamingResponse
from barcode import Code128
from barcode.writer import ImageWriter
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Query,
)
from sqlalchemy import func, select, and_
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, select, and_, or_, case
from app.core.emailer import send_email
from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.pharmacy_inventory import (
    InventoryLocation,
    Supplier,
    InventoryItem,
    ItemBatch,
    PurchaseOrder,
    PurchaseOrderItem,
    GRN,
    GRNItem,
    ReturnNote,
    ReturnNoteItem,
    StockTransaction,
    )
from app.schemas.pharmacy_inventory import (
    LocationCreate,
    LocationUpdate,
    LocationOut,
    SupplierCreate,
    SupplierUpdate,
    SupplierOut,
    ItemCreate,
    ItemUpdate,
    ItemOut,
    ItemBatchOut,
    StockSummaryOut,
    PurchaseOrderCreate,
    PurchaseOrderUpdate,
    PurchaseOrderOut,
    GRNCreate,
    GRNOut,
    ReturnCreate,
    ReturnOut,
    StockTransactionOut,
    DispenseRequestIn,
    GRNPostIn,
    GRNCancelIn
)
from app.services.inventory import (
    create_stock_transaction,
    allocate_batches_fefo,
    adjust_batch_qty,
)
from app.services.supplier_ledger import sync_supplier_invoice_from_grn, _d
# For PO PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

router = APIRouter(prefix="/inventory", tags=["Inventory - Pharmacy"])

# ---------- Permissions helper ----------
assert joinedload

def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


# ---------- Locations ----------
def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")



def _ensure_item_qr_number(db: Session, item: InventoryItem) -> None:
    """
    We still use the qr_number column, but treat it as BARCODE number.
    Auto-generate if empty. Pattern: MD_0001, MD_0002, ...
    """
    if not item.qr_number:
        item.qr_number = f"MD_{item.id:04d}"
        db.add(item)
        db.commit()
        db.refresh(item)


def user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    full_name = getattr(user, "full_name", None)
    if full_name:
        return full_name
    name = getattr(user, "name", None)
    if name:
        return name
    username = getattr(user, "username", None)
    if username:
        return username
    email = getattr(user, "email", None)
    if email:
        return email
    return f"User #{getattr(user, 'id', 'unknown')}"


@router.get("/locations", response_model=List[LocationOut])
def list_locations(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.locations.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    q = db.query(InventoryLocation).order_by(InventoryLocation.name)
    return q.all()


@router.post("/locations", response_model=LocationOut)
def create_location(
        payload: LocationCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.locations.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    exists = db.query(InventoryLocation).filter_by(code=payload.code).first()
    if exists:
        raise HTTPException(status_code=400,
                            detail="Location code already exists")
    loc = InventoryLocation(**payload.model_dump())
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


@router.put("/locations/{loc_id}", response_model=LocationOut)
def update_location(
        loc_id: int,
        payload: LocationUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.locations.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    loc = db.get(InventoryLocation, loc_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(loc, k, v)
    db.commit()
    db.refresh(loc)
    return loc


# ---------- Suppliers ----------


@router.get("/suppliers", response_model=List[SupplierOut])
def list_suppliers(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        q: Optional[str] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    query = db.query(Supplier)
    if q:
        like = f"%{q}%"
        query = query.filter(Supplier.name.ilike(like))
    return query.order_by(Supplier.name).all()


@router.post("/suppliers", response_model=SupplierOut)
def create_supplier(
        payload: SupplierCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    exists = db.query(Supplier).filter_by(code=payload.code).first()
    if exists:
        raise HTTPException(status_code=400,
                            detail="Supplier code already exists")
    sup = Supplier(**payload.model_dump())
    db.add(sup)
    db.commit()
    db.refresh(sup)
    return sup


@router.put("/suppliers/{sup_id}", response_model=SupplierOut)
def update_supplier(
        sup_id: int,
        payload: SupplierUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    sup = db.get(Supplier, sup_id)
    if not sup:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(sup, k, v)
    db.commit()
    db.refresh(sup)
    return sup


# ---------- Items + CSV ----------

# FILE: app/api/routes_inventory.py


@router.get("/items", response_model=List[ItemOut])
def list_items(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        q: Optional[str] = Query(None),
        is_active: Optional[bool] = Query(None),
        type: Optional[str] = Query(
            None,
            regex="^(drug|consumable)$",
            description="Filter by item type for doctor prescribing",
        ),
        limit: int = Query(100, ge=1, le=500),
):
    """
    List inventory items (medicines + consumables).

    Extra filters:
    - q: search code / name / generic_name
    - is_active: True/False
    - type:
        - 'drug'       -> is_consumable = False
        - 'consumable' -> is_consumable = True
    """
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    query = db.query(InventoryItem)

    if q:
        like = f"%{q}%"
        query = query.filter((InventoryItem.name.ilike(like))
                             | (InventoryItem.code.ilike(like))
                             | (InventoryItem.generic_name.ilike(like)))

    if is_active is not None:
        query = query.filter(InventoryItem.is_active == is_active)

    if type == "drug":
        query = query.filter(InventoryItem.is_consumable.is_(False))
    elif type == "consumable":
        query = query.filter(InventoryItem.is_consumable.is_(True))

    query = query.order_by(InventoryItem.name.asc()).limit(limit)
    return query.all()


@router.post("/items", response_model=ItemOut)
def create_item(
        payload: ItemCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    exists = db.query(InventoryItem).filter_by(code=payload.code).first()
    if exists:
        raise HTTPException(status_code=400, detail="Item code already exists")
    item = InventoryItem(**payload.model_dump())
    db.add(item)
    db.flush()  # get item.id

    # auto-generate QR if not provided
    if not item.qr_number:
        item.qr_number = f"MED-{item.id:06d}"

    db.commit()
    db.refresh(item)
    return item


@router.put("/items/{item_id}", response_model=ItemOut)
def update_item(
        item_id: int,
        payload: ItemUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    data = payload.model_dump(exclude_unset=True)

    # If QR number is being changed, ensure unique
    new_qr = data.get("qr_number")
    if new_qr:
        existing_qr = (db.query(InventoryItem).filter(
            InventoryItem.qr_number == new_qr, InventoryItem.id
            != item_id).first())
        if existing_qr:
            raise HTTPException(status_code=400,
                                detail="QR number already in use")

    for k, v in data.items():
        setattr(item, k, v)

    db.commit()
    db.refresh(item)
    return item


@router.get("/items/by-qr/{qr_number}", response_model=ItemOut)
def get_item_by_qr(
        qr_number: str,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Scan or enter QR number → return medicine.
    Ideal for QR scanners (keyboard wedge).
    """
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = (db.query(InventoryItem).filter(
        InventoryItem.qr_number == qr_number).first())
    if not item:
        raise HTTPException(status_code=404,
                            detail="No medicine found for this QR number")

    return item


@router.get("/items/{item_id}/qr")
def get_item_barcode_image(
        item_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Returns a Code128 BARCODE PNG for the item's qr_number (scan code).
    Path kept as /qr for compatibility, but image is now BARCODE.
    """
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Ensure we have a scan code
    _ensure_item_qr_number(db, item)
    code_str = item.qr_number

    # Generate barcode PNG
    buf = BytesIO()
    barcode_obj = Code128(code_str, writer=ImageWriter())
    # write_text=True will print human-readable text under bars – looks nice on label
    barcode_obj.write(buf, options={"write_text": True})
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{code_str}.png"'},
    )


@router.get("/items/sample-csv")
def download_sample_items_csv(
        current_user: User = Depends(auth_current_user), ):
    """
    Returns sample CSV structure for bulk upload of medicines/consumables.
    """
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "code",
        "Bar Code Number",
        "name",
        "generic_name",
        "form",
        "strength",
        "unit",
        "pack_size",
        "manufacturer",
        "class_name",
        "atc_code",
        "hsn_code",
        "lasa_flag",
        "is_consumable",
        "default_tax_percent",
        "default_price",
        "default_mrp",
        "reorder_level",
        "max_level",
        "is_active",
    ])
    writer.writerow([
        "AMOX500",
        "",
        "Amoxicillin 500",
        "Amoxicillin",
        "tablet",
        "500 mg",
        "tablet",
        "10",
        "ACME Pharma",
        "Antibiotic",
        "J01CA04",
        "3004",
        "FALSE",
        "FALSE",
        "12",
        "3.50",
        "5.00",
        "50",
        "500",
        "TRUE",
    ])
    output.seek(0)
    filename = "pharmacy_items_sample.csv"
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/items/bulk-upload")
def bulk_upload_items_csv(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Upload items via CSV. Uses same columns as sample CSV.
    """
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    content = file.file.read().decode("utf-8")
    reader = csv.DictReader(StringIO(content))

    created = 0
    updated = 0
    for row in reader:
        code = (row.get("code") or "").strip()
        if not code:
            continue

        qr_number_raw = (row.get("qr_number") or "").strip() or None
        existing = db.query(InventoryItem).filter_by(code=code).first()
        data = {
            "code": code,
            "name": (row.get("name") or "").strip(),
            "generic_name": (row.get("generic_name") or "").strip(),
            "form": (row.get("form") or "").strip(),
            "strength": (row.get("strength") or "").strip(),
            "unit": (row.get("unit") or "unit").strip(),
            "pack_size": (row.get("pack_size") or "1").strip(),
            "manufacturer": (row.get("manufacturer") or "").strip(),
            "class_name": (row.get("class_name") or "").strip(),
            "atc_code": (row.get("atc_code") or "").strip(),
            "hsn_code": (row.get("hsn_code") or "").strip(),
            "lasa_flag": str(row.get("lasa_flag") or "").upper() == "TRUE",
            "is_consumable": str(row.get("is_consumable")
                                 or "").upper() == "TRUE",
            "default_tax_percent":
            Decimal(row.get("default_tax_percent") or "0"),
            "default_price": Decimal(row.get("default_price") or "0"),
            "default_mrp": Decimal(row.get("default_mrp") or "0"),
            "reorder_level": Decimal(row.get("reorder_level") or "0"),
            "max_level": Decimal(row.get("max_level") or "0"),
            "is_active": str(row.get("is_active") or "TRUE").upper() == "TRUE",
        }

        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            if qr_number_raw:  # only overwrite if provided
                existing.qr_number = qr_number_raw
            updated += 1
        else:
            item = InventoryItem(**data)
            if qr_number_raw:
                item.qr_number = qr_number_raw
            db.add(item)
            db.flush()  # get item.id
            if not item.qr_number:
                item.qr_number = f"MED-{item.id:06d}"
            created += 1

    db.commit()
    return {"created": created, "updated": updated}


# ---------- Stock summary & alerts ----------


@router.get("/stock", response_model=List[StockSummaryOut])
def stock_summary(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
        q: Optional[str] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.stock.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = InventoryItem
    batch = ItemBatch
    loc = InventoryLocation

    # Only count ACTIVE + SALEABLE batches as "available" stock
    saleable_condition = and_(
        batch.is_saleable == True,  # noqa: E712
        batch.status == "ACTIVE",
    )

    total_qty_expr = func.coalesce(
        func.sum(case(
            (saleable_condition, batch.current_qty),
            else_=0,
        )),
        0,
    )

    j = (db.query(
        item.id.label("item_id"),
        item.code,
        item.name,
        total_qty_expr.label("total_qty"),
        item.reorder_level,
        item.max_level,
        loc.id.label("location_id"),
        loc.name.label("location_name"),
    ).select_from(item).outerjoin(batch, batch.item_id == item.id).outerjoin(
        loc, batch.location_id == loc.id))

    if location_id:
        j = j.filter(batch.location_id == location_id)
    if q:
        like = f"%{q}%"
        j = j.filter((item.name.ilike(like))
                     | (item.code.ilike(like))
                     | (item.generic_name.ilike(like)))

    j = j.group_by(
        item.id,
        item.code,
        item.name,
        item.reorder_level,
        item.max_level,
        loc.id,
        loc.name,
    )

    rows = j.all()
    result: List[StockSummaryOut] = []
    for r in rows:
        total = r.total_qty or Decimal("0")
        reorder = r.reorder_level or Decimal("0")
        max_level = r.max_level or Decimal("0")
        result.append(
            StockSummaryOut(
                item_id=r.item_id,
                code=r.code,
                name=r.name,
                location_id=r.location_id,
                location_name=r.location_name,
                total_qty=total,
                reorder_level=reorder,
                max_level=max_level,
                is_low=(reorder > 0 and total < reorder),
                is_over=(max_level > 0 and total > max_level),
            ))
    return result


@router.get("/alerts/expiry", response_model=List[ItemBatchOut])
def expiry_alerts(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
        within_days: int = Query(90, ge=1, le=365),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    today = date.today()

    # If location has its own expiry_alert_days, honour that
    if location_id:
        loc = db.get(InventoryLocation, location_id)
        if not loc or not loc.is_active:
            raise HTTPException(status_code=400, detail="Invalid location")
        days = loc.expiry_alert_days or within_days
    else:
        days = within_days

    limit = today + timedelta(days=days)

    q = (
        db.query(ItemBatch).join(ItemBatch.item).join(
            ItemBatch.location).filter(
                ItemBatch.current_qty > 0,
                ItemBatch.expiry_date.isnot(None),
                ItemBatch.expiry_date >= today,
                ItemBatch.expiry_date <= limit,
                ItemBatch.is_active == True,  # noqa: E712
                ItemBatch.is_saleable == True,  # only things still saleable
                ItemBatch.status == "ACTIVE",
                InventoryItem.is_active == True,  # noqa: E712
                InventoryLocation.is_active == True,  # noqa: E712
            ))

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    batches = q.order_by(ItemBatch.expiry_date.asc()).all()
    return batches


@router.get("/alerts/expired", response_model=List[ItemBatchOut])
def expired_alerts(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
):
    """
    Batches that are already expired but still ACTIVE + SALEABLE.
    These are the ones you must remove from sale and either
    return to supplier or write off internally.
    """
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    today = date.today()

    q = (
        db.query(ItemBatch).join(ItemBatch.item).join(
            ItemBatch.location).filter(
                ItemBatch.current_qty > 0,
                ItemBatch.expiry_date.isnot(None),
                ItemBatch.expiry_date < today,
                ItemBatch.is_active == True,  # noqa: E712
                ItemBatch.is_saleable == True,  # still wrongly saleable
                ItemBatch.status == "ACTIVE",
                InventoryItem.is_active == True,  # noqa: E712
                InventoryLocation.is_active == True,  # noqa: E712
            ))

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    return q.order_by(ItemBatch.expiry_date.asc()).all()


@router.get("/stock/quarantine", response_model=List[ItemBatchOut])
def quarantine_stock(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
):
    """
    Batches that are not saleable anymore:
    - EXPIRED / WRITTEN_OFF / RETURNED / QUARANTINE
    Typically physically kept in a separate quarantine shelf.
    """
    if not has_perm(current_user, "pharmacy.inventory.stock.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = (
        db.query(ItemBatch).join(ItemBatch.item).join(
            ItemBatch.location).filter(
                ItemBatch.current_qty > 0,
                ItemBatch.is_active == True,  # noqa: E712
                InventoryItem.is_active == True,  # noqa: E712
                InventoryLocation.is_active == True,  # noqa: E712
                or_(
                    ItemBatch.is_saleable == False,  # noqa: E712
                    ItemBatch.status != "ACTIVE",
                ),
            ))

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    return q.order_by(
        ItemBatch.expiry_date.is_(
            None),  # FALSE (0) for NOT NULL, TRUE (1) for NULL
        ItemBatch.expiry_date.asc(),  # then sort by date
    ).all()


@router.get("/alerts/low-stock", response_model=List[StockSummaryOut])
def low_stock_alerts(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(db=db,
                              current_user=current_user,
                              location_id=location_id)
    return [s for s in all_stock if s.is_low]


@router.get("/alerts/max-stock", response_model=List[StockSummaryOut])
def max_stock_alerts(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(db=db,
                              current_user=current_user,
                              location_id=location_id)
    return [s for s in all_stock if s.is_over]


# ---------- Purchase Orders ----------


def _generate_po_number(db: Session) -> str:
    """
    Generate a unique PO number for today.

    Pattern: POYYYYMMDDNNN
    Example: PO20251129001

    We iterate 001, 002, 003... and check existence so we never
    collide with an existing po_number (even if old data is weird).
    """
    today_str = date.today().strftime("%Y%m%d")
    prefix = f"PO{today_str}"
    seq = 1

    while True:
        candidate = f"{prefix}{seq:03d}"
        exists = (db.query(PurchaseOrder.id).filter(
            PurchaseOrder.po_number == candidate).first())
        if not exists:
            return candidate
        seq += 1


@router.post("/purchase-orders", response_model=PurchaseOrderOut)
def create_purchase_order(
        payload: PurchaseOrderCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po_number = _generate_po_number(db)
    po = PurchaseOrder(
        po_number=po_number,
        supplier_id=payload.supplier_id,
        location_id=payload.location_id,
        order_date=payload.order_date or date.today(),
        expected_date=payload.expected_date,
        notes=payload.notes or "",
        status="DRAFT",
        created_by_id=current_user.id,
    )
    db.add(po)
    db.flush()

    for line in payload.items:
        item = db.get(InventoryItem, line.item_id)
        if not item:
            raise HTTPException(status_code=400,
                                detail=f"Item {line.item_id} not found")
        line_total = (line.ordered_qty or Decimal("0")) * (line.unit_cost
                                                           or Decimal("0"))
        pli = PurchaseOrderItem(
            po_id=po.id,
            item_id=line.item_id,
            ordered_qty=line.ordered_qty,
            received_qty=Decimal("0"),
            unit_cost=line.unit_cost,
            tax_percent=line.tax_percent,
            mrp=line.mrp,
            line_total=line_total,
        )
        db.add(pli)

    db.commit()
    db.refresh(po)
    return po


@router.get("/purchase-orders", response_model=List[PurchaseOrderOut])
def list_purchase_orders(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        status: Optional[str] = Query(None),
        supplier_id: Optional[int] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = db.query(PurchaseOrder)
    if status:
        q = q.filter(PurchaseOrder.status == status)
    if supplier_id:
        q = q.filter(PurchaseOrder.supplier_id == supplier_id)
    if from_date:
        q = q.filter(PurchaseOrder.order_date >= from_date)
    if to_date:
        q = q.filter(PurchaseOrder.order_date <= to_date)
    q = q.order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc())
    return q.all()


@router.get("/purchase-orders/{po_id}", response_model=PurchaseOrderOut)
def get_purchase_order(
        po_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    return po


@router.put("/purchase-orders/{po_id}", response_model=PurchaseOrderOut)
def update_purchase_order(
        po_id: int,
        payload: PurchaseOrderUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status not in ("DRAFT", "SENT"):
        raise HTTPException(status_code=400,
                            detail="Only DRAFT/SENT POs can be edited")

    for k, v in payload.model_dump(exclude_unset=True,
                                   exclude={"items"}).items():
        setattr(po, k, v)

    if payload.items is not None:
        # replace lines
        db.query(PurchaseOrderItem).filter(
            PurchaseOrderItem.po_id == po.id).delete()
        db.flush()
        for line in payload.items:
            item = db.get(InventoryItem, line.item_id)
            if not item:
                raise HTTPException(status_code=400,
                                    detail=f"Item {line.item_id} not found")
            line_total = (line.ordered_qty or Decimal("0")) * (line.unit_cost
                                                               or Decimal("0"))
            pli = PurchaseOrderItem(
                po_id=po.id,
                item_id=line.item_id,
                ordered_qty=line.ordered_qty,
                received_qty=Decimal("0"),
                unit_cost=line.unit_cost,
                tax_percent=line.tax_percent,
                mrp=line.mrp,
                line_total=line_total,
            )
            db.add(pli)

    db.commit()
    db.refresh(po)
    return po


@router.post("/purchase-orders/{po_id}/status",
             response_model=PurchaseOrderOut)
def change_purchase_order_status(
        po_id: int,
        status: str = Query(
            ...,
            regex="^(DRAFT|SENT|PARTIALLY_RECEIVED|COMPLETED|CANCELLED|CLOSED)$"
        ),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    po.status = status
    db.commit()
    db.refresh(po)
    return po


# ----- PO PDF & email (email integration to be wired into your existing email util) -----


def _build_po_pdf(po: PurchaseOrder) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Purchase Order: {po.po_number}")
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Supplier: {po.supplier.name}")
    y -= 15
    if po.supplier.address:
        c.drawString(40, y, f"Address: {po.supplier.address}")
        y -= 15
    if po.supplier.gstin:
        c.drawString(40, y, f"GSTIN: {po.supplier.gstin}")
        y -= 15

    y -= 10
    c.drawString(40, y, f"Location: {po.location.name}")
    y -= 15
    c.drawString(40, y, f"Order date: {po.order_date.isoformat()}")
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

    for li in po.items:
        if y < 80:
            c.showPage()
            y = height - 40
        c.drawString(40, y, li.item.code)
        c.drawString(120, y, li.item.name[:30])
        c.drawRightString(350, y, f"{li.ordered_qty}")
        c.drawRightString(410, y, f"{li.unit_cost}")
        c.drawRightString(460, y, f"{li.tax_percent}")
        c.drawRightString(550, y, f"{li.line_total}")
        grand_total += li.line_total or Decimal("0")
        y -= 14

    y -= 20
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(550, y, f"Grand Total: {grand_total}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


@router.get("/purchase-orders/{po_id}/pdf")
def download_po_pdf(
        po_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/purchase-orders/{po_id}/mark-sent",
             response_model=PurchaseOrderOut)
def mark_po_sent(
        po_id: int,
        email_to: str = Query(
            ...,
            description="Supplier email address to send PO PDF to",
        ),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.po.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if not email_to:
        raise HTTPException(status_code=400, detail="email_to is required")

    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    # Build PDF
    pdf_bytes = _build_po_pdf(po)
    filename = f"PO_{po.po_number}.pdf"

    sender_name = user_display_name(current_user)

    body_text = (f"Dear {po.supplier.name if po.supplier else 'Supplier'},\n\n"
                 f"Please find attached Purchase Order {po.po_number} "
                 f"for {po.location.name if po.location else ''}.\n\n"
                 "Regards,\n"
                 f"{sender_name}")

    try:
        # Now valid: our send_email supports `attachments=`
        send_email(
            email_to,  # recipient
            f"Purchase Order {po.po_number}",  # subject
            body_text,  # body
            attachments=[(filename, pdf_bytes, "application/pdf")],
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send PO email: {e}",
        )

    po.status = "SENT"
    po.email_sent_to = email_to
    po.email_sent_at = datetime.utcnow()
    po.email_sent_by_name = sender_name

    db.commit()
    db.refresh(po)
    return po


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def recalc_grn_totals(grn: GRN) -> None:
    """
    Recalculate:
    - each item's discount/tax/totals
    - header totals + calculated_grn_amount + amount_difference
    """
    taxable = Decimal("0")
    disc = Decimal("0")
    cgst = sgst = igst = Decimal("0")
    total = Decimal("0")

    for it in (grn.items or []):
        qty = _d(it.quantity)
        rate = _d(it.unit_cost)
        gross = (qty * rate).quantize(Decimal("0.01"))

        # discount
        disc_amt = _d(getattr(it, "discount_amount", 0))
        disc_pct = _d(getattr(it, "discount_percent", 0))
        if disc_amt <= 0 and disc_pct > 0:
            disc_amt = (gross * disc_pct / Decimal("100")).quantize(Decimal("0.01"))
        if disc_amt < 0:
            disc_amt = Decimal("0")

        tax_base = (gross - disc_amt).quantize(Decimal("0.01"))
        if tax_base < 0:
            tax_base = Decimal("0")

        # tax split (preferred)
        igst_pct = _d(getattr(it, "igst_percent", 0))
        cgst_pct = _d(getattr(it, "cgst_percent", 0))
        sgst_pct = _d(getattr(it, "sgst_percent", 0))
        tax_pct = _d(getattr(it, "tax_percent", 0))

        igst_amt = (tax_base * igst_pct / Decimal("100")).quantize(Decimal("0.01"))
        cgst_amt = (tax_base * cgst_pct / Decimal("100")).quantize(Decimal("0.01"))
        sgst_amt = (tax_base * sgst_pct / Decimal("100")).quantize(Decimal("0.01"))

        # fallback: tax_percent if split not provided
        if (igst_amt + cgst_amt + sgst_amt) == 0 and tax_pct > 0:
            t = (tax_base * tax_pct / Decimal("100")).quantize(Decimal("0.01"))
            cgst_amt = (t / 2).quantize(Decimal("0.01"))
            sgst_amt = (t - cgst_amt).quantize(Decimal("0.01"))

        line_total = (tax_base + igst_amt + cgst_amt + sgst_amt).quantize(Decimal("0.01"))

        # write back
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


def _get_grn_or_404(db: Session, grn_id: int) -> GRN:
    grn = (
        db.query(GRN)
        .options(
            joinedload(GRN.items).joinedload(GRNItem.item),
            joinedload(GRN.supplier),
            joinedload(GRN.location),
            joinedload(GRN.purchase_order),
        )
        .filter(GRN.id == grn_id)
        .first()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")
    return grn


# ---------------------------
# Routes
# ---------------------------

def ensure_grn_fk(grn):
    # try repair from relationships (if loaded)
    if not getattr(grn, "supplier_id", None) and getattr(grn, "supplier", None):
        grn.supplier_id = grn.supplier.id

    if not getattr(grn, "location_id", None) and getattr(grn, "location", None):
        grn.location_id = grn.location.id

    # final strict validation
    if not getattr(grn, "supplier_id", None):
        raise HTTPException(status_code=400, detail="GRN is missing supplier_id. Please select Supplier.")
    if not getattr(grn, "location_id", None):
        raise HTTPException(status_code=400, detail="GRN is missing location_id. Please select Location.")

def dbg(*args):
    print("[GRN]", *args)

@router.post("/grn", response_model=GRNOut)
def create_grn(
    payload: GRNCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        grn_number = _generate_grn_number(db)

        grn = GRN(
            grn_number=grn_number,
            po_id=payload.po_id,
            supplier_id=payload.supplier_id,
            location_id=payload.location_id,
            received_date=payload.received_date or date.today(),
            invoice_number=payload.invoice_number or "",
            invoice_date=payload.invoice_date,
            notes=payload.notes or "",
            status="DRAFT",
            created_by_id=current_user.id,

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

            gli = GRNItem(
                grn_id=grn.id,
                po_item_id=line.po_item_id,
                item_id=line.item_id,
                batch_no=line.batch_no.strip(),
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
            db.add(gli)

        db.flush()
        recalc_grn_totals(grn)

        db.commit()
        return _get_grn_or_404(db, grn.id)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        dbg("CREATE FAILED:", repr(e))
        raise HTTPException(status_code=500, detail=f"Failed to create GRN: {e}")


@router.get("/grn", response_model=List[GRNOut])
def list_grns(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    status: Optional[str] = Query(None),
    supplier_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.grn.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = (
        db.query(GRN)
        .options(
            joinedload(GRN.supplier),
            joinedload(GRN.location),
            joinedload(GRN.purchase_order),
            joinedload(GRN.items).joinedload(GRNItem.item),
        )
    )

    if status:
        q = q.filter(GRN.status == status)
    if supplier_id:
        q = q.filter(GRN.supplier_id == supplier_id)
    if from_date:
        q = q.filter(GRN.received_date >= from_date)
    if to_date:
        q = q.filter(GRN.received_date <= to_date)

    return q.order_by(GRN.received_date.desc(), GRN.id.desc()).all()


@router.get("/grn/{grn_id}", response_model=GRNOut)
def get_grn(
    grn_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.grn.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return _get_grn_or_404(db, grn_id)


@router.post("/grn/{grn_id}/post", response_model=GRNOut)
def post_grn(
    grn_id: int,
    body: GRNPostIn = Body(default_factory=GRNPostIn),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = (
        db.query(GRN)
        .options(joinedload(GRN.items), joinedload(GRN.purchase_order).joinedload(PurchaseOrder.items))
        .filter(GRN.id == grn_id)
        .with_for_update()
        .first()
    )
    if not grn:
        raise HTTPException(status_code=404, detail="GRN not found")

    if grn.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be posted")

    if not grn.items:
        raise HTTPException(status_code=400, detail="Cannot post GRN with no line items")

    if _d(getattr(grn, "supplier_invoice_amount", 0)) <= Decimal("0"):
        raise HTTPException(status_code=400, detail="Supplier invoice amount must be > 0 to post GRN")

    for li in grn.items:
        if not (li.batch_no or "").strip():
            raise HTTPException(status_code=400, detail="Batch number is required for all GRN items")

    try:
        recalc_grn_totals(grn)

        if _d(grn.amount_difference) != Decimal("0"):
            reason = (body.difference_reason or grn.difference_reason or "").strip()
            if not reason:
                raise HTTPException(
                    status_code=400,
                    detail="Invoice mismatch detected. Provide difference_reason to post GRN.",
                )
            grn.difference_reason = reason

        # ... your batch/stock update loop remains same ...

        grn.status = "POSTED"
        grn.posted_by_id = current_user.id
        grn.posted_at = datetime.utcnow()

       
        sync_supplier_invoice_from_grn(db, grn)

        db.commit()
        return _get_grn_or_404(db, grn.id)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        dbg("POST FAILED:", repr(e))
        raise HTTPException(status_code=500, detail=f"Failed to post GRN: {e}")



@router.post("/grn/{grn_id}/cancel", response_model=GRNOut)
def cancel_grn(
    grn_id: int,
    body: GRNCancelIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.grn.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    grn = _get_grn_or_404(db, grn_id)

    if grn.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT GRN can be cancelled")

    reason = (body.cancel_reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="cancel_reason is required")

    # ✅ ADD HERE (before cancelling)
    tenant_id = get_tenant_id_from_user(current_user)
    inv = (
        db.query(SupplierInvoice)
        .filter(
            SupplierInvoice.tenant_id == tenant_id,
            SupplierInvoice.grn_id == grn.id
        )
        .first()
    )
    if inv and _d(inv.paid_amount) > Decimal("0.00"):
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel GRN: payments already recorded for this invoice"
        )

    try:
        grn.status = "CANCELLED"
        grn.cancel_reason = reason
        grn.cancelled_by_id = current_user.id
        grn.cancelled_at = datetime.utcnow()

        # ✅ Sync supplier invoice to CANCELLED
        sync_supplier_invoice_from_grn(db, tenant_id=tenant_id, grn=grn)

        db.commit()
        return _get_grn_or_404(db, grn.id)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to cancel GRN: {e}")



# ---------- Returns ----------


def _generate_return_number(db: Session) -> str:
    """
    Generate a unique Return Note number for today.

    Pattern: RTNYYYYMMDDNNN
    Example: RTN20251129001

    We iterate 001, 002, 003... and check existence so we never
    collide with an existing return_number.
    """
    today_str = date.today().strftime("%Y%m%d")
    prefix = f"RTN{today_str}"
    seq = 1

    while True:
        candidate = f"{prefix}{seq:03d}"
        exists = (db.query(ReturnNote.id).filter(
            ReturnNote.return_number == candidate).first())
        if not exists:
            return candidate
        seq += 1


@router.post("/returns", response_model=ReturnOut)
def create_return(
        payload: ReturnCreate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.returns.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    ret_no = _generate_return_number(db)
    rn = ReturnNote(
        return_number=ret_no,
        type=payload.type,
        supplier_id=payload.supplier_id,
        location_id=payload.location_id,
        return_date=payload.return_date or date.today(),
        status="DRAFT",
        reason=payload.reason or "",
        created_by_id=current_user.id,
    )
    db.add(rn)
    db.flush()

    for li in payload.items:
        item = db.get(InventoryItem, li.item_id)
        if not item:
            raise HTTPException(
                status_code=400,
                detail=f"Item {li.item_id} not found",
            )

        # --- Resolve batch using id OR batch_no ---
        batch_id = li.batch_id

        if not batch_id and getattr(li, "batch_no", None):
            batch = (db.query(ItemBatch).filter(
                ItemBatch.item_id == li.item_id,
                ItemBatch.location_id == payload.location_id,
                ItemBatch.batch_no == li.batch_no,
            ).first())
            if not batch:
                raise HTTPException(
                    status_code=400,
                    detail=(f"Batch '{li.batch_no}' not found for this item "
                            f"in location {payload.location_id}"),
                )
            batch_id = batch.id

        rli = ReturnNoteItem(
            return_id=rn.id,
            item_id=li.item_id,
            batch_id=batch_id,
            quantity=li.quantity,
            reason=li.reason or "",
        )
        db.add(rli)

    db.commit()
    db.refresh(rn)
    return rn


@router.get("/returns", response_model=List[ReturnOut])
def list_returns(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        type: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.returns.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = db.query(ReturnNote)
    if type:
        q = q.filter(ReturnNote.type == type)
    if status:
        q = q.filter(ReturnNote.status == status)
    if from_date:
        q = q.filter(ReturnNote.return_date >= from_date)
    if to_date:
        q = q.filter(ReturnNote.return_date <= to_date)
    q = q.order_by(ReturnNote.return_date.desc(), ReturnNote.id.desc())
    return q.all()


@router.get("/returns/{ret_id}", response_model=ReturnOut)
def get_return(
        ret_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.returns.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    rn = db.get(ReturnNote, ret_id)
    if not rn:
        raise HTTPException(status_code=404, detail="Return note not found")
    return rn


@router.post("/returns/{ret_id}/post", response_model=ReturnOut)
def post_return(
        ret_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.returns.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    rn = db.get(ReturnNote, ret_id)
    if not rn:
        raise HTTPException(status_code=404, detail="Return note not found")
    if rn.status != "DRAFT":
        raise HTTPException(status_code=400,
                            detail="Only DRAFT return can be posted")

    # Direction of stock movement
    direction = 1
    txn_type = "RETURN"
    if rn.type == "TO_SUPPLIER":
        direction = -1
        txn_type = "RETURN_TO_SUPPLIER"
    elif rn.type == "FROM_CUSTOMER":
        direction = 1
        txn_type = "RETURN_FROM_CUSTOMER"
    else:
        # INTERNAL write-off / internal movement
        direction = -1
        txn_type = "RETURN_INTERNAL"

    today = date.today()

    for li in rn.items:
        qty = li.quantity or Decimal("0")
        if qty <= 0:
            continue

        if not li.batch_id:
            raise HTTPException(status_code=400,
                                detail="Return item must have batch_id")

        batch = db.get(ItemBatch, li.batch_id)
        if not batch:
            raise HTTPException(status_code=400,
                                detail=f"Batch {li.batch_id} not found")

        # ---- VALIDATION PER TYPE ----

        if rn.type in ("TO_SUPPLIER", "INTERNAL"):
            # For supplier/internal returns, we allow non-saleable
            # (expired / quarantine, etc.), but batch must be active
            # and there must be enough stock.
            if not batch.is_active:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Batch {batch.batch_no} is inactive (no stock to return)",
                )
            if batch.current_qty < qty:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Insufficient stock in batch {batch.batch_no} (have {batch.current_qty}, trying to return {qty})",
                )

        elif rn.type == "FROM_CUSTOMER":
            # Customer returns add stock back.
            # We generally should not put **expired** stock back into ACTIVE saleable.
            if batch.expiry_date and batch.expiry_date < today:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Batch {batch.batch_no} is already expired and "
                        "cannot be returned to saleable stock from customer."),
                )

        # ---- APPLY STOCK CHANGE ----
        delta = direction * qty
        adjust_batch_qty(batch=batch, delta=delta)

        # ---- UPDATE BATCH STATE PER TYPE ----
        if rn.type == "TO_SUPPLIER":
            # If completely returned, close the batch
            if batch.current_qty <= 0:
                batch.is_saleable = False
                batch.is_active = False
                batch.status = "RETURNED"

        elif rn.type == "INTERNAL":
            # Internal write-off / destruction
            batch.is_saleable = False
            if batch.expiry_date and batch.expiry_date < today:
                batch.status = "WRITTEN_OFF"
            else:
                batch.status = "QUARANTINE"
            if batch.current_qty <= 0:
                batch.is_active = False

        elif rn.type == "FROM_CUSTOMER":
            # Patient returned unused stock; if not expired, make it ACTIVE + saleable
            batch.is_active = True
            if not batch.expiry_date or batch.expiry_date >= today:
                batch.is_saleable = True
                batch.status = "ACTIVE"

        # ---- STOCK TRANSACTION ----
        create_stock_transaction(
            db,
            user=current_user,
            location_id=batch.location_id,
            item_id=batch.item_id,
            batch_id=batch.id,
            qty_delta=delta,
            txn_type=txn_type,
            ref_type="RETURN",
            ref_id=rn.id,
            unit_cost=batch.unit_cost,
            mrp=batch.mrp,
            remark=f"Return {rn.return_number}: {li.reason}",
        )

    rn.status = "POSTED"
    db.commit()
    db.refresh(rn)
    return rn


# ---------- Dispense ----------


@router.post("/dispense")
def dispense_stock(
        payload: DispenseRequestIn,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Dispense medicines: FEFO allocation and automatic stock deduction.
    This version:
    - Never dispenses EXPIRED or non-saleable batches
    - Uses allocate_batches_fefo() which already skips expired/non-saleable
    """
    if not has_perm(current_user, "pharmacy.inventory.dispense"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    location = db.get(InventoryLocation, payload.location_id)
    if not location or not location.is_active:
        raise HTTPException(status_code=400, detail="Invalid location")

    today = date.today()

    for line in payload.lines:
        item = db.get(InventoryItem, line.item_id)
        if not item or not item.is_active:
            raise HTTPException(status_code=400,
                                detail=f"Invalid item {line.item_id}")

        qty = Decimal(line.quantity)

        if line.batch_id:
            # Use specific batch
            batch = db.get(ItemBatch, line.batch_id)
            if not batch or batch.location_id != payload.location_id:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid batch {line.batch_id}")

            if not batch.is_active or not batch.is_saleable:
                raise HTTPException(
                    status_code=400,
                    detail=f"Batch {batch.batch_no} is not active/saleable",
                )

            if batch.expiry_date and batch.expiry_date < today:
                batch.status = "EXPIRED"
                batch.is_saleable = False
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Batch {batch.batch_no} is expired and cannot be dispensed",
                )

            if batch.current_qty < qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock in batch {batch.batch_no}",
                )

            adjust_batch_qty(batch=batch, delta=-qty)
            create_stock_transaction(
                db,
                user=current_user,
                location_id=payload.location_id,
                item_id=line.item_id,
                batch_id=batch.id,
                qty_delta=-qty,
                txn_type="DISPENSE",
                ref_type="DISPENSE",
                ref_id=None,
                unit_cost=batch.unit_cost,
                mrp=batch.mrp,
                remark=payload.remark or "",
                patient_id=payload.patient_id,
                visit_id=payload.visit_id,
            )
        else:
            # FEFO across multiple batches
            try:
                allocations = allocate_batches_fefo(
                    db,
                    location_id=payload.location_id,
                    item_id=line.item_id,
                    quantity=qty,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            if not allocations:
                raise HTTPException(
                    status_code=400,
                    detail="No saleable stock available for this item",
                )

            for batch, used in allocations:
                if not batch.is_active or not batch.is_saleable:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Batch {batch.batch_no} is not active/saleable",
                    )
                if batch.expiry_date and batch.expiry_date < today:
                    batch.status = "EXPIRED"
                    batch.is_saleable = False
                    raise HTTPException(
                        status_code=400,
                        detail=
                        f"Batch {batch.batch_no} is expired and cannot be dispensed",
                    )

                adjust_batch_qty(batch=batch, delta=-used)
                create_stock_transaction(
                    db,
                    user=current_user,
                    location_id=payload.location_id,
                    item_id=line.item_id,
                    batch_id=batch.id,
                    qty_delta=-used,
                    txn_type="DISPENSE",
                    ref_type="DISPENSE",
                    ref_id=None,
                    unit_cost=batch.unit_cost,
                    mrp=batch.mrp,
                    remark=payload.remark or "",
                    patient_id=payload.patient_id,
                    visit_id=payload.visit_id,
                )

    db.commit()
    return {"status": "ok"}


# ---------- Transactions listing ----------


@router.get("/transactions", response_model=List[StockTransactionOut])
def list_transactions(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
        location_id: Optional[int] = Query(None),
        item_id: Optional[int] = Query(None),
        txn_type: Optional[str] = Query(None),
        from_date: Optional[datetime] = Query(None),
        to_date: Optional[datetime] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.txns.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = db.query(StockTransaction)

    if location_id:
        q = q.filter(StockTransaction.location_id == location_id)
    if item_id:
        q = q.filter(StockTransaction.item_id == item_id)
    if txn_type:
        q = q.filter(StockTransaction.txn_type == txn_type)
    if from_date:
        q = q.filter(StockTransaction.txn_time >= from_date)
    if to_date:
        q = q.filter(StockTransaction.txn_time <= to_date)

    q = q.order_by(StockTransaction.txn_time.desc(),
                   StockTransaction.id.desc())

    rows = q.all()

    result: List[StockTransactionOut] = []
    for tx in rows:
        item = db.get(InventoryItem, tx.item_id) if tx.item_id else None
        batch = db.get(ItemBatch, tx.batch_id) if tx.batch_id else None
        location = (db.get(InventoryLocation, tx.location_id)
                    if tx.location_id else None)
        user = db.get(User, tx.user_id) if tx.user_id else None

        # Nice reference text: "GRN GRN20251129001", "Return RTN20251129001"
        ref_display = None
        if tx.ref_type == "GRN" and tx.ref_id:
            grn = db.get(GRN, tx.ref_id)
            if grn:
                ref_display = f"GRN {grn.grn_number}"
        elif tx.ref_type == "RETURN" and tx.ref_id:
            rn = db.get(ReturnNote, tx.ref_id)
            if rn:
                ref_display = f"Return {rn.return_number}"

        result.append(
            StockTransactionOut(
                id=tx.id,
                txn_time=tx.txn_time,
                location_id=tx.location_id,
                item_id=tx.item_id,
                batch_id=tx.batch_id,
                quantity_change=tx.quantity_change,
                txn_type=tx.txn_type,
                ref_type=tx.ref_type,
                ref_id=tx.ref_id,
                unit_cost=tx.unit_cost,
                mrp=tx.mrp,
                patient_id=tx.patient_id,
                visit_id=tx.visit_id,
                user_id=tx.user_id,
                # display fields
                item_name=item.name if item else None,
                item_code=item.code if item else None,
                batch_no=batch.batch_no if batch else None,
                location_name=location.name if location else None,
                user_name=user_display_name(user) if user else "System",
                ref_display=ref_display,
            ))

    return result


@router.get("/items/by-qr-number", response_model=ItemOut)
def get_item_by_qr_number(
        qr_number: str = Query(..., min_length=1),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    We treat qr_number as the BARCODE number.
    """
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = (db.query(InventoryItem).filter(
        InventoryItem.qr_number == qr_number).first())
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item
