# FILE: app/schemas/ipd_nursing.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    # your API seems to use date_time; model uses performed_at — keep whatever you use
    date_time: Optional[datetime] = None
    performed_at: Optional[datetime] = None

    wound_site: str = ""
    dressing_type: str = ""
    indication: str = ""
    findings: Optional[str] = None
    next_dressing_due: Optional[datetime] = None

    # ✅ prevent ValidationError if missing
    assessment: Dict[str, Any] = Field(default_factory=dict)
    procedure: Dict[str, Any] = Field(default_factory=dict)
    asepsis: Dict[str, Any] = Field(default_factory=dict)

    pain_score: Optional[int] = None
    patient_response: str = ""

    created_by_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    updated_at: Optional[datetime] = None
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
    id: int
    admission_id: int
    recorded_at: datetime
    shift: Optional[str] = None

    vitals: Dict[str, Any]
    ventilator: Dict[str, Any]
    infusions: List[Dict[str, Any]]

    gcs_score: Optional[int] = None
    urine_output_ml: Optional[int] = None
    notes: str

    recorded_by_id: Optional[int] = None
    verified_by_id: Optional[int] = None

    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    edit_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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
    id: int
    admission_id: int
    status: str

    precaution_type: str
    indication: str

    ordered_at: datetime
    ordered_by_id: Optional[int] = None

    measures: Dict[str, Any]
    review_due_at: Optional[datetime] = None

    started_at: datetime
    ended_at: Optional[datetime] = None

    stopped_at: Optional[datetime] = None
    stopped_by_id: Optional[int] = None
    stop_reason: Optional[str] = None

    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    edit_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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
    valid_till: Optional[datetime] = None

    consent_taken: bool
    consent_doc_ref: Optional[str] = None

    started_at: datetime
    ended_at: Optional[datetime] = None

    monitoring_log: List[Dict[str, Any]]

    stopped_at: Optional[datetime] = None
    stopped_by_id: Optional[int] = None
    stop_reason: Optional[str] = None

    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    edit_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# =========================
# Blood Transfusion
# =========================
class VitalPoint(BaseModel):
    at: datetime
    temp_c: Optional[float] = None
    pulse: Optional[int] = Field(None, ge=0)
    rr: Optional[int] = Field(None, ge=0)
    spo2: Optional[int] = Field(None, ge=0, le=100)
    bp_systolic: Optional[int] = Field(None, ge=0)
    bp_diastolic: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None


class TransfusionCreate(BaseModel):
    # order
    indication: Optional[str] = None
    ordered_at: Optional[datetime] = None
    consent_taken: bool = False
    consent_doc_ref: Optional[str] = None

    # unit/traceability
    unit: Dict[str, Any] = Field(default_factory=dict)                 # {component_type, bag_number, abo_rh, expiry_at, donation_id}
    compatibility: Dict[str, Any] = Field(default_factory=dict)        # {crossmatch_status, report_no, tested_at}
    issue: Dict[str, Any] = Field(default_factory=dict)                # {issued_at, issued_by_id, collected_by_id, transport_notes}
    bedside_verification: Dict[str, Any] = Field(default_factory=dict) # {verified_at, verifier1_id, verifier2_id, checks:{}}

    administration: Dict[str, Any] = Field(default_factory=dict)       # {start_time, end_time, rate_ml_hr, volume_ml, iv_site, filter_used}
    baseline_vitals: Dict[str, Any] = Field(default_factory=dict)
    monitoring_vitals: List[VitalPoint] = Field(default_factory=list)

    reaction: Dict[str, Any] = Field(default_factory=dict)             # {occurred, started_at, symptoms, actions_taken,...}


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


class TransfusionOut(BaseModel):
    id: int
    admission_id: int
    status: str

    indication: str
    ordered_at: Optional[datetime] = None
    ordered_by_id: Optional[int] = None

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
    created_at: datetime
    updated_at: Optional[datetime] = None
    updated_by_id: Optional[int] = None
    edit_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
