# app/schemas/lab_integration.py
from __future__ import annotations

from datetime import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field, field_validator


Protocol = Literal["HL7_MLLP", "HL7_HTTP", "ASTM_HTTP", "RAW_HTTP", "MISPA_VIVA_HTTP"]


class DeviceCreate(BaseModel):
    tenant_code: str = Field(..., min_length=2, max_length=32)
    name: str = Field(..., min_length=1, max_length=120)
    protocol: Protocol
    sending_facility_code: str = Field(..., min_length=1, max_length=64)
    enabled: bool = True
    allowed_remote_ips: Optional[List[str]] = None

    @field_validator("tenant_code", "sending_facility_code")
    @classmethod
    def _upper_trim(cls, v: str) -> str:
        return (v or "").strip().upper()

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        return (v or "").strip()


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    protocol: Optional[Protocol] = None
    sending_facility_code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[bool] = None
    allowed_remote_ips: Optional[List[str]] = None

    @field_validator("sending_facility_code")
    @classmethod
    def _upper_trim_fac(cls, v: Optional[str]) -> Optional[str]:
        return (v.strip().upper() if v else v)

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: Optional[str]) -> Optional[str]:
        return (v.strip() if v else v)


class DeviceOut(BaseModel):
    id: int
    tenant_code: str
    name: str
    protocol: str
    sending_facility_code: str
    enabled: bool
    allowed_remote_ips: Optional[List[str]] = None
    last_seen_at: Optional[str] = None
    last_error_at: Optional[str] = None
    last_error: Optional[str] = None


class MappingCreate(BaseModel):
    tenant_code: str = Field(..., min_length=2, max_length=32)
    source_device_id: int = Field(..., gt=0)
    external_code: str = Field(..., min_length=1, max_length=80)
    internal_test_id: int = Field(..., gt=0)

    @field_validator("tenant_code")
    @classmethod
    def _upper_tenant(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("external_code")
    @classmethod
    def _trim_code(cls, v: str) -> str:
        return v.strip()


class MappingOut(BaseModel):
    id: int
    tenant_code: str
    source_device_id: int
    external_code: str
    internal_test_id: int
    active: bool


class IngestPayload(BaseModel):
    facility_code: str = Field(..., min_length=1, max_length=64)
    payload: str = Field(..., min_length=1)
    # "HL7" | "ASTM" | "MISPA_VIVA" | "RAW"
    kind: str = Field(default="AUTO", max_length=32)

    @field_validator("facility_code")
    @classmethod
    def _upper_fac(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("kind")
    @classmethod
    def _upper_kind(cls, v: str) -> str:
        return (v or "AUTO").strip().upper()


class StatsOut(BaseModel):
    received: int = 0
    parsed: int = 0
    processed: int = 0
    error: int = 0
    duplicate: int = 0
    last_24h: int = 0
