# FILE: app/schemas/billing.py
from __future__ import annotations

from typing import Optional, Literal, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator
from decimal import Decimal


class PatientMiniOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uhid: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    gender: Optional[str] = None


class AutoBedChargesIn(BaseModel):
    admission_id: int
    mode: Literal["daily", "hourly", "mixed"] = "daily"
    upto_ts: Optional[datetime] = None
    skip_if_already_billed: bool = True


class AutoOtChargesIn(BaseModel):
    case_id: int


class AutoOtInvoiceIn(BaseModel):
    """✅ One-shot: find/create invoice + add OT charges"""
    case_id: int
    finalize: bool = False


class ProviderBase(BaseModel):
    name: str
    code: Optional[str] = None
    provider_type: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: bool = True


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    provider_type: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None


class ProviderOut(ProviderBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class InvoiceCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    billing_type: Optional[str] = None
    provider_id: Optional[int] = None
    consultant_id: Optional[int] = None
    visit_no: Optional[str] = None
    remarks: Optional[str] = None


class InvoiceUpdate(BaseModel):
    billing_type: Optional[str] = None
    provider_id: Optional[int] = None
    consultant_id: Optional[int] = None
    visit_no: Optional[str] = None
    remarks: Optional[str] = None
    header_discount_percent: Optional[float] = None
    header_discount_amount: Optional[float] = None
    discount_remarks: Optional[str] = None
    discount_authorized_by: Optional[int] = None
    status: Optional[str] = None


class AddServiceIn(BaseModel):
    service_type: Literal["lab", "radiology", "ot_procedure", "ot_bed",
                          "pharmacy", "opd", "ipd_bed", "manual", "other"]
    service_ref_id: int = 0
    description: Optional[str] = None
    quantity: Decimal = Decimal("1")
    unit_price: Optional[Decimal] = None
    tax_rate: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")


class ManualItemIn(BaseModel):
    description: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal
    tax_rate: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    service_type: Optional[str] = "manual"
    service_ref_id: Optional[int] = 0


class UpdateItemIn(BaseModel):
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    tax_rate: Optional[Decimal] = None
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    description: Optional[str] = None


class VoidItemIn(BaseModel):
    reason: Optional[str] = None


class PaymentIn(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    amount: Decimal
    mode: str
    reference_no: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("amount")
    def non_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("Amount must be non-zero.")
        return v


class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    seq: int
    service_type: str
    service_ref_id: int
    description: str
    quantity: float
    unit_price: float
    tax_rate: float
    discount_percent: float
    discount_amount: float
    tax_amount: float
    line_total: float
    is_voided: bool


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount: float
    mode: str
    reference_no: Optional[str] = None
    notes: Optional[str] = None
    paid_at: Optional[datetime] = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int

    invoice_uid: Optional[str] = None
    invoice_number: Optional[str] = None

    patient_id: int
    patient: Optional[PatientMiniOut] = None   # ✅ ADD THIS
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    billing_type: Optional[str] = None

    provider_id: Optional[int] = None
    consultant_id: Optional[int] = None
    visit_no: Optional[str] = None
    status: str

    gross_total: float
    discount_total: float
    tax_total: float
    net_total: float

    amount_paid: float
    advance_adjusted: float
    balance_due: float

    header_discount_percent: float
    header_discount_amount: float
    discount_remarks: Optional[str] = None

    finalized_at: Optional[datetime] = None
    created_at: datetime

    items: List[InvoiceItemOut] = []
    payments: List[PaymentOut] = []


class AdvanceCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    amount: Decimal
    mode: Literal["cash", "card", "upi", "cheque", "neft", "rtgs", "wallet",
                  "other"]
    reference_no: Optional[str] = None
    remarks: Optional[str] = None


class ApplyAdvanceIn(BaseModel):
    advance_ids: Optional[List[int]] = None
    max_to_use: Optional[Decimal] = None
