# FILE: app/schemas/ot.py
from __future__ import annotations
from datetime import date, time, datetime, timezone
from typing import Any, Dict, List, Optional, Union
from sqlalchemy import Boolean, Text, JSON, Column
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from decimal import Decimal
from .user import UserOut, UserMiniOut
from .patient import PatientOut
from zoneinfo import ZoneInfo

JsonDict = Dict[str, Any]
JsonValue = Union[JsonDict, List[Any], None]

# ============================================================
#  OT MASTERS
# ============================================================

# ---------- OtSpeciality ----------

IST = ZoneInfo("Asia/Kolkata")


# -------------------------
# Speciality
# -------------------------
class OtSpecialityCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool = True


class OtSpecialityUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtSpecialityOut(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# Equipment Master
# -------------------------
class OtEquipmentMasterCreate(BaseModel):
    code: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    is_critical: bool = False
    is_active: bool = True


class OtEquipmentMasterUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    is_critical: Optional[bool] = None
    is_active: Optional[bool] = None


class OtEquipmentMasterOut(BaseModel):
    id: int
    code: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    is_critical: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# Procedure Master (UPDATED)
# -------------------------
class OtProcedureCreate(BaseModel):
    code: str
    name: str
    speciality_id: Optional[int] = None

    default_duration_min: Optional[int] = None
    rate_per_hour: Optional[float] = None
    description: Optional[str] = None

    # ✅ NEW fixed-cost split-up
    base_cost: float = 0
    anesthesia_cost: float = 0
    surgeon_cost: float = 0
    petitory_cost: float = 0
    asst_doctor_cost: float = 0

    is_active: bool = True


class OtProcedureUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    speciality_id: Optional[int] = None

    default_duration_min: Optional[int] = None
    rate_per_hour: Optional[float] = None
    description: Optional[str] = None

    # ✅ NEW fixed-cost split-up (optional on update)
    base_cost: Optional[float] = None
    anesthesia_cost: Optional[float] = None
    surgeon_cost: Optional[float] = None
    petitory_cost: Optional[float] = None
    asst_doctor_cost: Optional[float] = None

    is_active: Optional[bool] = None


class OtProcedureOut(BaseModel):
    id: int
    code: str
    name: str
    speciality_id: Optional[int] = None

    default_duration_min: Optional[int] = None
    rate_per_hour: Optional[float] = None
    description: Optional[str] = None

    base_cost: float
    anesthesia_cost: float
    surgeon_cost: float
    petitory_cost: float
    asst_doctor_cost: float
    total_fixed_cost: float

    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class OtPreopChecklistIn(BaseModel):
    """
    Input payload from frontend for pre-op checklist.
    Matches the fields you send in handleSubmit.
    """
    patient_identity_confirmed: bool = False
    consent_checked: bool = False
    site_marked: bool = False
    investigations_checked: bool = False
    implants_available: bool = False
    blood_products_arranged: bool = False
    fasting_status: Optional[str] = None
    device_checks: Optional[str] = None
    notes: Optional[str] = None
    completed: bool = False


class OtSpecialityUpdate(BaseModel):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtSpecialityOut(OtSpecialityCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ---------- OtEquipmentMaster ----------


class OtEquipmentMasterBase(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    is_critical: bool = False
    is_active: bool = True


class OtEquipmentMasterCreate(OtEquipmentMasterBase):
    pass


class OtEquipmentMasterUpdate(BaseModel):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    is_critical: Optional[bool] = None
    is_active: Optional[bool] = None


class OtEquipmentMasterOut(OtEquipmentMasterBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ---------- OtSchedule ----------

# ---------- OtSchedule ----------


def _db_utc_to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    # DB stores naive UTC -> assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


# ---------- Theater Mini ----------
class OtTheaterOutMini(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# ---------- Admission Mini (keep - not OT location) ----------
class OtScheduleAdmissionOut(BaseModel):
    id: int
    admission_code: Optional[str] = None
    display_code: str
    admitted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("admitted_at")
    def _ser_admitted_at(self, v: datetime | None):
        return _db_utc_to_ist(v)


# ---------- Procedure link out (as you already have) ----------
class OtScheduleProcedureLinkOut(BaseModel):
    id: int
    procedure_id: int
    is_primary: bool
    procedure: Optional[
        "OtProcedureOut"] = None  # keep your existing OtProcedureOut

    model_config = ConfigDict(from_attributes=True)


class OtScheduleBase(BaseModel):
    # 🗓️ Timing (IST slot)
    date: date
    planned_start_time: time
    planned_end_time: Optional[time] = None

    # 👤 Patient context
    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    # ✅ OT THEATER (NEW)
    ot_theater_id: Optional[int] = None

    # 👨‍⚕️ Staff
    surgeon_user_id: int
    anaesthetist_user_id: int  # ✅ required as per your request

    petitory_user_id: Optional[int] = None
    asst_doctor_user_id: Optional[int] = None

    # 🧠 Clinical
    procedure_name: str
    side: Optional[str] = None
    priority: str = "Elective"
    notes: Optional[str] = None


class OtScheduleCreate(OtScheduleBase):
    primary_procedure_id: Optional[int] = None
    additional_procedure_ids: List[int] = []


class OtScheduleUpdate(BaseModel):
    date: Optional[date] = None
    planned_start_time: Optional[time] = None
    planned_end_time: Optional[time] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    ot_theater_id: Optional[int] = None

    surgeon_user_id: Optional[int] = None
    anaesthetist_user_id: Optional[int] = None
    petitory_user_id: Optional[int] = None
    asst_doctor_user_id: Optional[int] = None

    procedure_name: Optional[str] = None
    side: Optional[str] = None
    priority: Optional[str] = None
    notes: Optional[str] = None

    primary_procedure_id: Optional[int] = None
    additional_procedure_ids: Optional[List[int]] = None


class OtScheduleOut(OtScheduleBase):
    id: int
    status: str
    case_id: Optional[int] = None

    primary_procedure_id: Optional[int] = None

    # Relations
    patient: Optional[PatientOut] = None
    admission: Optional[OtScheduleAdmissionOut] = None

    theater: Optional[OtTheaterOutMini] = None

    surgeon: Optional[UserMiniOut] = None
    anaesthetist: Optional[UserMiniOut] = None
    petitory: Optional[UserMiniOut] = None
    asst_doctor: Optional[UserMiniOut] = None

    primary_procedure: Optional["OtProcedureOut"] = None
    procedures: List[OtScheduleProcedureLinkOut] = []

    op_no: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("created_at", "updated_at")
    def _ser_created_updated(self, v: datetime):
        return _db_utc_to_ist(v)


class OtScheduleUserOut(BaseModel):
    """
    Minimal doctor / anaesthetist info returned along with an OT schedule.
    This should match fields on app.models.user.User (or hybrids).
    """
    model_config = ConfigDict(from_attributes=True)

    id: int

    # Name fields
    full_name: Optional[str] = None  # from hybrid_property User.full_name
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    # Optional extra doctor info – only if you have these columns
    code: Optional[str] = None  # doctor code / short code
    speciality_name: Optional[str] = None
    registration_no: Optional[str] = None


class OtCaseBase(BaseModel):

    preop_diagnosis: Optional[str] = None
    postop_diagnosis: Optional[str] = None
    final_procedure_name: Optional[str] = None
    speciality_id: Optional[int] = None
    schedule: Optional[OtScheduleOut] = None
    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None

    outcome: Optional[str] = None  # completed / abandoned / converted
    icu_required: bool = False
    immediate_postop_condition: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OtCaseCreate(OtCaseBase):
    schedule_id: int


class OtCaseUpdate(BaseModel):
    schedule_id: Optional[int] = None
    preop_diagnosis: Optional[str] = None
    postop_diagnosis: Optional[str] = None
    final_procedure_name: Optional[str] = None
    speciality_id: Optional[int] = None

    actual_start_time: Optional[datetime] = None
    actual_end_time: Optional[datetime] = None

    outcome: Optional[str] = None
    icu_required: Optional[bool] = None
    immediate_postop_condition: Optional[str] = None


class OtCaseOut(OtCaseBase):
    model_config = ConfigDict(from_attributes=True)
    schedule: Optional[OtScheduleOut] = None
    id: int

    patient_id: Optional[int] = None
    patient_name: Optional[str] = None
    uhid: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None

    op_no: Optional[str] = None

    created_at: datetime
    updated_at: datetime
    schedule_id: Optional[int] = None


# ============================================================
#  CLINICAL RECORDS LINKED TO OT CASE
# ============================================================

# ---------- PreAnaesthesiaEvaluation ----------


class PreAnaesthesiaEvaluationBase(BaseModel):
    case_id: int
    anaesthetist_user_id: int

    asa_grade: str = Field(..., max_length=10)  # ASA I–V
    comorbidities: Optional[str] = None
    airway_assessment: Optional[str] = None
    allergies: Optional[str] = None
    previous_anaesthesia_issues: Optional[str] = None
    plan: Optional[str] = None
    risk_explanation: Optional[str] = None


class PreAnaesthesiaEvaluationCreate(PreAnaesthesiaEvaluationBase):
    pass


class PreAnaesthesiaEvaluationUpdate(BaseModel):
    case_id: Optional[int] = None
    anaesthetist_user_id: Optional[int] = None
    asa_grade: Optional[str] = Field(None, max_length=10)
    comorbidities: Optional[str] = None
    airway_assessment: Optional[str] = None
    allergies: Optional[str] = None
    previous_anaesthesia_issues: Optional[str] = None
    plan: Optional[str] = None
    risk_explanation: Optional[str] = None


class PreAnaesthesiaEvaluationOut(PreAnaesthesiaEvaluationBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- PreOpChecklist ----------


class PreOpChecklistBase(BaseModel):
    case_id: int
    nurse_user_id: int
    data: JsonValue
    completed: bool = False
    completed_at: Optional[datetime] = None


class PreOpChecklistCreate(PreOpChecklistBase):
    pass


class PreOpChecklistUpdate(BaseModel):
    case_id: Optional[int] = None
    nurse_user_id: Optional[int] = None
    data: Optional[JsonValue] = None
    completed: Optional[bool] = None
    completed_at: Optional[datetime] = None


class PreOpChecklistOut(PreOpChecklistBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class OtPreopChecklistRow(BaseModel):
    handover: bool = False
    receiving: bool = False
    comments: Optional[str] = None


class OtPreopInvestigations(BaseModel):
    hb: Optional[str] = None
    platelet: Optional[str] = None
    urea: Optional[str] = None
    creatinine: Optional[str] = None
    potassium: Optional[str] = None
    rbs: Optional[str] = None
    other: Optional[str] = None


class OtPreopVitals(BaseModel):
    temp: Optional[str] = None
    pulse: Optional[str] = None
    resp: Optional[str] = None
    bp: Optional[str] = None
    spo2: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None


class OtPreopChecklistIn(BaseModel):
    """
    Payload from UI for OT Pre-op checklist.
    This goes into PreOpChecklist.data (except `completed`).
    """

    # 🔹 FULL CHECKLIST (all rows)
    checklist: Dict[str, OtPreopChecklistRow] = Field(
        default_factory=dict,
        description=
        "All checklist rows keyed by item key (allergy, consent_form_signed, ...)",
    )

    # 🔹 Boxes on right side
    investigations: OtPreopInvestigations = OtPreopInvestigations()
    vitals: OtPreopVitals = OtPreopVitals()

    # 🔹 Extra fields
    shave_completed: Optional[str] = None  # "yes" / "no"
    nurse_signature: Optional[str] = None

    # 🔹 Summary flags (for reporting, filters, etc.)
    patient_identity_confirmed: bool = False
    consent_checked: bool = False
    site_marked: bool = False
    investigations_checked: bool = False
    implants_available: bool = False
    blood_products_arranged: bool = False
    fasting_status: Optional[str] = None
    device_checks: Optional[str] = None
    notes: Optional[str] = None

    # 🔹 Status
    completed: bool = False


class OtPreopChecklistOut(OtPreopChecklistIn):
    """
    Response to UI: everything from In + case_id & timestamps.
    """
    case_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------- SurgicalSafetyChecklist ----------
# ---------- OT WHO Surgical Safety Checklist (UI DTO) ----------


class OtSafetyPhaseSignIn(BaseModel):
    """
    BEFORE INDUCTION OF ANAESTHESIA
    (with at least nurse + anaesthetist)
    """
    identity_site_procedure_consent_confirmed: bool = False
    site_marked: Optional[str] = None  # 'yes' | 'no' | 'na'
    machine_and_medication_check_complete: bool = False
    known_allergy: Optional[str] = None  # 'yes' | 'no'
    difficult_airway_or_aspiration_risk: Optional[str] = None  # 'yes' | 'no'
    blood_loss_risk_gt500ml_or_7mlkg: Optional[str] = None  # 'yes' | 'no'
    equipment_assistance_available: bool = False  # “Yes, and equipment / assistance available”
    iv_central_access_and_fluids_planned: bool = False  # “Yes, and two IVs / central access and fluids planned”


class OtSafetyPhaseTimeOut(BaseModel):
    """
    BEFORE SKIN INCISION (Time-out)
    """
    team_members_introduced: bool = False
    patient_name_procedure_incision_site_confirmed: bool = False
    antibiotic_prophylaxis_given: Optional[str] = None  # 'yes' | 'no' | 'na'

    # To surgeon
    surgeon_critical_steps: Optional[str] = None
    surgeon_case_duration_estimate: Optional[str] = None
    surgeon_anticipated_blood_loss: Optional[str] = None

    # To anaesthetist
    anaesthetist_patient_specific_concerns: Optional[str] = None

    # To nursing team
    sterility_confirmed: bool = False
    equipment_issues_or_concerns: bool = False
    essential_imaging_displayed: Optional[str] = None  # 'yes' | 'no' | 'na'


class OtSafetyPhaseSignOut(BaseModel):
    """
    BEFORE PATIENT LEAVES OPERATING ROOM (Sign-out)
    """
    procedure_name_confirmed: bool = False
    counts_complete: bool = False  # instruments / sponge / needle
    specimens_labelled_correctly: bool = False  # with patient name etc.
    equipment_problems_to_be_addressed: Optional[str] = None
    key_concerns_for_recovery_and_management: Optional[str] = None


class OtSafetyChecklistIn(BaseModel):
    """
    Payload used by the SafetyTab frontend.
    Times are simple 'HH:MM' strings.
    """
    sign_in_done: bool = False
    sign_in_time: Optional[str] = None

    time_out_done: bool = False
    time_out_time: Optional[str] = None

    sign_out_done: bool = False
    sign_out_time: Optional[str] = None

    sign_in: OtSafetyPhaseSignIn = Field(default_factory=OtSafetyPhaseSignIn)
    time_out: OtSafetyPhaseTimeOut = Field(
        default_factory=OtSafetyPhaseTimeOut)
    sign_out: OtSafetyPhaseSignOut = Field(
        default_factory=OtSafetyPhaseSignOut)


class OtSafetyChecklistOut(BaseModel):
    """
    What we send back to the SafetyTab.
    """
    case_id: int

    sign_in_done: bool = False
    sign_in_time: Optional[str] = None

    time_out_done: bool = False
    time_out_time: Optional[str] = None

    sign_out_done: bool = False
    sign_out_time: Optional[str] = None

    sign_in: OtSafetyPhaseSignIn
    time_out: OtSafetyPhaseTimeOut
    sign_out: OtSafetyPhaseSignOut

    created_at: datetime
    updated_at: Optional[datetime] = None


# ---------- AnaesthesiaRecord + vitals + drugs ----------


class AnaesthesiaRecordBase(BaseModel):
    case_id: int
    anaesthetist_user_id: int

    preop_vitals: Optional[JsonValue] = None
    plan: Optional[str] = None
    airway_plan: Optional[str] = None
    intraop_summary: Optional[str] = None
    complications: Optional[str] = None


class AnaesthesiaRecordCreate(AnaesthesiaRecordBase):
    pass


class AnaesthesiaRecordUpdate(BaseModel):
    case_id: Optional[int] = None
    anaesthetist_user_id: Optional[int] = None

    preop_vitals: Optional[JsonValue] = None
    plan: Optional[str] = None
    airway_plan: Optional[str] = None
    intraop_summary: Optional[str] = None
    complications: Optional[str] = None


# FILE: app/schemas/ot.py


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _to_int_or_none(v: Any) -> Optional[int]:
    if _blank(v):
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, )):
        return v
    s = str(v).strip()
    try:
        return int(float(s))  # handles "12", "12.0"
    except Exception:
        return None


def _to_float_or_none(v: Any) -> Optional[float]:
    if _blank(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except Exception:
        return None


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if _blank(v):
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _to_list_int(v: Any) -> List[int]:
    if v is None:
        return []
    if isinstance(v, list):
        out: List[int] = []
        seen = set()
        for x in v:
            n = _to_int_or_none(x)
            if n is None:
                continue
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
    # single value
    n = _to_int_or_none(v)
    return [n] if n is not None else []


class OtAnaesthesiaRecordIn(BaseModel):
    """
    ✅ Accepts your frontend payload safely:
    - ignores extra keys like id/case_id/created_at if they accidentally come
    - converts "" -> None for int/float fields
    - accepts asa_grade as "1"/1/"I"/etc (stored as string)
    """
    model_config = ConfigDict(extra="allow")

    # ============================================================
    # PRE-OP RECORD
    # ============================================================
    anaesthesia_type: Optional[str] = None

    asa_grade: Optional[Union[str, int]] = None  # accepts "1"/1/"I" etc.
    asa_emergency: bool = False  # ✅ added
    airway_assessment: Optional[str] = None
    comorbidities: Optional[str] = None
    allergies: Optional[str] = None

    patient_prefix: Optional[str] = None
    diagnosis: Optional[str] = None
    proposed_operation: Optional[str] = None

    weight: Optional[str] = None
    height: Optional[str] = None
    hb: Optional[str] = None
    blood_group: Optional[str] = None
    investigation_reports: Optional[str] = None
    history: Optional[str] = None

    preop_pulse: Optional[int] = None
    preop_bp: Optional[str] = None
    preop_rr: Optional[int] = None
    preop_temp_c: Optional[float] = None
    preop_cvs: Optional[str] = None
    preop_rs: Optional[str] = None
    preop_cns: Optional[str] = None
    preop_pa: Optional[str] = None
    preop_veins: Optional[str] = None
    preop_spine: Optional[str] = None

    airway_teeth_status: Optional[str] = None
    airway_denture: Optional[str] = None
    airway_neck_movements: Optional[str] = None
    airway_mallampati_class: Optional[str] = None
    difficult_airway_anticipated: Optional[bool] = None

    risk_factors: Optional[str] = None
    anaesthetic_plan_detail: Optional[str] = None
    preop_instructions: Optional[str] = None

    # ✅ checklist coming from UI
    preop_checklist: Dict[str, bool] = Field(default_factory=dict)

    # ============================================================
    # INTRA-OP SETTINGS
    # ============================================================
    preoxygenation: Optional[bool] = None
    cricoid_pressure: Optional[bool] = None
    induction_route: Optional[str] = None

    intubation_done: Optional[bool] = None
    intubation_route: Optional[str] = None
    intubation_state: Optional[str] = None
    intubation_technique: Optional[str] = None

    tube_type: Optional[str] = None
    tube_size: Optional[str] = None
    tube_fixed_at: Optional[str] = None
    cuff_used: Optional[bool] = None
    cuff_medium: Optional[str] = None
    bilateral_breath_sounds: Optional[str] = None
    added_sounds: Optional[str] = None
    laryngoscopy_grade: Optional[str] = None

    airway_devices: List[str] = Field(default_factory=list)

    ventilation_mode_baseline: Optional[str] = None
    ventilator_vt: Optional[int] = None
    ventilator_rate: Optional[int] = None
    ventilator_peep: Optional[int] = None
    breathing_system: Optional[str] = None

    monitors: Dict[str, Any] = Field(default_factory=dict)
    lines: Dict[str, Any] = Field(default_factory=dict)
    tourniquet_used: Optional[bool] = None

    patient_position: Optional[str] = None
    eyes_taped: Optional[bool] = None
    eyes_covered_with_foil: Optional[bool] = None
    pressure_points_padded: Optional[bool] = None

    iv_fluids_plan: Optional[str] = None
    blood_components_plan: Optional[str] = None

    regional_block_type: Optional[str] = None
    regional_position: Optional[str] = None
    regional_approach: Optional[str] = None
    regional_space_depth: Optional[str] = None
    regional_needle_type: Optional[str] = None
    regional_drug_dose: Optional[str] = None
    regional_level: Optional[str] = None
    regional_complications: Optional[str] = None

    block_adequacy: Optional[str] = None
    sedation_needed: Optional[bool] = None
    conversion_to_ga: Optional[bool] = None

    airway_device_ids: List[int] = Field(default_factory=list)
    monitor_device_ids: List[int] = Field(default_factory=list)

    notes: Optional[str] = None

    # ---------------- validators (accept frontend strings safely) ----------------
    @field_validator(
        "preop_pulse",
        "preop_rr",
        "ventilator_vt",
        "ventilator_rate",
        "ventilator_peep",
        mode="before",
    )
    @classmethod
    def _v_ints(cls, v):
        return _to_int_or_none(v)

    @field_validator("preop_temp_c", mode="before")
    @classmethod
    def _v_float(cls, v):
        return _to_float_or_none(v)

    @field_validator("airway_device_ids", "monitor_device_ids", mode="before")
    @classmethod
    def _v_ids(cls, v):
        return _to_list_int(v)

    @field_validator("asa_emergency", mode="before")
    @classmethod
    def _v_asa_e(cls, v):
        return _to_bool(v)

    @field_validator("asa_grade", mode="before")
    @classmethod
    def _v_asa_grade(cls, v):
        if _blank(v):
            return None
        # keep as string in storage/output
        return str(v).strip()

    @field_validator("preop_checklist", mode="before")
    @classmethod
    def _v_checklist(cls, v):
        if v is None:
            return {}
        if isinstance(v, dict):
            return {str(k): _to_bool(val) for k, val in v.items()}
        return {}


class OtAnaesthesiaRecordOut(OtAnaesthesiaRecordIn):
    model_config = ConfigDict(from_attributes=True, extra="allow")
    id: int
    case_id: int
    anaesthetist_user_id: Optional[int] = None
    airway_device_ids: List[int] = Field(default_factory=list)
    monitor_device_ids: List[int] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    raw_json: Dict[str, Any] = Field(default_factory=dict)


class OtAnaesthesiaRecordDefaultsOut(BaseModel):
    intra_date: str = ""
    intra_anaesthesiologist: str = ""
    intra_surgeon: str = ""
    intra_or_no: str = ""
    intra_case_type: str = ""  # Elective/Emergency
    intra_surgical_procedure: str = ""
    intra_anaesthesia_type: str = ""


# -----------------------
# VITALS
# -----------------------


class OtAnaesthesiaVitalIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    time: str  # HH:MM

    hr: Optional[float] = None
    bp: Optional[str] = None
    spo2: Optional[float] = None
    rr: Optional[float] = None
    temp_c: Optional[float] = None
    etco2: Optional[float] = None

    ventilation_mode: Optional[str] = None
    peak_airway_pressure: Optional[float] = None
    cvp_pcwp: Optional[float] = None
    st_segment: Optional[str] = None
    urine_output_ml: Optional[float] = None
    blood_loss_ml: Optional[float] = None
    comments: Optional[str] = None

    # ✅ NEW paper gas row
    oxygen_fio2: Optional[str] = None
    n2o: Optional[str] = None
    air: Optional[str] = None
    agent: Optional[str] = None
    iv_fluids: Optional[str] = None


class OtAnaesthesiaVitalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    record_id: int
    time: str

    hr: Optional[int] = None
    bp: Optional[str] = None
    spo2: Optional[int] = None
    rr: Optional[int] = None
    temp_c: Optional[float] = None
    etco2: Optional[float] = None
    comments: Optional[str] = None

    ventilation_mode: Optional[str] = None
    peak_airway_pressure: Optional[float] = None
    cvp_pcwp: Optional[float] = None
    st_segment: Optional[str] = None
    urine_output_ml: Optional[int] = None
    blood_loss_ml: Optional[int] = None

    # ✅ NEW
    oxygen_fio2: Optional[str] = None
    n2o: Optional[str] = None
    air: Optional[str] = None
    agent: Optional[str] = None
    iv_fluids: Optional[str] = None


class OtCaseCloseBody(BaseModel):
    """
    Payload used when closing an OT case from UI.

    Currently your frontend sends:
        { "outcome": "Completed" }

    You can also optionally send an explicit actual_end_time.
    """
    outcome: Optional[str] = Field(
        default="Completed",
        max_length=50,
        description=
        "Final outcome of OT case (Completed / Abandoned / Converted etc.)",
    )
    actual_end_time: Optional[datetime] = Field(
        default=None,
        description=
        "If sent, will override or set the actual end time of the surgery.",
    )


class OtAnaesthesiaDrugIn(BaseModel):
    time: Optional[str] = None  # "HH:MM"
    drug_name: Optional[str] = Field(None, max_length=255)
    dose: Optional[str] = Field(None, max_length=50)
    route: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=255)


class OtAnaesthesiaDrugOut(OtAnaesthesiaDrugIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    record_id: int
    time: Optional[str] = None


class AnaesthesiaVitalLogBase(BaseModel):
    record_id: int
    time: datetime
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    rr: Optional[int] = None
    etco2: Optional[float] = None
    temperature: Optional[float] = None
    comments: Optional[str] = None


class AnaesthesiaVitalLogCreate(AnaesthesiaVitalLogBase):
    pass


class AnaesthesiaVitalLogUpdate(BaseModel):
    record_id: Optional[int] = None
    time: Optional[datetime] = None
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    rr: Optional[int] = None
    etco2: Optional[float] = None
    temperature: Optional[float] = None
    comments: Optional[str] = None


class AnaesthesiaVitalLogOut(AnaesthesiaVitalLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class AnaesthesiaDrugLogBase(BaseModel):
    record_id: int
    time: datetime
    drug_name: str = Field(..., max_length=255)
    dose: Optional[str] = Field(None, max_length=50)
    route: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=255)


class AnaesthesiaDrugLogCreate(AnaesthesiaDrugLogBase):
    pass


class AnaesthesiaDrugLogUpdate(BaseModel):
    record_id: Optional[int] = None
    time: Optional[datetime] = None
    drug_name: Optional[str] = Field(None, max_length=255)
    dose: Optional[str] = Field(None, max_length=50)
    route: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=255)


class AnaesthesiaDrugLogOut(AnaesthesiaDrugLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class OtUserBasic(BaseModel):
    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OtNursingRecordBase(BaseModel):
    case_id: int

    # optional – if not sent, backend will use current user
    primary_nurse_id: Optional[int] = None

    scrub_nurse_name: Optional[str] = Field(None, max_length=255)
    circulating_nurse_name: Optional[str] = Field(None, max_length=255)

    positioning: Optional[str] = Field(None, max_length=255)
    skin_prep: Optional[str] = None
    catheterisation: Optional[str] = None
    diathermy_plate_site: Optional[str] = None

    counts_initial_done: Optional[bool] = False
    counts_closure_done: Optional[bool] = False

    antibiotics_time: Optional[time] = None

    warming_measures: Optional[str] = None
    notes: Optional[str] = None


class OtNursingRecordCreate(OtNursingRecordBase):
    """
    For create, everything is still optional except case_id.
    primary_nurse_id will default to current user in route if absent.
    """
    pass


class OtNursingRecordUpdate(BaseModel):
    case_id: Optional[int] = None
    primary_nurse_id: Optional[int] = None

    scrub_nurse_name: Optional[str] = Field(None, max_length=255)
    circulating_nurse_name: Optional[str] = Field(None, max_length=255)

    positioning: Optional[str] = Field(None, max_length=255)
    skin_prep: Optional[str] = None
    catheterisation: Optional[str] = None
    diathermy_plate_site: Optional[str] = None

    counts_initial_done: Optional[bool] = None
    counts_closure_done: Optional[bool] = None

    antibiotics_time: Optional[time] = None

    warming_measures: Optional[str] = None
    notes: Optional[str] = None


class OtNursingRecordOut(OtNursingRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    primary_nurse: Optional[OtUserBasic] = None


# ---------- OtSpongeInstrumentCount ----------
class OtCountItemLineIn(BaseModel):
    id: Optional[int] = None
    instrument_id: Optional[int] = None

    initial_qty: int = Field(default=0, ge=0)
    added_qty: int = Field(default=0, ge=0)
    final_qty: int = Field(default=0, ge=0)

    remarks: str = ""


class OtCountItemLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    instrument_id: Optional[int] = None
    instrument_code: str = ""
    instrument_name: str = ""
    uom: str = "Nos"

    initial_qty: int
    added_qty: int
    final_qty: int

    expected_final: int
    variance: int
    has_discrepancy: bool

    remarks: str = ""
    updated_at: Optional[datetime] = None


class OtInstrumentMasterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    available_qty: int
    cost_per_qty: Decimal
    uom: str
    description: str = ""
    is_active: bool = True

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OtCountItemsUpsertIn(BaseModel):
    lines: List[OtCountItemLineIn] = Field(default_factory=list)


class OtSpongeInstrumentCountBase(BaseModel):
    case_id: int
    initial_count_data: Optional[JsonValue] = None
    final_count_data: Optional[JsonValue] = None
    discrepancy: bool = False
    discrepancy_notes: Optional[str] = None


class OtSpongeInstrumentCountCreate(OtSpongeInstrumentCountBase):
    pass


class OtSpongeInstrumentCountUpdate(BaseModel):
    case_id: Optional[int] = None
    initial_count_data: Optional[JsonValue] = None
    final_count_data: Optional[JsonValue] = None
    discrepancy: Optional[bool] = None
    discrepancy_notes: Optional[str] = None


class OtSpongeInstrumentCountOut(OtSpongeInstrumentCountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- OtImplantRecord ----------


class OtImplantRecordBase(BaseModel):
    case_id: int
    implant_name: str = Field(..., max_length=255)
    size: Optional[str] = Field(None, max_length=50)
    batch_no: Optional[str] = Field(None, max_length=100)
    lot_no: Optional[str] = Field(None, max_length=100)
    manufacturer: Optional[str] = Field(None, max_length=255)
    expiry_date: Optional[date] = None
    inventory_item_id: Optional[int] = None


class OtImplantRecordCreate(OtImplantRecordBase):
    pass


class OtImplantRecordUpdate(BaseModel):
    case_id: Optional[int] = None
    implant_name: Optional[str] = Field(None, max_length=255)
    size: Optional[str] = Field(None, max_length=50)
    batch_no: Optional[str] = Field(None, max_length=100)
    lot_no: Optional[str] = Field(None, max_length=100)
    manufacturer: Optional[str] = Field(None, max_length=255)
    expiry_date: Optional[date] = None
    inventory_item_id: Optional[int] = None


class OtImplantRecordOut(OtImplantRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- OperationNote ----------

# ---------- OperationNote ----------


class OperationNoteBase(BaseModel):
    case_id: int
    # optional – backend will default to current user if None
    surgeon_user_id: Optional[int] = None

    preop_diagnosis: Optional[str] = None
    postop_diagnosis: Optional[str] = None
    indication: Optional[str] = None
    findings: Optional[str] = None
    procedure_steps: Optional[str] = None
    blood_loss_ml: Optional[int] = None
    complications: Optional[str] = None
    drains_details: Optional[str] = None
    postop_instructions: Optional[str] = None


class OperationNoteCreate(OperationNoteBase):
    pass


class OperationNoteUpdate(BaseModel):
    case_id: Optional[int] = None
    surgeon_user_id: Optional[int] = None

    preop_diagnosis: Optional[str] = None
    postop_diagnosis: Optional[str] = None
    indication: Optional[str] = None
    findings: Optional[str] = None
    procedure_steps: Optional[str] = None
    blood_loss_ml: Optional[int] = None
    complications: Optional[str] = None
    drains_details: Optional[str] = None
    postop_instructions: Optional[str] = None


class OperationNoteOut(OperationNoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    surgeon: Optional[UserMiniOut] = None


# ---------- OtBloodTransfusionRecord ----------


class OtBloodTransfusionRecordBase(BaseModel):
    case_id: int

    component: str = Field(..., max_length=50)  # PRBC / FFP / Platelet / etc.
    units: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    reaction: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None


class OtBloodTransfusionRecordCreate(OtBloodTransfusionRecordBase):
    pass


class OtBloodTransfusionRecordUpdate(BaseModel):
    case_id: Optional[int] = None

    component: Optional[str] = Field(None, max_length=50)
    units: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    reaction: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None


class OtBloodTransfusionRecordOut(OtBloodTransfusionRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- OT Sponge / Instrument Count (UI) ----------


class OtCountsIn(BaseModel):
    sponges_initial: Optional[int] = None
    sponges_added: Optional[int] = None
    sponges_final: Optional[int] = None

    instruments_initial: Optional[int] = None
    instruments_final: Optional[int] = None

    needles_initial: Optional[int] = None
    needles_final: Optional[int] = None

    discrepancy_text: Optional[str] = None
    xray_done: bool = False
    resolved_by: Optional[str] = None
    notes: Optional[str] = None


class OtCountsOut(OtCountsIn):
    model_config = ConfigDict(from_attributes=False)

    id: int
    case_id: int
    created_at: datetime
    updated_at: Optional[datetime] = None


class PacuVitalsEntry(BaseModel):
    time: Optional[str] = None  # "HH:MM"
    spo2: Optional[str] = None
    hr: Optional[str] = None
    bp: Optional[str] = None  # "120/80"
    cvp: Optional[str] = None
    rbs: Optional[str] = None  # blood glucose
    remarks: Optional[str] = None


class PacuUiBase(BaseModel):
    time_to_recovery: Optional[str] = None
    time_to_ward_icu: Optional[str] = None
    disposition: Optional[str] = None

    anaesthesia_methods: Optional[List[str]] = None
    airway_support: Optional[List[str]] = None
    monitoring: Optional[List[str]] = None

    post_op_charts: Optional[List[str]] = None
    tubes_drains: Optional[List[str]] = None

    vitals_log: Optional[List[PacuVitalsEntry]] = None

    post_op_instructions: Optional[str] = None
    iv_fluids_orders: Optional[str] = None
    notes: Optional[str] = None


class PacuUiIn(PacuUiBase):
    pass


class PacuUiOut(PacuUiBase):
    id: int
    case_id: int
    nurse_user_id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ============================================================
#  OT ADMIN / STATUTORY LOGS
# ============================================================

# ---------- OtEquipmentDailyChecklist ----------


class OtEquipmentDailyChecklistBase(BaseModel):
    bed_id: Optional[int] = None  # ward/room/bed location
    date: date
    shift: Optional[str] = Field(None,
                                 max_length=50)  # Morning / Evening / Night
    checked_by_user_id: int
    data: JsonValue  # {equipment_id or code: {ok: bool, remark: str}}


class OtEquipmentDailyChecklistCreate(OtEquipmentDailyChecklistBase):
    pass


class OtEquipmentDailyChecklistUpdate(BaseModel):
    bed_id: Optional[int] = None
    date: Optional[date] = None
    shift: Optional[str] = Field(None, max_length=50)
    checked_by_user_id: Optional[int] = None
    data: Optional[JsonValue] = None


class OtEquipmentDailyChecklistOut(OtEquipmentDailyChecklistBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- OtCleaningLog ----------


class OtCleaningLogBase(BaseModel):
    bed_id: Optional[int] = None
    date: date
    session: Optional[str] = Field(
        None, max_length=50)  # pre-list / between-cases / end-of-day
    case_id: Optional[int] = None

    method: Optional[str] = Field(
        None, max_length=255)  # mopping, fumigation, UV, etc.
    done_by_user_id: int
    remarks: Optional[str] = None


class OtCleaningLogCreate(OtCleaningLogBase):
    pass


class OtCleaningLogUpdate(BaseModel):
    bed_id: Optional[int] = None
    date: Optional[date] = None
    session: Optional[str] = Field(None, max_length=50)
    case_id: Optional[int] = None

    method: Optional[str] = Field(None, max_length=255)
    done_by_user_id: Optional[int] = None
    remarks: Optional[str] = None


class OtCleaningLogOut(OtCleaningLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    done_by: Optional[UserMini] = None

    # convenience flat fields if you want
    done_by_name: Optional[str] = None


# ---------- OtEnvironmentLog ----------


class OtEnvironmentLogBase(BaseModel):
    bed_id: Optional[int] = None
    date: date
    time: time

    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    pressure_diff_pa: Optional[float] = None

    logged_by_user_id: int


class OtEnvironmentLogCreate(OtEnvironmentLogBase):
    pass


class OtEnvironmentLogUpdate(BaseModel):
    bed_id: Optional[int] = None
    date: Optional[date] = None
    time: Optional[time] = None

    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    pressure_diff_pa: Optional[float] = None

    logged_by_user_id: Optional[int] = None


class OtEnvironmentLogOut(OtEnvironmentLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# class OtTheatreMini(BaseModel):
#     id: int
#     name: str

#     model_config = ConfigDict(from_attributes=True)


class UserMini(BaseModel):
    id: int
    full_name: str

    model_config = ConfigDict(from_attributes=True)
