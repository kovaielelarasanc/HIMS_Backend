# FILE: app/api/charge_item_master.py
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_, desc, asc

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.charge_item_master import (
    ChargeItemMaster,
    ChargeItemModuleHeader,
    ChargeItemServiceHeader,
)
from app.schemas.charge_item_master import (
    ChargeItemCreate,
    ChargeItemUpdate,
    ChargeItemOut,
    ChargeItemListOut,
    AddChargeItemLineIn,
    AddChargeItemLineOut,
    ModuleHeaderCreate,
    ModuleHeaderUpdate,
    ModuleHeaderOut,
    ModuleHeaderListOut,
    ServiceHeaderCreate,
    ServiceHeaderUpdate,
    ServiceHeaderOut,
    ServiceHeaderListOut,
)
from app.models.billing import BillingInvoice, BillingCase, ServiceGroup

from app.services.billing_charge_item_service import (
    add_charge_item_line_to_invoice,
    fetch_idempotent_existing_line,
    expected_invoice_module_for_charge_item,
    get_or_create_draft_invoice_for_case_module,
)

router = APIRouter(prefix="/masters/charge-items",
                   tags=["Masters: Charge Items"])

ALLOWED_CATEGORIES = {"ADM", "DIET", "MISC", "BLOOD"}

# If your DB has system headers seeded, these are optional.
# Kept as backward-safe fallback if header tables are missing in some installs.
DEFAULT_MODULE_HEADERS = {
    "OPD", "IPD", "OT", "ER", "LAB", "RIS", "PHARM", "ROOM", "MISC"
}
DEFAULT_SERVICE_HEADERS = {
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
def _norm(v: str | None) -> str:
    return (v or "").strip().upper()


def _norm_category(v: str) -> str:
    s = _norm(v)
    if s not in ALLOWED_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid category. Allowed: {sorted(ALLOWED_CATEGORIES)}",
        )
    return s


def _norm_code(v: str) -> str:
    s = _norm(v)
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
    s = _norm(v)
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
    """
    ✅ IMPORTANT FIX:
    service_header is NOT restricted to Billing.ServiceGroup enum list.
    It is a HEADER CODE (dynamic), so we only validate format/length here.
    The "exists in master table" validation happens in _validate_misc_headers().
    """
    if v is None:
        return None
    s = _norm(v)
    if s == "":
        return None
    if len(s) > 16:
        raise HTTPException(status_code=422,
                            detail="Service header too long (max 16)")
    for ch in s:
        if not (ch.isalnum() or ch in "-_/"):
            raise HTTPException(
                status_code=422,
                detail=
                "Service header can contain only A-Z, 0-9, '-', '_', '/'",
            )
    return s


def _d(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else "0"))
    except Exception:
        return Decimal("0")


def _exists_active_module_header(db: Session, code: str) -> bool:
    try:
        return ((db.query(func.count(ChargeItemModuleHeader.id)).filter(
            func.upper(ChargeItemModuleHeader.code) == code,
            ChargeItemModuleHeader.is_active.is_(True),
        ).scalar() or 0) > 0)
    except Exception:
        return False


def _exists_active_service_header(db: Session, code: str) -> bool:
    try:
        return ((db.query(func.count(ChargeItemServiceHeader.id)).filter(
            func.upper(ChargeItemServiceHeader.code) == code,
            ChargeItemServiceHeader.is_active.is_(True),
        ).scalar() or 0) > 0)
    except Exception:
        return False


def _validate_misc_headers(
    db: Session,
    category: str,
    module_header: Optional[str],
    service_header: Optional[str],
):
    """
    Rule:
      - If category == MISC:
          module_header + service_header REQUIRED
          AND must exist in header masters (active) OR fallback defaults
      - Else: both forced None
    """
    if category != "MISC":
        return None, None

    mh = _norm_module_header(module_header)
    sh = _norm_service_header(service_header)

    if not mh:
        raise HTTPException(
            status_code=422,
            detail="module_header is required when category is MISC")
    if not sh:
        raise HTTPException(
            status_code=422,
            detail="service_header is required when category is MISC")

    ok_mh = _exists_active_module_header(db, mh) or (mh
                                                     in DEFAULT_MODULE_HEADERS)
    if not ok_mh:
        raise HTTPException(
            status_code=422,
            detail=
            f"Unknown module_header '{mh}'. Add it in Charge Item Header Master first.",
        )

    ok_sh = _exists_active_service_header(
        db, sh) or (sh in DEFAULT_SERVICE_HEADERS)
    if not ok_sh:
        raise HTTPException(
            status_code=422,
            detail=
            f"Unknown service_header '{sh}'. Add it in Charge Item Header Master first.",
        )

    return mh, sh


def _norm_header_code(v: str) -> str:
    s = _norm(v)
    if not s:
        raise HTTPException(status_code=422, detail="code is required")
    if len(s) > 16:
        raise HTTPException(status_code=422, detail="code too long (max 16)")
    for ch in s:
        if not (ch.isalnum() or ch in "-_/"):
            raise HTTPException(
                status_code=422,
                detail="code can contain only A-Z, 0-9, '-', '_', '/'",
            )
    return s


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
        user: User = Depends(require_permissions("masters.charge_items.manage")),
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
        # ✅ now dynamic-safe (no enum restriction)
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
        user: User = Depends(require_permissions("masters.charge_items.manage")),
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
    sh = _norm_service_header(inp.service_header)

    mh, sh = _validate_misc_headers(db, cat, mh, sh)

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
        # ✅ now dynamic-safe (no enum restriction)
        new_sh = _norm_service_header(inp.service_header)

    new_mh, new_sh = _validate_misc_headers(db, new_category, new_mh, new_sh)

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
# ✅ Header Masters (Module Header / Service Header)
# ============================================================
@router.get("/headers/module", response_model=ModuleHeaderListOut)
def list_module_headers(
        q: str = Query(""),
        is_active: Optional[bool] = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    qy = db.query(ChargeItemModuleHeader)
    if is_active is not None:
        qy = qy.filter(ChargeItemModuleHeader.is_active.is_(bool(is_active)))

    s = (q or "").strip().lower()
    if s:
        qy = qy.filter(
            or_(
                func.lower(ChargeItemModuleHeader.code).like(f"%{s}%"),
                func.lower(func.coalesce(ChargeItemModuleHeader.name,
                                         "")).like(f"%{s}%"),
            ))

    rows = qy.order_by(ChargeItemModuleHeader.is_system.desc(),
                       ChargeItemModuleHeader.code.asc()).all()
    return {"items": rows}


@router.post("/headers/module", response_model=ModuleHeaderOut)
def create_module_header(
    inp: ModuleHeaderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    code = _norm_header_code(inp.code)
    row = ChargeItemModuleHeader(
        code=code,
        name=(inp.name or None),
        is_active=bool(inp.is_active),
        is_system=False,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail="Duplicate module header code")
    db.refresh(row)
    return row


@router.patch("/headers/module/{hid}", response_model=ModuleHeaderOut)
def update_module_header(
    hid: int,
    inp: ModuleHeaderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    row = db.get(ChargeItemModuleHeader, hid)
    if not row:
        raise HTTPException(status_code=404, detail="Module header not found")

    if inp.name is not None:
        row.name = (inp.name or None)
    if inp.is_active is not None:
        row.is_active = bool(inp.is_active)

    db.commit()
    db.refresh(row)
    return row


@router.get("/headers/service", response_model=ServiceHeaderListOut)
def list_service_headers(
        q: str = Query(""),
        is_active: Optional[bool] = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    qy = db.query(ChargeItemServiceHeader)
    if is_active is not None:
        qy = qy.filter(ChargeItemServiceHeader.is_active.is_(bool(is_active)))

    s = (q or "").strip().lower()
    if s:
        qy = qy.filter(
            or_(
                func.lower(ChargeItemServiceHeader.code).like(f"%{s}%"),
                func.lower(func.coalesce(ChargeItemServiceHeader.name,
                                         "")).like(f"%{s}%"),
            ))

    rows = qy.order_by(ChargeItemServiceHeader.is_system.desc(),
                       ChargeItemServiceHeader.code.asc()).all()
    return {"items": rows}


@router.post("/headers/service", response_model=ServiceHeaderOut)
def create_service_header(
    inp: ServiceHeaderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    code = _norm_header_code(inp.code)

    sg = _norm(inp.service_group or "MISC")
    try:
        sg_enum = ServiceGroup(sg)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail=
            f"Invalid service_group. Allowed: {[e.value for e in ServiceGroup]}",
        )

    row = ChargeItemServiceHeader(
        code=code,
        name=(inp.name or None),
        service_group=sg_enum,
        is_active=bool(inp.is_active),
        is_system=False,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail="Duplicate service header code")
    db.refresh(row)
    return row


@router.patch("/headers/service/{hid}", response_model=ServiceHeaderOut)
def update_service_header(
    hid: int,
    inp: ServiceHeaderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permissions("masters.charge_items.manage")),
):
    row = db.get(ChargeItemServiceHeader, hid)
    if not row:
        raise HTTPException(status_code=404, detail="Service header not found")

    if inp.name is not None:
        row.name = (inp.name or None)

    if inp.service_group is not None:
        sg = _norm(inp.service_group or "MISC")
        try:
            row.service_group = ServiceGroup(sg)
        except Exception:
            raise HTTPException(
                status_code=422,
                detail=
                f"Invalid service_group. Allowed: {[e.value for e in ServiceGroup]}",
            )

    if inp.is_active is not None:
        row.is_active = bool(inp.is_active)

    db.commit()
    db.refresh(row)
    return row


# ============================================================
# ✅ Add Charge Item -> Invoice Line
# ============================================================
@router.post("/invoices/{invoice_id}/lines/charge-item",
             response_model=AddChargeItemLineOut)
def add_charge_item_line(
    invoice_id: int,
    inp: AddChargeItemLineIn,
    db: Session = Depends(get_db),
    user: User = Depends(
        require_permissions("billing.manage",
                            "billing.invoices.create")),
):
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    st = str(getattr(inv.status, "value", inv.status) or "").strip().upper()
    if st in ("POSTED", "VOID"):
        raise HTTPException(status_code=409,
                            detail=f"Invoice is {st}; cannot modify")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    ci = db.get(ChargeItemMaster, int(inp.charge_item_id))
    if not ci or not getattr(ci, "is_active", False):
        raise HTTPException(status_code=404,
                            detail="Charge item not found / inactive")

    expected_mod = expected_invoice_module_for_charge_item(ci)
    cur_mod = (getattr(inv, "module", None) or "").strip().upper() or "MISC"

    target_inv = inv
    if cur_mod != expected_mod:
        try:
            target_inv = get_or_create_draft_invoice_for_case_module(
                db,
                case=case,
                module=expected_mod,
                like_invoice=inv,
                created_by=getattr(user, "id", None),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))

    try:
        inv2, line = add_charge_item_line_to_invoice(
            db,
            invoice_id=int(target_inv.id),
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
                invoice_id=int(target_inv.id),
                idempotency_key=str(inp.idempotency_key),
            )
            if existing:
                inv3 = db.get(BillingInvoice, int(target_inv.id))
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
