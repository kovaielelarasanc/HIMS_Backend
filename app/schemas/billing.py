from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from app.models.billing import PayerType, PayMode, AdvanceType


class BillingCaseOut(BaseModel):
    id: int
    case_number: str
    patient_id: int
    encounter_type: str
    encounter_id: int
    status: str

    class Config:
        from_attributes = True


class BillingInvoiceOut(BaseModel):
    id: int
    invoice_number: str
    invoice_type: str
    status: str
    grand_total: float
    module: Optional[str] = None
    module_label: Optional[str] = None

    class Config:
        from_attributes = True


class ManualLineIn(BaseModel):
    billing_case_id: int
    item_type: Optional[str] = None  # LAB_TEST/RAD_TEST/OT_PROC/...
    item_id: Optional[int] = None
    description: Optional[str] = None

    qty: float = 1
    unit_price: float = 0
    gst_rate: float = 0
    discount_amount: float = 0

    doctor_id: Optional[int] = None
    revenue_head_id: Optional[int] = None
    cost_center_id: Optional[int] = None

    manual_reason: Optional[str] = "Manual Add"


class PaymentIn(BaseModel):
    invoice_id: Optional[int] = None
    payer_type: PayerType = PayerType.PATIENT
    payer_id: Optional[int] = None
    mode: PayMode = PayMode.CASH
    amount: float
    txn_ref: Optional[str] = None


class AdvanceIn(BaseModel):
    entry_type: AdvanceType = AdvanceType.ADVANCE
    mode: PayMode = PayMode.CASH
    amount: float
    txn_ref: Optional[str] = None
    remarks: Optional[str] = None
