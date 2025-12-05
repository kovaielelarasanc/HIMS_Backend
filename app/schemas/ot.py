# FILE: app/schemas/ot.py
from __future__ import annotations

from datetime import date, time, datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

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


# ---------- OtEnvironmentSetting ----------


class OtEnvironmentSettingBase(BaseModel):
    theatre_id: int
    min_temperature_c: Optional[float] = None
    max_temperature_c: Optional[float] = None
    min_humidity_percent: Optional[float] = None
    max_humidity_percent: Optional[float] = None
    min_pressure_diff_pa: Optional[float] = None
    max_pressure_diff_pa: Optional[float] = None


class OtEnvironmentSettingCreate(OtEnvironmentSettingBase):
    pass


class OtEnvironmentSettingUpdate(BaseModel):
    theatre_id: Optional[int] = None
    min_temperature_c: Optional[float] = None
    max_temperature_c: Optional[float] = None
    min_humidity_percent: Optional[float] = None
    max_humidity_percent: Optional[float] = None
    min_pressure_diff_pa: Optional[float] = None
    max_pressure_diff_pa: Optional[float] = None


class OtEnvironmentSettingOut(OtEnvironmentSettingBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ---------- OtTheatre ----------


class OtTheatreBase(BaseModel):
    code: str = Field(..., max_length=50)
    name: str = Field(..., max_length=255)
    location: Optional[str] = Field(None, max_length=255)
    speciality_id: Optional[int] = None
    is_active: bool = True


class OtTheatreCreate(OtTheatreBase):
    pass


class OtTheatreUpdate(BaseModel):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=255)
    location: Optional[str] = Field(None, max_length=255)
    speciality_id: Optional[int] = None
    is_active: Optional[bool] = None


class OtTheatreOut(OtTheatreBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ============================================================
#  CORE TRANSACTIONS: SCHEDULE & CASE
# ============================================================

# ---------- OtSchedule ----------


class OtScheduleBase(BaseModel):
    theatre_id: int
    theatre: Optional[OtTheatreOut] = None
    date: date
    planned_start_time: time
    planned_end_time: Optional[time] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None
    patient: Optional[OtSchedulePatientOut] = None
    surgeon_user_id: int
    anaesthetist_user_id: Optional[int] = None
    surgeon: Optional[OtScheduleUserOut] = None
    anaesthetist_user_id: Optional[int] = None
    anaesthetist: Optional[OtScheduleUserOut] = None
    procedure_name: str = Field(..., max_length=255)
    side: Optional[str] = Field(None,
                                max_length=50)  # Left / Right / Bilateral / NA
    priority: str = Field(default="Elective",
                          max_length=50)  # Elective / Emergency
    status: str = Field(
        default="planned",
        max_length=50)  # planned / in_progress / completed / cancelled
    notes: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class OtScheduleCreate(OtScheduleBase):
    # ignore status from client, always start as 'planned'
    status: str = Field(default="planned", max_length=50)


class OtSchedulePatientOut(BaseModel):
    """
    Minimal patient info returned along with an OT schedule.
    This should match fields on app.models.patient.Patient.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    uhid: Optional[str] = None

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None  # if you have a hybrid_property

    gender: Optional[str] = None  # 'M' / 'F' / 'O' or full text
    dob: Optional[date] = None
    age_years: Optional[
        int] = None  # optional helper fields if you compute them
    age_months: Optional[int] = None

    phone: Optional[str] = None


class OtScheduleUpdate(BaseModel):
    theatre_id: Optional[int] = None
    date: Optional[date] = None
    planned_start_time: Optional[time] = None
    planned_end_time: Optional[time] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    surgeon_user_id: Optional[int] = None
    anaesthetist_user_id: Optional[int] = None

    procedure_name: Optional[str] = None
    side: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


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


class OtScheduleOut(OtScheduleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


# ---------- OtCase ----------


class OtCaseBase(BaseModel):
    schedule_id: int
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
    pass


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


# ---------- SurgicalSafetyChecklist ----------


class SurgicalSafetyChecklistBase(BaseModel):
    case_id: int

    sign_in_data: Optional[JsonValue] = None
    sign_in_done_by_id: Optional[int] = None
    sign_in_time: Optional[datetime] = None

    time_out_data: Optional[JsonValue] = None
    time_out_done_by_id: Optional[int] = None
    time_out_time: Optional[datetime] = None

    sign_out_data: Optional[JsonValue] = None
    sign_out_done_by_id: Optional[int] = None
    sign_out_time: Optional[datetime] = None


class SurgicalSafetyChecklistCreate(SurgicalSafetyChecklistBase):
    pass


class SurgicalSafetyChecklistUpdate(BaseModel):
    case_id: Optional[int] = None

    sign_in_data: Optional[JsonValue] = None
    sign_in_done_by_id: Optional[int] = None
    sign_in_time: Optional[datetime] = None

    time_out_data: Optional[JsonValue] = None
    time_out_done_by_id: Optional[int] = None
    time_out_time: Optional[datetime] = None

    sign_out_data: Optional[JsonValue] = None
    sign_out_done_by_id: Optional[int] = None
    sign_out_time: Optional[datetime] = None


class SurgicalSafetyChecklistOut(SurgicalSafetyChecklistBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


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


class AnaesthesiaRecordOut(AnaesthesiaRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


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


# ---------- OtNursingRecord ----------


class OtNursingRecordBase(BaseModel):
    case_id: int
    primary_nurse_id: int

    positioning: Optional[str] = Field(None, max_length=255)
    skin_prep_details: Optional[str] = None
    catheter_details: Optional[str] = None
    drains_details: Optional[str] = None
    notes: Optional[str] = None


class OtNursingRecordCreate(OtNursingRecordBase):
    pass


class OtNursingRecordUpdate(BaseModel):
    case_id: Optional[int] = None
    primary_nurse_id: Optional[int] = None

    positioning: Optional[str] = Field(None, max_length=255)
    skin_prep_details: Optional[str] = None
    catheter_details: Optional[str] = None
    drains_details: Optional[str] = None
    notes: Optional[str] = None


class OtNursingRecordOut(OtNursingRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


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


class OperationNoteBase(BaseModel):
    case_id: int
    surgeon_user_id: int

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


# ---------- PacuRecord ----------


class PacuRecordBase(BaseModel):
    case_id: int
    nurse_user_id: int

    admission_time: Optional[datetime] = None
    discharge_time: Optional[datetime] = None
    pain_scores: Optional[JsonValue] = None  # {time: score}
    vitals: Optional[JsonValue] = None  # time-series or summary
    complications: Optional[str] = None
    disposition: Optional[str] = Field(None,
                                       max_length=100)  # ward / ICU / etc.


class PacuRecordCreate(PacuRecordBase):
    pass


class PacuRecordUpdate(BaseModel):
    case_id: Optional[int] = None
    nurse_user_id: Optional[int] = None

    admission_time: Optional[datetime] = None
    discharge_time: Optional[datetime] = None
    pain_scores: Optional[JsonValue] = None
    vitals: Optional[JsonValue] = None
    complications: Optional[str] = None
    disposition: Optional[str] = Field(None, max_length=100)


class PacuRecordOut(PacuRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ============================================================
#  OT ADMIN / STATUTORY LOGS
# ============================================================

# ---------- OtEquipmentDailyChecklist ----------


class OtEquipmentDailyChecklistBase(BaseModel):
    theatre_id: int
    date: date
    shift: Optional[str] = Field(None,
                                 max_length=50)  # Morning / Evening / Night
    checked_by_user_id: int
    data: JsonValue  # {equipment_id or code: {ok: bool, remark: str}}


class OtEquipmentDailyChecklistCreate(OtEquipmentDailyChecklistBase):
    pass


class OtEquipmentDailyChecklistUpdate(BaseModel):
    theatre_id: Optional[int] = None
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
    theatre_id: int
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
    theatre_id: Optional[int] = None
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


# ---------- OtEnvironmentLog ----------


class OtEnvironmentLogBase(BaseModel):
    theatre_id: int
    date: date
    time: time

    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    pressure_diff_pa: Optional[float] = None

    logged_by_user_id: int


class OtEnvironmentLogCreate(OtEnvironmentLogBase):
    pass


class OtEnvironmentLogUpdate(BaseModel):
    theatre_id: Optional[int] = None
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
