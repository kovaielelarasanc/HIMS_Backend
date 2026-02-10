# FILE: app/api/routes_inventory.py
from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO, BytesIO
from typing import Any, Dict, List, Optional, Set, Tuple, Literal
from pathlib import Path
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
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy.orm import Session, joinedload


from barcode import Code128
from barcode.writer import ImageWriter

from app.api.deps import get_db, current_user as auth_current_user
from app.models.ui_branding import UiBranding
from app.models.user import User
from app.models.pharmacy_inventory import (
    InventoryLocation,
    Supplier,
    InventoryItem,
    ItemBatch,
    ReturnNote,
    ReturnNoteItem,
    StockTransaction,
    GRN,  # used only for ref_display
)
from app.models.pharmacy_prescription import (
    PharmacyPrescription,
    PharmacyPrescriptionLine,
)
from app.services.drug_schedules import IN_DCA, US_CSA, get_schedule_meta
from app.schemas.common import ApiResponse
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
    PharmacyBatchPickOut,
)
from app.schemas.inventory_bulk_upload import (
    BulkUploadCommitOut,
    BulkUploadErrorOut,
    BulkUploadPreviewOut,
)
from app.services.inventory_bulk_upload import (
    TEMPLATE_HEADERS,
    REQUIRED_HEADERS,
    UploadError,
    parse_upload_to_rows,
    validate_item_rows,
    apply_items_import,
    make_csv_template_bytes,
    make_excel_template_bytes,
)
from app.services.inventory import (
    create_stock_transaction,
    allocate_batches_fefo,
    adjust_batch_qty,
)

# ✅ FIX: use pdf/ not pdfs/
from app.services.pdfs.inventory_transactions_pdf import build_stock_transactions_pdf
from app.services.pdfs.pharmacy_schedule_medicine_report_pdf import (
    build_schedule_medicine_report_pdf,
)
try:
    from app.services.drug_schedules import get_schedule_meta
except Exception:
    def get_schedule_meta(system: Optional[str], code: Optional[str]) -> dict:  # type: ignore
        return {"system": system or "", "code": (code or "").strip().upper()}


from app.utils.resp import ok, err

router = APIRouter(prefix="/inventory", tags=["Inventory - Pharmacy"])


# ------------------------------------------------------------
# ✅ FIX: SupplierPaymentMethod typing (your old code used Enum style)
# ------------------------------------------------------------
SupplierPaymentMethod = Literal["UPI", "BANK_TRANSFER", "CASH", "CHEQUE", "OTHER"]

# ============================================================
# ✅ Item payload normalization + friendly DB errors
# ============================================================

# columns that are NOT NULL in DB but frontend may send null
_ITEM_NON_NULL_STR_FIELDS = {
    "manufacturer",
    "storage_condition",
    "schedule_system",
    "schedule_code",
    "schedule_notes",
    "generic_name",
    "brand_name",
    "dosage_form",
    "strength",
    "route",
    "therapeutic_class",
    "prescription_status",
    "side_effects",
    "drug_interactions",
    "material_type",
    "sterility_status",
    "size_dimensions",
    "intended_use",
    "reusable_status",
    "atc_code",
    "hsn_code",
    "unit",
    "pack_size",
    "base_uom",
    "purchase_uom",
    "item_type",
    "name",
    "code",
}

_ITEM_NON_NULL_NUM_FIELDS = {
    "reorder_level",
    "max_level",
    "default_tax_percent",
    "default_price",
    "default_mrp",
    "conversion_factor",
}

# nullable columns
_ITEM_NULLABLE_FIELDS = {
    "qr_number",
    "default_supplier_id",
    "procurement_date",
    "active_ingredients",
}

def _normalize_item_data_for_db(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prevent NOT NULL column crashes when client sends null.
    - strings -> "" for non-null columns
    - numbers -> 0 (or 1 for conversion_factor) for non-null numeric columns
    - schedule_code: None -> ""
    - schedule_system: None -> "IN_DCA"
    """
    d = dict(data or {})

    # strings
    for k in list(d.keys()):
        if k in _ITEM_NON_NULL_STR_FIELDS and d[k] is None:
            # schedule_code is usually stored as "" when empty
            d[k] = ""

    # numbers
    for k in list(d.keys()):
        if k in _ITEM_NON_NULL_NUM_FIELDS and d[k] is None:
            d[k] = Decimal("1") if k == "conversion_factor" else Decimal("0")

    # schedule defaults
    if "schedule_system" in d and not (d.get("schedule_system") or "").strip():
        d["schedule_system"] = "IN_DCA"
    if "schedule_code" in d and d.get("schedule_code") is None:
        d["schedule_code"] = ""

    return d


def _integrity_error_message(e: IntegrityError) -> str:
    s = str(getattr(e, "orig", e)).lower()

    # duplicates
    if "uq_inv_items_code" in s or ("duplicate" in s and "code" in s):
        return "Item code already exists"
    if "uq_inv_items_qr_number" in s or ("duplicate" in s and ("qr_number" in s or "qr" in s)):
        return "QR/Barcode number already exists"

    # FK (adjust name if your FK constraint differs)
    if "fk_inv_items_default_supplier" in s or ("foreign key" in s and "default_supplier" in s):
        return "Invalid default_supplier_id (Supplier not found)"

    return "Database constraint error"


def _qty_on_hand_for_item(db: Session, item_id: int, location_id: Optional[int] = None) -> float:
    saleable = and_(
        ItemBatch.is_active.is_(True),
        ItemBatch.is_saleable.is_(True),
        ItemBatch.status == "ACTIVE",
        ItemBatch.item_id == item_id,
    )
    q = db.query(func.coalesce(func.sum(ItemBatch.current_qty), 0)).filter(saleable)
    if location_id:
        q = q.filter(ItemBatch.location_id == location_id)
    return float(q.scalar() or 0)


def _item_out_dict(db: Session, item: InventoryItem, *, location_id: Optional[int] = None) -> Dict[str, Any]:
    # supplier relationship name in your model is "supplier"
    sup = getattr(item, "supplier", None)

    base = ItemOut.model_validate(item).model_dump()
    base["qty_on_hand"] = _qty_on_hand_for_item(db, int(item.id), location_id=location_id)
    base["supplier"] = {"id": int(sup.id), "name": sup.name} if sup else None
    return base


def _validate_payment_block(
    method: SupplierPaymentMethod,
    upi_id: str,
    bank_account_name: str,
    bank_account_number: str,
    bank_ifsc: str,
):
    """
    Hard requirement:
    - UPI -> upi_id required
    - BANK_TRANSFER -> account_name + account_number + ifsc required
    """
    m = (method or "UPI").strip().upper()

    if m == "UPI":
        if not (upi_id or "").strip():
            raise HTTPException(status_code=422, detail="UPI ID is required for UPI payment method")

    if m == "BANK_TRANSFER":
        if not (bank_account_name or "").strip():
            raise HTTPException(status_code=422, detail="Bank account name is required for Bank Transfer")
        if not (bank_account_number or "").strip():
            raise HTTPException(status_code=422, detail="Bank account number is required for Bank Transfer")
        if not (bank_ifsc or "").strip():
            raise HTTPException(status_code=422, detail="Bank IFSC is required for Bank Transfer")


# ============================================================
# Branding
# ============================================================
def _branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).order_by(UiBranding.id.asc()).first()


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


def user_display_name(u: Optional[User]) -> str:
    """Single source of truth (avoid duplicates in file)."""
    if not u:
        return "System"
    try:
        v = getattr(u, "full_name", None)
        if v:
            return str(v)
    except Exception:
        pass
    v = getattr(u, "name", None)
    if v:
        return str(v)
    v = getattr(u, "email", None)
    if v:
        return str(v)
    return f"User #{getattr(u, 'id', 'unknown')}"


def smart_title(s: Any) -> str:
    """Lightweight title-casing without breaking None/empty."""
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    parts = []
    for w in s.split():
        if len(w) <= 6 and w.isupper():
            parts.append(w)
        else:
            parts.append(w[:1].upper() + w[1:].lower() if w else "")
    return " ".join(parts)


def _d(x: Any) -> Decimal:
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
    if not getattr(item, "qr_number", None):
        item.qr_number = f"MD_{item.id:04d}"
        db.add(item)
        db.commit()
        db.refresh(item)


# ============================================================
# ✅ SCHEDULE (H/H1/X...) helpers
# ============================================================
SCHEDULE_RE = re.compile(r"^[A-Z0-9]{1,6}$")


def _norm_schedule_code(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    s = s.replace("SCHEDULE", "")
    s = s.replace(" ", "").replace("-", "").replace("_", "")
    if not s:
        return None
    # allow RX/OTC
    if s in ("RX", "OTC"):
        return s
    if not SCHEDULE_RE.match(s):
        raise HTTPException(
            status_code=422,
            detail="Invalid schedule_code. Use H / H1 / X (or RX/OTC).",
        )
    return s


def _apply_schedule_sync(
    item,
    *,
    schedule_system=None,
    schedule_code=None,
    prescription_status=None,
    # ✅ accept BOTH naming styles (prevents TypeError)
    system_provided: bool | None = None,
    schedule_system_provided: bool | None = None,
    schedule_provided: bool | None = None,
    ps_provided: bool | None = None,
    **_ignored,  # ✅ swallow any unexpected kwargs safely
) -> None:
    """
    Keeps schedule fields consistent.

    Rules:
    - If schedule_code has value -> prescription_status forced to "SCHEDULED"
    - If schedule_code empty -> prescription_status cannot be "SCHEDULED"
    - Update only changes fields when corresponding *_provided flags are True
    """

    def _clean_code(v) -> str:
        s = ("" if v is None else str(v)).strip().upper()
        if not s:
            return ""
        # normalize: "Schedule H1" / "SCHEDULE-H1" / "H-1" -> "H1"
        s = s.replace("SCHEDULE", "").replace(" ", "").replace("-", "")
        # "USII" -> keep "II" at PDF layer; DB can store "II" too
        if s.startswith("US"):
            s = s[2:]
        return s

    def _clean_ps(v) -> str:
        s = ("" if v is None else str(v)).strip().upper()
        if s in ("SCHEDULE", "SCHEDULED"):
            return "SCHEDULED"
        if s in ("RX", "OTC"):
            return s
        return "RX"

    # ✅ derive provided flags if caller didn’t pass
    sys_prov = bool(
        (system_provided if system_provided is not None else False)
        or (schedule_system_provided if schedule_system_provided is not None else False)
    )
    if schedule_provided is None:
        schedule_provided = schedule_code is not None
    if ps_provided is None:
        ps_provided = prescription_status is not None

    # only for drugs
    it = (str(getattr(item, "item_type", "") or "").strip().upper() or "DRUG")
    is_drug = it in ("DRUG", "MEDICINE")

    # ---------- schedule_system ----------
    if is_drug:
        if sys_prov:
            sysv = (schedule_system or "IN_DCA")
        else:
            sysv = (getattr(item, "schedule_system", None) or "IN_DCA")

        sysv = str(sysv).strip().upper()
        item.schedule_system = sysv if sysv in ("IN_DCA", "US_CSA") else "IN_DCA"
    else:
        # for non-drug items force safe defaults
        item.schedule_system = "IN_DCA"

    # ---------- schedule_code ----------
    if is_drug:
        if schedule_provided:
            sc = _clean_code(schedule_code)
            item.schedule_code = sc  # allow "" (clear)
        else:
            sc = _clean_code(getattr(item, "schedule_code", "") or "")
            item.schedule_code = sc
    else:
        item.schedule_code = ""
        sc = ""

    # ---------- prescription_status ----------
    if ps_provided:
        ps = _clean_ps(prescription_status)
    else:
        ps = _clean_ps(getattr(item, "prescription_status", None) or "RX")

    if is_drug and sc:
        # ✅ any schedule_code -> scheduled
        item.prescription_status = "SCHEDULED"
    else:
        # no schedule_code -> cannot be scheduled
        if ps == "SCHEDULED":
            raise HTTPException(
                status_code=422,
                detail="schedule_code is required when prescription_status is SCHEDULED",
            )
        item.prescription_status = ps


# ============================================================
# Locations
# ============================================================
# -------------------------
# Locations (Masters)
# -------------------------
def _perm_or_403(user: User, perm: str) -> None:
    if not has_perm(user, perm):
        raise HTTPException(status_code=403, detail="Not enough permissions")

def _need_any(user: User, perms: list[str]) -> None:
    if any(has_perm(user, p) for p in (perms or [])):
        return
    raise HTTPException(status_code=403, detail="Not enough permissions")
    
@router.get("/locations", response_model=List[LocationOut])
def list_locations(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    pharmacy_only: bool = Query(True, description="Return only pharmacy-enabled locations"),
    active_only: bool = Query(True, description="Return only active locations"),
    q: Optional[str] = Query(None, description="Search by code/name"),
):
    _need_any(current_user, [
        "pharmacy.inventory.locations.view",
        "inventory.locations.view",
        "inventory.catalog.view",
        "inventory.view",
    ])

    qry = db.query(InventoryLocation)

    if pharmacy_only:
        qry = qry.filter(InventoryLocation.is_pharmacy.is_(True))

    if active_only:
        qry = qry.filter(InventoryLocation.is_active.is_(True))

    if q and q.strip():
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                InventoryLocation.code.ilike(like),
                InventoryLocation.name.ilike(like),
            )
        )

    return qry.order_by(InventoryLocation.name.asc()).all()


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
@router.get("/suppliers")
def list_suppliers(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.view"):
        return err("Not enough permissions", status_code=403)

    query = db.query(Supplier)

    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Supplier.name.ilike(like),
                Supplier.code.ilike(like),
                Supplier.phone.ilike(like),
                Supplier.email.ilike(like),
                Supplier.gstin.ilike(like),
                Supplier.address.ilike(like),
                Supplier.contact_person.ilike(like),
                Supplier.upi_id.ilike(like),
                Supplier.bank_ifsc.ilike(like),
                Supplier.bank_account_number.ilike(like),
                Supplier.bank_name.ilike(like),
            )
        )

    rows = query.order_by(Supplier.name).all()
    return ok([SupplierOut.model_validate(r).model_dump() for r in rows])


@router.post("/suppliers")
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.manage"):
        return err("Not enough permissions", status_code=403)

    exists = db.query(Supplier).filter(Supplier.code == payload.code).first()
    if exists:
        return err("Supplier code already exists", status_code=400)

    sup = Supplier(**payload.model_dump())
    db.add(sup)
    db.commit()
    db.refresh(sup)
    return ok(SupplierOut.model_validate(sup).model_dump(), status_code=201)


@router.put("/suppliers/{sup_id}")
def update_supplier(
    sup_id: int,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.suppliers.manage"):
        return err("Not enough permissions", status_code=403)

    sup = db.get(Supplier, sup_id)
    if not sup:
        return err("Supplier not found", status_code=404)

    data = payload.model_dump(exclude_unset=True)

    pm = (data.get("payment_method") or sup.payment_method or "UPI").strip().upper()

    if pm == "UPI":
        upi = data.get("upi_id", sup.upi_id)
        if not upi:
            return err("UPI ID is required when payment_method is UPI", 400)

    if pm == "BANK_TRANSFER":
        acc_name = data.get("bank_account_name", sup.bank_account_name)
        acc_no = data.get("bank_account_number", sup.bank_account_number)
        ifsc = data.get("bank_ifsc", sup.bank_ifsc)
        if not acc_name or not acc_no or not ifsc:
            return err("Bank transfer requires account name, number and IFSC", 400)

    for k, v in data.items():
        setattr(sup, k, v)

    db.commit()
    db.refresh(sup)
    return ok(SupplierOut.model_validate(sup).model_dump())


# ============================================================
# Items + Barcode + Bulk Upload
# ============================================================
def _apply_type_sync(item: InventoryItem) -> None:
    t = (getattr(item, "item_type", None) or "DRUG").upper().strip()
    if t not in ("DRUG", "CONSUMABLE", "EQUIPMENT"):
        t = "DRUG"
    item.item_type = t
    item.is_consumable = (t == "CONSUMABLE")


@router.get("/items", response_model=ApiResponse[List[ItemOut]])
def list_items(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    q: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    type_: Optional[str] = Query(
        None,
        alias="type",
        pattern="^(drug|consumable|all)$",
        description="drug -> item_type DRUG, consumable -> item_type CONSUMABLE, all -> no type filter",
    ),
    lasa: Optional[bool] = Query(None, description="Filter by LASA flag"),
    supplier_id: Optional[int] = Query(None),
    include_qty: bool = Query(True, description="Include computed quantity on hand"),
    location_id: Optional[int] = Query(None, description="If set, qty_on_hand for that location only"),
    limit: int = Query(5000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    if not any(
        has_perm(current_user, p)
        for p in [
            "pharmacy.inventory.items.view",
            "inventory.items.view",
            "inventory.catalog.view",
            "inventory.view",
        ]
    ):
        return err("Not enough permissions", status_code=403)

    item = InventoryItem

    qry = (
        db.query(item)
        .options(joinedload(item.supplier))  # ✅ no N+1 supplier
    )

    if q and q.strip():
        like = f"%{q.strip()}%"
        qry = qry.filter(
            (item.name.ilike(like))
            | (item.code.ilike(like))
            | (item.generic_name.ilike(like))
            | (item.brand_name.ilike(like))
            | (item.qr_number.ilike(like))
        )

    if is_active is not None:
        qry = qry.filter(item.is_active == is_active)

    if type_ == "drug":
        qry = qry.filter(item.item_type == "DRUG")
    elif type_ == "consumable":
        qry = qry.filter(item.item_type == "CONSUMABLE")

    if lasa is not None:
        qry = qry.filter(item.lasa_flag == lasa)

    if supplier_id:
        qry = qry.filter(item.default_supplier_id == supplier_id)

    rows = qry.order_by(item.name.asc()).offset(offset).limit(limit).all()

    # ✅ qty map in one query (only if include_qty)
    qty_map: Dict[int, float] = {}
    if include_qty and rows:
        saleable = and_(
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == "ACTIVE",
        )
        bq = (
            db.query(
                ItemBatch.item_id.label("item_id"),
                func.coalesce(func.sum(ItemBatch.current_qty), 0).label("qty"),
            )
            .filter(saleable, ItemBatch.item_id.in_([r.id for r in rows]))
        )
        if location_id:
            bq = bq.filter(ItemBatch.location_id == location_id)
        bq = bq.group_by(ItemBatch.item_id)
        qty_map = {int(r.item_id): float(r.qty or 0) for r in bq.all()}

    out: List[Dict[str, Any]] = []
    for it in rows:
        sup = getattr(it, "supplier", None)
        base = ItemOut.model_validate(it).model_dump()
        base["qty_on_hand"] = float(qty_map.get(int(it.id), 0))
        base["supplier"] = {"id": int(sup.id), "name": sup.name} if sup else None
        out.append(base)

    return ok(out)



@router.get("/item-batches", response_model=List[PharmacyBatchPickOut])
def search_item_batches_for_billing(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: int = Query(..., description="Dispense/Billing location (pharmacy store)"),
    q: Optional[str] = Query(None, description="Search medicine name/code/generic/batch"),
    only_in_stock: bool = Query(True, description="Show only batches with available stock"),
    exclude_expired: bool = Query(True, description="Exclude expired batches"),
    active_only: bool = Query(True, description="Only ACTIVE + saleable batches"),
    type_: Optional[str] = Query(
        "drug",
        alias="type",
        pattern="^(drug|consumable|all)$",
        description="drug -> is_consumable False, consumable -> True, all -> no filter",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    if not has_perm(current_user, "pharmacy.inventory.items.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        today = date.today()

        qry = (
            db.query(
                ItemBatch.id.label("batch_id"),
                InventoryItem.id.label("item_id"),
                InventoryItem.code,
                InventoryItem.name,
                InventoryItem.generic_name,
                InventoryItem.dosage_form.label("form"),
                InventoryItem.strength,
                InventoryItem.unit,
                ItemBatch.batch_no,
                ItemBatch.expiry_date,
                ItemBatch.current_qty.label("available_qty"),
                ItemBatch.unit_cost,
                ItemBatch.mrp,
                ItemBatch.tax_percent,
                ItemBatch.location_id,
                InventoryLocation.name.label("location_name"),
            )
            .join(InventoryItem, InventoryItem.id == ItemBatch.item_id)
            .join(InventoryLocation, InventoryLocation.id == ItemBatch.location_id)
            .filter(ItemBatch.location_id == location_id)
        )

        if q and q.strip():
            like = f"%{q.strip()}%"
            qry = qry.filter(
                (InventoryItem.name.ilike(like))
                | (InventoryItem.code.ilike(like))
                | (InventoryItem.generic_name.ilike(like))
                | (ItemBatch.batch_no.ilike(like))
            )

        if active_only:
            qry = qry.filter(
                InventoryItem.is_active.is_(True),
                ItemBatch.is_active.is_(True),
                ItemBatch.is_saleable.is_(True),
                ItemBatch.status == "ACTIVE",
            )

        if type_ == "drug":
            qry = qry.filter(InventoryItem.is_consumable.is_(False))
        elif type_ == "consumable":
            qry = qry.filter(InventoryItem.is_consumable.is_(True))

        if only_in_stock:
            qry = qry.filter(ItemBatch.current_qty > 0)

        if exclude_expired:
            qry = qry.filter(or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today))

        qry = (
            qry.order_by(
                InventoryItem.name.asc(),
                ItemBatch.expiry_date.is_(None),
                ItemBatch.expiry_date.asc(),
                ItemBatch.batch_no.asc(),
            )
            .limit(limit)
        )

        rows = qry.all()
        return [dict(r._mapping) for r in rows]

    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error while searching item batches.") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="Unexpected error while searching item batches.") from e


@router.get("/batches", response_model=List[ItemBatchOut])
def list_batches(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: int = Query(...),
    item_id: Optional[int] = Query(None),
    only_available: bool = Query(True),
):
    _need_any(current_user, [
        "pharmacy.inventory.stock.view",
        "inventory.batches.view",
        "inventory.stock.view",
        "inventory.view",
    ])

    q = db.query(ItemBatch).filter(ItemBatch.location_id == location_id)
    if item_id:
        q = q.filter(ItemBatch.item_id == item_id)
    if only_available:
        q = q.filter(
            ItemBatch.current_qty > 0,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
        )

    q = q.order_by(
        case((ItemBatch.expiry_date.is_(None), 1), else_=0),
        ItemBatch.expiry_date.asc(),
        ItemBatch.id.asc(),
    )

    return q.limit(1000).all()


# ✅ CREATE ITEM
@router.post("/items", response_model=ApiResponse[ItemOut])
def create_item(
    payload: ItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        return err("Not enough permissions", status_code=403)

    # ✅ only "actually provided" fields (important!)
    provided = set(getattr(payload, "model_fields_set", set()) or set())

    # basic uniqueness pre-check (fast message)
    if db.query(InventoryItem.id).filter(InventoryItem.code == payload.code).first():
        return err("Item code already exists", status_code=400)

    data = _normalize_item_data_for_db(payload.model_dump())

    data["name"] = smart_title(data.get("name", ""))
    data["generic_name"] = smart_title(data.get("generic_name", ""))
    data["brand_name"] = smart_title(data.get("brand_name", ""))

    # ✅ validate supplier id if provided
    if data.get("default_supplier_id"):
        sup = db.get(Supplier, int(data["default_supplier_id"]))
        if not sup:
            return err("Invalid default_supplier_id (Supplier not found)", status_code=400)

    try:
        item = InventoryItem(**data)

        _apply_type_sync(item)

        # ✅ schedule sync (only overwrite if field was truly provided by client)
        _apply_schedule_sync(
            item,
            schedule_system=getattr(payload, "schedule_system", None),
            schedule_code=getattr(payload, "schedule_code", None),
            prescription_status=getattr(payload, "prescription_status", None),
            system_provided=("schedule_system" in provided),
            schedule_provided=("schedule_code" in provided),
            ps_provided=("prescription_status" in provided),
        )

        # ✅ high alert rule (optional)
        if getattr(item, "high_alert_flag", False) and not getattr(item, "requires_double_check", False):
            item.requires_double_check = True

        db.add(item)
        db.flush()

        if not item.qr_number:
            item.qr_number = f"MED-{item.id:06d}"

        db.commit()
        db.refresh(item)

        return ok(_item_out_dict(db, item))

    except IntegrityError as e:
        db.rollback()
        return err(_integrity_error_message(e), status_code=400)
    except SQLAlchemyError:
        db.rollback()
        return err("Database error while creating item", status_code=500)



# ✅ UPDATE ITEM (this is what your PUT is hitting)
@router.put("/items/{item_id}", response_model=ApiResponse[ItemOut])
def update_item(
    item_id: int,
    payload: ItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        return err("Not enough permissions", status_code=403)

    item = db.get(InventoryItem, item_id)
    if not item:
        return err("Item not found", status_code=404)

    data = payload.model_dump(exclude_unset=True)
    data = _normalize_item_data_for_db(data)

    # ✅ uniqueness checks
    if "code" in data and data["code"]:
        exists = (
            db.query(InventoryItem.id)
            .filter(InventoryItem.code == data["code"], InventoryItem.id != item_id)
            .first()
        )
        if exists:
            return err("Item code already exists", status_code=400)

    if "qr_number" in data and data["qr_number"]:
        exists = (
            db.query(InventoryItem.id)
            .filter(InventoryItem.qr_number == data["qr_number"], InventoryItem.id != item_id)
            .first()
        )
        if exists:
            return err("QR/Barcode number already exists", status_code=400)

    # ✅ validate supplier id if provided
    if "default_supplier_id" in data and data["default_supplier_id"]:
        sup = db.get(Supplier, int(data["default_supplier_id"]))
        if not sup:
            return err("Invalid default_supplier_id (Supplier not found)", status_code=400)

    # title-case only when provided
    if "name" in data:
        data["name"] = smart_title(data.get("name", ""))
    if "generic_name" in data:
        data["generic_name"] = smart_title(data.get("generic_name", ""))
    if "brand_name" in data:
        data["brand_name"] = smart_title(data.get("brand_name", ""))

    try:
        # apply changes
        for k, v in data.items():
            setattr(item, k, v)

        _apply_type_sync(item)

        # ✅ schedule sync ONLY for provided fields (data contains only provided fields)
        _apply_schedule_sync(
            item,
            schedule_system=data.get("schedule_system") if "schedule_system" in data else None,
            schedule_code=data.get("schedule_code") if "schedule_code" in data else None,
            prescription_status=data.get("prescription_status") if "prescription_status" in data else None,
            system_provided=("schedule_system" in data),
            schedule_provided=("schedule_code" in data),
            ps_provided=("prescription_status" in data),
        )

        if getattr(item, "high_alert_flag", False) and not getattr(item, "requires_double_check", False):
            item.requires_double_check = True

        db.commit()
        db.refresh(item)

        return ok(_item_out_dict(db, item))

    except IntegrityError as e:
        db.rollback()
        return err(_integrity_error_message(e), status_code=400)
    except SQLAlchemyError:
        db.rollback()
        return err("Database error while updating item", status_code=500)


@router.get("/items/drug-schedules")
def get_drug_schedules_catalog(current_user: User = Depends(auth_current_user)):
    # optional: protect this endpoint
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        return {"IN_DCA": [], "US_CSA": []}

    def _to_list(src: dict, system: str):
        out = []
        # stable ordering: sort by code length then code (so C1 after C etc)
        for code in sorted(src.keys(), key=lambda x: (len(x), x)):
            meta = get_schedule_meta(system, code)
            out.append(meta)
        return out

    return {
        "IN_DCA": _to_list(IN_DCA, "IN_DCA"),
        "US_CSA": _to_list(US_CSA, "US_CSA"),
    }
    

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
    w = csv.writer(output)
    w.writerow(TEMPLATE_HEADERS)

    # sample DRUG row
    w.writerow([
        "AMOX500",          # code
        "",                 # qr_number
        "Amoxicillin 500",  # name

        "DRUG",             # item_type
        "FALSE",            # is_consumable (legacy)

        "FALSE",            # lasa_flag
        "TRUE",             # is_active

        "tablet",           # unit
        "10",               # pack_size
        "50",               # reorder_level
        "500",              # max_level

        "ACME Pharma",      # manufacturer
        "",                 # default_supplier_id
        "",                 # procurement_date

        "ROOM_TEMP",        # storage_condition

        "12",               # default_tax_percent
        "3.50",             # default_price
        "5.00",             # default_mrp

        "IN_DCA",           # schedule_system
        "H",                # schedule_code
        "",                 # schedule_notes

        "Amoxicillin",      # generic_name
        "",                 # brand_name
        "tablet",           # dosage_form
        "500 mg",           # strength
        "Amoxicillin",      # active_ingredients
        "oral",             # route
        "Antibiotic",       # therapeutic_class
        "RX",               # prescription_status
        "",                 # side_effects
        "",                 # drug_interactions

        "",                 # material_type
        "",                 # sterility_status
        "",                 # size_dimensions
        "",                 # intended_use
        "",                 # reusable_status

        "J01CA04",          # atc_code
        "3004",             # hsn_code
    ])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="pharmacy_items_sample.csv"'},
    )



# ============================================================
# TEMPLATE DOWNLOAD (CSV / XLSX / XLSM)
# ============================================================
@router.get("/items/bulk-upload/template")
def download_items_template(
    format: str = Query("xlsx", pattern="^(csv|xlsx|xlsm)$"),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.inventory.items.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    if format == "csv":
        data = make_csv_template_bytes()
        return StreamingResponse(
            BytesIO(data),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="items_template.csv"'},
        )

    # XLSM support (real xlsm only if base template exists)
    base_xlsm = Path("app/static/templates/items_template_base.xlsm")  # put your macro template here if you have
    macro_enabled = (format == "xlsm")
    try:
        content, ext = make_excel_template_bytes(macro_enabled=macro_enabled, base_xlsm_path=base_xlsm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if macro_enabled and ext != "xlsm":
        # No base_xlsm provided -> fallback to xlsx to avoid corrupted downloads
        ext = "xlsx"

    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext == "xlsm":
        media = "application/vnd.ms-excel.sheet.macroEnabled.12"

    return StreamingResponse(
        BytesIO(content),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="items_template.{ext}"'},
    )



# ============================================================
# PREVIEW
# ============================================================
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

    # compute per-row validity
    err_rows = {e.row for e in errs if e.row and e.row > 1}
    valid_rows = max(0, len(normalized) - len(err_rows))

    return BulkUploadPreviewOut(
        file_type=file_type,
        total_rows=len(rows),
        valid_rows=valid_rows,
        error_rows=len(err_rows),
        required_columns=REQUIRED_HEADERS,
        optional_columns=[c for c in TEMPLATE_HEADERS if c not in REQUIRED_HEADERS],
        sample_rows=normalized[:20],
        errors=[BulkUploadErrorOut(row=e.row, code=e.code, column=e.column, message=e.message) for e in errs],
    )


# ============================================================
# COMMIT
# ============================================================
@router.post("/items/bulk-upload/commit", response_model=BulkUploadCommitOut)
def commit_items_upload(
    file: UploadFile = File(...),
    update_blanks: bool = Query(False, description="If true, blank cells overwrite existing values"),
    strict: bool = Query(True, description="If true, any error stops commit"),
    create_missing_locations: bool = Query(True, description="Auto-create missing opening stock locations"),
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

    created, updated, skipped, db_errs = apply_items_import(
        db,
        normalized,
        update_blanks=update_blanks,
        create_missing_locations=create_missing_locations,
        user_id=getattr(current_user, "id", None),
    )

    out_errs = errs + db_errs
    return BulkUploadCommitOut(
        created=created,
        updated=updated,
        skipped=skipped,
        errors=[BulkUploadErrorOut(row=e.row, code=e.code, column=e.column, message=e.message) for e in out_errs],
    )

# --- rest of your file continues unchanged ---
# (Stock summary, alerts, returns, dispense, transactions, pdf, schedule report, get_item_by_qr_number)

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
    if not any(
        has_perm(current_user, p)
        for p in [
            "pharmacy.inventory.stock.view",
            "inventory.stock.view",
            "inventory.view",
        ]
    ):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    item = InventoryItem
    batch = ItemBatch
    loc = InventoryLocation

    saleable_condition = and_(
        batch.is_saleable == True,  # noqa: E712
        batch.status == "ACTIVE",
    )

    total_qty_expr = func.coalesce(
        func.sum(case((saleable_condition, batch.current_qty), else_=0)),
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

    if q and q.strip():
        like = f"%{q.strip()}%"
        j = j.filter((item.name.ilike(like)) | (item.code.ilike(like)) | (item.generic_name.ilike(like)))

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
        days = getattr(loc, "expiry_alert_days", None) or within_days
    else:
        days = within_days

    limit_dt = today + timedelta(days=days)

    q = (
        db.query(ItemBatch)
        .join(ItemBatch.item)
        .join(ItemBatch.location)
        .filter(
            ItemBatch.current_qty > 0,
            ItemBatch.expiry_date.isnot(None),
            ItemBatch.expiry_date >= today,
            ItemBatch.expiry_date <= limit_dt,
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

    return q.order_by(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date.asc()).all()


@router.get("/alerts/low-stock", response_model=List[StockSummaryOut])
def low_stock_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(db=db, current_user=current_user, location_id=location_id, q=None)
    return [s for s in all_stock if s.is_low]


@router.get("/alerts/max-stock", response_model=List[StockSummaryOut])
def max_stock_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
):
    if not has_perm(current_user, "pharmacy.inventory.alerts.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    all_stock = stock_summary(db=db, current_user=current_user, location_id=location_id, q=None)
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
            # doctor_id handled inside service if user.is_doctor
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

        qty = Decimal(str(line.quantity))

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
                remark=getattr(payload, "remark", "") or "",
                patient_id=getattr(payload, "patient_id", None),
                visit_id=getattr(payload, "visit_id", None),
                doctor_id=getattr(payload, "doctor_id", None),  # safe
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
                    remark=getattr(payload, "remark", "") or "",
                    patient_id=getattr(payload, "patient_id", None),
                    visit_id=getattr(payload, "visit_id", None),
                    doctor_id=getattr(payload, "doctor_id", None),  # safe
                )

    db.commit()
    return {"status": "ok"}


# ============================================================
# Transactions listing (✅ fixed doctor + RX display, no N+1)
# ============================================================
@router.get("/transactions", response_model=List[StockTransactionOut])
def list_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    location_id: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    txn_type: Optional[str] = Query(None),
    ref_type: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    if not has_perm(current_user, "pharmacy.inventory.txns.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        q = db.query(StockTransaction)

        if location_id:
            q = q.filter(StockTransaction.location_id == location_id)
        if item_id:
            q = q.filter(StockTransaction.item_id == item_id)
        if txn_type:
            q = q.filter(StockTransaction.txn_type == txn_type)
        if ref_type:
            q = q.filter(StockTransaction.ref_type == ref_type)
        if from_date:
            q = q.filter(StockTransaction.txn_time >= from_date)
        if to_date:
            q = q.filter(StockTransaction.txn_time <= to_date)

        rows = (
            q.order_by(StockTransaction.txn_time.desc(), StockTransaction.id.desc())
            .limit(limit)
            .all()
        )

        item_ids = {r.item_id for r in rows if r.item_id}
        batch_ids = {r.batch_id for r in rows if r.batch_id}
        loc_ids = {r.location_id for r in rows if r.location_id}
        user_ids = {r.user_id for r in rows if getattr(r, "user_id", None)}
        doctor_ids = {int(r.doctor_id) for r in rows if getattr(r, "doctor_id", None)}

        items = {i.id: i for i in db.query(InventoryItem).filter(InventoryItem.id.in_(item_ids)).all()} if item_ids else {}
        batches = {b.id: b for b in db.query(ItemBatch).filter(ItemBatch.id.in_(batch_ids)).all()} if batch_ids else {}
        locs = {l.id: l for l in db.query(InventoryLocation).filter(InventoryLocation.id.in_(loc_ids)).all()} if loc_ids else {}
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
        doctors = {u.id: u for u in db.query(User).filter(User.id.in_(doctor_ids)).all()} if doctor_ids else {}

        # ✅ ref maps
        grn_ids = {int(r.ref_id) for r in rows if (r.ref_type == "GRN" and r.ref_id)}
        rtn_ids = {int(r.ref_id) for r in rows if (r.ref_type == "RETURN" and r.ref_id)}

        grn_map = {}
        if grn_ids:
            grns = db.query(GRN).filter(GRN.id.in_(grn_ids)).all()
            for g in grns:
                grn_map[int(g.id)] = getattr(g, "grn_number", None) or f"GRN #{g.id}"

        rtn_map = {}
        if rtn_ids:
            rts = db.query(ReturnNote).filter(ReturnNote.id.in_(rtn_ids)).all()
            for rt in rts:
                rtn_map[int(rt.id)] = getattr(rt, "return_number", None) or f"RETURN #{rt.id}"

        # ✅ RX display map (ref_id = PharmacyPrescriptionLine.id)
        rx_line_ids = [int(r.ref_id) for r in rows if (r.ref_type == "PHARMACY_RX" and r.ref_id)]
        rx_map: Dict[int, str] = {}
        rx_doctor_map: Dict[int, Optional[int]] = {}  # line_id -> doctor_user_id
        if rx_line_ids:
            rx_lines = db.query(PharmacyPrescriptionLine).filter(PharmacyPrescriptionLine.id.in_(rx_line_ids)).all()
            rx_ids = list({int(ln.prescription_id) for ln in rx_lines if ln.prescription_id})
            rx_by_id: Dict[int, PharmacyPrescription] = {}
            if rx_ids:
                rxs = db.query(PharmacyPrescription).filter(PharmacyPrescription.id.in_(rx_ids)).all()
                rx_by_id = {int(r.id): r for r in rxs}

                rx_doc_ids = {int(r.doctor_user_id) for r in rxs if getattr(r, "doctor_user_id", None)}
                if rx_doc_ids:
                    rx_docs = db.query(User).filter(User.id.in_(rx_doc_ids)).all()
                    for d in rx_docs:
                        doctors[int(d.id)] = d

            for ln in rx_lines:
                rx = rx_by_id.get(int(ln.prescription_id)) if ln.prescription_id else None
                rx_no = (
                    getattr(rx, "rx_number", None)
                    or getattr(rx, "prescription_number", None)
                    or (f"RX #{ln.prescription_id}" if ln.prescription_id else f"RX LINE #{ln.id}")
                )
                rx_map[int(ln.id)] = str(rx_no)
                rx_doctor_map[int(ln.id)] = int(rx.doctor_user_id) if (rx and getattr(rx, "doctor_user_id", None)) else None

        out: List[StockTransactionOut] = []
        for tx in rows:
            it = items.get(tx.item_id) if tx.item_id else None
            bt = batches.get(tx.batch_id) if tx.batch_id else None
            lc = locs.get(tx.location_id) if tx.location_id else None
            usr = users.get(tx.user_id) if getattr(tx, "user_id", None) else None

            rtype = (tx.ref_type or "").strip()
            rid = tx.ref_id

            ref_display = None
            if rtype == "GRN" and rid:
                ref_display = grn_map.get(int(rid)) or f"GRN #{rid}"
            elif rtype == "RETURN" and rid:
                ref_display = rtn_map.get(int(rid)) or f"RETURN #{rid}"
            elif rtype == "PHARMACY_RX" and rid:
                ref_display = rx_map.get(int(rid)) or f"RX LINE #{rid}"
            elif rtype and rid:
                ref_display = f"{rtype} #{rid}"

            # ✅ doctor name ONLY from users table (3-level fallback)
            doctor_name = ""
            doc_id = getattr(tx, "doctor_id", None)
            if doc_id:
                doctor_name = user_display_name(doctors.get(int(doc_id)) or users.get(int(doc_id)))
            if (not doctor_name) and (rtype == "PHARMACY_RX" and rid):
                did = rx_doctor_map.get(int(rid))
                if did:
                    doctor_name = user_display_name(doctors.get(int(did)) or users.get(int(did)))
            if (not doctor_name) and usr and bool(getattr(usr, "is_doctor", False)):
                doctor_name = user_display_name(usr)

            out.append(
                StockTransactionOut(
                    id=tx.id,
                    txn_time=tx.txn_time,
                    location_id=tx.location_id,
                    item_id=tx.item_id,
                    batch_id=tx.batch_id,
                    quantity_change=tx.quantity_change,
                    txn_type=tx.txn_type,
                    ref_type=rtype,
                    ref_id=rid,
                    unit_cost=getattr(tx, "unit_cost", None),
                    mrp=getattr(tx, "mrp", None),
                    patient_id=getattr(tx, "patient_id", None),
                    visit_id=getattr(tx, "visit_id", None),
                    user_id=getattr(tx, "user_id", None),
                    doctor_id=getattr(tx, "doctor_id", None),
                    item_name=getattr(it, "name", None) if it else None,
                    item_code=getattr(it, "code", None) if it else None,
                    batch_no=getattr(bt, "batch_no", None) if bt else None,
                    location_name=getattr(lc, "name", None) if lc else None,
                    user_name=user_display_name(usr),
                    doctor_name=doctor_name or None,
                    ref_display=ref_display,
                )
            )

        return out

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail="Database error") from e


# ============================================================
# Transactions PDF (✅ doctor fixed, RX display fixed, filters no dots)
# ============================================================
@router.get("/transactions/pdf")
def transactions_pdf(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    ids: Optional[str] = Query(None, description="Comma separated StockTransaction ids"),
    location_id: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    txn_type: Optional[str] = Query(None),
    ref_type: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
):
    if not has_perm(current_user, "pharmacy.inventory.txns.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        q = db.query(StockTransaction)

        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
            if id_list:
                q = q.filter(StockTransaction.id.in_(id_list))

        if location_id:
            q = q.filter(StockTransaction.location_id == location_id)
        if item_id:
            q = q.filter(StockTransaction.item_id == item_id)
        if txn_type:
            q = q.filter(StockTransaction.txn_type == txn_type)
        if ref_type:
            q = q.filter(StockTransaction.ref_type == ref_type)
        if from_date:
            q = q.filter(StockTransaction.txn_time >= from_date)
        if to_date:
            q = q.filter(StockTransaction.txn_time <= to_date)

        rows = (
            q.order_by(StockTransaction.txn_time.desc(), StockTransaction.id.desc())
            .limit(limit)
            .all()
        )

        item_ids = {r.item_id for r in rows if r.item_id}
        batch_ids = {r.batch_id for r in rows if r.batch_id}
        loc_ids = {r.location_id for r in rows if r.location_id}
        user_ids = {r.user_id for r in rows if getattr(r, "user_id", None)}
        doctor_ids = {int(r.doctor_id) for r in rows if getattr(r, "doctor_id", None)}

        items = {i.id: i for i in db.query(InventoryItem).filter(InventoryItem.id.in_(item_ids)).all()} if item_ids else {}
        batches = {b.id: b for b in db.query(ItemBatch).filter(ItemBatch.id.in_(batch_ids)).all()} if batch_ids else {}
        locs = {l.id: l for l in db.query(InventoryLocation).filter(InventoryLocation.id.in_(loc_ids)).all()} if loc_ids else {}
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
        doctors = {u.id: u for u in db.query(User).filter(User.id.in_(doctor_ids)).all()} if doctor_ids else {}

        # RX display map (ref_id = PharmacyPrescriptionLine.id)
        rx_line_ids = [int(r.ref_id) for r in rows if (r.ref_type == "PHARMACY_RX" and r.ref_id)]
        rx_map: Dict[int, str] = {}
        rx_doctor_map: Dict[int, Optional[int]] = {}

        if rx_line_ids:
            rx_lines = db.query(PharmacyPrescriptionLine).filter(PharmacyPrescriptionLine.id.in_(rx_line_ids)).all()
            rx_ids = list({int(ln.prescription_id) for ln in rx_lines if ln.prescription_id})
            rx_by_id: Dict[int, PharmacyPrescription] = {}
            if rx_ids:
                rxs = db.query(PharmacyPrescription).filter(PharmacyPrescription.id.in_(rx_ids)).all()
                rx_by_id = {int(r.id): r for r in rxs}

                rx_doc_ids = {int(r.doctor_user_id) for r in rxs if getattr(r, "doctor_user_id", None)}
                if rx_doc_ids:
                    rx_docs = db.query(User).filter(User.id.in_(rx_doc_ids)).all()
                    for d in rx_docs:
                        doctors[int(d.id)] = d

            for ln in rx_lines:
                rx = rx_by_id.get(int(ln.prescription_id)) if ln.prescription_id else None
                rx_no = (
                    getattr(rx, "rx_number", None)
                    or getattr(rx, "prescription_number", None)
                    or (f"RX #{ln.prescription_id}" if ln.prescription_id else f"RX LINE #{ln.id}")
                )
                rx_map[int(ln.id)] = str(rx_no)
                rx_doctor_map[int(ln.id)] = int(rx.doctor_user_id) if (rx and getattr(rx, "doctor_user_id", None)) else None

        out_rows: List[Dict[str, Any]] = []
        for tx in rows:
            item = items.get(tx.item_id)
            batch = batches.get(tx.batch_id)
            location = locs.get(tx.location_id)
            usr = users.get(tx.user_id)

            rtype = (tx.ref_type or "").strip()
            rid = tx.ref_id

            ref_display = None
            if rtype == "PHARMACY_RX" and rid:
                ref_display = rx_map.get(int(rid)) or f"RX LINE #{rid}"
            elif rtype and rid:
                ref_display = f"{rtype} #{rid}"

            # doctor name only from users table (same 3-level fallback)
            doctor_name = ""
            if getattr(tx, "doctor_id", None):
                doctor_name = user_display_name(doctors.get(int(tx.doctor_id)) or users.get(int(tx.doctor_id)))
            if (not doctor_name) and (rtype == "PHARMACY_RX" and rid):
                did = rx_doctor_map.get(int(rid))
                if did:
                    doctor_name = user_display_name(doctors.get(int(did)) or users.get(int(did)))
            if (not doctor_name) and usr and bool(getattr(usr, "is_doctor", False)):
                doctor_name = user_display_name(usr)

            out_rows.append(
                dict(
                    id=tx.id,
                    txn_time=tx.txn_time,
                    txn_type=getattr(tx, "txn_type", "") or "",
                    ref_type=rtype,
                    ref_id=rid,
                    ref_display=ref_display,
                    item_name=getattr(item, "name", "") if item else "",
                    batch_no=getattr(batch, "batch_no", "") if batch else "",
                    location_name=getattr(location, "name", "") if location else "",
                    quantity_change=str(getattr(tx, "quantity_change", "") or ""),
                    mrp=str(getattr(tx, "mrp", "") or ""),
                    user_name=user_display_name(usr),
                    doctor_name=doctor_name,
                )
            )

        b = _branding(db)

        # ✅ remove dots, use pipes
        filters_txt = " | ".join(
            [
                x
                for x in [
                    f"Ref: {ref_type}" if ref_type else None,
                    f"Txn: {txn_type}" if txn_type else None,
                    f"Location: {location_id}" if location_id else None,
                    f"Item: {item_id}" if item_id else None,
                    f"From: {from_date.strftime('%d-%m-%Y')}" if from_date else None,
                    f"To: {to_date.strftime('%d-%m-%Y')}" if to_date else None,
                    f"Rows: {len(out_rows)}",
                ]
                if x
            ]
        )

        pdf_bytes = build_stock_transactions_pdf(
            rows=out_rows,
            branding=b,
            filters_text=filters_txt,
        )

        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="stock_transactions.pdf"'},
        )

    except SQLAlchemyError as e:
        raise HTTPException(status_code=500, detail="Database error") from e


@router.get("/schedule-medicine-report.pdf")
def download_schedule_medicine_report_pdf(
    date_from: date = Query(...),
    date_to: date = Query(...),
    location_id: int | None = Query(None),
    only_outgoing: bool = Query(True),
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    pdf_bytes = build_schedule_medicine_report_pdf(
        db,
        date_from=date_from,
        date_to=date_to,
        location_id=location_id,
        only_outgoing=only_outgoing,
    )

    filename = f"schedule_medicine_report_{date_from.isoformat()}_{date_to.isoformat()}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
