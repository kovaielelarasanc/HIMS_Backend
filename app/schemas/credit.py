# FILE: app/schemas/credit.py
from typing import Optional
from pydantic import BaseModel, ConfigDict


class CreditProviderOut(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    code: Optional[str] = None
    type: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class TpaOut(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    code: Optional[str] = None
    provider_name: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class CreditPlanOut(BaseModel):
    id: int
    name: str
    display_name: Optional[str] = None
    code: Optional[str] = None
    provider_name: Optional[str] = None
    tpa_name: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)
