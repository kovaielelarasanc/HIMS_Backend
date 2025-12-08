# FILE: app/schemas/billing.py
from __future__ import annotations

from typing import Optional, Literal, List
from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator
from decimal import Decimal

# ---------- Provider (credit / TPA) ----------

class AutoBedChargesIn(BaseModel):
    admission_id: int
    # "daily" -> charge by day
    # "hourly" -> charge by hour (derived from daily_rate / 24)
    # "mixed" -> <= 6h hourly, > 6h daily
    mode: Literal["daily", "hourly", "mixed"] = "daily"

    # For open-ended stays (to_ts is NULL), we cut at upto_ts (default: now)
    upto_ts: Optional[datetime] = None

    # If True, skip bed-assignments that are already billed in this invoice
    skip_if_already_billed: bool = True


class AutoOtChargesIn(BaseModel):
    # OT Case for which we auto-bill procedures
    case_id: int
class ProviderBase(BaseModel):
    name: str
    code: Optional[str] = None
    provider_type: Optional[str] = None  # insurance | tpa | corporate | other
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


# ---------- Invoice & Items ----------


class InvoiceCreate(BaseModel):
    """
    Create invoice for:
    - OP Billing
    - IP Billing
    - Pharmacy / Lab / Radiology / General
    """
    patient_id: int

    # Context link, optional
    context_type: Optional[
        str] = None  # opd | ipd | pharmacy | lab | radiology | other
    context_id: Optional[int] = None

    # Logical bill type for UI
    billing_type: Optional[
        str] = None  # op_billing | ip_billing | pharmacy | lab | radiology | general

    # Optional provider / consultant / visit
    provider_id: Optional[int] = None
    consultant_id: Optional[int] = None
    visit_no: Optional[str] = None
    remarks: Optional[str] = None


class InvoiceUpdate(BaseModel):
    """
    Update header details + header-discount.
    """
    billing_type: Optional[str] = None
    provider_id: Optional[int] = None
    consultant_id: Optional[int] = None
    visit_no: Optional[str] = None
    remarks: Optional[str] = None
    header_discount_percent: Optional[float] = None
    header_discount_amount: Optional[float] = None
    discount_remarks: Optional[str] = None
    discount_authorized_by: Optional[int] = None
    status: Optional[
        str] = None  # allow status change if needed (e.g., cancel)


class AddServiceIn(BaseModel):
    """
    Add item from service master or unbilled events.
    You can either:
    - resolve description & price in backend from service_ref_id, or
    - send description & unit_price from frontend.
    """
    service_type: Literal["lab", "radiology", "ot", "pharmacy", "opd", "ipd",
                          "manual", "other"]
    service_ref_id: int = 0
    description: Optional[str] = None
    quantity: int = 1
    unit_price: Optional[float] = None
    tax_rate: float = 0.0
    discount_percent: float = 0.0
    discount_amount: float = 0.0


class ManualItemIn(BaseModel):
    """
    Pure manual line item (used for "Add manual item").
    """
    description: str
    quantity: int = 1
    unit_price: float
    tax_rate: float = 0.0
    discount_percent: float = 0.0
    discount_amount: float = 0.0
    service_type: Optional[str] = "manual"
    service_ref_id: Optional[int] = 0  # 0 or synthetic id


class UpdateItemIn(BaseModel):
    quantity: Optional[int] = None
    unit_price: Optional[float] = None
    tax_rate: Optional[float] = None
    discount_percent: Optional[float] = None
    discount_amount: Optional[float] = None
    description: Optional[str] = None


class VoidItemIn(BaseModel):
    reason: Optional[str] = None  # audit note


class BulkAddFromUnbilledIn(BaseModel):
    """
    Optional helper if you pull unbilled lab / radiology etc.
    Format: ["lab:123", "radiology:55", ...]
    """
    uids: Optional[List[str]] = None  # if None => add all unbilled


# ---------- Payments ----------


class PaymentIn(BaseModel):
    """
    Input model for a single payment row.

    - Positive amount => payment received
    - Negative amount => refund to patient
    - 0 is not allowed (meaningless)
    """
    model_config = ConfigDict(
        from_attributes=True,
        extra="ignore",  # ignore any extra keys from FE instead of 422
    )

    amount: Decimal
    mode: str
    reference_no: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("amount")
    def non_zero(cls, v: Decimal) -> Decimal:
        # allow both positive & negative, only block 0
        if v == 0:
            raise ValueError(
                "Amount must be non-zero (positive for payment, negative for refund)."
            )
        return v


class PaymentBulkIn(BaseModel):
    payments: List[PaymentIn]


# ---------- Advances ----------


class AdvanceCreate(BaseModel):
    """
    Create an advance payment (IP advance, etc.).
    """
    patient_id: int
    context_type: Optional[str] = None  # usually ipd / opd
    context_id: Optional[int] = None
    amount: float
    mode: Literal["cash", "card", "upi", "other"]
    reference_no: Optional[str] = None
    remarks: Optional[str] = None


class ApplyAdvanceIn(BaseModel):
    """
    Auto-adjust advances to invoice.
    - If max_to_use is None: use up to full outstanding.
    """
    max_to_use: Optional[float] = None


# ---------- OUT models ----------


class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    seq: int
    service_type: str
    service_ref_id: int
    description: str
    quantity: int
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


class AdvanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    amount: float
    balance_remaining: float
    mode: str
    reference_no: Optional[str] = None
    remarks: Optional[str] = None
    received_at: Optional[datetime] = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_number: Optional[str] = None
    patient_id: int
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
    balance_due: float
    previous_balance_snapshot: float
    advance_adjusted: float

    header_discount_percent: float
    header_discount_amount: float
    discount_remarks: Optional[str] = None

    finalized_at: Optional[datetime] = None
    created_at: datetime

    # Nested
    items: List[InvoiceItemOut]
    payments: List[PaymentOut]


class ProviderWithInvoicesOut(ProviderOut):
    """
    Optional, if you ever want provider + invoices.
    """
    invoices: List[InvoiceOut] = []


class PatientBillingSummaryOut(BaseModel):
    """
    For patient-wise billing history & totals.
    """
    patient_id: int
    invoices: List[InvoiceOut]
    total_billed: float
    total_tax: float
    total_discount: float
    total_advance_received: float
    total_advance_balance: float
    total_paid: float
    total_outstanding: float
