from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.api.perm import has_perm
from app.models.user import User
from app.services.inventory_suggestions import po_suggestions, item_price_hint

router = APIRouter(prefix="/inventory", tags=["Inventory - PO Automation"])


@router.get("/purchase-orders/suggestions")
def get_po_suggestions(
    location_id: int = Query(..., ge=1),
    supplier_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return po_suggestions(db, location_id=location_id, supplier_id=supplier_id, limit=limit)


@router.get("/items/{item_id:int}/price-hint")
def get_item_price_hint(
    item_id: int,
    location_id: Optional[int] = Query(None, ge=1),
    supplier_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    me: User = Depends(auth_current_user),
):
    if not has_perm(me, "pharmacy.inventory.po.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return item_price_hint(db, item_id=item_id, location_id=location_id, supplier_id=supplier_id)
