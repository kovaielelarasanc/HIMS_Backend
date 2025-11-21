# backend/app/schemas/patient_masters.py
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, ConfigDict

# ---------- Shared small outputs ----------


class DoctorRefOut(BaseModel):
    id: int
    name: str
    department_name: Optional[str] = None


class ReferenceSourceOut(BaseModel):
    code: str
    label: str


# ---------- Payer ----------


class PayerBase(BaseModel):
    code: str
    name: str
    payer_type: Optional[str] = None  # insurance / corporate / govt / other
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None


class PayerCreate(PayerBase):
    pass


class PayerUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    payer_type: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None


class PayerOut(PayerBase):
    id: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- TPA ----------


class TpaBase(BaseModel):
    code: str
    name: str
    payer_id: Optional[int] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class TpaCreate(TpaBase):
    pass


class TpaUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    payer_id: Optional[int] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class TpaOut(TpaBase):
    id: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- Credit Plan ----------


class CreditPlanBase(BaseModel):
    code: str
    name: str
    payer_id: Optional[int] = None
    tpa_id: Optional[int] = None
    description: Optional[str] = None


class CreditPlanCreate(CreditPlanBase):
    pass


class CreditPlanUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    payer_id: Optional[int] = None
    tpa_id: Optional[int] = None
    description: Optional[str] = None


class CreditPlanOut(CreditPlanBase):
    id: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)
