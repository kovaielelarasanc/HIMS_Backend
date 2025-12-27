# FILE: app/models/ipd_nursing.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Text, ForeignKey, Boolean, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.utcnow()


# --------------------------
# Timeline (for user-friendly UI)
# --------------------------
class IpdNursingTimeline(Base):
    __tablename__ = "ipd_nursing_timeline"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    event_type = Column(String(40), nullable=False)  # dressing/transfusion/restraint/isolation/icu
    event_at = Column(DateTime, nullable=False, default=utcnow)

    title = Column(String(255), default="")
    summary = Column(Text, default="")

    ref_table = Column(String(80), nullable=True)
    ref_id = Column(Integer, nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    admission = relationship("IpdAdmission", back_populates="nursing_timeline")
    created_by = relationship("User")


# --------------------------
# Dressing
# --------------------------
class IpdDressingRecord(Base):
    __tablename__ = "ipd_dressing_records"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    performed_at = Column(DateTime, nullable=False, default=utcnow)

    wound_site = Column(String(255), default="")
    dressing_type = Column(String(255), default="")
    indication = Column(String(255), default="")

    assessment = Column(JSON, nullable=False, default=dict)  # structured wound assessment
    procedure_json = Column("procedure_json", JSON, nullable=False, default=dict)
    asepsis = Column(JSON, nullable=False, default=dict)     # checklist

    pain_score = Column(Integer, nullable=True)
    patient_response = Column(String(255), default="")

    findings = Column(Text, default="")
    next_dressing_due = Column(DateTime, nullable=True)

    performed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edit_reason = Column(String(255), nullable=True)

    admission = relationship("IpdAdmission", back_populates="dressing_records")
    performed_by = relationship("User", foreign_keys=[performed_by_id])
    verified_by = relationship("User", foreign_keys=[verified_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


# --------------------------
# Blood Transfusion (end-to-end)
# --------------------------
class IpdBloodTransfusion(Base):
    __tablename__ = "ipd_blood_transfusions"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    status = Column(String(30), nullable=False, default="ordered")
    # ordered -> issued -> in_progress -> completed
    # stopped / reaction

    indication = Column(String(255), default="")

    ordered_at = Column(DateTime, nullable=True)
    ordered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    consent_taken = Column(Boolean, default=False)
    consent_doc_ref = Column(String(255), nullable=True)

    unit = Column(JSON, nullable=False, default=dict)                # bag/unit/abo/expiry etc.
    compatibility = Column(JSON, nullable=False, default=dict)       # crossmatch report
    issue = Column(JSON, nullable=False, default=dict)               # issued/collected
    bedside_verification = Column(JSON, nullable=False, default=dict)  # 2-person checks

    administration = Column(JSON, nullable=False, default=dict)      # start/end/rate/volume/ivsite
    baseline_vitals = Column(JSON, nullable=False, default=dict)
    monitoring_vitals = Column(JSON, nullable=False, default=list)   # list vital points

    reaction = Column(JSON, nullable=False, default=dict)            # reaction workflow

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edit_reason = Column(String(255), nullable=True)

    admission = relationship("IpdAdmission", back_populates="blood_transfusions")
    ordered_by = relationship("User", foreign_keys=[ordered_by_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


# --------------------------
# Restraints (order + monitoring + stop)
# --------------------------
class IpdRestraintRecord(Base):
    __tablename__ = "ipd_restraint_records"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    status = Column(String(20), nullable=False, default="active")  # active/stopped
    restraint_type = Column(String(30), default="physical")        # physical/chemical
    device = Column(String(100), default="")                       # belt/wrist/side-rails etc.
    site = Column(String(100), default="")                         # wrist/ankle/bed etc.

    reason = Column(Text, default="")
    alternatives_tried = Column(Text, default="")

    ordered_at = Column(DateTime, nullable=False, default=utcnow)
    ordered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    valid_till = Column(DateTime, nullable=True)

    consent_taken = Column(Boolean, default=False)
    consent_doc_ref = Column(String(255), nullable=True)

    started_at = Column(DateTime, nullable=False, default=utcnow)
    ended_at = Column(DateTime, nullable=True)

    monitoring_log = Column(JSON, nullable=False, default=list)  # timeline of checks

    stopped_at = Column(DateTime, nullable=True)
    stopped_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    stop_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edit_reason = Column(String(255), nullable=True)

    admission = relationship("IpdAdmission", back_populates="restraints")
    ordered_by = relationship("User", foreign_keys=[ordered_by_id])
    stopped_by = relationship("User", foreign_keys=[stopped_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


# --------------------------
# Isolation Precautions (order + review + stop)
# --------------------------
class IpdIsolationPrecaution(Base):
    __tablename__ = "ipd_isolation_precautions"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    status = Column(String(20), nullable=False, default="active")  # active/stopped
    precaution_type = Column(String(30), nullable=False, default="contact")  # contact/droplet/airborne
    indication = Column(String(255), default="")

    ordered_at = Column(DateTime, nullable=False, default=utcnow)
    ordered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    measures = Column(JSON, nullable=False, default=dict)     # PPE/signage/visitor/equipment checklist
    review_due_at = Column(DateTime, nullable=True)

    started_at = Column(DateTime, nullable=False, default=utcnow)
    ended_at = Column(DateTime, nullable=True)

    stopped_at = Column(DateTime, nullable=True)
    stopped_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    stop_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edit_reason = Column(String(255), nullable=True)

    admission = relationship("IpdAdmission", back_populates="isolations")
    ordered_by = relationship("User", foreign_keys=[ordered_by_id])
    stopped_by = relationship("User", foreign_keys=[stopped_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


# --------------------------
# ICU Flow Sheet (structured JSON + audit)
# --------------------------
class IcuFlowSheet(Base):
    __tablename__ = "icu_flow_sheets"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    recorded_at = Column(DateTime, nullable=False, default=utcnow)
    shift = Column(String(20), nullable=True)  # morning/evening/night optional

    vitals = Column(JSON, nullable=False, default=dict)
    ventilator = Column(JSON, nullable=False, default=dict)
    infusions = Column(JSON, nullable=False, default=list)  # list of infusion objects

    gcs_score = Column(Integer, nullable=True)
    urine_output_ml = Column(Integer, nullable=True)
    notes = Column(Text, default="")

    recorded_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    edit_reason = Column(String(255), nullable=True)

    admission = relationship("IpdAdmission", back_populates="icu_flows")
    recorded_by = relationship("User", foreign_keys=[recorded_by_id])
    verified_by = relationship("User", foreign_keys=[verified_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])


Index("ix_ipd_nursing_timeline_adm_eventat", IpdNursingTimeline.admission_id, IpdNursingTimeline.event_at)
Index("ix_icu_flow_adm_recorded", IcuFlowSheet.admission_id, IcuFlowSheet.recorded_at)
