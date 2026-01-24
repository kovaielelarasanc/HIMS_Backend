# FILE: app/schemas/emr_meta.py
from __future__ import annotations
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, ConfigDict


class PhaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    label: str
    hint: Optional[str] = None
    display_order: int = 1000
    is_active: bool = True


class PresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    label: str
    description: Optional[str] = None
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    sections: List[str] = Field(default_factory=list)
    schema: Optional[Dict[str, Any]] = None
    display_order: int = 1000
    is_active: bool = True


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    label: str
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    phase_code: Optional[str] = None
    group: Optional[str] = None
    keywords: Optional[str] = None
    is_active: bool = True
    display_order: int = 1000


class SectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    code: Optional[str] = None
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    phase_code: Optional[str] = None
    group: Optional[str] = "CUSTOM"
    keywords: Optional[str] = None
    is_active: bool = True
    display_order: int = 1000


class MetaBootstrapOut(BaseModel):
    departments: List[Dict[str, Any]] = Field(default_factory=list)
    record_types: List[Dict[str, Any]] = Field(default_factory=list)
    phases: List[PhaseOut] = Field(default_factory=list)
    presets: List[PresetOut] = Field(default_factory=list)
