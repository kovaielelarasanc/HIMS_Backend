from __future__ import annotations
from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel

# ---------------- Masters ----------------


class WardIn(BaseModel):
    name: str
    code: str
    floor: Optional[str] = ""


class WardOut(WardIn):
    id: int
    is_active: bool = True

    class Config:
        orm_mode = True


class RoomIn(BaseModel):
    ward_id: int
    number: str
    type: Optional[str] = "General"


class RoomOut(RoomIn):
    id: int
    is_active: bool = True

    class Config:
        orm_mode = True


class BedIn(BaseModel):
    room_id: int
    code: str


class BedOut(BedIn):
    id: int
    state: str
    reserved_until: Optional[datetime]
    note: str = ""

    class Config:
        orm_mode = True


class PackageIn(BaseModel):
    name: str
    included: Optional[str] = ""
    excluded: Optional[str] = ""
    charges: float = 0.0


class PackageOut(PackageIn):
    id: int

    class Config:
        orm_mode = True


# ---------------- Bed Rates ----------------


class BedRateIn(BaseModel):
    room_type: str  # e.g., "General", "Private", "ICU"
    daily_rate: float
    effective_from: date
    effective_to: Optional[date] = None  # inclusive; null = open-ended


class BedRateOut(BedRateIn):
    id: int
    is_active: bool = True

    class Config:
        orm_mode = True


# ---------------- Admissions ----------------
# NOTE: add `admitted_at` (manual entry allowed)


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


class AdmissionOut(BaseModel):
    id: int
    patient_id: int
    admission_type: str
    admitted_at: datetime
    expected_discharge_at: Optional[datetime]
    current_bed_id: Optional[int]
    status: str

    class Config:
        orm_mode = True


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


class AdmissionIn(BaseModel):
    patient_id: int
    department_id: Optional[int] = None
    practitioner_user_id: Optional[int] = None
    # NOTE: keep primary_nurse_user_id optional; FE can simply omit it
    primary_nurse_user_id: Optional[int] = None
    admission_type: str = "planned"  # planned/emergency/daycare
    # Allow manual admission timestamp (for pre-booked / preoccupied flows)
    admitted_at: Optional[datetime] = None
    expected_discharge_at: Optional[datetime] = None
    package_id: Optional[int] = None
    payor_type: Optional[str] = "cash"
    insurer_name: Optional[str] = ""
    policy_number: Optional[str] = ""
    preliminary_diagnosis: Optional[str] = ""
    history: Optional[str] = ""
    care_plan: Optional[str] = ""
    bed_id: int  # allocate on create


class TransferIn(BaseModel):
    to_bed_id: int
    reason: Optional[str] = ""


class TransferOut(BaseModel):
    id: int
    admission_id: int
    from_bed_id: Optional[int]
    to_bed_id: int
    reason: str
    transferred_at: datetime

    class Config:
        orm_mode = True


# ---------------- Nursing & Clinical ----------------


class NursingNoteIn(BaseModel):
    entry_time: Optional[datetime] = None
    patient_condition: Optional[str] = ""
    clinical_finding: Optional[str] = ""
    significant_events: Optional[str] = ""
    response_progress: Optional[str] = ""


class NursingNoteOut(NursingNoteIn):
    id: int
    admission_id: int
    nurse_id: int
    entry_time: datetime

    class Config:
        orm_mode = True





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


class VitalIn(BaseModel):
    recorded_at: Optional[datetime] = None
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    temp_c: Optional[float] = None
    rr: Optional[int] = None
    spo2: Optional[int] = None
    pulse: Optional[int] = None


class VitalOut(VitalIn):
    id: int
    admission_id: int
    recorded_by: int
    recorded_at: datetime

    class Config:
        orm_mode = True


class IOIn(BaseModel):
    recorded_at: Optional[datetime] = None
    intake_ml: int = 0
    urine_ml: int = 0
    drains_ml: int = 0
    stools_count: int = 0
    remarks: Optional[str] = ""


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


# ---------------- Discharge ----------------


class DischargeSummaryIn(BaseModel):
    demographics: Optional[str] = ""
    medical_history: Optional[str] = ""
    treatment_summary: Optional[str] = ""
    medications: Optional[str] = ""
    follow_up: Optional[str] = ""
    icd10_codes: Optional[str] = ""  # CSV/JSON text
    finalize: bool = False


class DischargeSummaryOut(DischargeSummaryIn):
    id: int
    admission_id: int
    finalized: bool
    finalized_by: Optional[int]
    finalized_at: Optional[datetime]

    class Config:
        orm_mode = True


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


class DueDischargeOut(BaseModel):
    admission_id: int
    patient_id: int
    expected_discharge_at: Optional[datetime]
    status: str

    class Config:
        orm_mode = True


# ---------------- Referrals ----------------


class ReferralIn(BaseModel):
    type: Optional[str] = "internal"
    to_department: Optional[str] = ""
    to_user_id: Optional[int] = None
    external_org: Optional[str] = ""
    reason: Optional[str] = ""


class ReferralOut(ReferralIn):
    id: int
    admission_id: int
    status: str

    class Config:
        orm_mode = True


# ---------------- OT ----------------


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


# ---------------- Bed Charge Preview ----------------


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
