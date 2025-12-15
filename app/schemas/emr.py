from __future__ import annotations
from typing import List, Literal, Optional, Dict, Any
from datetime import datetime, date
from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------
# Shared DTOs
# ----------------------------------------------------------------------


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    label: str
    url: str
    content_type: Optional[str] = None
    note: Optional[str] = None
    size_bytes: Optional[int] = None


TimelineType = Literal[
    # OPD
    "opd_appointment",
    "opd_visit",
    "opd_vitals",
    "rx",
    "opd_lab_order",
    "opd_radiology_order",
    "followup",

    # LIS / RIS
    "lab",
    "radiology",

    # Pharmacy
    "pharmacy_rx",
    "pharmacy",

    # IPD
    "ipd_admission",
    "ipd_transfer",
    "ipd_discharge",
    "ipd_vitals",
    "ipd_nursing_note",
    "ipd_intake_output",
    "ipd_round",
    "ipd_progress",
    "ipd_risk",
    "ipd_med_order",
    "ipd_mar",
    "ipd_iv_fluid",

    # OT / Billing / Docs
    "ot",
    "billing",
    "attachment",
    "consent",
]


class TimelineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: TimelineType
    ts: datetime
    title: str
    subtitle: Optional[str] = None
    doctor_name: Optional[str] = None
    department_name: Optional[str] = None
    location_name: Optional[str] = None
    status: Optional[str] = None
    ref_kind: Optional[str] = None
    ref_display: Optional[str] = None
    attachments: List[AttachmentOut] = Field(default_factory=list)

    # NEW: full details per module (SOAP, vitals, items, amounts, etc.)
    # Keep it flexible so you can add fields without schema migrations.
    data: Dict[str, Any] = Field(default_factory=dict)


class TimelineFilterIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    patient_id: int
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    types: Optional[List[TimelineType]] = None
    doctor_user_id: Optional[int] = None  # backend filter only


class PatientMiniOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    uhid: str
    abha_number: Optional[str] = None
    name: str
    gender: str
    dob: Optional[date] = None
    phone: Optional[str] = None


class PatientLookupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    results: List[PatientMiniOut]


# ----------------------------------------------------------------------
# Export (PDF / JSON-FHIR)
# ----------------------------------------------------------------------


class EmrExportSections(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    opd: bool = True
    ipd: bool = True
    vitals: bool = True
    prescriptions: bool = True
    lab: bool = True
    radiology: bool = True
    pharmacy: bool = True
    ot: bool = True
    billing: bool = True
    attachments: bool = True
    consents: bool = True


class EmrExportRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    patient_id: Optional[int] = None  # optional (can use UHID)
    uhid: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    sections: EmrExportSections = EmrExportSections()
    consent_required: bool = True


class FhirBundleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    resourceType: str = "Bundle"
    type: str = "collection"
    entry: list
