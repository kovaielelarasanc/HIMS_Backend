# app/schemas/auth.py
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal


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
    tenant_code: str = Field(..., min_length=2, max_length=50)
    login_id: str = Field(..., min_length=6, max_length=6)
    password: str = Field(..., min_length=6, max_length=128)


class OtpVerifyIn(BaseModel):
    tenant_code: str = Field(..., min_length=2, max_length=50)
    login_id: str = Field(..., min_length=6, max_length=6)
    otp: str = Field(..., alias="otp_code") 

    # ✅ allows login flow to verify either login OTP or email_verify OTP
    purpose: Optional[Literal["login", "email_verify"]] = "login"

    class Config:
        allow_population_by_field_name = True
        allow_population_by_alias = True

class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
