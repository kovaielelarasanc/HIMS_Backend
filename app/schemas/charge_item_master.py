# FILE: app/schemas/charge_item_master.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, Field



# -------------------------
# Module headers
# -------------------------
class ModuleHeaderCreate(BaseModel):
    code: str = Field(..., max_length=16)
    name: Optional[str] = Field(default=None, max_length=64)
    is_active: bool = True


class ModuleHeaderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


class ModuleHeaderOut(BaseModel):
    id: int
    code: str
    name: Optional[str] = None
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModuleHeaderListOut(BaseModel):
    items: List[ModuleHeaderOut]


# -------------------------
# Service headers
# -------------------------
class ServiceHeaderCreate(BaseModel):
    code: str = Field(..., max_length=16)
    name: Optional[str] = Field(default=None, max_length=64)
    service_group: str = Field(default="MISC", description="Maps to Billing.ServiceGroup")
    is_active: bool = True


class ServiceHeaderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    service_group: Optional[str] = None
    is_active: Optional[bool] = None


class ServiceHeaderOut(BaseModel):
    id: int
    code: str
    name: Optional[str] = None
    service_group: str
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ServiceHeaderListOut(BaseModel):
    items: List[ServiceHeaderOut]

class ChargeItemCreate(BaseModel):
    category: str = Field(..., description="ADM | DIET | MISC | BLOOD")
    code: str
    name: str

    # Only required when category == MISC
    module_header: Optional[str] = Field(
        None,
        description=
        "Only for MISC. Example: OPD/IPD/OT/LAB/RIS/PHARM/ROOM/ER/MISC")
    service_header: Optional[str] = Field(
        None,
        description=
        "Only for MISC. Example: CONSULT/LAB/RAD/PHARM/OT/PROC/ROOM/NURSING/MISC"
    )

    price: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("0")
    is_active: bool = True


class ChargeItemUpdate(BaseModel):
    category: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None

    # Only meaningful when category == MISC
    module_header: Optional[str] = None
    service_header: Optional[str] = None

    price: Optional[Decimal] = None
    gst_rate: Optional[Decimal] = None
    is_active: Optional[bool] = None


class ChargeItemOut(BaseModel):
    id: int
    category: str
    code: str
    name: str

    module_header: Optional[str] = None
    service_header: Optional[str] = None

    price: Decimal
    gst_rate: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChargeItemListOut(BaseModel):
    items: List[ChargeItemOut]
    total: int
    page: int
    page_size: int


class AddChargeItemLineIn(BaseModel):
    charge_item_id: int

    qty: Decimal = Field(default=Decimal("1"))
    # Optional overrides (if omitted => tariff/master)
    unit_price: Optional[Decimal] = None
    gst_rate: Optional[Decimal] = None

    discount_percent: Decimal = Field(default=Decimal("0"))
    discount_amount: Decimal = Field(default=Decimal("0"))

    revenue_head_id: Optional[int] = None
    cost_center_id: Optional[int] = None
    doctor_id: Optional[int] = None

    manual_reason: Optional[str] = Field(default="CHARGE_ITEM")

    # Prevent double-click duplicates
    idempotency_key: Optional[str] = Field(
        default=None, description="Unique key (<=64 chars) for idempotency")


class BillingInvoiceTotalsOut(BaseModel):
    id: int
    module: Optional[str] = None

    sub_total: Decimal
    discount_total: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal

    updated_at: datetime

    model_config = {"from_attributes": True}


class BillingInvoiceLineOut(BaseModel):
    id: int
    invoice_id: int
    billing_case_id: int

    service_group: str

    item_type: Optional[str] = None
    item_id: Optional[int] = None
    item_code: Optional[str] = None
    description: str

    qty: Decimal
    unit_price: Decimal

    discount_percent: Decimal
    discount_amount: Decimal

    gst_rate: Decimal
    tax_amount: Decimal

    line_total: Decimal
    net_amount: Decimal

    is_manual: bool
    manual_reason: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AddChargeItemLineOut(BaseModel):
    invoice: BillingInvoiceTotalsOut
    line: BillingInvoiceLineOut
