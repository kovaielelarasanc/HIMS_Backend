# backend/app/schemas/tenant.py
from pydantic import BaseModel, EmailStr
from typing import Optional


class TenantBase(BaseModel):
    name: str
    code: str
    contact_email: Optional[EmailStr] = None
    is_active: bool = True


class TenantCreate(BaseModel):
    name: str
    code: Optional[str] = None  # allow auto-generate
    contact_email: Optional[EmailStr] = None


class TenantOut(TenantBase):
    id: int

    class Config:
        from_attributes = True
