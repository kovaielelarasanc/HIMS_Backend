# app/schemas/emr.py
from pydantic import BaseModel, ConfigDict
from typing import Optional


class EmrPatientMini(BaseModel):
    id: int
    uhid: str
    first_name: str
    last_name: Optional[str] = None
    prefix: Optional[str] = None
    phone: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[str] = None
    age_short_text: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EmrOpdVisitRow(BaseModel):
    visit_id: int
    episode_id: str
    visit_at: str

    department_name: str
    doctor_name: str

    department_id: Optional[int] = None
    doctor_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
