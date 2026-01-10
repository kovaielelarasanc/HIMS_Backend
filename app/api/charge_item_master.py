# FILE: app/api/charge_item_mater.py
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_, desc, asc

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.charge_item_master import ChargeItemMaster
from app.schemas.charge_item_master import (
    ChargeItemCreate,
    ChargeItemUpdate,
    ChargeItemOut,
    ChargeItemListOut,
    AddChargeItemLineIn,
    AddChargeItemLineOut,
)
from app.models.billing import (
    BillingInvoice,
    BillingCase,
    DocStatus,
)

from app.services.billing_charge_item_service import (
    add_charge_item_line_to_invoice,
    fetch_idempotent_existing_line,
    is_misc_module,
)

router = APIRouter(prefix="/masters/charge-items",
                   tags=["Masters: Charge Items"])

ALLOWED_CATEGORIES = {"ADM", "DIET", "MISC", "BLOOD"}

ALLOWED_SERVICE_HEADERS = {
    "CONSULT", "LAB", "RAD", "PHARM", "OT", "PROC", "ROOM", "NURSING", "MISC"
}

ALLOWED_SORT = {
    "name",
    "code",
    "price",
    "updated_at",
    "created_at",
    "module_header",
    "service_header",
}


# ============================================================
# Permissions (Dependency Factory)
# ============================================================
def require_permissions(*codes: str):
    required: Set[str] = set(codes)

    def _dep(user: User = Depends(current_user)) -> User:
        if getattr(user, "is_admin", False):
            return user

        for r in (getattr(user, "roles", None) or []):
            for p in (getattr(r, "permissions", None) or []):
                if getattr(p, "code", None) in required:
                    return user

        raise HTTPException(status_code=403, detail="Permission denied")

    return _dep


# ============================================================
# Validators / Helpers
# ============================================================
def _norm_category(v: str) -> str:
    s = (v or "").strip().upper()
    if s not in ALLOWED_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid category. Allowed: {sorted(ALLOWED_CATEGORIES)}",
        )
    return s


def _norm_code(v: str) -> str:
    s = (v or "").strip().upper()
    if not s:
        raise HTTPException(status_code=422, detail="Code is required")
    if len(s) > 40:
        raise HTTPException(status_code=422, detail="Code too long (max 40)")
    for ch in s:
        if not (ch.isalnum() or ch in "-_/"):
            raise HTTPException(
                status_code=422,
                detail="Code can contain only A-Z, 0-9, '-', '_', '/'",
            )
    return s


def _norm_name(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise HTTPException(status_code=422, detail="Name is required")
    if len(s) > 255:
        raise HTTPException(status_code=422, detail="Name too long (max 255)")
    return s


def _norm_module_header(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = (v or "").strip().upper()
    if s == "":
        return None
    if len(s) > 16:
        raise HTTPException(status_code=422,
                            detail="Module header too long (max 16)")
    for ch in s:
        if not (ch.isalnum() or ch in "-_/"):
            raise HTTPException(
                status_code=422,
                detail="Module header can contain only A-Z, 0-9, '-', '_', '/'",
            )
    return s


def _norm_service_header(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = (v or "").strip().upper()
    if s == "":
        return None
    if len(s) > 16:
        raise HTTPException(status_code=422,
                            detail="Service header too long (max 16)")
    if s not in ALLOWED_SERVICE_HEADERS:
        raise HTTPException(
            status_code=422,
            detail=
            f"Invalid service header. Allowed: {sorted(ALLOWED_SERVICE_HEADERS)}",
        )
    return s


def _d(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else "0"))
    except Exception:
        return Decimal("0")


def _apply_misc_rules(category: str, module_header: Optional[str],
                      service_header: Optional[str]):
    """
    Rule:
      - If category == MISC: module_header + service_header are REQUIRED
      - Else: both must be NULL (we force it)
    """
    if category == "MISC":
        if not module_header:
            raise HTTPException(
                status_code=422,
                detail="module_header is required when category is MISC")
        if not service_header:
            raise HTTPException(
                status_code=422,
                detail="service_header is required when category is MISC")
        return module_header, service_header

    return None, None


# ============================================================
# Routes: Masters CRUD
# ============================================================
@router.get("", response_model=ChargeItemListOut)
def list_charge_items(
        category: Optional[str] = Query(
            None, description="ADM | DIET | MISC | BLOOD"),
        is_active: Optional[bool] = Query(None),
        module_header: Optional[str] = Query(
            None, description="Filter (mainly for MISC)"),
        service_header: Optional[str] = Query(
            None, description="Filter (mainly for MISC)"),
        search: str = Query(""),
        q: Optional[str] = Query(None, description="Alias for search"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        limit: Optional[int] = Query(
            None,
            ge=1,
            le=200,
            description="Alias for page_size (page forced to 1)"),
        sort: str = Query("updated_at"),
        order: str = Query("desc"),
        db: Session = Depends(get_db),
        user: User = Depends(require_permissions("masters.charge_items.view")),
):
    if (not search) and q:
        search = q

    if limit is not None:
        page = 1
        page_size = int(limit)

    qy = db.query(ChargeItemMaster)

    if category:
        qy = qy.filter(
            func.upper(ChargeItemMaster.category) == _norm_category(category))

    if is_active is not None:
        qy = qy.filter(ChargeItemMaster.is_active.is_(bool(is_active)))

    if module_header:
        mh = _norm_module_header(module_header)
        qy = qy.filter(func.upper(ChargeItemMaster.module_header) == mh)

    if service_header:
        sh = _norm_service_header(service_header)
        qy = qy.filter(func.upper(ChargeItemMaster.service_header) == sh)

    s = (search or "").strip().lower()
    if s:
        qy = qy.filter(
            or_(
                func.lower(ChargeItemMaster.code).like(f"%{s}%"),
                func.lower(ChargeItemMaster.name).like(f"%{s}%"),
                func.lower(func.coalesce(ChargeItemMaster.module_header,
                                         "")).like(f"%{s}%"),
                func.lower(func.coalesce(ChargeItemMaster.service_header,
                                         "")).like(f"%{s}%"),
            ))

    total = qy.count()

    sort_key = sort if sort in ALLOWED_SORT and hasattr(
        ChargeItemMaster, sort) else "updated_at"
    col = getattr(ChargeItemMaster, sort_key, ChargeItemMaster.id)
    order_fn = desc if (order or "").lower() == "desc" else asc

    rows = (qy.order_by(order_fn(col), ChargeItemMaster.id.desc()).offset(
        (page - 1) * page_size).limit(page_size).all())

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.get("/{item_id}", response_model=ChargeItemOut)
def get_charge_item(
        item_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(require_permissions("masters.charge_items.view")),
):
    row = db.get(ChargeItemMaster, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Charge item not found")
    return row


@router.post("", response_model=ChargeItemOut)
def create_charge_item(
    inp: ChargeItemCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    cat = _norm_category(inp.category)

    mh = _norm_module_header(inp.module_header)
    sh = _norm_service_header(
        inp.service_header) if inp.service_header is not None else None
    mh, sh = _apply_misc_rules(cat, mh, sh)

    row = ChargeItemMaster(
        category=cat,
        code=_norm_code(inp.code),
        name=_norm_name(inp.name),
        module_header=mh,
        service_header=sh,
        price=_d(inp.price),
        gst_rate=_d(inp.gst_rate),
        is_active=bool(inp.is_active),
    )

    if row.price < 0:
        raise HTTPException(status_code=422, detail="Price cannot be negative")
    if row.gst_rate < 0 or row.gst_rate > 100:
        raise HTTPException(status_code=422,
                            detail="GST rate must be between 0 and 100")

    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail="Duplicate: category + code already exists")
    db.refresh(row)
    return row


@router.patch("/{item_id}", response_model=ChargeItemOut)
def update_charge_item(
    item_id: int,
    inp: ChargeItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    row = db.get(ChargeItemMaster, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Charge item not found")

    new_category = row.category
    if inp.category is not None:
        new_category = _norm_category(inp.category)

    new_code = row.code
    if inp.code is not None:
        new_code = _norm_code(inp.code)

    new_name = row.name
    if inp.name is not None:
        new_name = _norm_name(inp.name)

    new_mh = row.module_header
    if inp.module_header is not None:
        new_mh = _norm_module_header(inp.module_header)

    new_sh = row.service_header
    if inp.service_header is not None:
        new_sh = _norm_service_header(inp.service_header)

    new_mh, new_sh = _apply_misc_rules(new_category, new_mh, new_sh)

    row.category = new_category
    row.code = new_code
    row.name = new_name
    row.module_header = new_mh
    row.service_header = new_sh

    if inp.price is not None:
        row.price = _d(inp.price)
        if row.price < 0:
            raise HTTPException(status_code=422,
                                detail="Price cannot be negative")

    if inp.gst_rate is not None:
        row.gst_rate = _d(inp.gst_rate)
        if row.gst_rate < 0 or row.gst_rate > 100:
            raise HTTPException(status_code=422,
                                detail="GST rate must be between 0 and 100")

    if inp.is_active is not None:
        row.is_active = bool(inp.is_active)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail="Duplicate: category + code already exists")
    db.refresh(row)
    return row


@router.delete("/{item_id}")
def delete_charge_item(
    item_id: int,
    hard: bool = Query(False, description="If true: permanent delete"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    row = db.get(ChargeItemMaster, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Charge item not found")

    if hard:
        db.delete(row)
        db.commit()
        return {"ok": True, "deleted": "hard"}

    row.is_active = False
    db.commit()
    return {"ok": True, "deleted": "soft", "is_active": False}


# ============================================================
# ✅ Add Charge Item -> Invoice Line (STRICT: only MISC invoices)
# ============================================================
@router.post(
    "/invoices/{invoice_id}/lines/charge-item",
    response_model=AddChargeItemLineOut,
)
def add_charge_item_line(
    invoice_id: int,
    inp: AddChargeItemLineIn,
    db: Session = Depends(get_db),
    user: User = Depends(
        require_permissions("billing.invoices.update",
                            "billing.invoices.create")),
):
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ✅ HARD SERVER ENFORCEMENT
    if not is_misc_module(getattr(inv, "module", None)):
        cur_mod = (getattr(inv, "module", None)
                   or "").strip().upper() or "MISC"
        raise HTTPException(
            status_code=422,
            detail=
            f"Charge items can be added only to MISC invoices. Current invoice module is '{cur_mod}'.",
        )

    if inv.status in (DocStatus.POSTED, DocStatus.VOID):
        raise HTTPException(
            status_code=409,
            detail=f"Invoice is {inv.status.value}; cannot modify")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    try:
        inv2, line = add_charge_item_line_to_invoice(
            db,
            invoice_id=int(invoice_id),
            charge_item_id=int(inp.charge_item_id),
            qty=inp.qty,
            unit_price=inp.unit_price,
            gst_rate=inp.gst_rate,
            discount_percent=inp.discount_percent,
            discount_amount=inp.discount_amount,
            idempotency_key=inp.idempotency_key,
            revenue_head_id=inp.revenue_head_id,
            cost_center_id=inp.cost_center_id,
            doctor_id=inp.doctor_id,
            manual_reason=inp.manual_reason,
            created_by=getattr(user, "id", None),
        )

        db.commit()
        db.refresh(inv2)
        db.refresh(line)
        return {"invoice": inv2, "line": line}

    except IntegrityError:
        db.rollback()
        if inp.idempotency_key:
            existing = fetch_idempotent_existing_line(
                db,
                billing_case_id=int(case.id),
                invoice_id=int(inv.id),
                idempotency_key=str(inp.idempotency_key),
            )
            if existing:
                inv3 = db.get(BillingInvoice, int(invoice_id))
                return {"invoice": inv3, "line": existing}
        raise HTTPException(
            status_code=409,
            detail="Could not add line (duplicate or constraint error)")

    except LookupError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))

    except PermissionError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    except RuntimeError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
