# FILE: app/api/routes_inventory.py
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO, BytesIO
from typing import List, Optional

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
from fastapi.responses import StreamingResponse
from sqlalchemy import func, and_, or_, case
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.pharmacy_inventory import (
    InventoryLocation,
    Supplier,
    InventoryItem,
    ItemBatch,
    ReturnNote,
    ReturnNoteItem,
    StockTransaction,
    GRN,  # kept ONLY for transactions ref_display (no GRN routes here)
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
    ReturnCreate,
    ReturnOut,
    StockTransactionOut,
    DispenseRequestIn,
)

from app.services.inventory import (
    create_stock_transaction,
    allocate_batches_fefo,
    adjust_batch_qty,
)

from app.schemas.inventory_bulk_upload import (
    BulkUploadCommitOut,
    BulkUploadErrorOut,
    BulkUploadPreviewOut,
)

from app.services.inventory_bulk_upload import (
    TEMPLATE_HEADERS,
    REQUIRED_HEADERS,
    parse_upload_to_rows,
    validate_item_rows,
    apply_items_import,
)

router = APIRouter(prefix="/inventory", tags=["Inventory - Pharmacy"])


# ============================================================
# Permissions helper
# ============================================================
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


def _ensure_item_qr_number(db: Session, item: InventoryItem) -> None:
    """
    We still use the qr_number column, but treat it as BARCODE number.
    Auto-generate if empty.
    Pattern: MD_0001, MD_0002, ...
    """
    if not item.qr_number:
        item.qr_number = f"MD_{item.id:04d}"
        db.add(item)
        db.commit()
        db.refresh(item)


def user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    for k in ("full_name", "name", "username", "email"):
        v = getattr(user, k, None)
        if v:
            return v
    return f"User #{getattr(user, 'id', 'unknown')}"


# ============================================================
# Locations
# ============================================================
@router.get("/locations", response_model=List[LocationOut])
def list_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.locations.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return db.query(InventoryLocation).order_by(InventoryLocation.name).all()


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
        raise HTTPException(status_code=400, detail="Location code already exists")
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


# ============================================================
# Suppliers
# ============================================================
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
        like = f"%{q.strip()}%"
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
        raise HTTPException(status_code=400, detail="Supplier code already exists")
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


# ============================================================
# Items + Barcode + Bulk Upload
# ============================================================
@router.get("/items", response_model=List[ItemOut])
def list_items(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    type_: Optional[str] = Query(
        None,
        alias="type",
        pattern="^(drug|consumable)$",
        description="Filter by item type for doctor prescribing",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    """
    List inventory items (medicines + consumables).
    Filters:
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
        like = f"%{q.strip()}%"
        query = query.filter(
            (InventoryItem.name.ilike(like))
            | (InventoryItem.code.ilike(like))
            | (InventoryItem.generic_name.ilike(like))
        )

    if is_active is not None:
        query = query.filter(InventoryItem.is_active == is_active)

    if type_ == "drug":
        query = query.filter(InventoryItem.is_consumable.is_(False))
    elif type_ == "consumable":
        query = query.filter(InventoryItem.is_consumable.is_(True))

    return query.order_by(InventoryItem.name.asc()).limit(limit).all()


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
    db.flush()

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
    new_qr = data.get("qr_number")
    if new_qr:
        existing_qr = (
            db.query(InventoryItem)
            .filter(InventoryItem.qr_number == new_qr, InventoryItem.id != item_id)
            .first()
        )
        if existing_qr:
            raise HTTPException(status_code=400, detail="QR number already in use")

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
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = db.query(InventoryItem).filter(InventoryItem.qr_number == qr_number).first()
    if not item:
        raise HTTPException(status_code=404, detail="No medicine found for this QR number")
    return item


@router.get("/items/{item_id}/qr")
def get_item_barcode_image(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    """
    Returns a Code128 BARCODE PNG for the item's qr_number (scan code).
    Path kept as /qr for compatibility, but image is BARCODE.
    """
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    _ensure_item_qr_number(db, item)
    code_str = item.qr_number

    buf = BytesIO()
    barcode_obj = Code128(code_str, writer=ImageWriter())
    barcode_obj.write(buf, options={"write_text": True})
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{code_str}.png"'},
    )


@router.get("/items/sample-csv")
def download_sample_items_csv(current_user: User = Depends(auth_current_user)):
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


@router.get("/items/bulk-upload/template")
def download_items_template(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if format == "csv":
        output = StringIO()
        w = csv.writer(output)
        w.writerow(TEMPLATE_HEADERS)
        w.writerow([
            "ITEM001", "Paracetamol 500mg", "Paracetamol", "Tablet", "500mg",
            "TAB", "10", "ABC Pharma", "Analgesic", "N02BE01", "3004",
            "FALSE", "FALSE", "12", "1.50", "2.00", "50", "500", "TRUE", "MED-000001"
        ])
        data = output.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            BytesIO(data),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=items_template.csv"},
        )

    try:
        from openpyxl import Workbook
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="openpyxl required for xlsx template. Install: pip install openpyxl",
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(TEMPLATE_HEADERS)
    ws.append([
        "ITEM001", "Paracetamol 500mg", "Paracetamol", "Tablet", "500mg",
        "TAB", "10", "ABC Pharma", "Analgesic", "N02BE01", "3004",
        "FALSE", "FALSE", "12", "1.50", "2.00", "50", "500", "TRUE", "MED-000001"
    ])

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=items_template.xlsx"},
    )


@router.post("/items/bulk-upload/preview", response_model=BulkUploadPreviewOut)
def preview_items_upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        file_type, rows = parse_upload_to_rows(file.filename or "", file.content_type or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    normalized, errs = validate_item_rows(rows)
    sample_rows = normalized[:20]

    return BulkUploadPreviewOut(
        file_type=file_type,
        total_rows=len(rows),
        valid_rows=len(normalized) - len([e for e in errs if e.row != 0]),
        error_rows=len(errs),
        required_columns=REQUIRED_HEADERS,
        optional_columns=[c for c in TEMPLATE_HEADERS if c not in REQUIRED_HEADERS],
        sample_rows=sample_rows,
        errors=[BulkUploadErrorOut(row=e.row, code=e.code, column=e.column, message=e.message) for e in errs],
    )


@router.post("/items/bulk-upload/commit", response_model=BulkUploadCommitOut)
def commit_items_upload(
    file: UploadFile = File(...),
    update_blanks: bool = Query(False, description="If true, blank cells overwrite existing values"),
    strict: bool = Query(True, description="If true, any error stops commit"),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        _, rows = parse_upload_to_rows(file.filename or "", file.content_type or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    normalized, errs = validate_item_rows(rows)

    if errs and strict:
        raise HTTPException(
            status_code=422,
            detail=[{"row": e.row, "code": e.code, "column": e.column, "message": e.message} for e in errs],
        )

    created, updated, skipped, db_errs = apply_items_import(db, normalized, update_blanks=update_blanks)
    out_errs = errs + db_errs

    return BulkUploadCommitOut(
        created=created,
        updated=updated,
        skipped=skipped,
        errors=[BulkUploadErrorOut(row=e.row, code=e.code, column=e.column, message=e.message) for e in out_errs],
    )


# ============================================================
# Stock summary & alerts
# ============================================================
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

    j = (
        db.query(
            item.id.label("item_id"),
            item.code,
            item.name,
            total_qty_expr.label("total_qty"),
            item.reorder_level,
            item.max_level,
            loc.id.label("location_id"),
            loc.name.label("location_name"),
        )
        .select_from(item)
        .outerjoin(batch, batch.item_id == item.id)
        .outerjoin(loc, batch.location_id == loc.id)
    )

    if location_id:
        j = j.filter(batch.location_id == location_id)

    if q:
        like = f"%{q.strip()}%"
        j = j.filter(
            (item.name.ilike(like))
            | (item.code.ilike(like))
            | (item.generic_name.ilike(like))
        )

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
            )
        )
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

    if location_id:
        loc = db.get(InventoryLocation, location_id)
        if not loc or not loc.is_active:
            raise HTTPException(status_code=400, detail="Invalid location")
        days = loc.expiry_alert_days or within_days
    else:
        days = within_days

    limit = today + timedelta(days=days)

    q = (
        db.query(ItemBatch)
        .join(ItemBatch.item)
        .join(ItemBatch.location)
        .filter(
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            ItemBatch.expiry_date >= today,
            ItemBatch.expiry_date <= limit,
            ItemBatch.is_active == True,  # noqa: E712
            ItemBatch.is_saleable == True,
            ItemBatch.status == "ACTIVE",
            InventoryItem.is_active == True,  # noqa: E712
            InventoryLocation.is_active == True,  # noqa: E712
        )
    )

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    return q.order_by(ItemBatch.expiry_date.asc()).all()


@router.get("/alerts/expired", response_model=List[ItemBatchOut])
def expired_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    today = date.today()

    q = (
        db.query(ItemBatch)
        .join(ItemBatch.item)
        .join(ItemBatch.location)
        .filter(
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            ItemBatch.expiry_date < today,
            ItemBatch.is_active == True,  # noqa: E712
            ItemBatch.is_saleable == True,
            ItemBatch.status == "ACTIVE",
            InventoryItem.is_active == True,  # noqa: E712
            InventoryLocation.is_active == True,  # noqa: E712
        )
    )

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    return q.order_by(ItemBatch.expiry_date.asc()).all()


@router.get("/stock/quarantine", response_model=List[ItemBatchOut])
def quarantine_stock(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.stock.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = (
        db.query(ItemBatch)
        .join(ItemBatch.item)
        .join(ItemBatch.location)
        .filter(
            ItemBatch.current_qty > 0,
            ItemBatch.is_active == True,  # noqa: E712
            InventoryItem.is_active == True,  # noqa: E712
            InventoryLocation.is_active == True,  # noqa: E712
            or_(
                ItemBatch.is_saleable == False,  # noqa: E712
                ItemBatch.status != "ACTIVE",
            ),
        )
    )

    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)

    return q.order_by(
        ItemBatch.expiry_date.is_(None),
        ItemBatch.expiry_date.asc(),
    ).all()


@router.get("/alerts/low-stock", response_model=List[StockSummaryOut])
def low_stock_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(
        db=db,
        current_user=current_user,
        location_id=location_id,
        q=None,  # ✅ IMPORTANT
    )
    return [s for s in all_stock if s.is_low]


@router.get("/alerts/max-stock", response_model=List[StockSummaryOut])
def max_stock_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(
        db=db,
        current_user=current_user,
        location_id=location_id,
        q=None,  # ✅ IMPORTANT: prevent Query() object default
    )
    return [s for s in all_stock if s.is_over]



# ============================================================
# Returns
# ============================================================
def _generate_return_number(db: Session) -> str:
    today_str = date.today().strftime("%Y%m%d")
    prefix = f"RTN{today_str}"
    seq = 1
    while True:
        candidate = f"{prefix}{seq:03d}"
        exists = db.query(ReturnNote.id).filter(ReturnNote.return_number == candidate).first()
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
            raise HTTPException(status_code=400, detail=f"Item {li.item_id} not found")

        batch_id = li.batch_id
        if not batch_id and getattr(li, "batch_no", None):
            batch = (
                db.query(ItemBatch)
                .filter(
                    ItemBatch.item_id == li.item_id,
                    ItemBatch.location_id == payload.location_id,
                    ItemBatch.batch_no == li.batch_no,
                )
                .first()
            )
            if not batch:
                raise HTTPException(
                    status_code=400,
                    detail=f"Batch '{li.batch_no}' not found for this item in location {payload.location_id}",
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

    return q.order_by(ReturnNote.return_date.desc(), ReturnNote.id.desc()).all()


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
        raise HTTPException(status_code=400, detail="Only DRAFT return can be posted")

    direction = 1
    txn_type = "RETURN"
    if rn.type == "TO_SUPPLIER":
        direction = -1
        txn_type = "RETURN_TO_SUPPLIER"
    elif rn.type == "FROM_CUSTOMER":
        direction = 1
        txn_type = "RETURN_FROM_CUSTOMER"
    else:
        direction = -1
        txn_type = "RETURN_INTERNAL"

    today = date.today()

    for li in rn.items:
        qty = li.quantity or Decimal("0")
        if qty <= 0:
            continue

        if not li.batch_id:
            raise HTTPException(status_code=400, detail="Return item must have batch_id")

        batch = db.get(ItemBatch, li.batch_id)
        if not batch:
            raise HTTPException(status_code=400, detail=f"Batch {li.batch_id} not found")

        if rn.type in ("TO_SUPPLIER", "INTERNAL"):
            if not batch.is_active:
                raise HTTPException(
                    status_code=400,
                    detail=f"Batch {batch.batch_no} is inactive (no stock to return)",
                )
            if batch.current_qty < qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock in batch {batch.batch_no} (have {batch.current_qty}, trying to return {qty})",
                )

        elif rn.type == "FROM_CUSTOMER":
            if batch.expiry_date and batch.expiry_date < today:
                raise HTTPException(
                    status_code=400,
                    detail=f"Batch {batch.batch_no} is expired and cannot be returned to saleable stock from customer.",
                )

        delta = direction * qty
        adjust_batch_qty(batch=batch, delta=delta)

        if rn.type == "TO_SUPPLIER":
            if batch.current_qty <= 0:
                batch.is_saleable = False
                batch.is_active = False
                batch.status = "RETURNED"

        elif rn.type == "INTERNAL":
            batch.is_saleable = False
            if batch.expiry_date and batch.expiry_date < today:
                batch.status = "WRITTEN_OFF"
            else:
                batch.status = "QUARANTINE"
            if batch.current_qty <= 0:
                batch.is_active = False

        elif rn.type == "FROM_CUSTOMER":
            batch.is_active = True
            if not batch.expiry_date or batch.expiry_date >= today:
                batch.is_saleable = True
                batch.status = "ACTIVE"

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


# ============================================================
# Dispense
# ============================================================
@router.post("/dispense")
def dispense_stock(
    payload: DispenseRequestIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.dispense"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    location = db.get(InventoryLocation, payload.location_id)
    if not location or not location.is_active:
        raise HTTPException(status_code=400, detail="Invalid location")

    today = date.today()

    for line in payload.lines:
        item = db.get(InventoryItem, line.item_id)
        if not item or not item.is_active:
            raise HTTPException(status_code=400, detail=f"Invalid item {line.item_id}")

        qty = Decimal(line.quantity)

        if line.batch_id:
            batch = db.get(ItemBatch, line.batch_id)
            if not batch or batch.location_id != payload.location_id:
                raise HTTPException(status_code=400, detail=f"Invalid batch {line.batch_id}")

            if not batch.is_active or not batch.is_saleable:
                raise HTTPException(status_code=400, detail=f"Batch {batch.batch_no} is not active/saleable")

            if batch.expiry_date and batch.expiry_date < today:
                batch.status = "EXPIRED"
                batch.is_saleable = False
                raise HTTPException(status_code=400, detail=f"Batch {batch.batch_no} is expired and cannot be dispensed")

            if batch.current_qty < qty:
                raise HTTPException(status_code=400, detail=f"Insufficient stock in batch {batch.batch_no}")

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
                raise HTTPException(status_code=400, detail="No saleable stock available for this item")

            for batch, used in allocations:
                if not batch.is_active or not batch.is_saleable:
                    raise HTTPException(status_code=400, detail=f"Batch {batch.batch_no} is not active/saleable")
                if batch.expiry_date and batch.expiry_date < today:
                    batch.status = "EXPIRED"
                    batch.is_saleable = False
                    raise HTTPException(status_code=400, detail=f"Batch {batch.batch_no} is expired and cannot be dispensed")

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


# ============================================================
# Transactions listing
# ============================================================
def _first_attr(model, *names: str) -> Optional[str]:
    for n in names:
        if hasattr(model, n):
            return n
    return None


# figure out your actual column/attribute names safely
_TXN_TIME_F = _first_attr(StockTransaction, "txn_time", "created_at", "created_on")
_TXN_TYPE_F = _first_attr(StockTransaction, "txn_type", "transaction_type", "type")
_QTY_F = _first_attr(StockTransaction, "quantity_change", "qty_change", "qty_delta", "quantity")
_REF_TYPE_F = _first_attr(StockTransaction, "ref_type", "reference_type")
_REF_ID_F = _first_attr(StockTransaction, "ref_id", "reference_id")
_UNIT_COST_F = _first_attr(StockTransaction, "unit_cost")
_MRP_F = _first_attr(StockTransaction, "mrp")
_USER_ID_F = _first_attr(StockTransaction, "user_id", "created_by", "created_user_id")
_PATIENT_F = _first_attr(StockTransaction, "patient_id")
_VISIT_F = _first_attr(StockTransaction, "visit_id")


def _get(obj, field: Optional[str], default=None):
    return getattr(obj, field, default) if field else default


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

    # filters (only if the model has those attributes)
    if location_id and hasattr(StockTransaction, "location_id"):
        q = q.filter(StockTransaction.location_id == location_id)

    if item_id and hasattr(StockTransaction, "item_id"):
        q = q.filter(StockTransaction.item_id == item_id)

    if txn_type and _TXN_TYPE_F:
        q = q.filter(getattr(StockTransaction, _TXN_TYPE_F) == txn_type)

    if from_date and _TXN_TIME_F:
        q = q.filter(getattr(StockTransaction, _TXN_TIME_F) >= from_date)

    if to_date and _TXN_TIME_F:
        q = q.filter(getattr(StockTransaction, _TXN_TIME_F) <= to_date)

    # order
    if _TXN_TIME_F:
        q = q.order_by(getattr(StockTransaction, _TXN_TIME_F).desc(), StockTransaction.id.desc())
    else:
        q = q.order_by(StockTransaction.id.desc())

    rows = q.all()

    result: List[StockTransactionOut] = []

    for tx in rows:
        tx_item_id = getattr(tx, "item_id", None)
        tx_batch_id = getattr(tx, "batch_id", None)
        tx_location_id = getattr(tx, "location_id", None)
        tx_user_id = _get(tx, _USER_ID_F, None)

        item = db.get(InventoryItem, tx_item_id) if tx_item_id else None
        batch = db.get(ItemBatch, tx_batch_id) if tx_batch_id else None
        location = db.get(InventoryLocation, tx_location_id) if tx_location_id else None
        usr = db.get(User, tx_user_id) if tx_user_id else None

        ref_type = _get(tx, _REF_TYPE_F, "")
        ref_id = _get(tx, _REF_ID_F, None)

        ref_display = None
        if ref_type == "GRN" and ref_id:
            grn = db.get(GRN, ref_id)
            if grn:
                ref_display = f"GRN {grn.grn_number}"

        result.append(
            StockTransactionOut(
                id=tx.id,
                txn_time=_get(tx, _TXN_TIME_F, None),
                location_id=tx_location_id,
                item_id=tx_item_id,
                batch_id=tx_batch_id,
                quantity_change=_get(tx, _QTY_F, None),
                txn_type=_get(tx, _TXN_TYPE_F, None),
                ref_type=ref_type,
                ref_id=ref_id,
                unit_cost=_get(tx, _UNIT_COST_F, None),
                mrp=_get(tx, _MRP_F, None),

                # ✅ THIS FIXES YOUR ERROR:
                patient_id=_get(tx, _PATIENT_F, None),
                visit_id=_get(tx, _VISIT_F, None),

                user_id=tx_user_id,

                item_name=item.name if item else None,
                item_code=item.code if item else None,
                batch_no=batch.batch_no if batch else None,
                location_name=location.name if location else None,
                user_name=user_display_name(usr) if usr else "System",
                ref_display=ref_display,
            )
        )

    return result


@router.get("/items/by-qr-number", response_model=ItemOut)
def get_item_by_qr_number(
    qr_number: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = db.query(InventoryItem).filter(InventoryItem.qr_number == qr_number).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item
