# app/schemas/opd.py
from typing import List, Optional
from datetime import date, time, datetime
from pydantic import BaseModel, Field


# ---------- Schedules ----------
class OpdScheduleBase(BaseModel):
    doctor_user_id: int
    weekday: int  # 0=Mon .. 6=Sun
    start_time: time
    end_time: time
    slot_minutes: int = 15
    location: Optional[str] = ""
    is_active: bool = True


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

    class Config:
        orm_mode = True


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
    # routes_opd.py expects strings like "HH:MM"
    slot_start: str = Field(..., description="HH:MM (24h)")
    # optional; some backends compute from schedule
    slot_end: Optional[str] = Field(None, description="HH:MM (24h)")
    purpose: Optional[str] = "Consultation"


class AppointmentOut(BaseModel):
    id: int
    date: date
    slot_start: time
    slot_end: time
    status: str
    purpose: Optional[str]
    patient: dict = Field(default_factory=dict)
    doctor: dict = Field(default_factory=dict)
    department: dict = Field(default_factory=dict)

    class Config:
        orm_mode = True


class AppointmentStatusUpdate(BaseModel):
    # booked | checked_in | in_progress | completed | no_show | cancelled
    status: str


class DoctorWeekdaysOut(BaseModel):
    doctor_user_id: int
    weekdays: List[int]  # e.g. [0,2,4]


# ⬇️ **This is the piece you were missing**
class AppointmentRow(BaseModel):
    id: int
    uhid: str
    patient_name: str
    doctor_name: str
    department_name: str
    date: str  # "YYYY-MM-DD"
    slot_start: str  # "HH:MM"
    slot_end: str  # "HH:MM"
    status: str
    visit_id: Optional[int] = None
    vitals_registered: bool
    purpose: Optional[str] = None  # NEW

    class Config:
        orm_mode = True


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
    # created_at: datetime


class VisitOut(BaseModel):
    id: int
    uhid: str
    patient_name: str
    department_name: str
    doctor_name: str
    episode_id: str
    visit_at: str

    # NEW fields so FE can show controls safely
    patient_id: int
    doctor_id: int
    appointment_id: Optional[int] = None
    appointment_status: Optional[str] = None

    # If you're already returning vitals to FE:
    current_vitals: Optional[dict] = None

    chief_complaint: Optional[str] = None
    symptoms: Optional[str] = None
    soap_subjective: Optional[str] = None
    soap_objective: Optional[str] = None
    soap_assessment: Optional[str] = None
    plan: Optional[str] = None

    class Config:
        orm_mode = True


class VisitUpdate(BaseModel):
    chief_complaint: Optional[str] = None
    symptoms: Optional[str] = None
    soap_subjective: Optional[str] = None
    soap_objective: Optional[str] = None
    soap_assessment: Optional[str] = None
    plan: Optional[str] = None


# ---------- Vitals ----------


# ---------- Prescription ----------
class RxItemIn(BaseModel):
    drug_name: str
    strength: Optional[str] = ""
    frequency: Optional[str] = ""
    duration_days: int = 0
    quantity: int = 0
    unit_price: float = 0.0


class PrescriptionIn(BaseModel):
    items: List[RxItemIn] = []
    notes: Optional[str] = None


# ---------- Orders ----------
class OrderIdsIn(BaseModel):
    test_ids: List[int]


# ---------- Masters ----------
class MedicineOut(BaseModel):
    id: int
    name: str
    form: Optional[str]
    unit: Optional[str]
    price_per_unit: float

    class Config:
        orm_mode = True


class TestOut(BaseModel):
    id: int
    code: str
    name: str
    price: float

    class Config:
        orm_mode = True
