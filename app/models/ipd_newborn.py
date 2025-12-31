from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Text, ForeignKey, Boolean, Float,
    Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class IpdNewbornResuscitation(Base):
    """
    ONE record per Admission + Baby (typical).
    Works for Labour Room + NICU + IPD Nursing.
    """
    __tablename__ = "ipd_newborn_resuscitations"

    id = Column(Integer, primary_key=True)

    admission_id = Column(Integer, ForeignKey("ipd_admissions.id", ondelete="CASCADE"), nullable=False, index=True)

    # Optional links
    birth_register_id = Column(Integer, ForeignKey("birth_registers.id"), nullable=True, index=True)
    baby_patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    mother_patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)

    # ---------------- Mother Details (from your screenshot)
    mother_name = Column(String(120), nullable=True)
    mother_age_years = Column(Integer, nullable=True)
    mother_blood_group = Column(String(16), nullable=True)

    gravida = Column(Integer, nullable=True)
    para = Column(Integer, nullable=True)
    living = Column(Integer, nullable=True)
    abortion = Column(Integer, nullable=True)

    lmp_date = Column(Date, nullable=True)
    edd_date = Column(Date, nullable=True)

    hiv_status = Column(String(32), nullable=True)
    vdrl_status = Column(String(32), nullable=True)
    hbsag_status = Column(String(32), nullable=True)

    thyroid = Column(String(80), nullable=True)
    pih = Column(Boolean, nullable=True)
    gdm = Column(Boolean, nullable=True)
    fever = Column(Boolean, nullable=True)

    other_illness = Column(Text, nullable=True)
    drug_intake = Column(Text, nullable=True)
    antenatal_steroid = Column(String(80), nullable=True)

    gestational_age_weeks = Column(Integer, nullable=True)
    consanguinity = Column(String(32), nullable=True)
    mode_of_conception = Column(String(40), nullable=True)
    prev_sibling_neonatal_period = Column(Text, nullable=True)

    referred_from = Column(String(120), nullable=True)
    amniotic_fluid = Column(String(40), nullable=True)

    # ---------------- Baby Details
    date_of_birth = Column(Date, nullable=True)
    time_of_birth = Column(String(16), nullable=True)  # "HH:MM"
    sex = Column(String(16), nullable=True)

    birth_weight_kg = Column(Float, nullable=True)
    length_cm = Column(Float, nullable=True)
    head_circum_cm = Column(Float, nullable=True)

    mode_of_delivery = Column(String(40), nullable=True)
    baby_cried_at_birth = Column(Boolean, nullable=True)

    apgar_1_min = Column(Integer, nullable=True)
    apgar_5_min = Column(Integer, nullable=True)
    apgar_10_min = Column(Integer, nullable=True)

    # Resuscitation (structured + note)
    resuscitation = Column(JSON, nullable=True)  # checkbox-like fields
    resuscitation_notes = Column(Text, nullable=True)

    # ---------------- Examination (Page 2)
    hr = Column(Integer, nullable=True)
    rr = Column(Integer, nullable=True)
    cft_seconds = Column(Float, nullable=True)
    sao2 = Column(Integer, nullable=True)
    sugar_mgdl = Column(Integer, nullable=True)

    cvs = Column(Text, nullable=True)

    rs = Column(Text, nullable=True)
    icr = Column(Boolean, nullable=True)
    scr = Column(Boolean, nullable=True)
    grunting = Column(Boolean, nullable=True)
    apnea = Column(Boolean, nullable=True)
    downes_score = Column(Integer, nullable=True)

    pa = Column(Text, nullable=True)

    # CNS box
    cns_cry = Column(String(80), nullable=True)
    cns_activity = Column(String(80), nullable=True)
    cns_af = Column(String(80), nullable=True)
    cns_reflexes = Column(String(80), nullable=True)
    cns_tone = Column(String(80), nullable=True)

    musculoskeletal = Column(Text, nullable=True)
    spine_cranium = Column(Text, nullable=True)
    genitalia = Column(Text, nullable=True)

    diagnosis = Column(Text, nullable=True)
    treatment = Column(Text, nullable=True)

    oxygen = Column(Text, nullable=True)
    warmth = Column(Text, nullable=True)
    feed_initiation = Column(Text, nullable=True)

    vitamin_k_given = Column(Boolean, nullable=True)
    vitamin_k_at = Column(DateTime, nullable=True)
    vitamin_k_remarks = Column(String(255), nullable=True)

    others = Column(Text, nullable=True)
    vitals_monitor = Column(Text, nullable=True)

    vaccination = Column(JSON, nullable=True)  # {"BCG":{given,at,batch}, "OPV":..., "HepB":...}

    # ---------------- Workflow
    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT/VERIFIED/FINALIZED/VOIDED
    locked_at = Column(DateTime, nullable=True)

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=True)

    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    finalized_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    created_by = relationship("User", foreign_keys=[created_by_user_id], lazy="joined")
    verified_by = relationship("User", foreign_keys=[verified_by_user_id], lazy="joined")
    finalized_by = relationship("User", foreign_keys=[finalized_by_user_id], lazy="joined")

    __table_args__ = (
        # usually one record per admission; if you want multiple babies per admission remove this constraint
        UniqueConstraint("admission_id", name="uq_ipd_newborn_resus_admission"),
        Index("ix_ipd_newborn_status", "status", "created_at"),
    )
