# FILE: app/schemas/emr_all.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal, Union
from pydantic import BaseModel, Field, ConfigDict, field_validator
import json
from pydantic.aliases import AliasChoices


def _norm_code(v: str) -> str:
    v = (v or "").strip().upper()
    v = v.replace(" ", "_")
    return v


def _as_list(v: Any) -> List[str]:
    """
    Accepts:
      - list[str]
      - json string like '["A","B"]'
      - comma separated "A,B"
      - None
    """
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [x.strip() for x in s.split(",") if x.strip()]
    return []


def _as_json_text(v: Any, default: str) -> str:
    """
    Accept dict/list -> json string
    Accept string -> must be valid json (if not empty) else default
    """
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        # validate JSON
        try:
            json.loads(s)
            return s
        except Exception:
            # allow a non-json string ONLY for sections fallback (handled separately)
            return default
    return default


# -----------------------
# MASTER
# -----------------------
class DeptCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=120)
    is_active: bool = True
    display_order: int = 1000

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = _norm_code(v)
        if not v.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric (underscore allowed)")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v


class DeptUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    is_active: Optional[bool] = None
    display_order: Optional[int] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class DeptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    is_active: bool
    display_order: int


class TypeCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=32)
    label: str = Field(..., min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=64)
    is_active: bool = True
    display_order: int = 1000

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = _norm_code(v)
        if not v.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric (underscore allowed)")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("label is required")
        return v


class TypeUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None
    display_order: Optional[int] = None

    @field_validator("label", "category")
    @classmethod
    def strip_text(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class TypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    label: str
    category: Optional[str] = None
    is_active: bool
    display_order: int


# -----------------------
# TEMPLATE LIBRARY
# -----------------------
class TemplateCreateIn(BaseModel):
    """
    Frontend can send:
      { dept, type, name, description, premium, restricted, is_default, publish, sections, schema_json }
    """
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    dept_code: str = Field(..., alias="dept")
    record_type_code: str = Field(..., alias="type")

    name: str = Field(..., min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=800)

    restricted: bool = False
    premium: bool = False
    is_default: bool = False

    publish: bool = False
    changelog: Optional[str] = Field(default=None, max_length=255)

    sections: List[str] = Field(default_factory=list)
    schema_json: Any = Field(default_factory=dict)

    @field_validator("dept_code", "record_type_code")
    @classmethod
    def validate_codes(cls, v: str) -> str:
        v = _norm_code(v)
        if not v:
            raise ValueError("code is required")
        if not v.replace("_", "").isalnum():
            raise ValueError("code must be alphanumeric (underscore allowed)")
        return v

    @field_validator("sections", mode="before")
    @classmethod
    def coerce_sections(cls, v: Any) -> List[str]:
        return _as_list(v)

    @field_validator("schema_json", mode="before")
    @classmethod
    def keep_schema_raw(cls, v: Any) -> Any:
        return v

    def to_service_dict(self) -> Dict[str, Any]:
        return {
            "dept_code": self.dept_code,
            "record_type_code": self.record_type_code,
            "name": self.name.strip(),
            "description": ((self.description.strip() if isinstance(self.description, str) else "") or None),
            "restricted": bool(self.restricted),
            "premium": bool(self.premium),
            "is_default": bool(self.is_default),
            "publish": bool(self.publish),
            "changelog": (self.changelog.strip() if isinstance(self.changelog, str) and self.changelog.strip() else None),
            "sections_json": json.dumps(self.sections, ensure_ascii=False),
            "schema_json": _as_json_text(self.schema_json, "{}"),
        }

class TemplateUpsertIn(BaseModel):
    """
    Accept both old & new field names:
      - dept_code OR dept OR department
      - record_type_code OR type OR record_type
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dept_code: str = Field(validation_alias=AliasChoices("dept_code", "dept", "department"))
    record_type_code: str = Field(validation_alias=AliasChoices("record_type_code", "type", "record_type"))

    name: str
    description: Optional[str] = None

    premium: bool = False
    is_default: bool = False
    restricted: bool = False

    publish: bool = False

    sections: List[str] = Field(default_factory=list)
    schema_json: Union[str, dict, list] = Field(default_factory=dict)

    note: Optional[str] = None

class SectionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=140)
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    group: Optional[str] = None
    is_active: bool = True
    display_order: int = 1000

class SectionOut(BaseModel):
    id: int
    code: str
    label: str
    dept_code: Optional[str] = None
    record_type_code: Optional[str] = None
    group: Optional[str] = None
    is_active: bool
    display_order: int
    class Config:
        from_attributes = True

class TemplateUpdateIn(BaseModel):
    """
    ✅ Must be partial update (your old one inherited TemplateCreateIn and broke PUT /templates/{id})
    """
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=800)
    restricted: Optional[bool] = None
    premium: Optional[bool] = None
    is_default: Optional[bool] = None

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class TemplateVersionCreateIn(BaseModel):
    # tolerate extra keys sent by UI (e.g., status, version) for forward-compat
    model_config = ConfigDict(extra="ignore")

    changelog: Optional[str] = Field(default=None, max_length=255)
    sections: List[str] = Field(default_factory=list)
    schema_json: Union[str, Dict[str, Any], List[Any]] = Field(default_factory=dict)
    publish: bool = False

    # UI convenience: when true, update the current draft version in place (no version bump)
    keep_same_version: Optional[bool] = Field(default=False)
    # UI may send status; it is ignored (server derives status from publish)
    status: Optional[str] = Field(default=None)

    @field_validator("sections", mode="before")
    @classmethod
    def coerce_sections(cls, v: Any) -> List[str]:
        return _as_list(v)

    def to_service_dict(self) -> Dict[str, Any]:
        secs = [s.strip().upper() for s in (self.sections or []) if isinstance(s, str) and s.strip()]
        # NOTE: Do not raise here; let the service validate. This prevents accidental 500s.
        return {
            "changelog": (self.changelog.strip() if isinstance(self.changelog, str) and self.changelog.strip() else None),
            "sections_json": json.dumps(secs, ensure_ascii=False),
            "schema_json": _as_json_text(self.schema_json, "{}"),
            "publish": bool(self.publish or False),
            "keep_same_version": bool(self.keep_same_version or False),
        }


class TemplatePublishIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    publish: bool = True

EncounterTypeIn = Literal["OP", "IP", "ER", "OT", "OPD", "IPD", "ED", "EMERGENCY"]
# -----------------------
# EMR RECORDS (draft/update/sign/void)
# -----------------------
class RecordCreateDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: int
    encounter_type: EncounterTypeIn
    encounter_id: str = Field(..., min_length=1, max_length=64)
    dept_code: str = Field(..., min_length=1, max_length=32)
    record_type_code: str = Field(..., min_length=1, max_length=32)

    template_id: Optional[int] = None
    template_version_id: Optional[int] = None

    title: str = Field(..., min_length=3, max_length=255)
    note: Optional[str] = Field(default=None, max_length=4000)
    confidential: bool = False

    content: Dict[str, Any] = Field(default_factory=dict)
    draft_stage: Optional[Literal["INCOMPLETE", "READY"]] = "INCOMPLETE"

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def norm_codes(cls, v: Any) -> Any:
        return _norm_code(v) if isinstance(v, str) else v

    @field_validator("encounter_id", mode="before")
    @classmethod
    def validate_encounter_id(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("encounter_id is required")
        s = str(v).strip()
        if not s:
            raise ValueError("encounter_id is required")
        return s

    @field_validator("encounter_type", mode="before")
    @classmethod
    def normalize_encounter_type(cls, v: Any) -> Any:
        if v is None:
            return v
        s = str(v).strip().upper()
        m = {
            "OPD": "OP",
            "IPD": "IP",
            "ED": "ER",
            "EMERGENCY": "ER",
        }
        return m.get(s, s)



class RecordUpdateDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=255)
    note: Optional[str] = Field(default=None, max_length=4000)
    confidential: Optional[bool] = None
    content: Optional[Dict[str, Any]] = None
    draft_stage: Optional[Literal["INCOMPLETE", "READY"]] = None


class RecordSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sign_note: Optional[str] = Field(default=None, max_length=255)


class RecordVoidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=255)


class AttachmentAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(..., min_length=1, max_length=255)
    file_key: str = Field(..., min_length=1, max_length=512)
    mime_type: Optional[str] = Field(default=None, max_length=64)
    size_bytes: Optional[int] = Field(default=None, ge=0, le=500_000_000)


# -----------------------
# QUICK ACCESS
# -----------------------
class PinToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool = True


# -----------------------
# INBOX
# -----------------------
class InboxPushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["LAB", "RIS", "EMR"] = "LAB"
    patient_id: int
    title: str = Field(..., min_length=3, max_length=255)
    encounter_type: Optional[str] = Field(default=None, max_length=16)
    encounter_id: Optional[str] = Field(default=None, max_length=64)
    source_ref_type: Optional[str] = Field(default=None, max_length=64)
    source_ref_id: Optional[str] = Field(default=None, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)


# -----------------------
# EXPORTS
# -----------------------
class ExportCreateBundleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: int
    title: str = Field(..., min_length=3, max_length=255)
    encounter_type: Optional[str] = Field(default=None, max_length=16)
    encounter_id: Optional[str] = Field(default=None, max_length=64)

    record_ids: List[int] = Field(default_factory=list)
    from_date: Optional[str] = None  # "YYYY-MM-DD" (optional)
    to_date: Optional[str] = None    # "YYYY-MM-DD" (optional)

    watermark_text: Optional[str] = Field(default=None, max_length=255)


class ExportUpdateBundleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=255)
    record_ids: Optional[List[int]] = None
    watermark_text: Optional[str] = Field(default=None, max_length=255)


class ExportShareCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in_days: Optional[int] = Field(default=7, ge=1, le=365)
    max_downloads: Optional[int] = Field(default=5, ge=1, le=1000)