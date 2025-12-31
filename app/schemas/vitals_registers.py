from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any, List, Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


Sex = Literal["Male", "Female", "Transgender", "Unknown"]
Status = Literal["DRAFT", "VERIFIED", "FINALIZED", "SUBMITTED", "VOIDED"]


def _strip(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    x = s.strip()
    return x if x else None


class Address(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None


# --------------------
# Common outputs
# --------------------
class UserLite(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: Optional[str] = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entity_type: str
    entity_id: int
    action: str
    actor_user_id: Optional[int] = None
    note: Optional[str] = None
    created_at: datetime


# --------------------
# Birth Register
# --------------------
class BirthCreate(BaseModel):
    birth_datetime: datetime
    place_of_birth: str = "Hospital"
    delivery_method: Optional[str] = None
    gestation_weeks: Optional[int] = Field(default=None, ge=10, le=45)
    birth_weight_kg: Optional[str] = None
    child_sex: Sex
    child_name: Optional[str] = None
    plurality: Optional[int] = Field(default=None, ge=1, le=8)
    birth_order: Optional[int] = Field(default=None, ge=1, le=8)

    mother_name: str
    mother_age_years: Optional[int] = Field(default=None, ge=10, le=60)
    mother_dob: Optional[date] = None
    mother_id_no: Optional[str] = None
    mother_mobile: Optional[str] = None
    mother_address: Optional[Address] = None

    father_name: Optional[str] = None
    father_age_years: Optional[int] = Field(default=None, ge=10, le=80)
    father_dob: Optional[date] = None
    father_id_no: Optional[str] = None
    father_mobile: Optional[str] = None
    father_address: Optional[Address] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    informant_user_id: Optional[int] = None
    informant_name: Optional[str] = None
    informant_designation: Optional[str] = None

    @field_validator("mother_name", "father_name", "child_name", "place_of_birth", mode="before")
    @classmethod
    def v_strip_names(cls, v):
        return _strip(v)

    @field_validator("mother_mobile", "father_mobile", mode="before")
    @classmethod
    def v_mobile(cls, v):
        v = _strip(v)
        if not v:
            return v
        # basic sanity; don’t hard fail for country formats, just guard junk
        if len(v) < 8 or len(v) > 15:
            raise ValueError("Invalid mobile number length")
        return v


class BirthUpdate(BaseModel):
    # Only allowed while DRAFT or VERIFIED (controlled in routes)
    place_of_birth: Optional[str] = None
    delivery_method: Optional[str] = None
    gestation_weeks: Optional[int] = Field(default=None, ge=10, le=45)
    birth_weight_kg: Optional[str] = None
    child_sex: Optional[Sex] = None
    child_name: Optional[str] = None
    plurality: Optional[int] = Field(default=None, ge=1, le=8)
    birth_order: Optional[int] = Field(default=None, ge=1, le=8)

    mother_name: Optional[str] = None
    mother_age_years: Optional[int] = Field(default=None, ge=10, le=60)
    mother_dob: Optional[date] = None
    mother_id_no: Optional[str] = None
    mother_mobile: Optional[str] = None
    mother_address: Optional[Address] = None

    father_name: Optional[str] = None
    father_age_years: Optional[int] = Field(default=None, ge=10, le=80)
    father_dob: Optional[date] = None
    father_id_no: Optional[str] = None
    father_mobile: Optional[str] = None
    father_address: Optional[Address] = None

    informant_user_id: Optional[int] = None
    informant_name: Optional[str] = None
    informant_designation: Optional[str] = None


class BirthSubmit(BaseModel):
    crs_registration_unit: Optional[str] = None
    crs_registration_no: Optional[str] = None
    crs_registration_date: Optional[date] = None
    crs_ack_ref: Optional[str] = None


class BirthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    internal_no: str
    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    birth_datetime: datetime
    place_of_birth: str
    delivery_method: Optional[str] = None
    gestation_weeks: Optional[int] = None
    birth_weight_kg: Optional[str] = None
    child_sex: Sex
    child_name: Optional[str] = None
    plurality: Optional[int] = None
    birth_order: Optional[int] = None

    mother_name: str
    mother_age_years: Optional[int] = None
    mother_dob: Optional[date] = None
    mother_id_no: Optional[str] = None
    mother_mobile: Optional[str] = None
    mother_address: Optional[Dict[str, Any]] = None

    father_name: Optional[str] = None
    father_age_years: Optional[int] = None
    father_dob: Optional[date] = None
    father_id_no: Optional[str] = None
    father_mobile: Optional[str] = None
    father_address: Optional[Dict[str, Any]] = None

    informant_user_id: Optional[int] = None
    informant_name: Optional[str] = None
    informant_designation: Optional[str] = None

    status: str
    locked_at: Optional[datetime] = None

    crs_registration_unit: Optional[str] = None
    crs_registration_no: Optional[str] = None
    crs_registration_date: Optional[date] = None
    crs_ack_ref: Optional[str] = None

    verified_by_user_id: Optional[int] = None
    verified_at: Optional[datetime] = None
    finalized_by_user_id: Optional[int] = None
    finalized_at: Optional[datetime] = None
    submitted_by_user_id: Optional[int] = None
    submitted_at: Optional[datetime] = None

    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------
# Stillbirth
# --------------------
class StillBirthCreate(BaseModel):
    event_datetime: datetime
    place_of_occurrence: str = "Hospital"
    gestation_weeks: Optional[int] = Field(default=None, ge=10, le=45)
    foetus_sex: Optional[Sex] = None

    mother_name: str
    mother_age_years: Optional[int] = Field(default=None, ge=10, le=60)
    mother_address: Optional[Address] = None

    father_name: Optional[str] = None
    father_address: Optional[Address] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None


class StillBirthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    internal_no: str
    event_datetime: datetime
    place_of_occurrence: str
    gestation_weeks: Optional[int] = None
    foetus_sex: Optional[str] = None
    mother_name: str
    mother_age_years: Optional[int] = None
    mother_address: Optional[Dict[str, Any]] = None
    father_name: Optional[str] = None
    father_address: Optional[Dict[str, Any]] = None
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------
# Death + MCCD
# --------------------
Manner = Literal["Natural", "Accident", "Homicide", "Suicide", "Undetermined"]


class DeathCreate(BaseModel):
    death_datetime: datetime
    place_of_death: str = "Hospital"
    ward_or_unit: Optional[str] = None

    deceased_name: str
    sex: Sex
    age_years: Optional[int] = Field(default=None, ge=0, le=125)
    dob: Optional[date] = None

    address: Optional[Address] = None
    id_no: Optional[str] = None
    mobile: Optional[str] = None

    manner_of_death: Optional[Manner] = None

    patient_id: Optional[int] = None
    admission_id: Optional[int] = None


class DeathUpdate(BaseModel):
    place_of_death: Optional[str] = None
    ward_or_unit: Optional[str] = None

    deceased_name: Optional[str] = None
    sex: Optional[Sex] = None
    age_years: Optional[int] = Field(default=None, ge=0, le=125)
    dob: Optional[date] = None

    address: Optional[Address] = None
    id_no: Optional[str] = None
    mobile: Optional[str] = None

    manner_of_death: Optional[Manner] = None


class MCCDCreateOrUpdate(BaseModel):
    immediate_cause: str
    antecedent_cause: Optional[str] = None
    underlying_cause: Optional[str] = None
    other_significant_conditions: Optional[str] = None
    pregnancy_status: Optional[str] = None
    tobacco_use: Optional[Literal["Yes", "No", "Unknown"]] = None

    @field_validator("immediate_cause", mode="before")
    @classmethod
    def v_req(cls, v):
        v = _strip(v)
        if not v:
            raise ValueError("Immediate cause is required")
        if len(v) > 255:
            raise ValueError("Immediate cause too long")
        return v


class DeathSubmit(BaseModel):
    crs_registration_unit: Optional[str] = None
    crs_registration_no: Optional[str] = None
    crs_registration_date: Optional[date] = None
    crs_ack_ref: Optional[str] = None


class MCCDOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    immediate_cause: str
    antecedent_cause: Optional[str] = None
    underlying_cause: Optional[str] = None
    other_significant_conditions: Optional[str] = None
    pregnancy_status: Optional[str] = None
    tobacco_use: Optional[str] = None
    certifying_doctor_user_id: Optional[int] = None
    certified_at: Optional[datetime] = None
    signed: bool
    signed_at: Optional[datetime] = None


class DeathOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    internal_no: str
    patient_id: Optional[int] = None
    admission_id: Optional[int] = None

    death_datetime: datetime
    place_of_death: str
    ward_or_unit: Optional[str] = None

    deceased_name: str
    sex: Sex
    age_years: Optional[int] = None
    dob: Optional[date] = None

    address: Optional[Dict[str, Any]] = None
    id_no: Optional[str] = None
    mobile: Optional[str] = None
    manner_of_death: Optional[str] = None

    status: str
    locked_at: Optional[datetime] = None

    crs_registration_unit: Optional[str] = None
    crs_registration_no: Optional[str] = None
    crs_registration_date: Optional[date] = None
    crs_ack_ref: Optional[str] = None

    mccd_given_to_kin: bool
    mccd_given_to_name: Optional[str] = None
    mccd_given_at: Optional[datetime] = None

    mccd: Optional[MCCDOut] = None

    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------
# Verify / Finalize / Void
# --------------------
class ActionNote(BaseModel):
    note: Optional[str] = None


class VoidRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


# --------------------
# Amendment
# --------------------
class AmendmentCreate(BaseModel):
    entity_type: Literal["birth", "death", "stillbirth"]
    entity_id: int
    requested_changes: Dict[str, Dict[str, Any]]  # {"field": {"from":..., "to":...}}
    reason: str = Field(min_length=5, max_length=2000)


class AmendmentReview(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    review_note: Optional[str] = Field(default=None, max_length=2000)


class AmendmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entity_type: str
    entity_id: int
    requested_changes: Dict[str, Any]
    reason: str
    status: str
    requested_by_user_id: Optional[int] = None
    reviewed_by_user_id: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None
    created_at: datetime
