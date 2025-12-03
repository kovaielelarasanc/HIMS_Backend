# app/schemas/auth.py
from pydantic import BaseModel, EmailStr
from typing import Optional


class RegisterAdminIn(BaseModel):
    # Tenant / Hospital info
    tenant_name: str
    tenant_code: Optional[str] = None  # if None, backend will derive from name
    hospital_address: Optional[str] = None
    contact_person: str
    contact_phone: Optional[str] = None

    # Subscription / AMC
    subscription_plan: Optional[str] = None
    amc_percent: int = 30

    # Admin user info
    admin_name: str
    email: EmailStr
    password: str
    confirm_password: str


class LoginIn(BaseModel):
    tenant_code: str
    email: EmailStr
    password: str


class OtpVerifyIn(BaseModel):
    tenant_code: str
    email: EmailStr
    otp: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
