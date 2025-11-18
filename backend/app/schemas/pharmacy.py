from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, validator, ConfigDict

# -------- Masters --------


class MedicineIn(BaseModel):
    code: str
    name: str
    generic_name: Optional[str] = ""
    form: str
    strength: Optional[str] = ""
    unit: str = "unit"
    pack_size: Optional[int] = 1
    manufacturer: Optional[str] = ""
    class_name: Optional[str] = ""
    atc_code: Optional[str] = ""
    lasa_flag: bool = False
    default_tax_percent: Optional[Decimal] = None
    default_price: Optional[Decimal] = None
    default_mrp: Optional[Decimal] = None
    reorder_level: Optional[int] = 0
    is_active: bool = True


class MedicineOut(MedicineIn):
    id: int

    class Config:
        orm_mode = True


class SupplierIn(BaseModel):
    name: str
    contact_person: Optional[str] = ""
    phone: Optional[str] = ""
    email: Optional[str] = ""
    gstin: Optional[str] = ""
    address: Optional[str] = ""
    payment_terms: Optional[str] = ""
    is_active: bool = True


class SupplierOut(SupplierIn):
    id: int

    class Config:
        orm_mode = True


class LocationIn(BaseModel):
    code: str
    name: str
    is_active: bool = True


class LocationOut(LocationIn):
    id: int

    class Config:
        orm_mode = True


# -------- Inventory --------


class LotOut(BaseModel):
    id: int
    medicine_id: int
    location_id: int
    batch: str
    expiry: date
    on_hand: int
    unit_cost: Optional[Decimal] = None
    sell_price: Optional[Decimal] = None
    mrp: Optional[Decimal] = None

    class Config:
        orm_mode = True


class TxnOut(BaseModel):
    id: int
    ts: datetime
    medicine_id: int
    location_id: int
    lot_id: int
    type: str
    qty_change: int
    ref_type: Optional[str] = None
    ref_id: Optional[int] = None
    user_id: Optional[int] = None
    note: Optional[str] = ""

    class Config:
        orm_mode = True


class AdjustIn(BaseModel):
    lot_id: int
    qty_change: int
    reason: str = "stock_take"


class TransferIn(BaseModel):
    lot_id: int
    from_location_id: int
    to_location_id: int
    qty: int


# -------- Procurement --------


class PoItemIn(BaseModel):
    medicine_id: int
    qty: int = Field(gt=0)


class PoIn(BaseModel):
    supplier_id: int
    location_id: int
    items: List[PoItemIn]


class PoOut(BaseModel):
    id: int
    supplier_id: int
    location_id: int
    status: str
    created_at: datetime

    class Config:
        orm_mode = True


class GrnItemIn(BaseModel):
    medicine_id: int
    batch: str
    expiry: date
    qty: int = Field(gt=0)
    unit_cost: Decimal
    tax_percent: Optional[Decimal] = None
    mrp: Optional[Decimal] = None
    sell_price: Optional[Decimal] = None


class GrnIn(BaseModel):
    supplier_id: int
    location_id: int
    po_id: Optional[int] = None
    items: List[GrnItemIn]


class GrnOut(BaseModel):
    id: int
    supplier_id: int
    location_id: int
    po_id: Optional[int] = None
    received_at: datetime

    class Config:
        orm_mode = True


# -------- Dispense / Sales --------


class DispenseItemIn(BaseModel):
    prescription_item_id: Optional[int] = None
    medicine_id: int
    lot_id: Optional[int] = None  # if omitted → FEFO auto-pick
    qty: int = Field(gt=0)


class DispenseIn(BaseModel):
    context: Dict[
        str,
        Any]  # {"type":"opd","visit_id":..} or {"type":"ipd","admission_id":..}
    patient_id: int
    location_id: int
    items: List[DispenseItemIn]
    payment: Optional[Dict[
        str, Any]] = None  # {"mode":"cash|upi|card|on-account","amount": 0}


class SaleOut(BaseModel):
    id: int
    patient_id: int
    context_type: str
    visit_id: Optional[int] = None
    admission_id: Optional[int] = None
    location_id: int
    total_amount: Decimal
    created_at: datetime

    class Config:
        orm_mode = True


class SaleItemOut(BaseModel):
    id: int
    sale_id: int
    medicine_id: int
    lot_id: int
    qty: int
    unit_price: Decimal
    tax_percent: Optional[Decimal] = None
    amount: Decimal
    prescription_item_id: Optional[int] = None

    class Config:
        orm_mode = True


class SaleWithItemsOut(SaleOut):
    items: List[SaleItemOut] = []


# -------- Prescriptions --------


class RxItemIn(BaseModel):
    medicine_id: int
    dose: Optional[str] = ""
    am: bool = False
    af: bool = False
    pm: bool = False
    night: bool = False
    duration_days: int = Field(default=1, ge=1)
    quantity: Optional[int] = None  # server computes if None
    route: Optional[str] = "po"
    frequency: Optional[str] = None
    instructions: Optional[str] = ""

    @validator("quantity", always=True)
    def _ensure_qty_or_tod(cls, v, values):
        if v is None:
            tod = int(values.get("am", False)) + int(values.get("af", False)) + \
                  int(values.get("pm", False)) + int(values.get("night", False))
            if tod == 0:
                raise ValueError(
                    "Select at least one time-of-day or provide quantity")
        return v


class RxIn(BaseModel):
    patient_id: int
    context: Optional[dict] = None
    prescriber_user_id: Optional[int] = None
    notes: Optional[str] = ""
    items: List[RxItemIn] = []


class RxItemOut(BaseModel):
    id: int
    medicine_id: int
    dose: str
    route: str
    am: bool
    af: bool
    pm: bool
    night: bool
    duration_days: int
    quantity: int
    instructions: str
    status: str
    dispensed_qty: int

    model_config = ConfigDict(from_attributes=True)


class RxOut(BaseModel):
    id: int
    patient_id: int
    context_type: str
    visit_id: Optional[int]
    admission_id: Optional[int]
    prescriber_user_id: int
    status: str
    notes: str
    items: list[RxItemOut] = []

    model_config = ConfigDict(from_attributes=True)
