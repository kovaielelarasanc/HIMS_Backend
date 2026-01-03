# FILE: app/services/inventory.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy import or_, case
from sqlalchemy.inspection import inspect as sa_inspect

from app.models.pharmacy_inventory import ItemBatch, StockTransaction
from app.models.user import User


# ============================================================
# Helpers
# ============================================================
def _to_decimal(v: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Safe Decimal conversion for str/int/float/Decimal/None."""
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _model_columns(Model) -> Set[str]:
    """
    Robust column name fetch for SQLAlchemy model.
    Works even if __table__ isn't directly accessible in rare cases.
    """
    try:
        return set(Model.__table__.columns.keys())
    except Exception:
        try:
            mapper = sa_inspect(Model).mapper
            return {c.key for c in mapper.column_attrs}
        except Exception:
            return set()


def _pick_first(cols: Set[str], *candidates: str) -> Optional[str]:
    """Return first column name that exists in cols."""
    for c in candidates:
        if c in cols:
            return c
    return None


# ============================================================
# Stock Transactions
# ============================================================
def create_stock_transaction(
    db: Session,
    user: User | None,
    location_id: int,
    item_id: int,
    batch_id: int | None,
    qty_delta: Any,
    txn_type: str,
    ref_type: str = "",
    ref_id: int | None = None,
    unit_cost: Any | None = None,
    mrp: Any | None = None,
    remark: str = "",
    patient_id: int | None = None,  # only set if column exists
    visit_id: int | None = None,    # only set if column exists
    doctor_id: int | None = None,   # ✅ NEW: only set if column exists
    txn_time: datetime | None = None,  # ✅ allow override
    flush: bool = True,  # ✅ helpful when caller needs st.id immediately
) -> StockTransaction:
    """
    Central creator for StockTransaction.

    ✅ Defensive:
    - Will NOT pass unknown kwargs to StockTransaction
      (prevents invalid keyword argument errors).
    - Supports different schema column names (quantity_change vs qty_change etc.)

    ✅ Doctor behavior:
    - If doctor_id is provided and column exists -> saved
    - Else if user.is_doctor == True and doctor_id column exists -> uses user.id
    """
    cols = _model_columns(StockTransaction)
    data: Dict[str, Any] = {}

    # --- core foreign keys ---
    if "location_id" in cols:
        data["location_id"] = location_id
    if "item_id" in cols:
        data["item_id"] = item_id
    if "batch_id" in cols:
        data["batch_id"] = batch_id

    # --- txn time ---
    # your model uses txn_time; keep variants safe
    time_field = _pick_first(cols, "txn_time", "transaction_time", "time", "created_at")
    if time_field:
        data[time_field] = txn_time or datetime.utcnow()

    # --- quantity delta field variants ---
    qty_field = _pick_first(cols, "quantity_change", "qty_change", "qty_delta", "quantity")
    if qty_field:
        data[qty_field] = _to_decimal(qty_delta)

    # --- txn type field variants ---
    t_field = _pick_first(cols, "txn_type", "transaction_type", "type")
    if t_field:
        data[t_field] = txn_type

    # --- references ---
    if "ref_type" in cols:
        data["ref_type"] = ref_type or ""
    elif "reference_type" in cols:
        data["reference_type"] = ref_type or ""

    if "ref_id" in cols:
        data["ref_id"] = ref_id
    elif "reference_id" in cols:
        data["reference_id"] = ref_id

    # --- costs ---
    if "unit_cost" in cols:
        data["unit_cost"] = _to_decimal(unit_cost)
    if "mrp" in cols:
        data["mrp"] = _to_decimal(mrp)

    # --- remark/notes ---
    if "remark" in cols:
        data["remark"] = (remark or "")[:1000]
    elif "remarks" in cols:
        data["remarks"] = (remark or "")[:1000]
    elif "note" in cols:
        data["note"] = (remark or "")[:1000]

    # --- audit ---
    uid = getattr(user, "id", None) if user else None
    if "user_id" in cols:
        data["user_id"] = uid
    elif "created_by" in cols:
        data["created_by"] = uid

    # --- optional patient/visit links (ONLY if columns exist) ---
    if patient_id is not None and "patient_id" in cols:
        data["patient_id"] = patient_id
    if visit_id is not None and "visit_id" in cols:
        data["visit_id"] = visit_id

    # --- ✅ doctor link (ONLY if column exists) ---
    if "doctor_id" in cols:
        did = doctor_id
        # fallback: if current user is doctor, set them as doctor_id
        if did is None and user and bool(getattr(user, "is_doctor", False)):
            did = int(user.id)
        data["doctor_id"] = did

    st = StockTransaction(**data)
    db.add(st)

    # ✅ ensure st.id is available immediately (safe; does not commit)
    if flush:
        db.flush()

    return st


# ============================================================
# FEFO Allocation (First Expiry First Out)
# ============================================================
def allocate_batches_fefo(
    db: Session,
    location_id: int,
    item_id: int,
    quantity: Any,
) -> List[Tuple[ItemBatch, Decimal]]:
    """
    FEFO allocation (First-Expiry-First-Out) for a given item/location.

    - Uses only ACTIVE + SALEABLE batches
    - Skips EXPIRED batches
    - Orders by expiry date (earliest first), NULL expiry at the end
    - Locks rows FOR UPDATE to avoid race conditions
    - Returns list of (batch, qty_to_use)
    - Raises ValueError if insufficient stock
    """
    qty = _to_decimal(quantity)
    if qty <= 0:
        raise ValueError("Quantity must be > 0")

    today = date.today()

    q = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.current_qty > 0,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            or_(
                ItemBatch.expiry_date.is_(None),
                ItemBatch.expiry_date >= today,
            ),
        )
    )

    # MySQL-safe NULLS LAST emulation
    nulls_last_expr = case((ItemBatch.expiry_date.is_(None), 1), else_=0)

    q = (
        q.order_by(
            nulls_last_expr.asc(),       # non-null first, null last
            ItemBatch.expiry_date.asc(), # earliest expiry first
            ItemBatch.id.asc(),          # tie breaker
        )
        .with_for_update()
    )

    remaining = qty
    allocations: List[Tuple[ItemBatch, Decimal]] = []

    for batch in q.all():
        if remaining <= 0:
            break

        available = _to_decimal(batch.current_qty)
        if available <= 0:
            continue

        use_qty = available if available <= remaining else remaining
        if use_qty <= 0:
            continue

        allocations.append((batch, use_qty))
        remaining -= use_qty

    if remaining > 0:
        raise ValueError(f"Insufficient stock for FEFO allocation (short by {remaining} units)")

    return allocations


def adjust_batch_qty(*, batch: ItemBatch, delta: Any) -> None:
    """
    Safely adjust batch quantity and active flag.
    Positive delta = stock in, negative delta = stock out.
    """
    current = _to_decimal(batch.current_qty)
    d = _to_decimal(delta)
    new_qty = current + d

    if new_qty < 0:
        raise ValueError(f"Negative stock for batch {batch.id}")

    batch.current_qty = new_qty
    batch.is_active = new_qty > 0
