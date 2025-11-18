# app/schemas/lis.py
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class LisOrderItemIn(BaseModel):
    test_id: int  # app.models.opd.LabTest.id


class LisOrderCreate(BaseModel):
    patient_id: int
    context_type: Optional[str] = None  # opd | ipd
    context_id: Optional[int] = None  # visit_id | admission_id
    ordering_user_id: Optional[int] = None
    priority: Optional[str] = "routine"
    items: List[LisOrderItemIn]


class LisCollectIn(BaseModel):
    barcode: str


class LisResultIn(BaseModel):
    item_id: int
    result_value: str
    is_critical: bool = False


class LisAttachmentIn(BaseModel):
    item_id: int
    file_url: str
    note: Optional[str] = None


# ---- OUT models ----
class LisOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    test_id: int
    test_name: str
    test_code: str
    status: str
    sample_barcode: Optional[str] = None
    result_value: Optional[str] = None
    is_critical: bool = False
    result_at: Optional[str] = None


class LisOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    priority: str
    status: str
    collected_at: Optional[str] = None
    reported_at: Optional[str] = None
    items: List[LisOrderItemOut]


# Optional (if you later want analyte-level entry)
class AnalyteResultIn(BaseModel):
    analyte_id: int
    value: str
    unit: Optional[str] = None
    flag: Optional[str] = None  # H/L/N
    model_config = ConfigDict(from_attributes=True)


class ValidateItemIn(BaseModel):
    results: List[AnalyteResultIn]
    model_config = ConfigDict(from_attributes=True)
