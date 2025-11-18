from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, ConfigDict


class OtOrderCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None  # opd | ipd
    context_id: Optional[int] = None
    surgery_master_id: Optional[int] = None
    surgery_name: Optional[str] = None  # required if no master
    estimated_cost: Optional[float] = 0.0
    surgeon_id: Optional[int] = None
    anaesthetist_id: Optional[int] = None
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    preop_notes: Optional[str] = None


class OtOrderScheduleIn(BaseModel):
    scheduled_start: str
    scheduled_end: Optional[str] = None


class OtOrderStatusIn(BaseModel):
    status: str  # planned/scheduled/in_progress/completed/cancelled


class OtAttachmentIn(BaseModel):
    file_url: str
    note: Optional[str] = None


class OtOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    surgery_master_id: Optional[int] = None
    surgery_code: Optional[str] = None
    surgery_name: str
    estimated_cost: float
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    actual_start: Optional[str] = None
    actual_end: Optional[str] = None
    status: str
    surgeon_id: Optional[int] = None
    anaesthetist_id: Optional[int] = None
    preop_notes: Optional[str] = None
    postop_notes: Optional[str] = None
