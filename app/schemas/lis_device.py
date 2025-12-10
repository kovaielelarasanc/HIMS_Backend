# FILE: app/schemas/lis_device.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from app.models.lis_device import (
    DeviceConnectionType,
    DeviceProtocolType,
    DeviceResultStatus,
)


# ------------ Devices ------------

class LabDeviceBase(BaseModel):
    name: str = Field(..., max_length=255)
    code: str = Field(..., max_length=50)
    model: Optional[str] = Field(None, max_length=255)
    manufacturer: Optional[str] = Field(None, max_length=255)
    location: Optional[str] = Field(None, max_length=255)

    connection_type: DeviceConnectionType
    protocol: DeviceProtocolType = DeviceProtocolType.ASTM

    is_active: bool = True


class LabDeviceCreate(LabDeviceBase):
    # plain API key only for create, will be hashed
    api_key: str = Field(..., min_length=16, max_length=128)


class LabDeviceUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    location: Optional[str] = None

    connection_type: Optional[DeviceConnectionType] = None
    protocol: Optional[DeviceProtocolType] = None
    is_active: Optional[bool] = None

    # optional API key rotate
    api_key: Optional[str] = Field(None, min_length=16, max_length=128)


class LabDeviceOut(LabDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ------------ Channels ------------

class LabDeviceChannelBase(BaseModel):
    external_test_code: str = Field(..., max_length=64)
    external_test_name: Optional[str] = Field(None, max_length=255)
    lis_test_id: Optional[int] = None
    default_unit: Optional[str] = Field(None, max_length=64)
    reference_range: Optional[str] = Field(None, max_length=255)
    is_active: bool = True


class LabDeviceChannelCreate(LabDeviceChannelBase):
    device_id: int


class LabDeviceChannelUpdate(BaseModel):
    external_test_name: Optional[str] = None
    lis_test_id: Optional[int] = None
    default_unit: Optional[str] = None
    reference_range: Optional[str] = None
    is_active: Optional[bool] = None


class LabDeviceChannelOut(LabDeviceChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    created_at: datetime
    updated_at: datetime


# ------------ Logs & Results (UI read) ------------

class LabDeviceMessageLogOut(BaseModel):
    """
    Matches LabDeviceMessageLog model.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: Optional[int]
    direction: str  # "in" | "out"
    raw_payload: str
    created_at: datetime


class LabDeviceResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: Optional[int]
    sample_id: str
    external_test_code: str
    external_test_name: Optional[str]
    result_value: str
    unit: Optional[str]
    flag: Optional[str]
    reference_range: Optional[str]
    lis_order_id: Optional[int]
    lis_test_id: Optional[int]
    patient_id: Optional[int]
    status: DeviceResultStatus
    error_message: Optional[str]
    measured_at: Optional[datetime]
    received_at: datetime
    created_at: datetime
    updated_at: datetime


# ------------ Incoming payload from connector ------------

class DeviceResultItemIn(BaseModel):
    """
    Payload format from local connector (per test/parameter).
    """

    sample_id: str = Field(..., max_length=64)
    external_test_code: str = Field(..., max_length=64)
    external_test_name: Optional[str] = Field(None, max_length=255)
    result_value: str = Field(..., max_length=255)
    unit: Optional[str] = Field(None, max_length=64)
    flag: Optional[str] = Field(None, max_length=16)
    reference_range: Optional[str] = Field(None, max_length=255)
    measured_at: Optional[datetime] = None


class DeviceResultBatchIn(BaseModel):
    """
    Connector posts a batch of results for one device.
    """

    device_code: str = Field(..., description="Code configured for LabDevice")
    results: list[DeviceResultItemIn]
    raw_payload: Optional[str] = Field(
        None,
        description="Optional raw ASTM/HL7 message to store in logs for audit",
    )
