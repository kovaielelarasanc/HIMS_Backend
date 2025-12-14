# FILE: app/services/inventory_suggestions.py
from __future__ import annotations
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.pharmacy_inventory import (
    InventoryItem, ItemLocationStock, ItemPriceHistory
)

D0 = Decimal("0")

def po_suggestions(db: Session, location_id: int, supplier_id: int | None = None, limit: int = 200):
    """
    Returns low-stock suggestions:
    - on_hand < reorder_level  => suggest reorder to max_level
    - includes last price for auto-fill (supplier/location/item)
    """
    q = (
        db.query(InventoryItem, ItemLocationStock)
        .join(ItemLocationStock, (ItemLocationStock.item_id == InventoryItem.id) & (ItemLocationStock.location_id == location_id))
        .filter(InventoryItem.is_active == True)
        .order_by(InventoryItem.name.asc())
    )

    out = []
    for item, stock in q.limit(limit).all():
        reorder = Decimal(str(item.reorder_level or 0))
        maxlvl = Decimal(str(item.max_level or 0))
        onhand = Decimal(str(stock.on_hand_qty or 0))

        if reorder > 0 and onhand < reorder:
            suggested = max(D0, maxlvl - onhand) if maxlvl > 0 else max(D0, reorder - onhand)

            # price hint: supplier-specific last purchase first
            price = None
            if supplier_id:
                price = (
                    db.query(ItemPriceHistory)
                    .filter(
                        ItemPriceHistory.item_id == item.id,
                        ItemPriceHistory.supplier_id == supplier_id,
                        ItemPriceHistory.location_id == location_id,
                    )
                    .order_by(ItemPriceHistory.created_at.desc())
                    .first()
                )
            if not price:
                # fallback: stock's last price
                unit_cost = stock.last_unit_cost
                mrp = stock.last_mrp
                tax = stock.last_tax_percent
            else:
                unit_cost = price.unit_cost
                mrp = price.mrp
                tax = price.tax_percent

            out.append({
                "item_id": item.id,
                "code": item.code,
                "name": item.name,
                "generic_name": item.generic_name or "",
                "on_hand_qty": str(onhand),
                "reorder_level": str(reorder),
                "max_level": str(maxlvl),
                "suggested_qty": str(suggested),
                "unit_cost": str(unit_cost or 0),
                "mrp": str(mrp or 0),
                "tax_percent": str(tax or 0),
            })
    return out


def item_price_hint(db: Session, item_id: int, location_id: int | None, supplier_id: int | None):
    """
    Used by PO Add Item screen:
    - returns best price suggestion for this supplier/location
    """
    if supplier_id and location_id:
        ph = (
            db.query(ItemPriceHistory)
            .filter(
                ItemPriceHistory.item_id == item_id,
                ItemPriceHistory.supplier_id == supplier_id,
                ItemPriceHistory.location_id == location_id,
            )
            .order_by(ItemPriceHistory.created_at.desc())
            .first()
        )
        if ph:
            return {"unit_cost": str(ph.unit_cost), "mrp": str(ph.mrp), "tax_percent": str(ph.tax_percent)}

    # fallback to stock table if location exists
    if location_id:
        stock = (
            db.query(ItemLocationStock)
            .filter(ItemLocationStock.item_id == item_id, ItemLocationStock.location_id == location_id)
            .first()
        )
        if stock:
            return {
                "unit_cost": str(stock.last_unit_cost or 0),
                "mrp": str(stock.last_mrp or 0),
                "tax_percent": str(stock.last_tax_percent or 0),
            }

    # final fallback: item defaults (suggestion only)
    item = db.get(InventoryItem, item_id)
    if not item:
        return {"unit_cost": "0", "mrp": "0", "tax_percent": "0"}
    return {
        "unit_cost": str(item.default_price or 0),
        "mrp": str(item.default_mrp or 0),
        "tax_percent": str(item.default_tax_percent or 0),
    }
