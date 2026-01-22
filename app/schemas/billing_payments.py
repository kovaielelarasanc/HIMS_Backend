# FILE: app/schemas/billing_payments.py
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, validator

from app.models.billing import PayMode, PayerType


class PaymentAllocationIn(BaseModel):
    invoice_id: int
    amount: Decimal

    @validator("invoice_id")
    def _inv_id(cls, v):
        if int(v) <= 0:
            raise ValueError("invoice_id must be positive")
        return int(v)

    @validator("amount")
    def _amt(cls, v):
        if Decimal(str(v or 0)) <= 0:
            raise ValueError("allocation amount must be > 0")
        return Decimal(str(v))


class MultiInvoicePaymentIn(BaseModel):
    amount: Optional[Decimal] = None
    mode: PayMode = PayMode.CASH
    txn_ref: Optional[str] = None
    notes: Optional[str] = None

    payer_type: PayerType = PayerType.PATIENT
    payer_id: Optional[int] = None

    allocations: List[PaymentAllocationIn]

    @validator("allocations")
    def _allocs(cls, v):
        if not v or len(v) == 0:
            raise ValueError("allocations required")
        return v


class PaymentAllocationOut(BaseModel):
    invoice_id: int
    amount: Decimal
    payer_bucket: str


class PaymentOut(BaseModel):
    id: int
    receipt_number: Optional[str]
    amount: Decimal
    mode: str
    status: str
    allocations: List[PaymentAllocationOut]
