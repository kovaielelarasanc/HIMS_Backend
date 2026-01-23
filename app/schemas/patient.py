# FILE: app/schemas/patient.py
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


def _norm_str(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _norm_rch_id(v: Optional[str]) -> Optional[str]:
    v = _norm_str(v)
    if not v:
        return None
    # remove spaces and hyphens, uppercase
    v = v.replace(" ", "").replace("-", "").upper()
    return v or None


def _is_female(gender: Optional[str]) -> bool:
    g = (gender or "").strip().lower()
    return g in {"female", "f", "woman", "women", "girl"}


class AddressBase(BaseModel):
    type: Optional[str] = "current"
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = "India"


class AddressIn(AddressBase):
    pass


class AddressOut(AddressBase):
    id: int
    patient_id: int

    model_config = ConfigDict(from_attributes=True)


class DocumentOut(BaseModel):
    id: int
    patient_id: int
    type: Optional[str] = None
    filename: str
    mime: Optional[str] = None
    size: int
    uploaded_by: Optional[int] = None
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConsentIn(BaseModel):
    type: str
    text: str


class ConsentOut(BaseModel):
    id: int
    patient_id: int
    type: str
    text: str
    captured_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Patient Type master ----------

class PatientTypeBase(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class PatientTypeCreate(PatientTypeBase):
    pass


class PatientTypeUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class PatientTypeOut(PatientTypeBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# ---------- Patient core ----------

class PatientCreate(BaseModel):
    # mandatory core fields
    prefix: str
    first_name: str
    last_name: Optional[str] = None
    gender: str
    dob: date
    marital_status: str
    phone: str
    email: Optional[EmailStr] = None
    patient_type: str  # value should come from Patient Type master

    # optional
    aadhar_last4: Optional[str] = None
    blood_group: Optional[str] = None

    ref_source: Optional[str] = None
    ref_doctor_id: Optional[int] = None
    ref_details: Optional[str] = None

    id_proof_type: Optional[str] = None
    id_proof_no: Optional[str] = None

    guardian_name: Optional[str] = None
    guardian_phone: Optional[str] = None
    guardian_relation: Optional[str] = None

    tag: Optional[str] = None
    religion: Optional[str] = None
    occupation: Optional[str] = None

    file_number: Optional[str] = None
    file_location: Optional[str] = None

    credit_type: Optional[str] = None
    credit_payer_id: Optional[int] = None
    credit_tpa_id: Optional[int] = None
    credit_plan_id: Optional[int] = None

    principal_member_name: Optional[str] = None
    principal_member_address: Optional[str] = None

    policy_number: Optional[str] = None
    policy_name: Optional[str] = None

    family_id: Optional[int] = None

    # only for create
    address: Optional[AddressIn] = None

    # --- Pregnancy (optional) ---
    is_pregnant: Optional[bool] = False
    rch_id: Optional[str] = None  # optional even if pregnant

    # --------- validators for mandatory & formats ----------

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Prefix is required")
        return v

    @field_validator("first_name")
    @classmethod
    def validate_first_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Patient name is required")
        return v

    @field_validator("marital_status")
    @classmethod
    def validate_marital_status(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Marital status is required")
        return v

    @field_validator("patient_type")
    @classmethod
    def validate_patient_type(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Patient type is required")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) != 10:
            raise ValueError("Mobile number must be exactly 10 digits")
        return digits

    @field_validator("dob")
    @classmethod
    def validate_dob(cls, v: date) -> date:
        today = date.today()
        if v > today:
            raise ValueError("DOB cannot be in the future")
        if today.year - v.year > 120:
            raise ValueError("DOB is too far in the past")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v):
        return _norm_str(v)

    @field_validator("ref_source", "ref_details", mode="before")
    @classmethod
    def normalize_reference_fields(cls, v):
        return _norm_str(v)

    @field_validator("rch_id", mode="before")
    @classmethod
    def normalize_rch_id(cls, v):
        return _norm_rch_id(v)

    @field_validator("rch_id")
    @classmethod
    def validate_rch_id_chars(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        # allow only letters+digits (after normalization)
        if not all(ch.isalnum() for ch in v):
            raise ValueError("RCH ID must contain only letters and digits")
        if len(v) > 32:
            raise ValueError("RCH ID is too long (max 32)")
        return v

    @field_validator("is_pregnant")
    @classmethod
    def validate_pregnancy_with_gender(cls, is_pregnant: Optional[bool], info):
        # If pregnant True, gender must be female (strong rule)
        # Note: info.data contains already-validated fields in v2
        gender = info.data.get("gender") if hasattr(info, "data") else None
        if is_pregnant and not _is_female(gender):
            raise ValueError("Pregnancy can be marked only for Female patients")
        return is_pregnant


class PatientUpdate(BaseModel):
    prefix: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    aadhar_last4: Optional[str] = None
    blood_group: Optional[str] = None

    marital_status: Optional[str] = None

    ref_source: Optional[str] = None
    ref_doctor_id: Optional[int] = None
    ref_details: Optional[str] = None

    id_proof_type: Optional[str] = None
    id_proof_no: Optional[str] = None

    guardian_name: Optional[str] = None
    guardian_phone: Optional[str] = None
    guardian_relation: Optional[str] = None

    patient_type: Optional[str] = None
    tag: Optional[str] = None
    religion: Optional[str] = None
    occupation: Optional[str] = None

    file_number: Optional[str] = None
    file_location: Optional[str] = None

    credit_type: Optional[str] = None
    credit_payer_id: Optional[int] = None
    credit_tpa_id: Optional[int] = None
    credit_plan_id: Optional[int] = None

    principal_member_name: Optional[str] = None
    principal_member_address: Optional[str] = None

    policy_number: Optional[str] = None
    policy_name: Optional[str] = None

    family_id: Optional[int] = None

    # --- Pregnancy (optional) ---
    is_pregnant: Optional[bool] = None
    rch_id: Optional[str] = None
    address: Optional[AddressIn] = None

    @field_validator("phone")
    @classmethod
    def validate_phone_update(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) != 10:
            raise ValueError("Mobile number must be exactly 10 digits")
        return digits

    @field_validator("dob")
    @classmethod
    def validate_dob_update(cls, v: Optional[date]) -> Optional[date]:
        if v is None:
            return v
        today = date.today()
        if v > today:
            raise ValueError("DOB cannot be in the future")
        if today.year - v.year > 120:
            raise ValueError("DOB is too far in the past")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email_update(cls, v):
        return _norm_str(v)

    @field_validator("ref_source", "ref_details", mode="before")
    @classmethod
    def normalize_reference_fields_update(cls, v):
        return _norm_str(v)

    @field_validator("rch_id", mode="before")
    @classmethod
    def normalize_rch_id_update(cls, v):
        return _norm_rch_id(v)

    @field_validator("rch_id")
    @classmethod
    def validate_rch_id_chars_update(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        if not all(ch.isalnum() for ch in v):
            raise ValueError("RCH ID must contain only letters and digits")
        if len(v) > 32:
            raise ValueError("RCH ID is too long (max 32)")
        return v


class PatientOut(BaseModel):
    id: int
    uhid: str
    abha_number: Optional[str] = None

    prefix: Optional[str] = None
    first_name: str
    last_name: Optional[str] = None
    gender: str
    dob: Optional[date] = None
    blood_group: Optional[str] = None

    phone: Optional[str] = None
    email: Optional[EmailStr] = None

    marital_status: Optional[str] = None

    ref_source: Optional[str] = None
    ref_doctor_id: Optional[int] = None
    ref_details: Optional[str] = None

    id_proof_type: Optional[str] = None
    id_proof_no: Optional[str] = None

    guardian_name: Optional[str] = None
    guardian_phone: Optional[str] = None
    guardian_relation: Optional[str] = None

    patient_type: Optional[str] = None
    tag: Optional[str] = None
    religion: Optional[str] = None
    occupation: Optional[str] = None

    file_number: Optional[str] = None
    file_location: Optional[str] = None

    credit_type: Optional[str] = None
    credit_payer_id: Optional[int] = None
    credit_tpa_id: Optional[int] = None
    credit_plan_id: Optional[int] = None

    principal_member_name: Optional[str] = None
    principal_member_address: Optional[str] = None

    policy_number: Optional[str] = None
    policy_name: Optional[str] = None

    family_id: Optional[int] = None

    # --- Pregnancy / RCH ---
    is_pregnant: Optional[bool] = False
    rch_id: Optional[str] = None

    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # computed at API level
    age_years: Optional[int] = None
    age_months: Optional[int] = None
    age_days: Optional[int] = None
    age_text: Optional[str] = None  # "24 years 5 months 16 days"
    age_short_text: Optional[str] = None  # "24 yrs"

    # resolved / display-only fields (from masters)
    ref_doctor_name: Optional[str] = None
    credit_payer_name: Optional[str] = None
    credit_tpa_name: Optional[str] = None
    credit_plan_name: Optional[str] = None

    # for detail view
    addresses: List[AddressOut] = []

    model_config = ConfigDict(from_attributes=True)
