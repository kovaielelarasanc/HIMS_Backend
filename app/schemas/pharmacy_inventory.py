# FILE: app/schemas/pharmacy_inventory.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict, condecimal

# ---------- Locations ----------


class LocationBase(BaseModel):
    code: str
    name: str
    description: str | None = ""
    is_pharmacy: bool = True
    is_active: bool = True
    expiry_alert_days: int = 90


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_pharmacy: Optional[bool] = None
    is_active: Optional[bool] = None
    expiry_alert_days: Optional[int] = None


class LocationOut(LocationBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Suppliers ----------


class SupplierBase(BaseModel):
    code: str
    name: str
    contact_person: str | None = ""
    phone: str | None = ""
    email: str | None = ""
    address: str | None = ""
    gstin: str | None = ""
    is_active: bool = True


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    gstin: Optional[str] = None
    is_active: Optional[bool] = None


class SupplierOut(SupplierBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Items ----------

Quantity = condecimal(max_digits=14, decimal_places=4)
Money = condecimal(max_digits=14, decimal_places=4)
Percent = condecimal(max_digits=5, decimal_places=2)


class ItemBase(BaseModel):
    code: str
    name: str
    generic_name: str | None = ""
    form: str | None = ""
    strength: str | None = ""
    unit: str | None = "unit"
    pack_size: str | None = "1"
    manufacturer: str | None = ""
    class_name: str | None = ""
    atc_code: str | None = ""
    hsn_code: str | None = ""

    lasa_flag: bool = False
    is_consumable: bool = False

    default_tax_percent: Percent = 0
    default_price: Money = 0
    default_mrp: Money = 0

    reorder_level: Quantity = 0
    max_level: Quantity = 0
    qr_number: Optional[str] = None

    is_active: bool = True


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    generic_name: Optional[str] = None
    form: Optional[str] = None
    strength: Optional[str] = None
    unit: Optional[str] = None
    pack_size: Optional[str] = None
    manufacturer: Optional[str] = None
    class_name: Optional[str] = None
    atc_code: Optional[str] = None
    hsn_code: Optional[str] = None

    lasa_flag: Optional[bool] = None
    is_consumable: Optional[bool] = None

    default_tax_percent: Optional[Percent] = None
    default_price: Optional[Money] = None
    default_mrp: Optional[Money] = None

    reorder_level: Optional[Quantity] = None
    max_level: Optional[Quantity] = None
    qr_number: Optional[str] = None

    is_active: Optional[bool] = None


class ItemOut(ItemBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Batches & Stock ----------


class ItemBatchOut(BaseModel):
    id: int
    item_id: int
    location_id: int
    batch_no: str
    expiry_date: date | None
    current_qty: Quantity
    unit_cost: Money
    mrp: Money
    tax_percent: Percent
    is_active: bool
    is_saleable: bool
    status: str
    created_at: datetime
    updated_at: datetime

    # For nice UI: you get brand, generic, location name etc.
    item: ItemOut
    location: LocationOut

    model_config = ConfigDict(from_attributes=True)


class StockSummaryOut(BaseModel):
    item_id: int
    code: str
    name: str
    location_id: int | None = None
    location_name: str | None = None
    total_qty: Quantity
    reorder_level: Quantity
    max_level: Quantity
    is_low: bool
    is_over: bool


# ---------- Purchase Orders ----------


class PurchaseOrderItemIn(BaseModel):
    item_id: int
    ordered_qty: Quantity = Field(..., gt=0)
    unit_cost: Money = 0
    tax_percent: Percent = 0
    mrp: Money = 0


class PurchaseOrderItemOut(BaseModel):
    id: int
    item_id: int
    ordered_qty: Quantity
    received_qty: Quantity
    unit_cost: Money
    tax_percent: Percent
    mrp: Money
    line_total: Money
    item: ItemOut

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderBase(BaseModel):
    supplier_id: int
    location_id: int
    order_date: date | None = None
    expected_date: date | None = None
    notes: str | None = ""


class PurchaseOrderCreate(PurchaseOrderBase):
    items: List[PurchaseOrderItemIn]


class PurchaseOrderUpdate(BaseModel):
    supplier_id: Optional[int] = None
    location_id: Optional[int] = None
    order_date: Optional[date] = None
    expected_date: Optional[date] = None
    notes: Optional[str] = None
    items: Optional[
        List[PurchaseOrderItemIn]] = None  # replace all items in DRAFT


class PurchaseOrderOut(BaseModel):
    id: int
    po_number: str
    supplier: SupplierOut
    location: LocationOut
    order_date: date
    expected_date: date | None
    status: str
    notes: str
    email_sent_to: str
    email_sent_at: datetime | None
    created_at: datetime
    updated_at: datetime
    items: List[PurchaseOrderItemOut]

    model_config = ConfigDict(from_attributes=True)


# ---------- GRN ----------


class GRNItemIn(BaseModel):
    item_id: int
    po_item_id: int | None = None
    batch_no: str
    expiry_date: date | None = None
    quantity: Quantity = Field(..., gt=0)
    free_quantity: Quantity = 0
    unit_cost: Money = 0
    tax_percent: Percent = 0
    mrp: Money = 0


class GRNBase(BaseModel):
    po_id: int | None = None
    supplier_id: int
    location_id: int
    received_date: date | None = None
    invoice_number: str | None = ""
    invoice_date: date | None = None
    notes: str | None = ""


class GRNCreate(GRNBase):
    items: List[GRNItemIn]


class GRNOutItem(BaseModel):
    id: int
    item: ItemOut
    batch_no: str
    expiry_date: date | None
    quantity: Quantity
    free_quantity: Quantity
    unit_cost: Money
    tax_percent: Percent
    mrp: Money
    line_total: Money
    batch_id: int | None

    model_config = ConfigDict(from_attributes=True)


class GRNOut(BaseModel):
    id: int
    grn_number: str
    status: str
    purchase_order: PurchaseOrderOut | None
    supplier: SupplierOut
    location: LocationOut
    received_date: date
    invoice_number: str
    invoice_date: date | None
    notes: str
    created_at: datetime
    updated_at: datetime
    items: List[GRNOutItem]

    model_config = ConfigDict(from_attributes=True)


# ---------- Returns ----------


class ReturnItemIn(BaseModel):
    item_id: int
    batch_id: int | None = None
    batch_no: Optional[str] = None
    quantity: Quantity = Field(..., gt=0)
    reason: str | None = ""


class ReturnBase(BaseModel):
    type: str  # TO_SUPPLIER / FROM_CUSTOMER / INTERNAL
    supplier_id: int | None = None
    location_id: int
    return_date: date | None = None
    reason: str | None = ""


class ReturnCreate(ReturnBase):
    items: List[ReturnItemIn]


class ReturnItemOut(BaseModel):
    id: int
    item: ItemOut
    batch: ItemBatchOut | None
    quantity: Quantity
    reason: str

    model_config = ConfigDict(from_attributes=True)


class ReturnOut(BaseModel):
    id: int
    return_number: str
    type: str
    supplier: SupplierOut | None
    location: LocationOut
    return_date: date
    status: str
    reason: str
    created_at: datetime
    updated_at: datetime
    items: List[ReturnItemOut]

    model_config = ConfigDict(from_attributes=True)


# ---------- Transactions ----------


class StockTransactionOut(BaseModel):
    id: int
    location_id: int
    item_id: int
    batch_id: int | None
    txn_time: datetime
    txn_type: str
    ref_type: str
    ref_id: int | None
    quantity_change: Quantity
    unit_cost: Money
    mrp: Money
    remark: Optional[str] = None
    user_id: int | None
    patient_id: int | None
    visit_id: int | None
    
    item_name: Optional[str] = None
    item_code: Optional[str] = None
    batch_no: Optional[str] = None
    location_name: Optional[str] = None
    user_name: Optional[str] = None
    ref_display: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Dispense (stock OUT) ----------


class DispenseLineIn(BaseModel):
    item_id: int
    batch_id: int | None = None
    quantity: Quantity = Field(..., gt=0)


class DispenseRequestIn(BaseModel):
    location_id: int
    patient_id: int | None = None
    visit_id: int | None = None
    remark: str | None = ""
    lines: List[DispenseLineIn]
