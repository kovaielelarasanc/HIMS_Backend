from __future__ import annotations

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


# ---------- CREATE ADVANCE ----------
class AdvanceCreate(BaseModel):
    patient_id: int
    amount: float = Field(..., gt=0)
    mode: str
    reference_no: Optional[str] = None
    remarks: Optional[str] = None

    # Optional context
    context_type: Optional[
        str] = None  # opd | ipd | ot | pharmacy | lab | radiology | general
    context_id: Optional[int] = None


# ---------- ADVANCE OUT ----------
class AdvanceOut(BaseModel):
    id: int
    patient_id: int
    amount: float
    balance_remaining: float
    mode: str
    reference_no: Optional[str]
    remarks: Optional[str]
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    received_at: datetime
    is_voided: bool

    used_invoices: List[AdvanceAdjustmentMiniOut] = []

    class Config:
        from_attributes = True


# ---------- APPLY ADVANCE ----------
class ApplyAdvanceIn(BaseModel):
    amount: float = Field(..., gt=0)


# ---------- PATIENT ADVANCE SUMMARY ----------
class PatientAdvanceSummary(BaseModel):
    patient_id: int
    total_advance: float
    used_advance: float
    available_advance: float



class AdvanceAdjustmentMiniOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_id: int
    amount_applied: Decimal
    applied_at: datetime

    # invoice mini fields
    invoice_number: Optional[str] = None
    invoice_uid: Optional[str] = None
    billing_type: Optional[str] = None
    status: Optional[str] = None
    net_total: Optional[Decimal] = None
    balance_due: Optional[Decimal] = None



