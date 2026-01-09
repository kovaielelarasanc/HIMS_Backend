# FILE: app/api/routes/charge_item_master.py
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
)

router = APIRouter(prefix="/masters/charge-items",
                   tags=["Masters: Charge Items"])

ALLOWED_CATEGORIES = {"ADM", "DIET", "MISC", "BLOOD"}
ALLOWED_SORT = {"name", "code", "price", "updated_at", "created_at"}


# ============================================================
# Permissions (Dependency Factory)
# ============================================================
def require_permissions(*codes: str):
    required: Set[str] = set(codes)

    def _dep(user: User = Depends(current_user)) -> User:
        # Super admin / admin shortcut
        if getattr(user, "is_admin", False):
            return user

        # Role -> permissions check
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


def _d(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else "0"))
    except Exception:
        return Decimal("0")


# ============================================================
# Routes
# ============================================================
@router.get("", response_model=ChargeItemListOut)
def list_charge_items(
        category: Optional[str] = Query(
            None, description="ADM | DIET | MISC | BLOOD"),
        is_active: Optional[bool] = Query(None),
        search: str = Query(""),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        sort: str = Query("updated_at"),
        order: str = Query("desc"),
        db: Session = Depends(get_db),
        user: User = Depends(require_permissions("masters.charge_items.view")),
):
    q = db.query(ChargeItemMaster)

    if category:
        q = q.filter(
            func.upper(ChargeItemMaster.category) == _norm_category(category))
    if is_active is not None:
        q = q.filter(ChargeItemMaster.is_active.is_(bool(is_active)))

    s = (search or "").strip().lower()
    if s:
        q = q.filter(
            or_(
                func.lower(ChargeItemMaster.code).like(f"%{s}%"),
                func.lower(ChargeItemMaster.name).like(f"%{s}%"),
            ))

    total = q.count()

    sort_key = sort if sort in ALLOWED_SORT and hasattr(
        ChargeItemMaster, sort) else "updated_at"
    col = getattr(ChargeItemMaster, sort_key, ChargeItemMaster.id)
    order_fn = desc if (order or "").lower() == "desc" else asc

    rows = (q.order_by(order_fn(col), ChargeItemMaster.id.desc()).offset(
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
    row = ChargeItemMaster(
        category=_norm_category(inp.category),
        code=_norm_code(inp.code),
        name=_norm_name(inp.name),
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

    if inp.category is not None:
        row.category = _norm_category(inp.category)
    if inp.code is not None:
        row.code = _norm_code(inp.code)
    if inp.name is not None:
        row.name = _norm_name(inp.name)
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
