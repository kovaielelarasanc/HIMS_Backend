# FILE: app/schemas/charge_item_master.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, Field


class ChargeItemCreate(BaseModel):
    category: str = Field(..., description="ADM | DIET | MISC | BLOOD")
    code: str
    name: str
    price: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("0")
    is_active: bool = True


class ChargeItemUpdate(BaseModel):
    category: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None
    price: Optional[Decimal] = None
    gst_rate: Optional[Decimal] = None
    is_active: Optional[bool] = None


class ChargeItemOut(BaseModel):
    id: int
    category: str
    code: str
    name: str
    price: Decimal
    gst_rate: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Pydantic v2
    model_config = {"from_attributes": True}


class ChargeItemListOut(BaseModel):
    items: List[ChargeItemOut]
    total: int
    page: int
    page_size: int
