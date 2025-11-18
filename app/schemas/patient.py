from pydantic import BaseModel, field_validator, Field, model_validator
from typing import Optional, List, Literal
from datetime import date, datetime


# ----- Addresses -----
class AddressIn(BaseModel):
    type: Optional[str] = "current"
    line1: str
    line2: Optional[str] = ""
    city: Optional[str] = ""
    state: Optional[str] = ""
    pincode: Optional[str] = ""
    country: Optional[str] = "India"


class AddressOut(AddressIn):
    id: int

    class Config:
        from_attributes = True


# ----- Patients -----
class PatientCreate(BaseModel):
    first_name: str
    last_name: Optional[str] = ""
    gender: str
    dob: Optional[date] = None
    phone: Optional[str] = ""
    email: Optional[str] = ""
    aadhar_last4: Optional[str] = ""
    address: Optional[AddressIn] = None

    @field_validator('gender')
    @classmethod
    def g_ok(cls, v):
        v = (v or "").lower()
        if v not in ("male", "female", "other"):
            raise ValueError("gender must be male/female/other")
        return v


class PatientUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    aadhar_last4: Optional[str] = None


class DocumentOut(BaseModel):
    id: int
    type: str
    filename: str
    mime: str
    size: int
    storage_path: str
    uploaded_at: datetime

    class Config:
        from_attributes = True


class ConsentIn(BaseModel):
    type: Literal["general", "surgery", "anesthesia", "data",
                  "other"] | str = Field(..., description="Consent type")
    text: str = Field(..., min_length=1, description="Consent text / remarks")

    @model_validator(mode="before")
    @classmethod
    def _compat_accept_remark(cls, v):
        # Accept "remark" from older clients too and map to "text"
        if isinstance(v, dict):
            if "text" not in v and "remark" in v:
                v["text"] = v["remark"]
        return v


class ConsentOut(BaseModel):
    id: int
    patient_id: int
    type: str
    text: str
    captured_at: Optional[datetime]

    class Config:
        from_attributes = True  # Pydantic v2 equivalent of orm_mode=True


class PatientOut(BaseModel):
    id: int
    uhid: str
    abha_number: Optional[str]
    aadhar_last4: Optional[str]
    first_name: str
    last_name: Optional[str]
    gender: str
    dob: Optional[date]
    phone: Optional[str]
    email: Optional[str]
    is_active: bool
    addresses: List[AddressOut] = []

    class Config:
        from_attributes = True
