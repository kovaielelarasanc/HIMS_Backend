# app/schemas/user.py
from pydantic import BaseModel, EmailStr
from typing import Optional, List


class UserBase(BaseModel):
    name: str
    email: EmailStr
    is_active: bool = True
    # NEW FIELD (read/write)
    is_doctor: bool = False


class UserCreate(UserBase):
    password: str
    department_id: Optional[int] = None
    role_ids: List[int] = []


class UserOut(UserBase):
    id: int
    is_admin: bool
    department_id: Optional[int]
    role_ids: List[int]

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


class UserMiniOut(BaseModel):
    id: int
    name: Optional[str] = None
    email: Optional[str] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    name: str
    email: EmailStr
    department_id: Optional[int] = None
    is_active: bool = True
    is_doctor: bool = False
    password: Optional[str] = None

    # IMPORTANT: optional for old users; if None => keep existing roles
    # if [] => auto-assign default role
    role_ids: Optional[List[int]] = None
