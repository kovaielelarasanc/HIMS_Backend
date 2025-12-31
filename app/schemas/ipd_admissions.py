# app/schemas/ipd_admissions.py
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class IpdAdmissionListItem(BaseModel):
    id: int
    admission_code: str

    patient_id: int
    patient_name: str
    uhid: str

    doctor_user_id: Optional[int] = None
    doctor_name: str

    ward_name: Optional[str] = None
    room_number: Optional[str] = None
    bed_code: Optional[str] = None

    status: str
    admitted_at: Optional[datetime] = None
    discharge_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class IpdAdmissionListOut(BaseModel):
    items: List[IpdAdmissionListItem]
    total: int
    limit: int
    offset: int
