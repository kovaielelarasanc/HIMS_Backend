# FILE: app/schemas/patient.py
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, EmailStr


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


class PatientCreate(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    gender: str
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

    patient_type: Optional[str] = "none"
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


class PatientUpdate(BaseModel):
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


class PatientOut(BaseModel):
    id: int
    uhid: str
    abha_number: Optional[str] = None

    first_name: str
    last_name: Optional[str] = None
    gender: str
    dob: Optional[date] = None
    blood_group: Optional[str] = None

    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    aadhar_last4: Optional[str] = None

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
