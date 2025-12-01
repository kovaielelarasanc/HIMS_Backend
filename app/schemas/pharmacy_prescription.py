# FILE: app/schemas/pharmacy.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional, Literal

from pydantic import BaseModel, Field, ConfigDict

# ---------- Rx Lines ----------


class RxLineBase(BaseModel):
    item_id: int
    requested_qty: Decimal = Field(..., gt=0)

    dose_text: Optional[str] = None
    frequency_code: Optional[str] = None
    times_per_day: Optional[int] = None
    duration_days: Optional[int] = None
    route: Optional[str] = None
    timing: Optional[str] = None
    instructions: Optional[str] = None

    # IPD extras
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    schedule_pattern: Optional[str] = None
    is_prn: bool = False
    is_stat: bool = False


class RxLineCreate(RxLineBase):
    pass


class RxLineUpdate(BaseModel):
    requested_qty: Optional[Decimal] = Field(None, gt=0)

    dose_text: Optional[str] = None
    frequency_code: Optional[str] = None
    times_per_day: Optional[int] = None
    duration_days: Optional[int] = None
    route: Optional[str] = None
    timing: Optional[str] = None
    instructions: Optional[str] = None

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    schedule_pattern: Optional[str] = None
    is_prn: Optional[bool] = None
    is_stat: Optional[bool] = None

    status: Optional[str] = None  # allow cancelling line before any dispense

    model_config = ConfigDict(from_attributes=True)


class RxLineOut(BaseModel):
    id: int
    prescription_id: int
    item_id: int

    requested_qty: Decimal
    dispensed_qty: Decimal
    status: str

    dose_text: Optional[str]
    frequency_code: Optional[str]
    times_per_day: Optional[int]
    duration_days: Optional[int]
    route: Optional[str]
    timing: Optional[str]
    instructions: Optional[str]

    start_date: Optional[date]
    end_date: Optional[date]
    schedule_pattern: Optional[str]
    is_prn: bool
    is_stat: bool
    order_status: Optional[str]

    available_qty_snapshot: Optional[Decimal]
    is_out_of_stock: bool

    item_name: Optional[str]
    item_form: Optional[str]
    item_strength: Optional[str]
    item_type: Optional[str]

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Rx Header ----------


class PrescriptionBase(BaseModel):
    type: Literal["OPD", "IPD", "COUNTER", "GENERAL"]
    patient_id: Optional[int] = None
    visit_id: Optional[int] = None
    ipd_admission_id: Optional[int] = None
    location_id: Optional[int] = None
    doctor_user_id: Optional[int] = None
    notes: Optional[str] = None


class PrescriptionCreate(PrescriptionBase):
    lines: List[RxLineCreate] = []


class PrescriptionUpdate(BaseModel):
    location_id: Optional[int] = None
    doctor_user_id: Optional[int] = None
    notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PrescriptionSummaryOut(BaseModel):
    id: int
    prescription_number: str
    type: str

    patient_id: Optional[int]
    visit_id: Optional[int]
    ipd_admission_id: Optional[int]
    location_id: Optional[int]
    doctor_user_id: Optional[int]

    status: str
    notes: Optional[str]

    signed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PrescriptionOut(PrescriptionSummaryOut):
    lines: List[RxLineOut] = []


# ---------- Dispense ----------


class DispenseLineIn(BaseModel):
    line_id: int
    dispense_qty: Decimal = Field(..., gt=0)


class DispenseFromRxIn(BaseModel):
    location_id: Optional[
        int] = None  # optional override; will fall back to Rx.location
    lines: List[DispenseLineIn]

    create_sale: bool = False
    context_type: Optional[Literal["OPD", "IPD", "COUNTER"]] = None


class DispenseFromRxOut(BaseModel):
    prescription: PrescriptionOut
    sale_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Counter Sale ----------


class CounterSaleItemIn(BaseModel):
    item_id: int
    quantity: Decimal = Field(..., gt=0)
    # Optional – allows Rx-style instructions even for counter
    dose_text: Optional[str] = None
    frequency_code: Optional[str] = None
    duration_days: Optional[int] = None
    route: Optional[str] = None
    timing: Optional[str] = None
    instructions: Optional[str] = None


class CounterSaleCreateIn(BaseModel):
    patient_id: Optional[int] = None
    visit_id: Optional[int] = None
    location_id: int
    notes: Optional[str] = None
    items: List[CounterSaleItemIn]


# ---------- Pharmacy Sale ----------


class SaleItemOut(BaseModel):
    id: int
    item_id: int
    item_name: str
    batch_id: Optional[int]
    batch_no: Optional[str]
    expiry_date: Optional[date]

    quantity: Decimal
    unit_price: Decimal
    tax_percent: Decimal
    line_amount: Decimal
    tax_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal

    model_config = ConfigDict(from_attributes=True)


class SaleSummaryOut(BaseModel):
    id: int
    bill_number: str
    context_type: str

    prescription_id: Optional[int]
    patient_id: Optional[int]
    visit_id: Optional[int]
    ipd_admission_id: Optional[int]
    location_id: Optional[int]

    bill_datetime: datetime

    gross_amount: Decimal
    total_tax: Decimal
    discount_amount_total: Decimal
    net_amount: Decimal
    rounding_adjustment: Decimal

    invoice_status: str
    payment_status: str

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SaleOut(SaleSummaryOut):
    items: List[SaleItemOut]


# ---------- Payments ----------


class PaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    mode: Literal["CASH", "CARD", "UPI", "NEFT", "OTHER"]
    reference: Optional[str] = None
    paid_on: Optional[datetime] = None
    note: Optional[str] = None


class PaymentOut(BaseModel):
    id: int
    amount: Decimal
    mode: str
    reference: Optional[str]
    paid_on: datetime
    note: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
