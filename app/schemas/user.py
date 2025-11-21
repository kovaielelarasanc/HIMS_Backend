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
