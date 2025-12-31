from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any

from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Text, ForeignKey, Boolean,
    Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.utcnow()


# -----------------------
# System counter (safe sequential internal numbers)
# -----------------------
class VitalSequenceCounter(Base):
    __tablename__ = "vital_sequence_counters"
    id = Column(Integer, primary_key=True)
    kind = Column(String(32), nullable=False)   # "BIRTH" | "DEATH" | "STILLBIRTH"
    year = Column(Integer, nullable=False)
    next_value = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("kind", "year", name="uq_vital_seq_kind_year"),
        Index("ix_vital_seq_kind_year", "kind", "year"),
    )


# # -----------------------
# # Audit trail (NABH digital expectation)
# # -----------------------
# class VitalEventAudit(Base):
#     __tablename__ = "vital_event_audits"
#     id = Column(Integer, primary_key=True)

#     entity_type = Column(String(32), nullable=False)  # "birth"|"death"|"stillbirth"|"mccd"|"amendment"
#     entity_id = Column(Integer, nullable=False, index=True)

#     action = Column(String(40), nullable=False)  # create/update/verify/finalize/submit/print/export/amend/void
#     actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

#     # optional request metadata
#     ip_addr = Column(String(64), nullable=True)
#     user_agent = Column(String(255), nullable=True)

#     # snapshot / diff
#     before = Column(JSON, nullable=True)
#     after = Column(JSON, nullable=True)
#     note = Column(String(255), nullable=True)

#     created_at = Column(DateTime, nullable=False, default=utcnow)

#     actor = relationship("User", lazy="joined")


# -----------------------
# Amendment workflow (no silent edits after finalize/submit)
# -----------------------
class VitalAmendmentRequest(Base):
    __tablename__ = "vital_amendment_requests"
    id = Column(Integer, primary_key=True)

    entity_type = Column(String(32), nullable=False)  # "birth"|"death"|"stillbirth"
    entity_id = Column(Integer, nullable=False, index=True)

    requested_changes = Column(JSON, nullable=False)  # {"field": {"from":..., "to":...}, ...}
    reason = Column(Text, nullable=False)

    status = Column(String(16), nullable=False, default="PENDING")  # PENDING/APPROVED/REJECTED
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)

    requested_by = relationship("User", foreign_keys=[requested_by_user_id], lazy="joined")
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_user_id], lazy="joined")

    __table_args__ = (
        Index("ix_vital_amend_entity", "entity_type", "entity_id"),
    )


# -----------------------
# Birth Register (CRS Form 1)
# -----------------------
class BirthRegister(Base):
    __tablename__ = "birth_registers"
    id = Column(Integer, primary_key=True)

    # internal hospital register number (immutable once finalized)
    internal_no = Column(String(32), nullable=False, index=True)  # e.g. BR-2025-000001

    # link to patient/admission if available
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), nullable=True, index=True)

    birth_datetime = Column(DateTime, nullable=False, index=True)
    place_of_birth = Column(String(120), nullable=False, default="Hospital")
    delivery_method = Column(String(40), nullable=True)  # Normal/LSCS/Assisted/Other
    gestation_weeks = Column(Integer, nullable=True)
    birth_weight_kg = Column(String(16), nullable=True)
    child_sex = Column(String(16), nullable=False)  # Male/Female/Transgender/Unknown
    child_name = Column(String(120), nullable=True)  # may be blank initially
    plurality = Column(Integer, nullable=True)  # 1/2/3...
    birth_order = Column(Integer, nullable=True)  # order within plurality

    mother_name = Column(String(120), nullable=False)
    mother_age_years = Column(Integer, nullable=True)
    mother_dob = Column(Date, nullable=True)
    mother_id_no = Column(String(40), nullable=True)
    mother_mobile = Column(String(20), nullable=True)
    mother_address = Column(JSON, nullable=True)  # {"line1","line2","city","district","state","pincode"}

    father_name = Column(String(120), nullable=True)
    father_age_years = Column(Integer, nullable=True)
    father_dob = Column(Date, nullable=True)
    father_id_no = Column(String(40), nullable=True)
    father_mobile = Column(String(20), nullable=True)
    father_address = Column(JSON, nullable=True)

    informant_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    informant_name = Column(String(120), nullable=True)
    informant_designation = Column(String(80), nullable=True)

    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT/VERIFIED/FINALIZED/SUBMITTED/VOIDED
    locked_at = Column(DateTime, nullable=True)

    # CRS submission tracking (after portal/manual submission)
    crs_registration_unit = Column(String(80), nullable=True)
    crs_registration_no = Column(String(80), nullable=True, index=True)
    crs_registration_date = Column(Date, nullable=True)
    crs_ack_ref = Column(String(120), nullable=True)

    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    finalized_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)

    informant_user = relationship("User", foreign_keys=[informant_user_id], lazy="joined")
    verified_by = relationship("User", foreign_keys=[verified_by_user_id], lazy="joined")
    finalized_by = relationship("User", foreign_keys=[finalized_by_user_id], lazy="joined")
    submitted_by = relationship("User", foreign_keys=[submitted_by_user_id], lazy="joined")

    __table_args__ = (
        Index("ix_birth_status_dt", "status", "birth_datetime"),
    )


# -----------------------
# Stillbirth Register (CRS Form 3)
# -----------------------
class StillBirthRegister(Base):
    __tablename__ = "stillbirth_registers"
    id = Column(Integer, primary_key=True)
    internal_no = Column(String(32), nullable=False, index=True)  # SB-2025-000001

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), nullable=True, index=True)

    event_datetime = Column(DateTime, nullable=False, index=True)
    place_of_occurrence = Column(String(120), nullable=False, default="Hospital")
    gestation_weeks = Column(Integer, nullable=True)
    foetus_sex = Column(String(16), nullable=True)  # if known

    mother_name = Column(String(120), nullable=False)
    mother_age_years = Column(Integer, nullable=True)
    mother_address = Column(JSON, nullable=True)

    father_name = Column(String(120), nullable=True)
    father_address = Column(JSON, nullable=True)

    informant_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    informant_name = Column(String(120), nullable=True)
    informant_designation = Column(String(80), nullable=True)

    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT/VERIFIED/FINALIZED/SUBMITTED/VOIDED
    locked_at = Column(DateTime, nullable=True)

    crs_registration_unit = Column(String(80), nullable=True)
    crs_registration_no = Column(String(80), nullable=True, index=True)
    crs_registration_date = Column(Date, nullable=True)
    crs_ack_ref = Column(String(120), nullable=True)

    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    finalized_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)

    informant_user = relationship("User", foreign_keys=[informant_user_id], lazy="joined")


# -----------------------
# Death Register (CRS Form 2)
# -----------------------
class DeathRegister(Base):
    __tablename__ = "death_registers"
    id = Column(Integer, primary_key=True)

    internal_no = Column(String(32), nullable=False, index=True)  # DR-2025-000001

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), nullable=True, index=True)

    death_datetime = Column(DateTime, nullable=False, index=True)
    place_of_death = Column(String(120), nullable=False, default="Hospital")
    ward_or_unit = Column(String(80), nullable=True)

    deceased_name = Column(String(120), nullable=False)
    sex = Column(String(16), nullable=False)  # Male/Female/Transgender/Unknown
    age_years = Column(Integer, nullable=True)
    dob = Column(Date, nullable=True)

    address = Column(JSON, nullable=True)
    id_no = Column(String(40), nullable=True)
    mobile = Column(String(20), nullable=True)

    # manner (for reporting) - keep non-graphic, purely classification
    manner_of_death = Column(String(24), nullable=True)  # Natural/Accident/Homicide/Suicide/Undetermined

    informant_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    informant_name = Column(String(120), nullable=True)
    informant_designation = Column(String(80), nullable=True)

    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT/VERIFIED/FINALIZED/SUBMITTED/VOIDED
    locked_at = Column(DateTime, nullable=True)

    # CRS submission tracking
    crs_registration_unit = Column(String(80), nullable=True)
    crs_registration_no = Column(String(80), nullable=True, index=True)
    crs_registration_date = Column(Date, nullable=True)
    crs_ack_ref = Column(String(120), nullable=True)

    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    finalized_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    submitted_at = Column(DateTime, nullable=True)

    # MCCD certificate handling (copy given to kin)
    mccd_given_to_kin = Column(Boolean, nullable=False, default=False)
    mccd_given_to_name = Column(String(120), nullable=True)
    mccd_given_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)

    informant_user = relationship("User", foreign_keys=[informant_user_id], lazy="joined")

    mccd = relationship("MCCDRecord", back_populates="death", uselist=False, cascade="all, delete-orphan")


# -----------------------
# MCCD (Form 4) - one-to-one with DeathRegister
# -----------------------
class MCCDRecord(Base):
    __tablename__ = "mccd_records"
    id = Column(Integer, primary_key=True)

    death_id = Column(Integer, ForeignKey("death_registers.id", ondelete="CASCADE"), nullable=False, unique=True)

    # Causes of death (structured but simple)
    immediate_cause = Column(String(255), nullable=False)
    antecedent_cause = Column(String(255), nullable=True)
    underlying_cause = Column(String(255), nullable=True)
    other_significant_conditions = Column(Text, nullable=True)

    pregnancy_status = Column(String(40), nullable=True)  # if applicable
    tobacco_use = Column(String(24), nullable=True)       # Yes/No/Unknown (optional)

    certifying_doctor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    certified_at = Column(DateTime, nullable=True)

    # e-sign fields if you use e-sign later
    signed = Column(Boolean, nullable=False, default=False)
    signed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)

    death = relationship("DeathRegister", back_populates="mccd")
    certifying_doctor = relationship("User", foreign_keys=[certifying_doctor_user_id], lazy="joined")


# Optional uniqueness (CRS reg no should be unique if used)
Index("ix_birth_crs_reg_no", BirthRegister.crs_registration_no)
Index("ix_death_crs_reg_no", DeathRegister.crs_registration_no)
Index("ix_still_crs_reg_no", StillBirthRegister.crs_registration_no)
