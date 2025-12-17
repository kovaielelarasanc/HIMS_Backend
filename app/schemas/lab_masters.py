# FILE: app/schemas/lab_masters.py
from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field


# ---------- Departments ----------

class LabDepartmentBase(BaseModel):
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None
    is_active: bool = True
    display_order: Optional[int] = None


class LabDepartmentCreate(LabDepartmentBase):
    pass


class LabDepartmentUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class LabDepartmentOut(LabDepartmentBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ---------- Services (NO reference_ranges, NO rich html) ----------

class LabServiceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=50)
    unit: Optional[str] = Field(None, max_length=64)

    # plain text only, multiline allowed, but stored in VARCHAR(255)
    normal_range: Optional[str] = Field(None, max_length=255)

    comments_template: Optional[str] = None
    sample_type: Optional[str] = Field(None, max_length=128)
    method: Optional[str] = Field(None, max_length=128)
    display_order: Optional[int] = None
    is_active: Optional[bool] = True


class LabServiceCreate(LabServiceBase):
    department_id: int


class LabServiceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=50)
    unit: Optional[str] = Field(None, max_length=64)
    normal_range: Optional[str] = Field(None, max_length=255)

    comments_template: Optional[str] = None
    sample_type: Optional[str] = Field(None, max_length=128)
    method: Optional[str] = Field(None, max_length=128)
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


class LabServiceOut(LabServiceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    department_id: int


class LabServiceBulkCreateItem(BaseModel):
    department_id: int
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=50)
    unit: Optional[str] = Field(None, max_length=64)
    normal_range: Optional[str] = Field(None, max_length=255)
    sample_type: Optional[str] = Field(None, max_length=128)
    method: Optional[str] = Field(None, max_length=128)
    comments_template: Optional[str] = None
    display_order: Optional[int] = None


class LabServiceBulkCreateRequest(BaseModel):
    items: List[LabServiceBulkCreateItem]
