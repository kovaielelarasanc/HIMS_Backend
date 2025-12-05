# FILE: app/schemas/lis.py
from __future__ import annotations

from typing import Optional, List
from datetime import datetime, date

from pydantic import BaseModel, ConfigDict, Field


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
    result_at: Optional[datetime] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None


class LisOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    context_type: Optional[str] = None
    context_id: Optional[int] = None
    priority: str
    status: str
    collected_at: Optional[datetime] = None
    reported_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
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


class LisPanelResultItemIn(BaseModel):
    """
    Single row result for a LabService inside a panel.
    """
    service_id: int
    result_value: str
    flag: Optional[str] = None
    comments: Optional[str] = None


class LisPanelResultSaveIn(BaseModel):
    """
    Save all results for a selected Department + Sub-department for a given order.
    """
    department_id: int  # top-level (e.g. HAEMATOLOGY)
    sub_department_id: Optional[int] = None  # child panel (e.g. CBC)
    results: List[LisPanelResultItemIn]


class LisResultLineOut(BaseModel):
    """
    What frontend shows in the result entry table rows.
    """
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    order_id: int
    service_id: int

    department_id: Optional[int] = None
    department_name: Optional[str] = None
    sub_department_id: Optional[int] = None
    sub_department_name: Optional[str] = None

    service_name: str
    unit: Optional[str] = None
    normal_range: Optional[str] = None

    result_value: Optional[str] = None
    flag: Optional[str] = None
    comments: Optional[str] = None


# ---------- Structured report for PDF generation ----------


class LabReportRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service_name: str
    result_value: Optional[str] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None
    flag: Optional[str] = None
    comments: Optional[str] = None


class LabReportSectionOut(BaseModel):
    """
    Grouped by Department + Sub-department (like your sample PDF)
    """
    model_config = ConfigDict(from_attributes=True)

    department_id: int
    department_name: str

    sub_department_id: Optional[int] = None
    sub_department_name: Optional[str] = None

    rows: List[LabReportRowOut] = Field(default_factory=list)


class LabReportOut(BaseModel):
    """
    Full report payload for one order – frontend can use this to render
    a beautiful PDF exactly like the image you shared.
    """
    model_config = ConfigDict(from_attributes=True)

    order_id: int
    lab_no: str

    patient_id: int
    patient_uhid: Optional[str] = None  # ⬅️ NEW
    patient_name: Optional[str] = None
    patient_gender: Optional[str] = None
    patient_dob: Optional[date] = None
    patient_age_text: Optional[str] = None
    patient_type: Optional[str] = None  # OP / IP etc.

    bill_no: Optional[str] = None
    # Use datetime here (matches DB); JSON will still send as string
    received_on: Optional[datetime] = None
    reported_on: Optional[datetime] = None
    referred_by: Optional[str] = None

    sections: List[LabReportSectionOut] = Field(default_factory=list)
