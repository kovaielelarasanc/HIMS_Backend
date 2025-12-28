# app/schemas/ot_master.py
from __future__ import annotations
from typing import Optional, Literal, List
from pydantic import BaseModel, ConfigDict, Field


# =========================
# Surgery Master (legacy)
# =========================
class OtSurgeryMasterIn(BaseModel):
    code: str
    name: str
    default_cost: Optional[float] = 0
    hourly_cost: Optional[float] = 0
    description: Optional[str] = ""
    active: Optional[bool] = True


class OtSurgeryMasterUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    default_cost: Optional[float] = None
    hourly_cost: Optional[float] = None
    description: Optional[str] = None
    active: Optional[bool] = None


class OtSurgeryMasterOut(BaseModel):
    id: int
    code: str
    name: str
    default_cost: float
    hourly_cost: float
    description: str
    active: bool

    model_config = ConfigDict(from_attributes=True)


class OtSurgeryMasterPageOut(BaseModel):
    items: List[OtSurgeryMasterOut]
    total: int
    page: int
    page_size: int


# -------------------------
# OT THEATER MASTER
# -------------------------
class OtTheaterMasterCreate(BaseModel):
    code: str
    name: str
    cost_per_hour: float = 0
    description: Optional[str] = ""
    is_active: bool = True


class OtTheaterMasterUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    cost_per_hour: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtTheaterMasterOut(BaseModel):
    id: int
    code: str
    name: str
    cost_per_hour: float
    description: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# OT INSTRUMENT MASTER
# -------------------------
class OtInstrumentMasterCreate(BaseModel):
    code: str
    name: str
    available_qty: int = Field(0, ge=0)
    cost_per_qty: float = 0
    uom: str = "Nos"
    description: Optional[str] = ""
    is_active: bool = True


class OtInstrumentMasterUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    available_qty: Optional[int] = Field(None, ge=0)
    cost_per_qty: Optional[float] = None
    uom: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtInstrumentMasterOut(BaseModel):
    id: int
    code: str
    name: str
    available_qty: int
    cost_per_qty: float
    uom: str
    description: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# OT DEVICES (AIRWAY / MONITOR)
# -------------------------
OtDeviceCategory = Literal["AIRWAY", "MONITOR"]


class OtDeviceMasterCreate(BaseModel):
    category: OtDeviceCategory
    code: str
    name: str
    cost: float = 0
    description: Optional[str] = ""
    is_active: bool = True


class OtDeviceMasterUpdate(BaseModel):
    category: Optional[OtDeviceCategory] = None
    code: Optional[str] = None
    name: Optional[str] = None
    cost: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtDeviceMasterOut(BaseModel):
    id: int
    category: OtDeviceCategory
    code: str
    name: str
    cost: float
    description: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)
