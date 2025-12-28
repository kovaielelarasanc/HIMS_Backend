# FILE: app/schemas/ipd_nursing.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# -------------------------
# Minimal user object
# -------------------------
class UserMiniOut(BaseModel):
    id: int
    name: str
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# =========================
# Dressing
# =========================
class WoundSize(BaseModel):
    length_cm: Optional[float] = Field(None, ge=0)
    width_cm: Optional[float] = Field(None, ge=0)
    depth_cm: Optional[float] = Field(None, ge=0)


class DressingAssessment(BaseModel):
    wound_type: Optional[str] = None     # surgical/pressure/diabetic/trauma etc
    site_detail: Optional[str] = None
    size: Optional[WoundSize] = None
    edges: Optional[str] = None
    tissue: Optional[str] = None         # granulation/slough/eschar
    exudate_amount: Optional[str] = None # none/low/moderate/high
    exudate_type: Optional[str] = None   # serous/purulent/bloody
    odor: Optional[str] = None
    surrounding_skin: Optional[str] = None
    infection_signs: Optional[str] = None


class DressingMaterial(BaseModel):
    name: str
    qty: Optional[float] = None
    unit: Optional[str] = None


class DressingProcedure(BaseModel):
    old_dressing_condition: Optional[str] = None
    cleaning_solution: Optional[str] = None
    dressing_applied: Optional[str] = None
    secured_with: Optional[str] = None
    materials: List[DressingMaterial] = Field(default_factory=list)


class AsepsisChecklist(BaseModel):
    hand_hygiene: bool = True
    sterile_gloves: bool = True
    sterile_field: bool = True
    mask: bool = False
    ppe_notes: Optional[str] = None


class DressingCreate(BaseModel):
    performed_at: Optional[datetime] = None
    wound_site: Optional[str] = None
    dressing_type: Optional[str] = None
    indication: Optional[str] = None

    assessment: DressingAssessment = Field(default_factory=DressingAssessment)
    procedure: DressingProcedure = Field(default_factory=DressingProcedure)
    asepsis: AsepsisChecklist = Field(default_factory=AsepsisChecklist)

    pain_score: Optional[int] = Field(None, ge=0, le=10)
    patient_response: Optional[str] = None
    findings: Optional[str] = None
    next_dressing_due: Optional[datetime] = None

    verified_by_id: Optional[int] = None


class DressingUpdate(BaseModel):
    # NABH: updates must carry edit_reason
    edit_reason: str = Field(min_length=3)

    wound_site: Optional[str] = None
    dressing_type: Optional[str] = None
    indication: Optional[str] = None
    assessment: Optional[DressingAssessment] = None
    procedure: Optional[DressingProcedure] = None
    asepsis: Optional[AsepsisChecklist] = None
    pain_score: Optional[int] = Field(None, ge=0, le=10)
    patient_response: Optional[str] = None
    findings: Optional[str] = None
    next_dressing_due: Optional[datetime] = None
    verified_by_id: Optional[int] = None


class DressingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    admission_id: int
    performed_at: Optional[datetime] = None
    wound_site: str = ""
    dressing_type: str = ""
    indication: str = ""
    findings: Optional[str] = None
    next_dressing_due: Optional[datetime] = None
    asepsis: Dict[str, Any] = Field(default_factory=dict)
    pain_score: Optional[int] = None
    patient_response: str = ""

    performed_by_id: Optional[int] = None
    performed_by: Optional[UserMiniOut] = None
    verified_by_id: Optional[int] = None
    verified_by: Optional[UserMiniOut] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    updated_by: Optional[UserMiniOut] = None
    edit_reason: Optional[str] = None


# =========================
# ICU Flow Sheet
# =========================
class IcuFlowCreate(BaseModel):
    recorded_at: Optional[datetime] = None
    shift: Optional[Literal["morning", "evening", "night"]] = None

    vitals: Dict[str, Any] = Field(default_factory=dict)       # BP/Pulse/Temp/RR/Spo2
    ventilator: Dict[str, Any] = Field(default_factory=dict)   # mode/fiO2/peep etc
    infusions: List[Dict[str, Any]] = Field(default_factory=list)

    gcs_score: Optional[int] = Field(None, ge=0, le=15)
    urine_output_ml: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None
    verified_by_id: Optional[int] = None


class IcuFlowUpdate(BaseModel):
    edit_reason: str = Field(min_length=3)
    shift: Optional[Literal["morning", "evening", "night"]] = None
    vitals: Optional[Dict[str, Any]] = None
    ventilator: Optional[Dict[str, Any]] = None
    infusions: Optional[List[Dict[str, Any]]] = None
    gcs_score: Optional[int] = Field(None, ge=0, le=15)
    urine_output_ml: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None
    verified_by_id: Optional[int] = None


class IcuFlowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    admission_id: int
    recorded_at: datetime
    shift: Optional[str] = None
    vitals: Dict[str, Any]
    ventilator: Dict[str, Any]
    infusions: List[Dict[str, Any]]
    gcs_score: Optional[int] = None
    urine_output_ml: Optional[int] = None
    notes: str = ""

    recorded_by_id: Optional[int] = None
    recorded_by: Optional[UserMiniOut] = None

    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    updated_by: Optional[UserMiniOut] = None
    edit_reason: Optional[str] = None


# =========================
# Isolation
# =========================
class IsolationCreate(BaseModel):
    precaution_type: Literal["contact", "droplet", "airborne"] = "contact"
    indication: Optional[str] = None

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    measures: Dict[str, Any] = Field(default_factory=dict)
    review_due_at: Optional[datetime] = None


class IsolationUpdate(BaseModel):
    edit_reason: str = Field(min_length=3)
    precaution_type: Optional[Literal["contact", "droplet", "airborne"]] = None
    indication: Optional[str] = None
    measures: Optional[Dict[str, Any]] = None
    review_due_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class IsolationStop(BaseModel):
    stop_reason: str = Field(min_length=3)
    stopped_at: Optional[datetime] = None


class IsolationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    admission_id: int
    status: str
    precaution_type: str
    indication: str
    ordered_at: datetime
    ordered_by_id: Optional[int] = None
    ordered_by: Optional[UserMiniOut] = None
    measures: Dict[str, Any]
    review_due_at: Optional[datetime] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    stopped_by_id: Optional[int] = None
    stopped_by: Optional[UserMiniOut] = None
    stop_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    updated_by: Optional[UserMiniOut] = None
    edit_reason: Optional[str] = None


# =========================
# Restraints
# =========================
class RestraintMonitoringPoint(BaseModel):
    at: datetime
    circulation_ok: Optional[bool] = None
    skin_ok: Optional[bool] = None
    comfort_ok: Optional[bool] = None
    toileting_done: Optional[bool] = None
    rom_done: Optional[bool] = None
    vitals: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


class RestraintCreate(BaseModel):
    restraint_type: Literal["physical", "chemical"] = "physical"
    device: Optional[str] = None
    site: Optional[str] = None

    reason: Optional[str] = None
    alternatives_tried: Optional[str] = None

    valid_till: Optional[datetime] = None
    consent_taken: bool = False
    consent_doc_ref: Optional[str] = None

    started_at: Optional[datetime] = None


class RestraintUpdate(BaseModel):
    edit_reason: str = Field(min_length=3)
    device: Optional[str] = None
    site: Optional[str] = None
    reason: Optional[str] = None
    alternatives_tried: Optional[str] = None
    valid_till: Optional[datetime] = None
    consent_taken: Optional[bool] = None
    consent_doc_ref: Optional[str] = None
    ended_at: Optional[datetime] = None


class RestraintStop(BaseModel):
    stop_reason: str = Field(min_length=3)
    stopped_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class RestraintAppendMonitoring(BaseModel):
    point: RestraintMonitoringPoint


class RestraintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    admission_id: int
    status: str
    restraint_type: str
    device: str
    site: str
    reason: str
    alternatives_tried: str
    ordered_at: datetime
    ordered_by_id: Optional[int] = None
    ordered_by: Optional[UserMiniOut] = None
    valid_till: Optional[datetime] = None
    consent_taken: bool
    consent_doc_ref: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    monitoring_log: List[Dict[str, Any]]
    stopped_at: Optional[datetime] = None
    stopped_by_id: Optional[int] = None
    stopped_by: Optional[UserMiniOut] = None
    stop_reason: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    updated_by: Optional[UserMiniOut] = None
    edit_reason: Optional[str] = None


# -------------------------
# Vitals point (accept UI keys)
# -------------------------
class VitalPoint(BaseModel):
    at: datetime

    # UI may send temp or temp_c; keep both safe
    temp: Optional[float] = None
    temp_c: Optional[float] = None

    pulse: Optional[int] = Field(None, ge=0)
    rr: Optional[int] = Field(None, ge=0)
    spo2: Optional[int] = Field(None, ge=0, le=100)

    # UI sends bp_sys/bp_dia, some older sends bp_systolic/bp_diastolic
    bp_sys: Optional[int] = Field(None, ge=0)
    bp_dia: Optional[int] = Field(None, ge=0)
    bp_systolic: Optional[int] = Field(None, ge=0)
    bp_diastolic: Optional[int] = Field(None, ge=0)

    bp: Optional[str] = None  # "120/80" (optional)
    notes: Optional[str] = None

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _normalize(self):
        # temp normalization
        if self.temp is None and self.temp_c is not None:
            self.temp = self.temp_c
        if self.temp_c is None and self.temp is not None:
            self.temp_c = self.temp

        # BP normalization
        if self.bp_sys is None and self.bp_systolic is not None:
            self.bp_sys = self.bp_systolic
        if self.bp_dia is None and self.bp_diastolic is not None:
            self.bp_dia = self.bp_diastolic

        if (self.bp_sys is None or self.bp_dia is None) and self.bp:
            s = str(self.bp).strip()
            if "/" in s:
                a, b = s.split("/", 1)
                a = a.strip()
                b = b.strip()
                if a.isdigit() and b.isdigit():
                    self.bp_sys = self.bp_sys or int(a)
                    self.bp_dia = self.bp_dia or int(b)

        return self

    def to_json(self) -> Dict[str, Any]:
        # store clean UI-friendly keys
        out: Dict[str, Any] = {
            "at": self.at.isoformat(),
            "temp": self.temp,
            "temp_c": self.temp_c,
            "pulse": self.pulse,
            "rr": self.rr,
            "spo2": self.spo2,
            "bp_sys": self.bp_sys,
            "bp_dia": self.bp_dia,
            "bp": (f"{self.bp_sys}/{self.bp_dia}" if self.bp_sys and self.bp_dia else self.bp),
            "notes": self.notes,
        }
        return {k: v for k, v in out.items() if v is not None}


# -------------------------
# Transfusion
# -------------------------
class TransfusionCreate(BaseModel):
    indication: Optional[str] = None
    ordered_at: Optional[datetime] = None
    consent_taken: bool = False
    consent_doc_ref: Optional[str] = None

    unit: Dict[str, Any] = Field(default_factory=dict)                 # {component_type, bag_number,...}
    compatibility: Dict[str, Any] = Field(default_factory=dict)
    issue: Dict[str, Any] = Field(default_factory=dict)
    bedside_verification: Dict[str, Any] = Field(default_factory=dict)

    administration: Dict[str, Any] = Field(default_factory=dict)       # {start_time, end_time, end_vitals?...}
    baseline_vitals: Dict[str, Any] = Field(default_factory=dict)       # pre
    monitoring_vitals: List[VitalPoint] = Field(default_factory=list)

    reaction: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")


class TransfusionUpdate(BaseModel):
    edit_reason: str = Field(min_length=3)

    status: Optional[Literal["ordered", "issued", "in_progress", "completed", "stopped", "reaction"]] = None
    indication: Optional[str] = None
    consent_taken: Optional[bool] = None
    consent_doc_ref: Optional[str] = None

    unit: Optional[Dict[str, Any]] = None
    compatibility: Optional[Dict[str, Any]] = None
    issue: Optional[Dict[str, Any]] = None
    bedside_verification: Optional[Dict[str, Any]] = None

    administration: Optional[Dict[str, Any]] = None
    baseline_vitals: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="ignore")


class TransfusionAppendVital(BaseModel):
    point: VitalPoint


class TransfusionMarkReaction(BaseModel):
    occurred: bool = True
    started_at: Optional[datetime] = None
    symptoms: List[str] = Field(default_factory=list)
    actions_taken: Optional[str] = None
    doctor_notified_at: Optional[datetime] = None
    bloodbank_notified_at: Optional[datetime] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class TransfusionOut(BaseModel):
    id: int
    admission_id: int
    status: str

    indication: str
    ordered_at: Optional[datetime] = None

    ordered_by_id: Optional[int] = None
    ordered_by: Optional[UserMiniOut] = None

    consent_taken: bool
    consent_doc_ref: Optional[str] = None

    unit: Dict[str, Any]
    compatibility: Dict[str, Any]
    issue: Dict[str, Any]
    bedside_verification: Dict[str, Any]

    administration: Dict[str, Any]
    baseline_vitals: Dict[str, Any]
    monitoring_vitals: List[Dict[str, Any]]
    reaction: Dict[str, Any]

    created_by_id: Optional[int] = None
    created_by: Optional[UserMiniOut] = None
    created_at: datetime

    updated_by_id: Optional[int] = None
    updated_by: Optional[UserMiniOut] = None
    updated_at: Optional[datetime] = None

    edit_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
