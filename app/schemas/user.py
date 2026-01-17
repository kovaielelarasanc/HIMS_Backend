# app/schemas/user.py
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    login_id: str
    name: str
    email: Optional[str] = None
    email_verified: bool
    two_fa_enabled: bool
    multi_login_enabled: bool

    is_active: bool
    is_admin: bool
    is_doctor: bool
    department_id: Optional[int] = None

    # ✅ NEW
    doctor_qualification: Optional[str] = None
    doctor_registration_no: Optional[str] = None

    role_ids: List[int] = []

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    # ✅ per requirement: only name + password mandatory
    name: str = Field(..., min_length=2, max_length=120)
    password: str = Field(..., min_length=6, max_length=128)

    # optional admin inputs
    email: Optional[str] = None
    is_active: bool = True

    is_doctor: bool = False
    two_fa_enabled: bool = False
    multi_login_enabled: bool = True
    department_id: Optional[int] = None

    # ✅ NEW (optional)
    doctor_qualification: Optional[str] = Field(default=None, max_length=255)
    doctor_registration_no: Optional[str] = Field(default=None, max_length=64)

    role_ids: Optional[List[int]] = None


class UserUpdate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: Optional[str] = None

    is_active: bool = True
    is_doctor: bool = False
    department_id: Optional[int] = None

    # ✅ NEW (optional)
    doctor_qualification: Optional[str] = Field(default=None, max_length=255)
    doctor_registration_no: Optional[str] = Field(default=None, max_length=64)

    # toggles
    two_fa_enabled: bool = False
    multi_login_enabled: bool = True

    # password change (admin)
    password: Optional[str] = None

    # roles behavior:
    # None => keep existing
    # []   => clear then default role
    # [..] => set roles
    role_ids: Optional[List[int]] = None


class UserSaveResponse(BaseModel):
    user: UserOut
    needs_email_verify: bool = False
    otp_sent_to: Optional[str] = None
    otp_purpose: Optional[str] = None


class VerifyEmailOtpIn(BaseModel):
    otp: str = Field(..., min_length=6, max_length=6)


class UserMiniOut(BaseModel):
    id: int
    name: Optional[str] = None
    email: Optional[str] = None

    class Config:
        from_attributes = True


class UserLite(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    roles: List[str] = []
    is_doctor: bool = False

    class Config:
        from_attributes = True


class DoctorOut(BaseModel):
    id: int
    login_id: str
    name: str
    email: Optional[str] = None
    is_active: bool
    department_id: Optional[int] = None

    # ✅ NEW
    doctor_qualification: Optional[str] = None
    doctor_registration_no: Optional[str] = None

    class Config:
        from_attributes = True


class DoctorListResponse(BaseModel):
    items: List[DoctorOut]
    page: int
    page_size: int
    total: int
