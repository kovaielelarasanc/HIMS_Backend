from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any, Generic, TypeVar, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator
import re

T = TypeVar("T")

class ApiResponse(BaseModel, Generic[T]):
    status: bool = True
    data: T

class ApiError(BaseModel):
    status: bool = False
    error: Dict[str, Any]

Sex = Literal["Male", "Female", "Transgender", "Unknown"]

_time_re = re.compile(r"^\d{2}:\d{2}$")

def _strip(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    x = v.strip()
    return x if x else None

class VaccineItem(BaseModel):
    given: bool = False
    at: Optional[datetime] = None
    batch: Optional[str] = None
    remarks: Optional[str] = None

class Vaccination(BaseModel):
    model_config = ConfigDict(extra="ignore")
    BCG: Optional[VaccineItem] = None
    OPV: Optional[VaccineItem] = None
    HepB: Optional[VaccineItem] = None

class ResuscitationJSON(BaseModel):
    model_config = ConfigDict(extra="allow")
    suction: Optional[bool] = None
    stimulation: Optional[bool] = None
    bag_mask: Optional[bool] = None
    oxygen: Optional[bool] = None
    intubation: Optional[bool] = None
    chest_compressions: Optional[bool] = None
    drugs: Optional[bool] = None
    notes: Optional[str] = None

class NewbornCreate(BaseModel):
    admission_id: int

    birth_register_id: Optional[int] = None
    baby_patient_id: Optional[int] = None
    mother_patient_id: Optional[int] = None

    mother_name: Optional[str] = None
    mother_age_years: Optional[int] = Field(default=None, ge=10, le=60)
    mother_blood_group: Optional[str] = None
    gravida: Optional[int] = Field(default=None, ge=0, le=20)
    para: Optional[int] = Field(default=None, ge=0, le=20)
    living: Optional[int] = Field(default=None, ge=0, le=20)
    abortion: Optional[int] = Field(default=None, ge=0, le=20)

    lmp_date: Optional[date] = None
    edd_date: Optional[date] = None

    hiv_status: Optional[str] = None
    vdrl_status: Optional[str] = None
    hbsag_status: Optional[str] = None

    thyroid: Optional[str] = None
    pih: Optional[bool] = None
    gdm: Optional[bool] = None
    fever: Optional[bool] = None

    other_illness: Optional[str] = None
    drug_intake: Optional[str] = None
    antenatal_steroid: Optional[str] = None

    gestational_age_weeks: Optional[int] = Field(default=None, ge=10, le=45)
    consanguinity: Optional[str] = None
    mode_of_conception: Optional[str] = None
    prev_sibling_neonatal_period: Optional[str] = None
    referred_from: Optional[str] = None
    amniotic_fluid: Optional[str] = None

    date_of_birth: Optional[date] = None
    time_of_birth: Optional[str] = None
    sex: Optional[Sex] = None

    birth_weight_kg: Optional[float] = Field(default=None, ge=0.3, le=8.0)
    length_cm: Optional[float] = Field(default=None, ge=10.0, le=70.0)
    head_circum_cm: Optional[float] = Field(default=None, ge=10.0, le=50.0)

    mode_of_delivery: Optional[str] = None
    baby_cried_at_birth: Optional[bool] = None

    apgar_1_min: Optional[int] = Field(default=None, ge=0, le=10)
    apgar_5_min: Optional[int] = Field(default=None, ge=0, le=10)
    apgar_10_min: Optional[int] = Field(default=None, ge=0, le=10)

    resuscitation: Optional[ResuscitationJSON] = None
    resuscitation_notes: Optional[str] = None

    hr: Optional[int] = Field(default=None, ge=0, le=300)
    rr: Optional[int] = Field(default=None, ge=0, le=200)
    cft_seconds: Optional[float] = Field(default=None, ge=0, le=20)
    sao2: Optional[int] = Field(default=None, ge=0, le=100)
    sugar_mgdl: Optional[int] = Field(default=None, ge=0, le=1000)

    cvs: Optional[str] = None
    rs: Optional[str] = None
    icr: Optional[bool] = None
    scr: Optional[bool] = None
    grunting: Optional[bool] = None
    apnea: Optional[bool] = None
    downes_score: Optional[int] = Field(default=None, ge=0, le=10)

    pa: Optional[str] = None

    cns_cry: Optional[str] = None
    cns_activity: Optional[str] = None
    cns_af: Optional[str] = None
    cns_reflexes: Optional[str] = None
    cns_tone: Optional[str] = None

    musculoskeletal: Optional[str] = None
    spine_cranium: Optional[str] = None
    genitalia: Optional[str] = None

    diagnosis: Optional[str] = None
    treatment: Optional[str] = None

    oxygen: Optional[str] = None
    warmth: Optional[str] = None
    feed_initiation: Optional[str] = None

    vitamin_k_given: Optional[bool] = None
    vitamin_k_at: Optional[datetime] = None
    vitamin_k_remarks: Optional[str] = None

    others: Optional[str] = None
    vitals_monitor: Optional[str] = None

    vaccination: Optional[Vaccination] = None

    @field_validator("time_of_birth", mode="before")
    @classmethod
    def v_time(cls, v):
        v = _strip(v)
        if not v:
            return v
        if not _time_re.match(v):
            raise ValueError("time_of_birth must be HH:MM")
        return v

class NewbornUpdate(NewbornCreate):
    admission_id: Optional[int] = None  # not editable in update

class ActionNote(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)

class VoidRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=500)

class NewbornOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    admission_id: int
    birth_register_id: Optional[int] = None
    baby_patient_id: Optional[int] = None
    mother_patient_id: Optional[int] = None

    mother_name: Optional[str] = None
    mother_age_years: Optional[int] = None
    mother_blood_group: Optional[str] = None
    gravida: Optional[int] = None
    para: Optional[int] = None
    living: Optional[int] = None
    abortion: Optional[int] = None
    lmp_date: Optional[date] = None
    edd_date: Optional[date] = None
    hiv_status: Optional[str] = None
    vdrl_status: Optional[str] = None
    hbsag_status: Optional[str] = None
    thyroid: Optional[str] = None
    pih: Optional[bool] = None
    gdm: Optional[bool] = None
    fever: Optional[bool] = None
    other_illness: Optional[str] = None
    drug_intake: Optional[str] = None
    antenatal_steroid: Optional[str] = None
    gestational_age_weeks: Optional[int] = None
    consanguinity: Optional[str] = None
    mode_of_conception: Optional[str] = None
    prev_sibling_neonatal_period: Optional[str] = None
    referred_from: Optional[str] = None
    amniotic_fluid: Optional[str] = None

    date_of_birth: Optional[date] = None
    time_of_birth: Optional[str] = None
    sex: Optional[str] = None
    birth_weight_kg: Optional[float] = None
    length_cm: Optional[float] = None
    head_circum_cm: Optional[float] = None
    mode_of_delivery: Optional[str] = None
    baby_cried_at_birth: Optional[bool] = None
    apgar_1_min: Optional[int] = None
    apgar_5_min: Optional[int] = None
    apgar_10_min: Optional[int] = None
    resuscitation: Optional[Dict[str, Any]] = None
    resuscitation_notes: Optional[str] = None

    hr: Optional[int] = None
    rr: Optional[int] = None
    cft_seconds: Optional[float] = None
    sao2: Optional[int] = None
    sugar_mgdl: Optional[int] = None
    cvs: Optional[str] = None
    rs: Optional[str] = None
    icr: Optional[bool] = None
    scr: Optional[bool] = None
    grunting: Optional[bool] = None
    apnea: Optional[bool] = None
    downes_score: Optional[int] = None
    pa: Optional[str] = None

    cns_cry: Optional[str] = None
    cns_activity: Optional[str] = None
    cns_af: Optional[str] = None
    cns_reflexes: Optional[str] = None
    cns_tone: Optional[str] = None

    musculoskeletal: Optional[str] = None
    spine_cranium: Optional[str] = None
    genitalia: Optional[str] = None

    diagnosis: Optional[str] = None
    treatment: Optional[str] = None
    oxygen: Optional[str] = None
    warmth: Optional[str] = None
    feed_initiation: Optional[str] = None

    vitamin_k_given: Optional[bool] = None
    vitamin_k_at: Optional[datetime] = None
    vitamin_k_remarks: Optional[str] = None

    others: Optional[str] = None
    vitals_monitor: Optional[str] = None
    vaccination: Optional[Dict[str, Any]] = None

    status: str
    locked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    finalized_at: Optional[datetime] = None
