# FILE: app/schemas/inventory_indent.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict

from app.models.inv_indent_issue import IndentStatus, IssueStatus, IndentPriority


# ------------------------------------
# Common outputs for Inventory Catalog
# ------------------------------------
class LocationOut(BaseModel):
    id: int
    code: str
    name: str
    is_pharmacy: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class InventoryItemOut(BaseModel):
    id: int
    code: str
    name: str
    item_type: str
    is_consumable: bool
    unit: str
    pack_size: str
    is_active: bool

    schedule_system: str
    schedule_code: str
    lasa_flag: bool
    high_alert_flag: bool
    requires_double_check: bool

    model_config = ConfigDict(from_attributes=True)


class StockOut(BaseModel):
    id: int
    item_id: int
    location_id: int
    on_hand_qty: Decimal
    reserved_qty: Decimal
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BatchOut(BaseModel):
    id: int
    item_id: int
    location_id: int
    batch_no: str
    mfg_date: Optional[date]
    expiry_date: Optional[date]
    current_qty: Decimal
    reserved_qty: Decimal
    mrp: Decimal
    unit_cost: Decimal
    tax_percent: Decimal
    is_active: bool
    is_saleable: bool
    status: str

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# INDENT
# -------------------------
class IndentItemIn(BaseModel):
    item_id: int
    requested_qty: Decimal = Field(..., gt=0)
    is_stat: bool = False
    remarks: str = ""


class IndentCreateIn(BaseModel):
    priority: str = "ROUTINE"
    from_location_id: int
    to_location_id: int

    patient_id: Optional[int] = None
    visit_id: Optional[int] = None
    ipd_admission_id: Optional[int] = None

    encounter_type: Optional[str] = None
    encounter_id: Optional[int] = None

    notes: str = ""
    items: List[IndentItemIn] = []


class IndentUpdateIn(BaseModel):
    priority: Optional[str] = None
    notes: Optional[str] = None


class IndentItemOut(BaseModel):
    id: int
    indent_id: int
    item_id: int
    requested_qty: Decimal
    approved_qty: Decimal
    issued_qty: Decimal
    is_stat: bool
    remarks: str

    item: Optional[InventoryItemOut] = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class IndentOut(BaseModel):
    id: int
    indent_number: str
    indent_date: date
    priority: IndentPriority

    from_location_id: int
    to_location_id: int

    from_location: Optional[LocationOut] = None
    to_location: Optional[LocationOut] = None

    patient_id: Optional[int]
    visit_id: Optional[int]
    ipd_admission_id: Optional[int]
    encounter_type: Optional[str]
    encounter_id: Optional[int]

    status: IndentStatus
    notes: str
    cancel_reason: str

    created_by_id: Optional[int]
    submitted_by_id: Optional[int]
    approved_by_id: Optional[int]
    cancelled_by_id: Optional[int]

    submitted_at: Optional[datetime]
    approved_at: Optional[datetime]
    cancelled_at: Optional[datetime]

    created_at: datetime
    updated_at: datetime

    items: List[IndentItemOut] = []

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class ApproveIndentItemIn(BaseModel):
    indent_item_id: int
    approved_qty: Decimal = Field(..., ge=0)


class ApproveIndentIn(BaseModel):
    items: Optional[List[ApproveIndentItemIn]] = None
    notes: str = ""


class CancelIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


# -------------------------
# ISSUE
# -------------------------
class IssueItemCreateIn(BaseModel):
    indent_item_id: Optional[int] = None
    item_id: int
    issued_qty: Decimal = Field(..., gt=0)
    batch_id: Optional[int] = None
    remarks: str = ""


class IssueCreateFromIndentIn(BaseModel):
    notes: str = ""
    items: Optional[List[IssueItemCreateIn]] = None


class IssueItemUpdateIn(BaseModel):
    issued_qty: Optional[Decimal] = Field(None, gt=0)
    batch_id: Optional[int] = None
    remarks: Optional[str] = None


class IssueItemOut(BaseModel):
    id: int
    issue_id: int
    indent_item_id: Optional[int]
    item_id: int
    batch_id: Optional[int]
    issued_qty: Decimal
    stock_txn_id: Optional[int]
    remarks: str

    item: Optional[InventoryItemOut] = None
    batch: Optional[BatchOut] = None

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class IssueOut(BaseModel):
    id: int
    issue_number: str
    issue_date: date
    indent_id: Optional[int]
    from_location_id: int
    to_location_id: int

    from_location: Optional[LocationOut] = None
    to_location: Optional[LocationOut] = None

    status: IssueStatus
    notes: str
    cancel_reason: str

    created_by_id: Optional[int]
    posted_by_id: Optional[int]
    cancelled_by_id: Optional[int]
    posted_at: Optional[datetime]
    cancelled_at: Optional[datetime]

    created_at: datetime
    updated_at: datetime

    items: List[IssueItemOut] = []

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)
