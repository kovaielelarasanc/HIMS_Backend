# FILE: app/schemas/ot.py
from __future__ import annotations

from datetime import date, time, datetime
from typing import Any, Dict, List, Optional, Union
from sqlalchemy import Boolean, Text, JSON, Column
from pydantic import BaseModel, ConfigDict, Field
from .user import UserOut, UserMiniOut
from .patient import PatientOut

JsonDict = Dict[str, Any]
JsonValue = Union[JsonDict, List[Any], None]

# ============================================================
#  OT MASTERS
# ============================================================

# ---------- OtSpeciality ----------


class OtSpecialityBase(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    is_active: bool = True


class OtSpecialityCreate(OtSpecialityBase):
    pass


class OtProcedureBase(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)

    # ✅ must match DB + route + frontend
    speciality_id: Optional[int] = None
    default_duration_min: Optional[int] = Field(None, ge=0)
    rate_per_hour: Optional[float] = Field(None, ge=0)

    description: Optional[str] = None
    is_active: bool = True


class OtProcedureCreate(OtProcedureBase):
    """
    Used for POST /ot/procedures
    All fields allowed, code + name required.
    """
    pass


class OtProcedureUpdate(BaseModel):
    """
    Used for PUT /ot/procedures/{id}
    All fields optional; we use exclude_unset=True in the route.
    """
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)

    speciality_id: Optional[int] = None
    default_duration_min: Optional[int] = Field(None, ge=0)
    rate_per_hour: Optional[float] = Field(None, ge=0)

    description: Optional[str] = None
    is_active: Optional[bool] = None


class OtProcedureOut(OtProcedureBase):
    id: int
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


class OtSpecialityOut(OtSpecialityBase):
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


class OtScheduleBedOut(BaseModel):
    id: int
    code: str
    ward_name: Optional[str] = None
    room_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OtScheduleAdmissionOut(BaseModel):
    id: int
    admission_code: Optional[str] = None
    display_code: str  # from IpdAdmission.display_code
    admitted_at: Optional[datetime] = None

    # Ward/room/bed if you want to show *ward bed* instead of OT bed
    current_bed: Optional[OtScheduleBedOut] = None

    model_config = ConfigDict(from_attributes=True)


class OtScheduleProcedureLinkOut(BaseModel):
    id: int
    procedure_id: int
    is_primary: bool
    procedure: Optional[OtProcedureOut] = None

    model_config = ConfigDict(from_attributes=True)


class OtScheduleBase(BaseModel):
    # OT is now *bed-based*, not theatre-based
    date: date
    planned_start_time: time
    planned_end_time: Optional[time] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    # Location
    bed_id: Optional[int] = None  # OT location via Ward/Room/Bed

    # Surgeon / anaesthetist
    surgeon_user_id: int
    anaesthetist_user_id: Optional[int] = None

    # Clinical details
    procedure_name: str
    side: Optional[str] = None
    priority: str = "Elective"
    notes: Optional[str] = None


class OtScheduleCreate(OtScheduleBase):
    # 🔹 master procedure ids (write-only)
    primary_procedure_id: Optional[int] = None
    additional_procedure_ids: List[int] = []


class OtScheduleUpdate(BaseModel):
    date: Optional[date] = None
    planned_start_time: Optional[time] = None
    planned_end_time: Optional[time] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    bed_id: Optional[int] = None

    surgeon_user_id: Optional[int] = None
    anaesthetist_user_id: Optional[int] = None

    procedure_name: Optional[str] = None
    side: Optional[str] = None
    priority: Optional[str] = None
    notes: Optional[str] = None

    primary_procedure_id: Optional[int] = None
    additional_procedure_ids: Optional[List[int]] = None


class OtScheduleOut(OtScheduleBase):
    """
    Main DTO for listing / viewing OT schedules (bed-based).
    """
    id: int
    status: str
    case_id: Optional[int] = None

    primary_procedure_id: Optional[int] = None

    # Nested objects
    patient: Optional["PatientOut"] = None
    surgeon: Optional["UserMiniOut"] = None
    anaesthetist: Optional["UserMiniOut"] = None

    # NEW 🔹 admission + OT bed
    admission: Optional["OtScheduleAdmissionOut"] = None
    bed: Optional["OtScheduleBedOut"] = None

    # NEW 🔹 latest OP number (filled in route)
    op_no: Optional[str] = None

    primary_procedure: Optional[OtProcedureOut] = None
    procedures: List[OtScheduleProcedureLinkOut] = []

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


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

    id: int
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

# FILE: app/schemas/ot.py


class OtAnaesthesiaRecordIn(BaseModel):
    # ============================================================
    # PRE-OP RECORD (from pre-anaesthetic sheet)
    # ============================================================

    # General assessment
    anaesthesia_type: Optional[str] = None  # GA / Spinal / Epidural / MAC etc.
    asa_grade: Optional[str] = None  # I / II / III / IV / V / E
    airway_assessment: Optional[str] = None
    comorbidities: Optional[str] = None
    allergies: Optional[str] = None

    # Baseline vitals + systems exam
    preop_pulse: Optional[int] = None
    preop_bp: Optional[str] = None  # "120/80"
    preop_rr: Optional[int] = None
    preop_temp_c: Optional[float] = None
    preop_cvs: Optional[str] = None
    preop_rs: Optional[str] = None
    preop_cns: Optional[str] = None
    preop_pa: Optional[str] = None
    preop_veins: Optional[str] = None
    preop_spine: Optional[str] = None

    # Airway examination details
    airway_teeth_status: Optional[
        str] = None  # "Intact" / "Loose" / "Partially edentulous"
    airway_denture: Optional[str] = None  # "Present" / "Absent"
    airway_neck_movements: Optional[str] = None
    airway_mallampati_class: Optional[str] = None  # "Class 1/2/3/4"
    difficult_airway_anticipated: Optional[bool] = None

    # Risk & plan
    risk_factors: Optional[str] = None
    anaesthetic_plan_detail: Optional[str] = None  # free text plan
    preop_instructions: Optional[str] = None

    # ============================================================
    # INTRA-OP SETTINGS (from yellow sheet)
    # ============================================================

    # Airway / induction / intubation
    preoxygenation: Optional[bool] = None
    cricoid_pressure: Optional[bool] = None
    induction_route: Optional[
        str] = None  # "Intravenous" / "Inhalational" / "Rapid sequence"

    intubation_done: Optional[bool] = None
    intubation_route: Optional[str] = None  # Oral / Nasal
    intubation_state: Optional[str] = None  # "Awake" / "Anaesthetised"
    intubation_technique: Optional[
        str] = None  # "Visual" / "Blind" / "Fibreoptic" / "Retrograde"

    tube_type: Optional[str] = None  # ETT, LMA, Tracheostomy, etc.
    tube_size: Optional[str] = None  # e.g. "7.0"
    tube_fixed_at: Optional[str] = None  # e.g. "20 cm"
    cuff_used: Optional[bool] = None
    cuff_medium: Optional[str] = None  # Air / Saline / Not inflated
    bilateral_breath_sounds: Optional[str] = None
    added_sounds: Optional[str] = None
    laryngoscopy_grade: Optional[str] = None  # Grade I/II/III/IV

    airway_devices: Optional[list[str]] = None
    # eg: ["Face mask", "LMA/ILMA", "Oral airway", "Throat pack", "NG tube", "Other"]

    # Ventilation + breathing system (static settings)
    ventilation_mode_baseline: Optional[
        str] = None  # Spontaneous / Controlled / Manual / Ventilator
    ventilator_vt: Optional[int] = None  # Vt
    ventilator_rate: Optional[int] = None  # f
    ventilator_peep: Optional[int] = None  # PEEP
    breathing_system: Optional[str] = None  # Mapleson A/D/F, Circle, Other

    # Monitors (checklist)
    monitors: Optional[dict] = None
    # {
    #   "ecg": true, "nibp": true, "pulse_oximeter": true, "capnograph": true,
    #   "agent_monitor": false, "pns": false, "temperature": true,
    #   "urinary_catheter": false, "ibp": false, "cvp": false,
    #   "precordial_steth": false, "oesophageal_steth": false
    # }

    # Lines & tourniquet
    lines: Optional[dict] = None
    # e.g. {"peripheral_iv": true, "central_line": false, "arterial_line": false}
    tourniquet_used: Optional[bool] = None

    # Position & eye care
    patient_position: Optional[
        str] = None  # Supine / Lateral / Prone / Lithotomy / Other
    eyes_taped: Optional[bool] = None
    eyes_covered_with_foil: Optional[bool] = None
    pressure_points_padded: Optional[bool] = None

    # Fluids + blood components (plan / summary)
    iv_fluids_plan: Optional[str] = None  # RL / NS etc.
    blood_components_plan: Optional[str] = None  # PRBC / FFP, etc.

    # Regional technique / block details
    regional_block_type: Optional[
        str] = None  # Spinal / Epidural / Nerve block / None
    regional_position: Optional[str] = None
    regional_approach: Optional[str] = None
    regional_space_depth: Optional[str] = None
    regional_needle_type: Optional[str] = None
    regional_drug_dose: Optional[str] = None
    regional_level: Optional[str] = None
    regional_complications: Optional[str] = None

    # Adequacy of block section
    block_adequacy: Optional[str] = None  # Excellent / Adequate / Poor
    sedation_needed: Optional[bool] = None
    conversion_to_ga: Optional[bool] = None

    # Free-form notes (used for intra-op summary)
    notes: Optional[str] = None


class OtAnaesthesiaRecordOut(OtAnaesthesiaRecordIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    anaesthetist_user_id: Optional[int] = None
    created_at: datetime
    # we don’t have updated_at column; just expose created_at as “last updated”
    updated_at: Optional[datetime] = None


# FILE: app/schemas/ot.py


class OtAnaesthesiaVitalIn(BaseModel):
    # "HH:MM" from the UI; we convert to datetime in the route
    time: Optional[str] = None
    hr: Optional[int] = None  # maps to pulse
    bp: Optional[str] = None  # e.g. "120/80"
    spo2: Optional[int] = None
    rr: Optional[int] = None
    temp_c: Optional[float] = None
    etco2: Optional[float] = None  # <<--- NEW
    comments: Optional[str] = None

    # 🔸 NEW intra-op rows
    ventilation_mode: Optional[
        str] = None  # 'Spont', 'Assist', 'Control', 'Manual', 'Vent'
    peak_airway_pressure: Optional[float] = None
    cvp_pcwp: Optional[float] = None
    st_segment: Optional[str] = None  # eg. 'Normal', 'ST↑', 'ST↓'
    urine_output_ml: Optional[int] = None
    blood_loss_ml: Optional[int] = None


class OtAnaesthesiaVitalOut(OtAnaesthesiaVitalIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    record_id: int
    # keep time as "HH:MM" string for the UI
    time: Optional[str] = None


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


class PacuUiBase(BaseModel):
    # string "HH:MM" coming from <input type="time">
    arrival_time: Optional[str] = None
    departure_time: Optional[str] = None

    pain_score: Optional[str] = None
    nausea_vomiting: Optional[str] = None
    airway_status: Optional[str] = None
    vitals_summary: Optional[str] = None
    complications: Optional[str] = None
    discharge_criteria_met: Optional[bool] = False
    notes: Optional[str] = None


class PacuUiIn(PacuUiBase):
    """Payload from PACU tab."""
    pass


class PacuUiOut(PacuUiBase):
    """Response back to PACU tab."""
    id: int
    case_id: int
    nurse_user_id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=False)


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
