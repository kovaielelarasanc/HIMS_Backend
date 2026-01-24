# FILE: app/schemas/emr_template_library.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

CODE_32 = 32
LABEL_140 = 140


def _norm_code(v: str) -> str:
    return (v or "").strip().upper().replace(" ", "_")


def _clean_opt(v: Optional[str], max_len: int) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:max_len]


def _ensure_json_text(v: Any, default: str = "{}") -> str:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        # validate (raise ValueError on invalid JSON)
        json.loads(s)
        return s
    return default


# -----------------------
# Section Library
# -----------------------
class SectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(..., min_length=1, max_length=CODE_32)
    label: str = Field(..., min_length=1, max_length=LABEL_140)

    dept_code: Optional[str] = Field(default=None, max_length=CODE_32)
    record_type_code: Optional[str] = Field(default=None, max_length=CODE_32)
    group: Optional[str] = Field(default=None, max_length=50)

    is_active: bool = True
    display_order: int = 1000

    @field_validator("code")
    @classmethod
    def v_code(cls, v: str) -> str:
        v = _norm_code(v)
        if not v.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric (underscore allowed)")
        return v

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def v_codes(cls, v: Any) -> Any:
        return _norm_code(v) if isinstance(v, str) and v.strip() else v

    def to_db_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label.strip(),
            "dept_code": self.dept_code,
            "record_type_code": self.record_type_code,
            "group": _clean_opt(self.group, 50),
            "is_active": bool(self.is_active),
            "display_order": int(self.display_order or 1000),
        }


class SectionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: Optional[str] = Field(default=None, min_length=1, max_length=LABEL_140)
    dept_code: Optional[str] = Field(default=None, max_length=CODE_32)
    record_type_code: Optional[str] = Field(default=None, max_length=CODE_32)
    group: Optional[str] = Field(default=None, max_length=50)
    is_active: Optional[bool] = None
    display_order: Optional[int] = None

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def v_codes(cls, v: Any) -> Any:
        return _norm_code(v) if isinstance(v, str) and v.strip() else v


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    label: str
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    group: Optional[str] = None
    is_active: bool
    display_order: int


# -----------------------
# Block Library
# -----------------------
class BlockCreateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(..., min_length=1, max_length=CODE_32)
    label: str = Field(..., min_length=1, max_length=LABEL_140)
    description: Optional[str] = Field(default=None, max_length=800)

    dept_code: Optional[str] = Field(default=None, max_length=CODE_32)
    record_type_code: Optional[str] = Field(default=None, max_length=CODE_32)

    group: Optional[str] = Field(default=None, max_length=50)
    schema_json: Union[Dict[str, Any], List[Any], str] = Field(default_factory=dict)

    is_active: bool = True
    display_order: int = 1000

    @field_validator("code")
    @classmethod
    def v_code(cls, v: str) -> str:
        v = _norm_code(v)
        if not v.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric (underscore allowed)")
        return v

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def v_codes(cls, v: Any) -> Any:
        return _norm_code(v) if isinstance(v, str) and v.strip() else v

    def to_db_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label.strip(),
            "description": _clean_opt(self.description, 800),
            "dept_code": self.dept_code,
            "record_type_code": self.record_type_code,
            "group": _clean_opt(self.group, 50),
            "schema_json": _ensure_json_text(self.schema_json, "{}"),
            "is_active": bool(self.is_active),
            "display_order": int(self.display_order or 1000),
        }


class BlockUpdateIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: Optional[str] = Field(default=None, min_length=1, max_length=LABEL_140)
    description: Optional[str] = Field(default=None, max_length=800)
    dept_code: Optional[str] = Field(default=None, max_length=CODE_32)
    record_type_code: Optional[str] = Field(default=None, max_length=CODE_32)
    group: Optional[str] = Field(default=None, max_length=50)
    schema_json: Optional[Any] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def v_codes(cls, v: Any) -> Any:
        return _norm_code(v) if isinstance(v, str) and v.strip() else v


class BlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    label: str
    description: Optional[str] = None
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    group: Optional[str] = None
    schema_json: str
    is_active: bool
    display_order: int


# -----------------------
# Shared: schema validate input
# -----------------------
class TemplateSchemaValidateIn(BaseModel):
    """Used by /emr/templates/validate and /emr/templates/suggest"""
    model_config = ConfigDict(extra="ignore")

    dept_code: str = Field(..., min_length=1, max_length=CODE_32)
    record_type_code: str = Field(..., min_length=1, max_length=CODE_32)

    sections: List[str] = Field(default_factory=list)
    schema_json: Any = Field(default_factory=dict)

    @field_validator("dept_code", "record_type_code")
    @classmethod
    def v_code(cls, v: str) -> str:
        return _norm_code(v)
