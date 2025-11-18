from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, ConfigDict


class OtSurgeryMasterIn(BaseModel):
    code: str
    name: str
    default_cost: float
    description: Optional[str] = None
    active: bool = True


class OtSurgeryMasterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    default_cost: float
    description: Optional[str] = None
    active: bool
