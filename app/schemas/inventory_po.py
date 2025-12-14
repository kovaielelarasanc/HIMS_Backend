from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.pharmacy_inventory import POStatus


class POItemIn(BaseModel):
    item_id: int
    ordered_qty: Decimal = Field(default=Decimal("0"))
    unit_cost: Decimal = Field(default=Decimal("0"))
    tax_percent: Decimal = Field(default=Decimal("0"))
    mrp: Decimal = Field(default=Decimal("0"))
    remarks: str = ""


class POCreate(BaseModel):
    supplier_id: int
    location_id: int
    order_date: Optional[date] = None
    expected_date: Optional[date] = None

    currency: str = "INR"
    payment_terms: str = ""
    quotation_ref: str = ""
    notes: str = ""

    items: List[POItemIn] = Field(default_factory=list)


class POUpdate(BaseModel):
    order_date: Optional[date] = None
    expected_date: Optional[date] = None

    currency: Optional[str] = None
    payment_terms: Optional[str] = None
    quotation_ref: Optional[str] = None
    notes: Optional[str] = None

    items: Optional[List[POItemIn]] = None


class POItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    ordered_qty: Decimal
    received_qty: Decimal

    unit_cost: Decimal
    tax_percent: Decimal
    mrp: Decimal

    line_sub_total: Decimal
    line_tax_total: Decimal
    line_total: Decimal
    remarks: str

    # computed for UI
    pending_qty: Decimal


class POOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    po_number: str

    supplier_id: int
    location_id: int

    order_date: date
    expected_date: Optional[date]

    currency: str
    payment_terms: str
    quotation_ref: str
    notes: str

    status: POStatus

    sub_total: Decimal
    tax_total: Decimal
    grand_total: Decimal

    created_by_id: Optional[int]
    approved_by_id: Optional[int]
    approved_at: Optional[datetime]

    cancelled_by_id: Optional[int]
    cancelled_at: Optional[datetime]
    cancel_reason: str

    email_sent_to: str
    email_sent_at: Optional[datetime]

    created_at: datetime
    updated_at: datetime

    items: List[POItemOut]

class SupplierMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


class LocationMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: Optional[str] = None


class ItemMini(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: Optional[str] = None
    generic_name: Optional[str] = None

    # optional defaults (if your InventoryItem has these)
    default_price: Optional[Decimal] = None
    default_mrp: Optional[Decimal] = None
    default_tax_percent: Optional[Decimal] = None


class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int

    ordered_qty: Decimal = Field(default=Decimal("0"))
    received_qty: Decimal = Field(default=Decimal("0"))

    unit_cost: Decimal = Field(default=Decimal("0"))
    mrp: Decimal = Field(default=Decimal("0"))
    tax_percent: Decimal = Field(default=Decimal("0"))

    item: Optional[ItemMini] = None


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    po_number: str

    supplier_id: int
    location_id: int

    order_date: Optional[date] = None
    expected_date: Optional[date] = None

    status: str

    supplier: Optional[SupplierMini] = None
    location: Optional[LocationMini] = None
    items: List[PurchaseOrderItemOut] = Field(default_factory=list)


class PurchaseOrderPendingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    po_number: str
    supplier_id: int
    location_id: int
    order_date: Optional[date] = None
    expected_date: Optional[date] = None
    status: str

    pending_items_count: int = 0

    supplier: Optional[SupplierMini] = None
    location: Optional[LocationMini] = None