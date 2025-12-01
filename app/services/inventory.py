# FILE: app/services/inventory.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import select, asc

from app.models.pharmacy_inventory import (
    ItemBatch,
    StockTransaction,
    InventoryLocation,
    InventoryItem,
)
from app.models.user import User

from datetime import date
from sqlalchemy import and_, or_, case


def create_stock_transaction(
    db: Session,
    user,
    location_id: int,
    item_id: int,
    batch_id: int | None,
    qty_delta: Decimal,
    txn_type: str,
    ref_type: str = "",
    ref_id: int | None = None,
    unit_cost: Decimal | None = None,
    mrp: Decimal | None = None,
    remark: str = "",
    patient_id: int | None = None,
    visit_id: int | None = None,
) -> StockTransaction:
    """
    Central creator for StockTransaction – always use this so audit is consistent.
    """
    st = StockTransaction(
        location_id=location_id,
        item_id=item_id,
        batch_id=batch_id,
        quantity_change=qty_delta,
        txn_type=txn_type,
        ref_type=ref_type,
        ref_id=ref_id,
        unit_cost=unit_cost or Decimal("0"),
        mrp=mrp or Decimal("0"),
        remark=remark or "",
        user_id=getattr(user, "id", None),
        patient_id=patient_id,
        visit_id=visit_id,
    )
    db.add(st)
    return st


def allocate_batches_fefo(
    db: Session,
    location_id: int,
    item_id: int,
    quantity: Decimal,
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
    if quantity is None:
        raise ValueError("Quantity is required")
    if Decimal(quantity) <= 0:
        raise ValueError("Quantity must be > 0")

    today = date.today()

    q = (
        db.query(ItemBatch).filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.current_qty > 0,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            # Skip already expired batches
            or_(
                ItemBatch.expiry_date.is_(None),
                ItemBatch.expiry_date >= today,
            ),
        ))

    # --- IMPORTANT: MySQL-safe NULLS LAST emulation ---
    # CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END
    # So non-null expiry (0) comes first, NULL (1) last.
    nulls_last_expr = case(
        (ItemBatch.expiry_date.is_(None), 1),
        else_=0,
    )

    q = (
        q.order_by(
            nulls_last_expr.asc(),  # non-null first, null last
            ItemBatch.expiry_date.asc(),  # earliest expiry first
            ItemBatch.id.asc(),  # tie-breaker
        ).with_for_update())

    remaining = Decimal(quantity)
    allocations: List[Tuple[ItemBatch, Decimal]] = []

    for batch in q.all():
        if remaining <= 0:
            break

        available = batch.current_qty or Decimal("0")
        if available <= 0:
            continue

        use_qty = available if available <= remaining else remaining
        if use_qty <= 0:
            continue

        allocations.append((batch, use_qty))
        remaining -= use_qty

    if remaining > 0:
        # Not enough stock overall
        raise ValueError(f"Insufficient stock for FEFO allocation "
                         f"(short by {remaining} units)")

    return allocations


def adjust_batch_qty(*, batch: ItemBatch, delta: Decimal) -> None:
    """
    Safely adjust batch quantity and active flag.
    Positive delta = stock in, negative delta = stock out.
    """
    current = batch.current_qty or Decimal("0")
    new_qty = current + (delta or Decimal("0"))
    if new_qty < 0:
        # Callers (dispense/returns) should validate,
        # but this protects against accidental negative stock.
        raise ValueError(f"Negative stock for batch {batch.id}")
    batch.current_qty = new_qty
    batch.is_active = new_qty > 0
