from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


# ---------- ORDERS ----------
class RisOrderCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None  # 'opd' | 'ipd'
    context_id: Optional[int] = None
    ordering_user_id: Optional[int] = None
    test_id: int  # app.models.opd.RadiologyTest.id


class RisScheduleIn(BaseModel):
    scheduled_at: str  # ISO datetime


class RisReportIn(BaseModel):
    report_text: str


class RisAttachmentIn(BaseModel):
    file_url: str
    note: Optional[str] = None


class RisOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    test_id: int
    test_name: str
    test_code: str
    modality: Optional[str] = None
    status: str
    scheduled_at: Optional[str] = None
    scanned_at: Optional[str] = None
    reported_at: Optional[str] = None
    report_text: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: Optional[str] = None


# ---------- MASTERS (RadiologyTest) ----------
class RadiologyTestIn(BaseModel):
    code: str
    name: str
    price: float = 0
    modality: Optional[str] = None
    body_part: Optional[str] = None
    is_active: Optional[bool] = True


class RadiologyTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    price: float
    modality: Optional[str] = None
    body_part: Optional[str] = None
    is_active: Optional[bool] = True
