from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, Field


class GRNStatus(str, Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"

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


class GRNItemIn(BaseModel):
    po_item_id: Optional[int] = None
    item_id: int

    batch_no: str = ""
    expiry_date: Optional[date] = None

    quantity: Decimal = Field(default=Decimal("0"))
    free_quantity: Decimal = Field(default=Decimal("0"))

    unit_cost: Decimal = Field(default=Decimal("0"))
    mrp: Decimal = Field(default=Decimal("0"))

    discount_percent: Decimal = Field(default=Decimal("0"))
    discount_amount: Decimal = Field(default=Decimal("0"))

    tax_percent: Decimal = Field(default=Decimal("0"))
    cgst_percent: Decimal = Field(default=Decimal("0"))
    sgst_percent: Decimal = Field(default=Decimal("0"))
    igst_percent: Decimal = Field(default=Decimal("0"))

    scheme: str = ""
    remarks: str = ""


class GRNCreateUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    po_id: Optional[int] = None
    supplier_id: int
    location_id: int

    received_date: Optional[date] = None
    invoice_number: str = ""
    invoice_date: Optional[date] = None

    supplier_invoice_amount: Decimal = Field(default=Decimal("0"))
    freight_amount: Decimal = Field(default=Decimal("0"))
    other_charges: Decimal = Field(default=Decimal("0"))
    round_off: Decimal = Field(default=Decimal("0"))

    notes: str = ""
    difference_reason: str = ""  # used when mismatch

    items: List[GRNItemIn] = Field(default_factory=list)


class GRNCancelIn(BaseModel):
    reason: str


class GRNItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    po_item_id: Optional[int] = None
    item_id: int

    batch_no: str
    expiry_date: Optional[date] = None
    batch_id: Optional[int] = None

    quantity: Decimal
    free_quantity: Decimal
    unit_cost: Decimal
    mrp: Decimal

    discount_percent: Decimal
    discount_amount: Decimal

    tax_percent: Decimal
    cgst_percent: Decimal
    sgst_percent: Decimal
    igst_percent: Decimal

    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal

    scheme: str
    remarks: str

    item: Optional[ItemMini] = None


class GRNOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    grn_number: str

    po_id: Optional[int] = None
    supplier_id: int
    location_id: int
    status: GRNStatus
    received_date: date
    invoice_number: str
    invoice_date: Optional[date] = None

    supplier_invoice_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    discount_amount: Decimal
    freight_amount: Decimal
    other_charges: Decimal
    round_off: Decimal

    calculated_grn_amount: Decimal
    amount_difference: Decimal
    difference_reason: str

    status: str
    notes: str

    created_by_id: Optional[int] = None
    posted_by_id: Optional[int] = None
    posted_at: Optional[datetime] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    supplier: Optional[SupplierMini] = None
    location: Optional[LocationMini] = None
    items: List[GRNItemOut] = Field(default_factory=list)


class PostGRNBody(BaseModel):
    difference_reason: str = ""
