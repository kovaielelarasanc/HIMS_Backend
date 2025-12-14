from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, Field, ConfigDict


class WalletDepositIn(BaseModel):
    patient_id: int
    amount: Decimal = Field(..., gt=0)
    mode: Optional[str] = "cash"
    reference_no: Optional[str] = None
    notes: Optional[str] = None


class WalletTxnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    txn_type: str
    amount: Decimal
    mode: Optional[str] = None
    reference_no: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    created_by: Optional[int] = None


class WalletAllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    invoice_id: int
    amount: Decimal
    notes: Optional[str] = None
    allocated_at: datetime
    allocated_by: Optional[int] = None


class WalletSummaryOut(BaseModel):
    patient_id: int
    total_deposit: Decimal
    total_allocated: Decimal
    total_refund: Decimal
    available_balance: Decimal
    recent_txns: List[WalletTxnOut] = []
    recent_allocations: List[WalletAllocationOut] = []


class ApplyWalletToInvoiceIn(BaseModel):
    amount: Decimal = Field(..., gt=0)
    notes: Optional[str] = None
