# FILE: app/schemas/lab_masters.py
from typing import Optional, List
from pydantic import BaseModel, ConfigDict

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


# ---------- Services ----------


class LabServiceBase(BaseModel):
    department_id: int
    name: str
    code: Optional[str] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None
    sample_type: Optional[str] = None
    method: Optional[str] = None
    comments_template: Optional[str] = None
    is_active: bool = True
    display_order: Optional[int] = None


class LabServiceCreate(LabServiceBase):
    pass


class LabServiceUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None
    sample_type: Optional[str] = None
    method: Optional[str] = None
    comments_template: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class LabServiceOut(LabServiceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class LabServiceBulkCreateItem(BaseModel):
    department_id: int
    name: str
    code: Optional[str] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None
    sample_type: Optional[str] = None
    method: Optional[str] = None
    comments_template: Optional[str] = None
    display_order: Optional[int] = None


class LabServiceBulkCreateRequest(BaseModel):
    items: List[LabServiceBulkCreateItem]
