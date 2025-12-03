from typing import Optional
from pydantic import BaseModel, ConfigDict


class OtSurgeryMasterIn(BaseModel):
    code: str
    name: str
    default_cost: Optional[float] = 0
    hourly_cost: Optional[float] = 0  # NEW
    active: Optional[bool] = True


class OtSurgeryMasterOut(BaseModel):
    id: int
    code: str
    name: str
    default_cost: float
    hourly_cost: float  # NEW
    active: bool

    model_config = ConfigDict(from_attributes=True)
