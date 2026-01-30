# FILE: app/schemas/ipd.py
from __future__ import annotations
import json
from datetime import datetime, date, time
from typing import Optional, List, Dict
from decimal import Decimal
from pydantic import BaseModel, Field, validator, ConfigDict, model_validator, field_validator

# =====================================================================
# ------------------------------- Masters ------------------------------
# =====================================================================

TRANSFER_TYPES = {"transfer", "upgrade", "downgrade", "isolation", "operational"}
TRANSFER_PRIORITIES = {"routine", "urgent"}
TRANSFER_STATUSES = {"requested", "approved", "rejected", "scheduled", "completed", "cancelled"}


class Paginated(BaseModel):
    total: int
    items: list

    model_config = ConfigDict(arbitrary_types_allowed=True)

class WardIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=1, max_length=20)
    floor: Optional[str] = ""


class WardOut(WardIn):
    id: int
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class RoomIn(BaseModel):
    ward_id: int = Field(..., gt=0)
    number: str = Field(..., min_length=1, max_length=30)
    type: Optional[str] = "General"


class RoomOut(RoomIn):
    id: int
    is_active: bool = True

    class Config:
        orm_mode = True


class BedIn(BaseModel):
    room_id: int = Field(..., gt=0)
    code: str = Field(..., min_length=1, max_length=30)


class BedOut(BedIn):
    id: int
    state: str
    reserved_until: Optional[datetime]
    note: str = ""

    class Config:
        orm_mode = True


class PackageIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    included: Optional[str] = ""
    excluded: Optional[str] = ""
    charges: float = Field(0.0, ge=0)


class PackageOut(PackageIn):
    id: int

    class Config:
        orm_mode = True


# =====================================================================
# ----------------------------- Bed Rates ------------------------------
# =====================================================================


class BedRateIn(BaseModel):
    room_type: str  # e.g., "General", "Private", "ICU"
    rate_basis: str = Field("daily", description="daily|hourly")
    daily_rate: float = Field(..., ge=0)
    effective_from: date
    effective_to: Optional[date] = None  # inclusive; null = open-ended

    @model_validator(mode="after")
    def validate_dates(self) -> "BedRateIn":
        """
        Ensure effective_to is not before effective_from.
        Replaces old @root_validator from Pydantic v1.
        """
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be >= effective_from")
        return self


class BedRateOut(BedRateIn):
    id: int
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


# =====================================================================
# ---------------------------- Admissions ------------------------------
# =====================================================================

ADMISSION_TYPES = {"planned", "emergency", "daycare"}
PAYOR_TYPES = {"cash", "insurance", "tpa"}


class AdmissionUpdateIn(BaseModel):
    department_id: Optional[int] = None
    practitioner_user_id: Optional[int] = None
    admission_type: Optional[str] = None  # planned/emergency/daycare
    expected_discharge_at: Optional[datetime] = None
    package_id: Optional[int] = None
    payor_type: Optional[str] = None  # cash/insurance/tpa/...
    insurer_name: Optional[str] = None
    policy_number: Optional[str] = None
    preliminary_diagnosis: Optional[str] = None
    history: Optional[str] = None
    care_plan: Optional[str] = None

    @validator("admission_type")
    def validate_admission_type(cls, v):
        if v is not None and v not in ADMISSION_TYPES:
            raise ValueError(f"admission_type must be one of {sorted(ADMISSION_TYPES)}")
        return v

    @validator("payor_type")
    def validate_payor_type(cls, v):
        if v is not None and v not in PAYOR_TYPES:
            raise ValueError(f"payor_type must be one of {sorted(PAYOR_TYPES)}")
        return v


class AdmissionOut(BaseModel):
    id: int
    display_code: Optional[str] = None  # ✅ needed for NHIP... in frontend

    patient_id: int
    department_id: Optional[int] = None
    practitioner_user_id: Optional[int] = None
    primary_nurse_user_id: Optional[int] = None

    admission_type: Optional[str] = None
    admitted_at: Optional[datetime] = None
    expected_discharge_at: Optional[datetime] = None
    discharge_at: Optional[datetime] = None

    status: str
    current_bed_id: Optional[int] = None

    # (optional, but good)
    admission_code: Optional[str] = None
    admission_no: Optional[str] = None
    ipd_no: Optional[str] = None
    ip_uhid: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AdmissionDetailOut(BaseModel):
    id: int
    display_code: str
    patient_id: int
    patient_uhid: str
    patient_name: str
    department_id: Optional[int]
    practitioner_user_id: Optional[int]
    practitioner_name: Optional[str] = None
    admission_type: str
    admitted_at: datetime
    expected_discharge_at: Optional[datetime]
    status: str
    current_bed_id: Optional[int]
    current_bed_code: Optional[str] = None
    current_room_number: Optional[str] = None
    current_ward_name: Optional[str] = None

    class Config:
        orm_mode = True


class AdmissionIn(BaseModel):
    patient_id: int = Field(..., gt=0)
    department_id: Optional[int] = None
    practitioner_user_id: Optional[int] = None
    primary_nurse_user_id: Optional[int] = None
    admission_type: str = "planned"  # planned/emergency/daycare
    admitted_at: Optional[datetime] = None
    expected_discharge_at: Optional[datetime] = None
    package_id: Optional[int] = None
    payor_type: Optional[str] = "cash"
    insurer_name: Optional[str] = ""
    policy_number: Optional[str] = ""
    preliminary_diagnosis: Optional[str] = ""
    history: Optional[str] = ""
    care_plan: Optional[str] = ""
    bed_id: int = Field(..., gt=0)  # allocate on create

    @validator("admission_type")
    def validate_admission_type(cls, v):
        if v not in ADMISSION_TYPES:
            raise ValueError(f"admission_type must be one of {sorted(ADMISSION_TYPES)}")
        return v

    @validator("payor_type")
    def validate_payor_type(cls, v):
        if v not in PAYOR_TYPES:
            raise ValueError(f"payor_type must be one of {sorted(PAYOR_TYPES)}")
        return v
# -------------------------------------------------------------------------------
# ----------------------------bed transfer---------------------------------------
# -------------------------------------------------------------------------------

class TransferRequestIn(BaseModel):
    # optional at request stage (you can select later in assign step)
    to_bed_id: Optional[int] = Field(None, gt=0)

    transfer_type: str = Field("transfer")
    priority: str = Field("routine")

    reason: str = Field("", max_length=255)
    request_note: Optional[str] = ""

    scheduled_at: Optional[datetime] = None
    reserve_minutes: Optional[int] = Field(30, ge=0, le=24 * 60)  # 0 disables reserve

    @validator("transfer_type")
    def _vt(cls, v):
        if v not in TRANSFER_TYPES:
            raise ValueError(f"transfer_type must be one of {sorted(TRANSFER_TYPES)}")
        return v

    @validator("priority")
    def _vp(cls, v):
        if v not in TRANSFER_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(TRANSFER_PRIORITIES)}")
        return v


class TransferApproveIn(BaseModel):
    approve: bool = True
    approval_note: Optional[str] = ""
    rejected_reason: Optional[str] = ""


class TransferAssignBedIn(BaseModel):
    to_bed_id: int = Field(..., gt=0)
    scheduled_at: Optional[datetime] = None
    reserve_minutes: Optional[int] = Field(30, ge=0, le=24 * 60)


class TransferCompleteIn(BaseModel):
    vacated_at: Optional[datetime] = None
    occupied_at: Optional[datetime] = None

    # store dict → backend will json.dumps()
    handover: Optional[Dict[str, Any]] = None


class TransferCancelIn(BaseModel):
    reason: Optional[str] = ""


class BedLocOut(BaseModel):
    bed_id: Optional[int] = None
    bed_code: Optional[str] = None
    room_id: Optional[int] = None
    room_number: Optional[str] = None
    ward_id: Optional[int] = None
    ward_name: Optional[str] = None
    room_type: Optional[str] = None
    bed_state: Optional[str] = None


class TransferOutV2(BaseModel):
    id: int
    admission_id: int

    status: str
    transfer_type: str
    priority: str

    reason: str
    request_note: str

    scheduled_at: Optional[datetime] = None
    reserved_until: Optional[datetime] = None

    requested_by: int
    requested_at: datetime

    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    approval_note: str = ""

    rejected_reason: str = ""

    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: str = ""

    vacated_at: Optional[datetime] = None
    occupied_at: Optional[datetime] = None
    completed_by: Optional[int] = None
    completed_at: Optional[datetime] = None

    from_assignment_id: Optional[int] = None
    to_assignment_id: Optional[int] = None

    from_location: Optional[BedLocOut] = None
    to_location: Optional[BedLocOut] = None

    handover: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


# ✅ Backward compatibility (old UI still sending TransferIn)
class TransferIn(TransferRequestIn):
    # old required to_bed_id
    to_bed_id: int = Field(..., gt=0)


class TransferOut(TransferOutV2):
    pass


# =====================================================================
# ---------------------- Nursing & Clinical Core ----------------------
# =====================================================================


class NurseMini(BaseModel):
    id: int
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class NursingNoteBase(BaseModel):
    
    note_type: str = Field(
        "routine",
        description="Type of note: routine / incident / shift_handover",
    )
    # 🔹 NABH – core description fields
    # patient_condition: str = Field(
    #     "",
    #     description="Conscious / oriented / stable / drowsy / critical / breathless…",
    # )
    significant_events: str = Field(
        "",
        description="Shifting, fall, vomiting, seizure, desaturation, transfusion, etc.",
    )
    nursing_interventions: str = Field(
        "",
        description="Medications given, oxygen started, IV fluids, dressing, catheter care, etc.",
    )
    response_progress: str = Field(
        "",
        description="Improved / no change / worsened, tolerated procedure, etc.",
    )
    handover_note: str = Field(
        "",
        description="What next nurse should watch / continue.",
    )

    # 🔹 Structured clinical observation fields (replaces clinical_finding)
    # wound_status: str = Field(
    #     "",
    #     description="Clean / dry / soaked / oozing / dressing intact etc.",
    # )
    # oxygen_support: str = Field(
    #     "",
    #     description="Room air / NC 2L / NRBM 10L / BiPAP / Ventilator settings etc.",
    # )
    # urine_output: str = Field(
    #     "",
    #     description="E.g. '200 ml clear in last 4 hrs; catheter in situ'.",
    # )
    # drains_tubes: str = Field(
    #     "",
    #     description="Status of drains / tubes (ICD, RT, etc.).",
    # )
    # pain_score: str = Field(
    #     "",
    #     description="E.g. 'Pain 3/10 on VAS'.",
    # )
    other_findings: str = Field(
        "",
        description="Any other clinical findings not covered above.",
    )

    shift: Optional[str] = Field(
        None,
        description="Morning / Evening / Night",
    )
    is_icu: bool = Field(
        False,
        description="ICU patient? (hourly notes expected).",
    )
     # 🔹 Shift handover specific fields
    vital_signs_summary: str = Field(
        "",
        description="Short summary of vitals at handover.",
    )
    todays_procedures: str = Field(
        "",
        description="Procedures / interventions done today or this shift.",
    )
    current_condition: str = Field(
        "",
        description="Current overall condition at handover.",
    )
    recent_changes: str = Field(
        "",
        description="New symptoms, changes in vitals, changes in treatment during shift.",
    )
    ongoing_treatment: str = Field(
        "",
        description="Ongoing IV fluids, infusions, oxygen, antibiotics, etc.",
    )
    watch_next_shift: str = Field(
        "",
        description="What next shift must watch – risks, pending results, etc.",
    )

class NursingNoteCreate(NursingNoteBase):
    entry_time: Optional[datetime] = Field(
        None,
        description="If not provided, server uses current time.",
    )

    # 💡 How to link vitals:
    # 1) If frontend passes linked_vital_id → link that row
    # 2) Else backend auto-links latest vitals for this admission
    linked_vital_id: Optional[int] = Field(
        None,
        description="Optional – ID of vitals row to link; if omitted, server will link latest vitals for this admission (if exists).",
    )


class NursingNoteUpdate(BaseModel):
    patient_condition: Optional[str] = None
    significant_events: Optional[str] = None
    nursing_interventions: Optional[str] = None
    response_progress: Optional[str] = None
    handover_note: Optional[str] = None

    wound_status: Optional[str] = None
    oxygen_support: Optional[str] = None
    urine_output: Optional[str] = None
    drains_tubes: Optional[str] = None
    pain_score: Optional[str] = None
    other_findings: Optional[str] = None
    
    vital_signs_summary: Optional[str] = None
    todays_procedures: Optional[str] = None
    current_condition: Optional[str] = None
    recent_changes: Optional[str] = None
    ongoing_treatment: Optional[str] = None
    watch_next_shift: Optional[str] = None

    shift: Optional[str] = None
    entry_time: Optional[datetime] = None
    # DO NOT allow changing linked_vital_id from UI easily unless you want to


class NursingNoteOut(NursingNoteBase):
    id: int
    admission_id: int
    nurse_id: int
    entry_time: datetime
    created_at: datetime
    updated_at: datetime
    is_locked: bool

    linked_vital_id: Optional[int] = None
    nurse: Optional[NurseMini] = None
    vitals: Optional[VitalSnapshot] = None  # 👈 for UI

    model_config = ConfigDict(from_attributes=True)


class ShiftHandoverIn(BaseModel):
    vital_signs: Optional[str] = ""
    procedure_undergone: Optional[str] = ""
    todays_diagnostics: Optional[str] = ""
    current_condition: Optional[str] = ""
    recent_changes: Optional[str] = ""
    ongoing_treatment: Optional[str] = ""
    possible_changes: Optional[str] = ""
    other_info: Optional[str] = ""


class ShiftHandoverOut(ShiftHandoverIn):
    id: int
    admission_id: int
    nurse_id: int
    created_at: datetime

    class Config:
        orm_mode = True


# ---------- Base (common fields) ----------
class VitalBase(BaseModel):
    bp_systolic: Optional[int] = Field(
        None, description="Systolic BP in mmHg"
    )
    bp_diastolic: Optional[int] = Field(
        None, description="Diastolic BP in mmHg"
    )
    temp_c: Optional[float] = Field(
        None, description="Temperature in °C"
    )
    rr: Optional[int] = Field(
        None, description="Respiratory rate per minute"
    )
    spo2: Optional[int] = Field(
        None, description="SpO₂ percentage"
    )
    pulse: Optional[int] = Field(
        None, description="Pulse rate per minute"
    )


# ---------- Create (input from UI) ----------
class VitalCreate(VitalBase):
    recorded_at: Optional[datetime] = Field(
        None,
        description="Optional; if not provided, server uses current time",
    )


# ---------- Full output for vitals module ----------
class VitalOut(VitalBase):
    id: int
    admission_id: int
    recorded_by: int
    recorded_at: datetime

    model_config = ConfigDict(from_attributes=True)

class VitalSnapshot(VitalBase):
    id: int
    recorded_at: datetime

    model_config = ConfigDict(from_attributes=True)

class IOIn(BaseModel):
    recorded_at: Optional[datetime] = None

    # ✅ NEW split intake (ml)
    intake_oral_ml: int = Field(0, ge=0)
    intake_iv_ml: int = Field(0, ge=0)
    intake_blood_ml: int = Field(0, ge=0)

    # ✅ NEW split urine (ml)
    urine_foley_ml: int = Field(0, ge=0)
    urine_voided_ml: int = Field(0, ge=0)

    drains_ml: int = Field(0, ge=0)
    stools_count: int = Field(0, ge=0)
    remarks: Optional[str] = ""

    # ✅ legacy totals (still accepted from old UI; also we can store computed totals here)
    intake_ml: int = Field(0, ge=0)
    urine_ml: int = Field(0, ge=0)


class IOOut(IOIn):
    id: int
    admission_id: int
    recorded_by: int
    recorded_at: datetime

    class Config:
        orm_mode = True


class RoundIn(BaseModel):
    notes: Optional[str] = ""


class RoundOut(RoundIn):
    id: int
    admission_id: int
    by_user_id: int
    created_at: datetime

    class Config:
        orm_mode = True


class ProgressIn(BaseModel):
    observation: Optional[str] = ""
    plan: Optional[str] = ""


class ProgressOut(ProgressIn):
    id: int
    admission_id: int
    by_user_id: int
    created_at: datetime

    class Config:
        orm_mode = True


# =====================================================================
# -------------------- NEW: Risk / Clinical Assessments ---------------
# =====================================================================


class PainAssessmentIn(BaseModel):
    recorded_at: Optional[datetime] = None
    scale_type: Optional[str] = None  # NRS, VAS, etc.
    score: Optional[int] = Field(None, ge=0, le=10)
    location: Optional[str] = None
    character: Optional[str] = None
    intervention: Optional[str] = None
    post_intervention_score: Optional[int] = Field(None, ge=0, le=10)


class PainAssessmentOut(PainAssessmentIn):
    id: int
    admission_id: int
    recorded_by: Optional[int]

    class Config:
        orm_mode = True


class FallRiskAssessmentIn(BaseModel):
    recorded_at: Optional[datetime] = None
    tool: Optional[str] = None
    score: Optional[int] = None
    risk_level: Optional[str] = None  # low / moderate / high
    precautions: Optional[str] = None


class FallRiskAssessmentOut(FallRiskAssessmentIn):
    id: int
    admission_id: int
    recorded_by: Optional[int]

    class Config:
        orm_mode = True


class PressureUlcerAssessmentIn(BaseModel):
    recorded_at: Optional[datetime] = None
    tool: Optional[str] = None
    score: Optional[int] = None
    risk_level: Optional[str] = None
    existing_ulcer: bool = False
    site: Optional[str] = None
    stage: Optional[str] = None
    management_plan: Optional[str] = None


class PressureUlcerAssessmentOut(PressureUlcerAssessmentIn):
    id: int
    admission_id: int
    recorded_by: Optional[int]

    class Config:
        orm_mode = True


class NutritionAssessmentIn(BaseModel):
    recorded_at: Optional[datetime] = None
    bmi: Optional[float] = Field(None, ge=0)
    weight_kg: Optional[float] = Field(None, ge=0)
    height_cm: Optional[float] = Field(None, ge=0)
    screening_tool: Optional[str] = None
    score: Optional[int] = None
    risk_level: Optional[str] = None
    dietician_referral: bool = False


class NutritionAssessmentOut(NutritionAssessmentIn):
    id: int
    admission_id: int
    recorded_by: Optional[int]

    class Config:
        orm_mode = True


# =====================================================================
# ----------------- NEW: Orders & Medication / Drug Chart -------------
# =====================================================================


class OrderIn(BaseModel):
    order_type: str  # lab / radiology / procedure / diet / nursing / device
    linked_order_id: Optional[int] = None
    order_text: Optional[str] = ""
    order_status: str = "ordered"
    ordered_at: Optional[datetime] = None
    performed_at: Optional[datetime] = None

    @validator("order_status")
    def validate_order_status(cls, v):
        allowed = {"ordered", "in_progress", "completed", "cancelled"}
        if v not in allowed:
            raise ValueError(f"order_status must be one of {sorted(allowed)}")
        return v


class OrderOut(OrderIn):
    id: int
    admission_id: int
    ordered_by: Optional[int]
    performed_by: Optional[int]

    class Config:
        orm_mode = True


class MedicationOrderBase(BaseModel):
    drug_id: Optional[int] = None
    drug_name: str

    dose: Optional[Decimal] = Field(None, ge=0)
    dose_unit: Optional[str] = ""
    route: Optional[str] = ""
    frequency: Optional[str] = ""
    duration_days: Optional[int] = Field(None, ge=0)

    start_datetime: Optional[datetime] = None
    stop_datetime: Optional[datetime] = None

    special_instructions: Optional[str] = ""

    # NEW: match model column
    order_type: Optional[str] = "regular"  # regular / sos / stat / premed

    order_status: Optional[str] = "active"
    ordered_by_id: Optional[int] = None

    @validator("order_status")
    def validate_med_order_status(cls, v):
        if v is None:
            return v
        allowed = {"active", "stopped", "completed"}
        if v not in allowed:
            raise ValueError(f"order_status must be one of {sorted(allowed)}")
        return v

    @validator("order_type")
    def validate_order_type(cls, v):
        if v is None:
            return v
        allowed = {"regular", "sos", "stat", "premed"}
        if v not in allowed:
            raise ValueError(f"order_type must be one of {sorted(allowed)}")
        return v


class IpdMedicationOrderCreate(MedicationOrderBase):
    admission_id: Optional[int] = None  # set from path in router


class IpdMedicationOrderUpdate(BaseModel):
    # same fields as before, but ensure they include order_type
    drug_id: Optional[int] = None
    drug_name: Optional[str] = None
    dose: Optional[float] = Field(None, ge=0)
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    duration_days: Optional[int] = Field(None, ge=0)
    start_datetime: Optional[datetime] = None
    stop_datetime: Optional[datetime] = None
    special_instructions: Optional[str] = None
    order_status: Optional[str] = None
    order_type: Optional[str] = None
    ordered_by_id: Optional[int] = None

    # validators for order_status + order_type as above
    @validator("order_status")
    def validate_med_order_status(cls, v):
        if v is None:
            return v
        allowed = {"active", "stopped", "completed"}
        if v not in allowed:
            raise ValueError(f"order_status must be one of {sorted(allowed)}")
        return v

    @validator("order_type")
    def validate_order_type(cls, v):
        if v is None:
            return v
        allowed = {"regular", "sos", "stat", "premed"}
        if v not in allowed:
            raise ValueError(f"order_type must be one of {sorted(allowed)}")
        return v


class IpdMedicationOrderOut(MedicationOrderBase):
    id: int
    admission_id: int

    class Config:
        orm_mode = True


class IpdMedicationAdministrationBase(BaseModel):
    admission_id: int
    med_order_id: int
    scheduled_datetime: datetime
    given_status: str = "pending"
    given_datetime: Optional[datetime] = None
    given_by: Optional[int] = None
    remarks: Optional[str] = None

    @validator("given_status")
    def validate_given_status(cls, v):
        allowed = {"pending", "given", "missed", "refused", "held"}
        if v not in allowed:
            raise ValueError(f"given_status must be one of {sorted(allowed)}")
        return v


class IpdMedicationAdministrationCreate(IpdMedicationAdministrationBase):
    pass


class IpdMedicationAdministrationUpdate(BaseModel):
    scheduled_datetime: Optional[datetime] = None
    given_status: Optional[str] = None
    given_datetime: Optional[datetime] = None
    given_by: Optional[int] = None
    remarks: Optional[str] = None

    @validator("given_status")
    def validate_given_status(cls, v):
        if v is None:
            return v
        allowed = {"pending", "given", "missed", "refused", "held"}
        if v not in allowed:
            raise ValueError(f"given_status must be one of {sorted(allowed)}")
        return v


class IpdMedicationAdministrationOut(IpdMedicationAdministrationBase):
    id: int

    class Config:
        orm_mode = True





# =====================================================================
# --------------------------- Discharge -------------------------------
# =====================================================================


# ---------------- Discharge Summary ----------------

class DischargeSummaryBase(BaseModel):
    # NOTE: demographics is NOT editable from UI now, so not in Base/In.
    # It will still exist in the model & be auto-filled for PDF.

    medical_history: Optional[str] = ""
    treatment_summary: Optional[str] = ""
    medications: Optional[str] = ""
    follow_up: Optional[str] = ""
    icd10_codes: Optional[str] = ""  # human readable, one per line

    # A. MUST-HAVE
    final_diagnosis_primary: Optional[str] = ""
    final_diagnosis_secondary: Optional[str] = ""
    hospital_course: Optional[str] = ""
    discharge_condition: Optional[str] = "stable"
    discharge_type: Optional[str] = "routine"
    allergies: Optional[str] = ""

    # B. Recommended
    procedures: Optional[str] = ""
    investigations: Optional[str] = ""
    diet_instructions: Optional[str] = ""
    activity_instructions: Optional[str] = ""
    warning_signs: Optional[str] = ""
    referral_details: Optional[str] = ""

    # C. Operational / billing
    insurance_details: Optional[str] = ""
    stay_summary: Optional[str] = ""
    patient_ack_name: Optional[str] = ""
    patient_ack_datetime: Optional[datetime] = None

    # D. Doctor & system validation
    prepared_by_name: Optional[str] = ""
    reviewed_by_name: Optional[str] = ""
    reviewed_by_regno: Optional[str] = ""
    discharge_datetime: Optional[datetime] = None

    # E. Safety & quality
    implants: Optional[str] = ""
    pending_reports: Optional[str] = ""
    patient_education: Optional[str] = ""
    # followup_appointment_ref: Optional[str] = ""


class DischargeSummaryIn(DischargeSummaryBase):
    # UI flag to mark as finalized
    finalize: bool = False


class DischargeSummaryOut(DischargeSummaryBase):
    id: int
    admission_id: int

    # system fields
    finalized: bool
    finalized_by: Optional[int]
    finalized_at: Optional[datetime]

    # OPTIONAL: expose demographics only in OUT (for PDFs/debug)
    demographics: Optional[str] = ""

    class Config:
        orm_mode = True


# ---------------- Discharge Checklist ----------------

class DischargeChecklistIn(BaseModel):
    financial_clearance: Optional[bool] = None
    clinical_clearance: Optional[bool] = None
    delay_reason: Optional[str] = None
    submit: Optional[bool] = False


class DischargeChecklistOut(DischargeChecklistIn):
    id: int
    admission_id: int
    submitted: bool
    submitted_at: Optional[datetime]

    class Config:
        orm_mode = True


# ---------------- Due Discharges (queue) ----------------

class DueDischargeOut(BaseModel):
    admission_id: int
    patient_id: int
    expected_discharge_at: Optional[datetime]
    status: str

    class Config:
        orm_mode = True


# ---------------- Structured Discharge Medications ----------------

class DischargeMedicationIn(BaseModel):
    drug_name: str
    dose: Optional[float] = Field(None, ge=0)
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    duration_days: Optional[int] = Field(None, ge=0)
    advice_text: Optional[str] = None


class DischargeMedicationOut(DischargeMedicationIn):
    id: int
    admission_id: int

    class Config:
        orm_mode = True



# =====================================================================
# ------------------------------- OT ----------------------------------
# =====================================================================


class OtCaseIn(BaseModel):
    admission_id: int
    surgery_name: str
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    status: Optional[str] = "planned"
    surgeon_id: Optional[int] = None
    anaesthetist_id: Optional[int] = None
    staff_tags: Optional[str] = ""
    preop_notes: Optional[str] = ""


class OtCaseForAdmissionIn(BaseModel):
    surgery_name: str
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    status: Optional[str] = "planned"  # planned/unplanned/cancelled/completed
    surgeon_id: Optional[int] = None
    anaesthetist_id: Optional[int] = None
    staff_tags: Optional[str] = ""
    preop_notes: Optional[str] = ""


class OtCaseOut(OtCaseIn):
    id: int
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    postop_notes: Optional[str] = ""
    instrument_tracking: Optional[str] = ""

    class Config:
        orm_mode = True


class AnaesthesiaIn(BaseModel):
    ot_case_id: int
    pre_assessment: Optional[str] = ""
    anaesthesia_type: Optional[str] = "general"
    intraop_monitoring: Optional[str] = ""  # JSON
    drugs_administered: Optional[str] = ""  # JSON
    post_status: Optional[str] = ""


class AnaesthesiaOut(AnaesthesiaIn):
    id: int

    class Config:
        orm_mode = True


# =====================================================================
# ---------------------- Bed Charge Preview ---------------------------
# =====================================================================


class BedChargeDay(BaseModel):
    date: date
    bed_id: Optional[int]
    room_type: str
    rate: float
    assignment_id: Optional[int]


class BedChargePreviewOut(BaseModel):
    admission_id: int
    from_date: date
    to_date: date
    days: List[BedChargeDay]
    total_amount: float
    missing_rate_days: int = 0


# =====================================================================
# ----------------------------- Feedback -------------------------------
# =====================================================================


class IpdFeedbackIn(BaseModel):
    rating_overall: Optional[int] = Field(None, ge=1, le=5)
    rating_nursing: Optional[int] = Field(None, ge=1, le=5)
    rating_doctor: Optional[int] = Field(None, ge=1, le=5)
    rating_cleanliness: Optional[int] = Field(None, ge=1, le=5)
    comments: Optional[str] = None


class IpdFeedbackOut(IpdFeedbackIn):
    id: int
    admission_id: int
    patient_id: int
    collected_at: datetime
    collected_by: Optional[int]

    class Config:
        orm_mode = True


# ===========================================
# Assessments
# ===========================================
class IpdAssessmentBase(BaseModel):
    assessment_type: str = "nursing"
    assessed_at: Optional[datetime] = None
    summary: Optional[str] = None
    plan: Optional[str] = None


class IpdAssessmentCreate(IpdAssessmentBase):
    pass


class IpdAssessmentOut(IpdAssessmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admission_id: int
    created_by_id: Optional[int] = None
    created_at: datetime


# ===========================================
# Medications
# ===========================================
class IpdMedicationBase(BaseModel):
    drug_name: str
    route: str = "oral"
    frequency: str = "od"
    dose: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    instructions: Optional[str] = None
    status: Optional[str] = "active"


class IpdMedicationCreate(IpdMedicationBase):
    pass


class IpdMedicationUpdate(BaseModel):
    drug_name: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    dose: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    instructions: Optional[str] = None
    status: Optional[str] = None


class IpdMedicationOut(IpdMedicationBase):
    id: int
    admission_id: int
    created_by_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


# ===========================================
# Discharge Medications
# ===========================================
class IpdDischargeMedicationBase(BaseModel):
    drug_name: str
    dose: Optional[Decimal] = None
    dose_unit: Optional[str] = ""
    route: Optional[str] = ""
    frequency: Optional[str] = ""
    duration_days: Optional[int] = None
    advice_text: Optional[str] = ""


class IpdDischargeMedicationCreate(IpdDischargeMedicationBase):
    pass


class IpdDischargeMedicationOut(IpdDischargeMedicationBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admission_id: int
    created_by_id: Optional[int] = None
    created_at: datetime


# ===========================================
# Feedback
# ===========================================
class IpdAdmissionFeedbackBase(BaseModel):
    rating_nursing: Optional[int] = None
    rating_doctor: Optional[int] = None
    rating_cleanliness: Optional[int] = None
    comments: Optional[str] = None
    suggestions: Optional[str] = None


class IpdAdmissionFeedbackCreate(IpdAdmissionFeedbackBase):
    pass


class IpdAdmissionFeedbackOut(IpdAdmissionFeedbackBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admission_id: int
    created_by_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

class RestraintRecordBase(BaseModel):
    type: str = ""  # physical / chemical
    reason: str = ""
    start_time: datetime
    end_time: Optional[datetime] = None
    monitoring_notes: str = ""


class RestraintRecordIn(RestraintRecordBase):
    pass


class RestraintRecordOut(RestraintRecordBase):
    id: int
    admission_id: int
    doctor_order_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# -------------------------------
# Isolation Precautions
# -------------------------------
class IsolationPrecautionBase(BaseModel):
    indication: str = ""         # Airborne / Droplet / Contact
    start_date: datetime
    end_date: Optional[datetime] = None
    measures: str = ""           # mask / gown / etc.
    status: str = "active"       # active / stopped


class IsolationPrecautionIn(IsolationPrecautionBase):
    pass


class IsolationPrecautionOut(IsolationPrecautionBase):
    id: int
    admission_id: int

    model_config = ConfigDict(from_attributes=True)


# -------------------------------
# ICU Flow Sheet
# -------------------------------
class IcuFlowSheetBase(BaseModel):
    recorded_at: Optional[datetime] = None

    vital_data: str = ""            # JSON/string
    ventilator_settings: str = ""   # JSON/string
    infusions: str = ""             # JSON/string

    gcs_score: Optional[int] = None
    urine_output_ml: Optional[int] = None
    notes: str = ""


class IcuFlowSheetIn(IcuFlowSheetBase):
    pass


class IcuFlowSheetOut(IcuFlowSheetBase):
    id: int
    admission_id: int
    recorded_by: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
    
class DischargeSummaryIn(BaseModel):
    demographics: str = ""
    medical_history: str = ""
    treatment_summary: str = ""
    medications: str = ""
    follow_up: str = ""
    icd10_codes: str = ""

    final_diagnosis_primary: str = ""
    final_diagnosis_secondary: str = ""
    hospital_course: str = ""
    discharge_condition: str = "stable"
    discharge_type: str = "routine"
    allergies: str = ""

    procedures: str = ""
    investigations: str = ""
    diet_instructions: str = ""
    activity_instructions: str = ""
    warning_signs: str = ""
    referral_details: str = ""

    insurance_details: str = ""
    stay_summary: str = ""

    patient_ack_name: str = ""
    patient_ack_datetime: Optional[datetime] = None

    prepared_by_name: str = ""
    reviewed_by_name: str = ""
    reviewed_by_regno: str = ""
    discharge_datetime: Optional[datetime] = None

    implants: str = ""
    pending_reports: str = ""
    patient_education: str = ""
    # followup_appointment_ref: str = ""

    finalize: bool = False


class DischargeSummaryOut(BaseModel):
    id: int
    admission_id: int

    demographics: str = ""
    medical_history: str = ""
    treatment_summary: str = ""
    medications: str = ""
    follow_up: str = ""
    icd10_codes: str = ""

    final_diagnosis_primary: str | None = None
    final_diagnosis_secondary: str | None = None
    hospital_course: str | None = None
    discharge_condition: str | None = None
    discharge_type: str | None = None
    allergies: str | None = None

    procedures: str | None = None
    investigations: str | None = None
    diet_instructions: str | None = None
    activity_instructions: str | None = None
    warning_signs: str | None = None
    referral_details: str | None = None

    insurance_details: str | None = None
    stay_summary: str | None = None
    patient_ack_name: str | None = None
    patient_ack_datetime: Optional[datetime] = None

    prepared_by_name: str | None = None
    reviewed_by_name: str | None = None
    reviewed_by_regno: str | None = None
    discharge_datetime: Optional[datetime] = None

    implants: str | None = None
    pending_reports: str | None = None
    patient_education: str | None = None
    # followup_appointment_ref: str | None = None

    finalized: bool = False
    finalized_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
    
    @field_validator("patient_ack_datetime", "discharge_datetime", mode="before")
    @classmethod
    def _normalize_zero_datetime(cls, v):
        # Accept already-parsed None/datetime
        if v is None:
            return None
        # In case driver gives a string "0000-00-00 00:00:00"
        if isinstance(v, str) and v.startswith("0000-00-00"):
            return None
        return v
    
class IpdDrugChartMetaBase(BaseModel):
    admission_id: int

    allergic_to: Optional[str] = ""
    diagnosis: Optional[str] = ""

    weight_kg: Optional[Decimal] = Field(None, ge=0)
    height_cm: Optional[Decimal] = Field(None, ge=0)
    blood_group: Optional[str] = ""
    bsa: Optional[Decimal] = Field(None, ge=0)
    bmi: Optional[Decimal] = Field(None, ge=0)

    oral_fluid_per_day_ml: Optional[int] = Field(None, ge=0)
    salt_gm_per_day: Optional[Decimal] = Field(None, ge=0)
    calorie_per_day_kcal: Optional[int] = Field(None, ge=0)
    protein_gm_per_day: Optional[Decimal] = Field(None, ge=0)
    diet_remarks: Optional[str] = ""


class IpdDrugChartMetaCreate(IpdDrugChartMetaBase):

    @model_validator(mode="before")
    @classmethod
    def auto_calc_bmi(cls, values: dict):
        weight = values.get("weight_kg")
        height_cm = values.get("height_cm")
        bmi = values.get("bmi")

        if bmi is None and weight is not None and height_cm is not None and height_cm > 0:
            h_m = float(height_cm) / 100.0
            bmi_val = float(weight) / (h_m * h_m)
            values["bmi"] = Decimal(str(round(bmi_val, 2)))

        return values


class IpdDrugChartMetaUpdate(BaseModel):
    allergic_to: Optional[str] = None
    diagnosis: Optional[str] = None
    weight_kg: Optional[Decimal] = Field(None, ge=0)
    height_cm: Optional[Decimal] = Field(None, ge=0)
    blood_group: Optional[str] = None
    bsa: Optional[Decimal] = Field(None, ge=0)
    bmi: Optional[Decimal] = Field(None, ge=0)
    oral_fluid_per_day_ml: Optional[int] = Field(None, ge=0)
    salt_gm_per_day: Optional[Decimal] = Field(None, ge=0)
    calorie_per_day_kcal: Optional[int] = Field(None, ge=0)
    protein_gm_per_day: Optional[Decimal] = Field(None, ge=0)
    diet_remarks: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def auto_calc_bmi(cls, values):
        # Only recompute if bmi not provided but weight+height changed
        if values.get("bmi") is not None:
            return values
        weight = values.get("weight_kg")
        height_cm = values.get("height_cm")
        if weight is not None and height_cm is not None and height_cm > 0:
            h_m = float(height_cm) / 100.0
            bmi_val = float(weight) / (h_m * h_m)
            values["bmi"] = Decimal(str(round(bmi_val, 2)))
        return values


class IpdDrugChartMetaOut(IpdDrugChartMetaBase):
    id: int

    class Config:
        orm_mode = True

class IpdIvFluidOrderBase(BaseModel):
    admission_id: int

    ordered_datetime: Optional[datetime] = None

    fluid: str
    additive: Optional[str] = ""
    dose_ml: Optional[Decimal] = Field(None, ge=0)
    rate_ml_per_hr: Optional[Decimal] = Field(None, ge=0)

    doctor_name: Optional[str] = ""
    doctor_id: Optional[int] = None

    start_datetime: Optional[datetime] = None
    start_nurse_name: Optional[str] = ""
    start_nurse_id: Optional[int] = None

    stop_datetime: Optional[datetime] = None
    stop_nurse_name: Optional[str] = ""
    stop_nurse_id: Optional[int] = None

    remarks: Optional[str] = ""


class IpdIvFluidOrderCreate(IpdIvFluidOrderBase):
    admission_id: Optional[int] = None


class IpdIvFluidOrderUpdate(BaseModel):
    fluid: Optional[str] = None
    additive: Optional[str] = None
    dose_ml: Optional[Decimal] = Field(None, ge=0)
    rate_ml_per_hr: Optional[Decimal] = Field(None, ge=0)
    doctor_name: Optional[str] = None
    doctor_id: Optional[int] = None
    ordered_datetime: Optional[datetime] = None
    start_datetime: Optional[datetime] = None
    start_nurse_name: Optional[str] = None
    start_nurse_id: Optional[int] = None
    stop_datetime: Optional[datetime] = None
    stop_nurse_name: Optional[str] = None
    stop_nurse_id: Optional[int] = None
    remarks: Optional[str] = None


class IpdIvFluidOrderOut(IpdIvFluidOrderBase):
    id: int

    class Config:
        orm_mode = True

class IpdDrugChartNurseRowBase(BaseModel):
    admission_id: int
    serial_no: Optional[int] = None
    nurse_name: str
    specimen_sign: Optional[str] = ""
    emp_no: Optional[str] = ""


class IpdDrugChartNurseRowCreate(IpdDrugChartNurseRowBase):
    pass


class IpdDrugChartNurseRowUpdate(BaseModel):
    serial_no: Optional[int] = None
    nurse_name: Optional[str] = None
    specimen_sign: Optional[str] = None
    emp_no: Optional[str] = None


class IpdDrugChartNurseRowOut(IpdDrugChartNurseRowBase):
    id: int

    class Config:
        orm_mode = True

class IpdDrugChartDoctorAuthBase(BaseModel):
    admission_id: int
    auth_date: date

    doctor_name: Optional[str] = ""
    doctor_id: Optional[int] = None
    doctor_sign: Optional[str] = ""
    remarks: Optional[str] = ""


class IpdDrugChartDoctorAuthCreate(IpdDrugChartDoctorAuthBase):
    pass


class IpdDrugChartDoctorAuthUpdate(BaseModel):
    auth_date: Optional[date] = None
    doctor_name: Optional[str] = None
    doctor_id: Optional[int] = None
    doctor_sign: Optional[str] = None
    remarks: Optional[str] = None


class IpdDrugChartDoctorAuthOut(IpdDrugChartDoctorAuthBase):
    id: int

    class Config:
        orm_mode = True


# ===========================================
# Clinical Notes Update
# ===========================================
class ClinicalNotesUpdateIn(BaseModel):
    preliminary_diagnosis: Optional[str] = None
    history: Optional[str] = None
    care_plan: Optional[str] = None
