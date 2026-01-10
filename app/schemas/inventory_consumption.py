from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, conlist


class EligibleItemOut(BaseModel):
    item_id: int
    code: str
    name: str
    item_type: str
    unit: str
    on_hand_qty: Decimal


class PatientConsumeLineIn(BaseModel):
    item_id: int
    qty: Decimal = Field(..., gt=0)
    batch_id: Optional[int] = None
    remark: str = ""


class PatientConsumeIn(BaseModel):
    location_id: int
    patient_id: int
    visit_id: Optional[int] = None
    doctor_id: Optional[int] = None
    notes: str = ""
    items: conlist(PatientConsumeLineIn, min_length=1)


class BatchAllocationOut(BaseModel):
    batch_id: Optional[int]
    qty: Decimal


class PatientConsumeLineOut(BaseModel):
    item_id: int
    requested_qty: Decimal
    allocations: List[BatchAllocationOut]


class PatientConsumeOut(BaseModel):
    consumption_id: int
    consumption_number: str
    posted_at: datetime
    location_id: int
    patient_id: int
    visit_id: Optional[int] = None
    doctor_id: Optional[int] = None
    notes: str = ""
    items: List[PatientConsumeLineOut]


class ConsumptionListRowOut(BaseModel):
    consumption_id: int
    consumption_number: str
    posted_at: datetime
    location_id: int
    patient_id: Optional[int]
    visit_id: Optional[int]
    doctor_id: Optional[int]
    user_id: Optional[int]
    total_lines: int
    total_qty: Decimal


class BulkReconcileLineIn(BaseModel):
    item_id: int
    closing_qty: Decimal = Field(..., ge=0)
    batch_id: Optional[int] = None
    remark: str = ""


class BulkReconcileIn(BaseModel):
    location_id: int
    on_date: Optional[date] = None
    notes: str = ""
    lines: conlist(BulkReconcileLineIn, min_length=1)


class BulkReconcileLineOut(BaseModel):
    item_id: int
    before_qty: Decimal
    closing_qty: Decimal
    auto_consumed_qty: Decimal  # positive means consumed
    adjusted_in_qty: Decimal    # positive means added (found extra)
    allocations: List[BatchAllocationOut]


class BulkReconcileOut(BaseModel):
    reconcile_id: int
    reconcile_number: str
    posted_at: datetime
    location_id: int
    on_date: date
    notes: str = ""
    lines: List[BulkReconcileLineOut]
