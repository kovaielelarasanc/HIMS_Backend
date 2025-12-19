# app/schemas/opd.py
from datetime import date, time, datetime
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field, ConfigDict


# ---------- Schedules ----------
class OpdScheduleBase(BaseModel):
    doctor_user_id: int
    weekday: int  # 0=Mon .. 6=Sun
    start_time: time
    end_time: time
    slot_minutes: int = 15
    location: Optional[str] = ""
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class OpdScheduleCreate(OpdScheduleBase):
    pass


class OpdScheduleUpdate(BaseModel):
    weekday: Optional[int] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    slot_minutes: Optional[int] = None
    location: Optional[str] = None
    is_active: Optional[bool] = None


class OpdScheduleOut(OpdScheduleBase):
    id: int


# ---------- Slots ----------
class SlotOut(BaseModel):
    start: str  # "HH:MM"
    end: str  # "HH:MM"


# ---------- Appointments ----------
class AppointmentCreate(BaseModel):
    patient_id: int
    department_id: int
    doctor_user_id: int
    date: date

    # ✅ NEW: slot | free
    appointment_type: str = Field("slot", description="slot | free")

    # ✅ slot_start optional now (required only when appointment_type='slot')
    slot_start: Optional[str] = Field(
        None, description="HH:MM (24h), required for slot")
    slot_end: Optional[str] = None

    purpose: Optional[str] = "Consultation"


class AppointmentOut(BaseModel):
    id: int
    date: date
    appointment_type: str
    queue_no: int

    slot_start: Optional[time] = None
    slot_end: Optional[time] = None
    status: str
    purpose: Optional[str] = None

    patient: Dict[str, Any] = Field(default_factory=dict)
    doctor: Dict[str, Any] = Field(default_factory=dict)
    department: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class AppointmentRow(BaseModel):
    id: int
    queue_no: int
    appointment_type: str

    uhid: str
    patient_name: str
    doctor_name: str
    department_name: str
    date: str

    slot_start: str
    slot_end: str
    status: str
    visit_id: Optional[int] = None
    vitals_registered: bool
    purpose: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class AppointmentRescheduleIn(BaseModel):
    date: date
    # ✅ optional now (free appointments don’t need it)
    slot_start: Optional[str] = Field(
        None, description="HH:MM (24h), required for slot appointments")
    create_new: bool = False


class AppointmentStatusUpdate(BaseModel):
    # booked | checked_in | in_progress | completed | no_show | cancelled
    status: str


class DoctorWeekdaysOut(BaseModel):
    doctor_user_id: int
    weekdays: List[int]  # e.g. [0,2,4]

    model_config = ConfigDict(from_attributes=True)


# ---------- Doctor fees ----------
class DoctorFeeBase(BaseModel):
    doctor_user_id: int
    base_fee: float = Field(..., ge=0)
    followup_fee: Optional[float] = Field(None, ge=0)
    currency: str = Field("INR", max_length=8)
    is_active: bool = True
    notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DoctorFeeCreate(DoctorFeeBase):
    pass


class DoctorFeeUpdate(BaseModel):
    base_fee: Optional[float] = Field(None, ge=0)
    followup_fee: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, max_length=8)
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class DoctorFeeOut(DoctorFeeBase):
    id: int
    doctor_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------- Visits ----------
class VisitCreate(BaseModel):
    appointment_id: int


class VitalsIn(BaseModel):
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    bmi: Optional[float] = None
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    rr: Optional[int] = None
    temp_c: Optional[float] = None
    spo2: Optional[int] = None
    notes: Optional[str] = None


class VisitOut(BaseModel):
    id: int
    uhid: str
    patient_name: str
    department_name: str
    doctor_name: str
    episode_id: str
    visit_at: str

    patient_id: int
    doctor_id: int
    appointment_id: Optional[int] = None
    appointment_status: Optional[str] = None
    current_vitals: Optional[Dict[str, Any]] = None

    # existing
    chief_complaint: Optional[str] = None
    symptoms: Optional[str] = None
    soap_subjective: Optional[str] = None
    soap_objective: Optional[str] = None
    soap_assessment: Optional[str] = None
    plan: Optional[str] = None

    # NEW
    presenting_illness: Optional[str] = None
    review_of_systems: Optional[str] = None

    medical_history: Optional[str] = None
    surgical_history: Optional[str] = None
    medication_history: Optional[str] = None
    drug_allergy: Optional[str] = None

    family_history: Optional[str] = None
    personal_history: Optional[str] = None

    menstrual_history: Optional[str] = None
    obstetric_history: Optional[str] = None
    immunization_history: Optional[str] = None

    general_examination: Optional[str] = None
    systemic_examination: Optional[str] = None
    local_examination: Optional[str] = None

    provisional_diagnosis: Optional[str] = None
    differential_diagnosis: Optional[str] = None
    final_diagnosis: Optional[str] = None
    diagnosis_codes: Optional[str] = None

    investigations: Optional[str] = None
    treatment_plan: Optional[str] = None
    advice: Optional[str] = None
    followup_plan: Optional[str] = None
    referral_notes: Optional[str] = None
    procedure_notes: Optional[str] = None
    counselling_notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class VisitUpdate(BaseModel):
    # existing
    chief_complaint: Optional[str] = None
    symptoms: Optional[str] = None
    soap_subjective: Optional[str] = None
    soap_objective: Optional[str] = None
    soap_assessment: Optional[str] = None
    plan: Optional[str] = None

    # NEW
    presenting_illness: Optional[str] = None
    review_of_systems: Optional[str] = None

    medical_history: Optional[str] = None
    surgical_history: Optional[str] = None
    medication_history: Optional[str] = None
    drug_allergy: Optional[str] = None

    family_history: Optional[str] = None
    personal_history: Optional[str] = None

    menstrual_history: Optional[str] = None
    obstetric_history: Optional[str] = None
    immunization_history: Optional[str] = None

    general_examination: Optional[str] = None
    systemic_examination: Optional[str] = None
    local_examination: Optional[str] = None

    provisional_diagnosis: Optional[str] = None
    differential_diagnosis: Optional[str] = None
    final_diagnosis: Optional[str] = None
    diagnosis_codes: Optional[str] = None

    investigations: Optional[str] = None
    treatment_plan: Optional[str] = None
    advice: Optional[str] = None
    followup_plan: Optional[str] = None
    referral_notes: Optional[str] = None
    procedure_notes: Optional[str] = None
    counselling_notes: Optional[str] = None


class RxItemIn(BaseModel):
    drug_name: str
    strength: Optional[str] = ""
    frequency: Optional[str] = ""
    duration_days: int = 0
    quantity: int = 0
    unit_price: float = 0.0


class PrescriptionIn(BaseModel):
    items: List[RxItemIn] = Field(default_factory=list)
    notes: Optional[str] = None


# ---------- Orders ----------
class OrderIdsIn(BaseModel):
    test_ids: List[int]


# ---------- Masters ----------
class MedicineOut(BaseModel):
    id: int
    name: str
    form: Optional[str] = None
    unit: Optional[str] = None
    price_per_unit: float

    model_config = ConfigDict(from_attributes=True)


class TestOut(BaseModel):
    id: int
    code: str
    name: str
    price: float

    model_config = ConfigDict(from_attributes=True)


# ---------- Follow-up ----------
class FollowUpCreate(BaseModel):
    """
    Called from clinical screen after doctor finishes Visit.
    """
    due_date: date
    note: Optional[str] = None


class FollowUpUpdate(BaseModel):
    due_date: date
    note: Optional[str] = None


class FollowUpScheduleIn(BaseModel):
    """
    Used on waiting-time management screen to confirm a waiting follow-up
    into a real Appointment.

    - `date` is optional: if not sent, backend uses follow-up.due_date
    - `slot_start` should be "HH:MM" but we keep it Optional
      so that Pydantic never throws 422 before our route logic.
    """

    model_config = ConfigDict(extra="ignore")

    date: Optional[date] = None
    slot_start: Optional[str] = None


class FollowUpRow(BaseModel):
    """
    Flattened row for FE grids (waiting list, follow-up MIS, etc.)
    """

    id: int
    visit_id: int
    appointment_id: Optional[int]

    due_date: date
    status: str

    patient_id: int
    patient_uhid: str
    patient_name: str

    doctor_id: int
    doctor_name: str

    department_id: int
    department_name: str

    note: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class FollowUpListItem(BaseModel):
    id: int
    due_date: Optional[date] = None
    status: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    source_visit_id: Optional[int] = None
    source_episode_id: Optional[str] = None

    class Config:
        from_attributes = True