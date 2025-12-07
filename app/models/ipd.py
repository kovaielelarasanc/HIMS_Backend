from __future__ import annotations
from datetime import datetime, date
from sqlalchemy import (Column, Integer, String, DateTime, Date, Text,
                        ForeignKey, Boolean, Numeric, UniqueConstraint, Index)
from sqlalchemy.orm import relationship
from app.db.base import Base

# ---------------------------------------------------------------------
# IPD Masters
# ---------------------------------------------------------------------


class IpdWard(Base):
    __tablename__ = "ipd_wards"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    code = Column(String(20), unique=True, nullable=False)
    floor = Column(String(50), default="")
    is_active = Column(Boolean, default=True)
    rooms = relationship(
        "IpdRoom",
        back_populates="ward",
        cascade="all, delete-orphan",
    )


class IpdRoom(Base):
    __tablename__ = "ipd_rooms"
    __table_args__ = (
        UniqueConstraint("ward_id",
                         "number",
                         name="uq_ipd_room_number_per_ward"),
        Index("ix_ipd_rooms_ward_active", "ward_id", "is_active"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id = Column(Integer, primary_key=True)
    ward_id = Column(Integer,
                     ForeignKey("ipd_wards.id"),
                     nullable=False,
                     index=True)
    number = Column(String(30), nullable=False)
    type = Column(String(30), default="General")
    is_active = Column(Boolean, default=True)

    ward = relationship("IpdWard", back_populates="rooms")
    beds = relationship(
        "IpdBed",
        back_populates="room",
        cascade="all, delete-orphan",
    )


class IpdBed(Base):
    __tablename__ = "ipd_beds"
    __table_args__ = (
        Index("ix_ipd_beds_state", "state"),
        Index("ix_ipd_beds_room_state", "room_id", "state"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer,
                     ForeignKey("ipd_rooms.id"),
                     nullable=False,
                     index=True)
    code = Column(String(30), unique=True, nullable=False)
    state = Column(String(20),
                   default="vacant")  # vacant/occupied/reserved/preoccupied
    reserved_until = Column(DateTime, nullable=True)
    note = Column(String(255), default="")

    room = relationship("IpdRoom", back_populates="beds")
    
    @property
    def ward_name(self) -> str | None:
        return self.room.ward.name if self.room and self.room.ward else None

    @property
    def room_name(self) -> str | None:
        return self.room.number if self.room else None


class IpdBedRate(Base):
    __tablename__ = "ipd_bed_rates"
    id = Column(Integer, primary_key=True)
    room_type = Column(String(30), nullable=False, index=True)
    daily_rate = Column(Numeric(12, 2), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True)

    # NEW: matches the resolve-billing query pattern
    __table_args__ = (Index("ix_ipd_bed_rates_lookup", "room_type",
                            "effective_from", "effective_to", "is_active"), )


class IpdBedAssignment(Base):
    __tablename__ = "ipd_bed_assignments"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    bed_id = Column(Integer, ForeignKey("ipd_beds.id"), index=True)
    from_ts = Column(DateTime, default=datetime.utcnow)
    to_ts = Column(DateTime, nullable=True)
    reason = Column(String(120), default="admission")

    # NEW: accelerate “active at end-of-day” and close-last-assignment lookups
    __table_args__ = (
        Index("ix_ipd_bed_assignments_adm_from", "admission_id", "from_ts"),
        Index("ix_ipd_bed_assignments_adm_to", "admission_id", "to_ts"),
    )


class IpdPackage(Base):
    __tablename__ = "ipd_packages"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    included = Column(Text, default="")
    excluded = Column(Text, default="")
    charges = Column(Numeric(12, 2), default=0)


# ---------------------------------------------------------------------
# Bed Rates (dynamic, by Room Type with validity dates)
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# IPD Core Workflow
# ---------------------------------------------------------------------


class IpdAdmission(Base):
    __tablename__ = "ipd_admissions"
    id = Column(Integer, primary_key=True)
    admission_code = Column(String(20), unique=True, index=True, nullable=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    department_id = Column(Integer,
                           ForeignKey("departments.id"),
                           nullable=True)

    practitioner_user_id = Column(Integer,
                                  ForeignKey("users.id"),
                                  nullable=True)  # Primary doctor
    primary_nurse_user_id = Column(Integer,
                                   ForeignKey("users.id"),
                                   nullable=True)

    admission_type = Column(String(20),
                            default="planned")  # emergency/planned/daycare
    admitted_at = Column(DateTime, default=datetime.utcnow)
    expected_discharge_at = Column(DateTime, nullable=True)

    package_id = Column(Integer, ForeignKey("ipd_packages.id"), nullable=True)
    payor_type = Column(String(20), default="cash")  # cash/insurance/tpa/...
    insurer_name = Column(String(120), default="")
    policy_number = Column(String(120), default="")

    preliminary_diagnosis = Column(Text, default="")
    history = Column(Text, default="")
    care_plan = Column(Text, default="")

    current_bed_id = Column(Integer, ForeignKey("ipd_beds.id"), nullable=True)
    status = Column(String(20), default="admitted"
                    )  # admitted/transferred/discharged/lama/dama/disappeared

    abha_shared_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    current_bed = relationship("IpdBed")

    @property
    def display_code(self) -> str:
        return self.admission_code or f"IP-{self.id:06d}"


class IpdTransfer(Base):
    __tablename__ = "ipd_transfers"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    from_bed_id = Column(Integer, ForeignKey("ipd_beds.id"))
    to_bed_id = Column(Integer, ForeignKey("ipd_beds.id"))
    reason = Column(String(255), default="")
    requested_by = Column(Integer, ForeignKey("users.id"))
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    transferred_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------
# Nursing / Clinical
# ---------------------------------------------------------------------


class IpdNursingNote(Base):
    __tablename__ = "ipd_nursing_notes"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    entry_time = Column(DateTime, default=datetime.utcnow)
    nurse_id = Column(Integer, ForeignKey("users.id"))
    patient_condition = Column(Text, default="")
    clinical_finding = Column(Text, default="")
    significant_events = Column(Text, default="")
    response_progress = Column(Text, default="")


class IpdShiftHandover(Base):
    __tablename__ = "ipd_shift_handovers"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    nurse_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    vital_signs = Column(Text, default="")  # JSON/plain text
    procedure_undergone = Column(Text, default="")
    todays_diagnostics = Column(Text, default="")
    current_condition = Column(Text, default="")
    recent_changes = Column(Text, default="")
    ongoing_treatment = Column(Text, default="")
    possible_changes = Column(Text, default="")
    other_info = Column(Text, default="")


class IpdVital(Base):
    __tablename__ = "ipd_vitals"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)
    recorded_by = Column(Integer, ForeignKey("users.id"))
    bp_systolic = Column(Integer, nullable=True)
    bp_diastolic = Column(Integer, nullable=True)
    temp_c = Column(Numeric(5, 2), nullable=True)
    rr = Column(Integer, nullable=True)
    spo2 = Column(Integer, nullable=True)
    pulse = Column(Integer, nullable=True)


class IpdIntakeOutput(Base):
    __tablename__ = "ipd_intake_output"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)
    recorded_by = Column(Integer, ForeignKey("users.id"))
    intake_ml = Column(Integer, default=0)
    urine_ml = Column(Integer, default=0)
    drains_ml = Column(Integer, default=0)
    stools_count = Column(Integer, default=0)
    remarks = Column(Text, default="")


class IpdRound(Base):
    __tablename__ = "ipd_rounds"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    by_user_id = Column(Integer, ForeignKey("users.id"))
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class IpdProgressNote(Base):
    __tablename__ = "ipd_progress_notes"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    by_user_id = Column(Integer, ForeignKey("users.id"))
    observation = Column(Text, default="")
    plan = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------
# Discharge
# ---------------------------------------------------------------------


class IpdDischargeSummary(Base):
    __tablename__ = "ipd_discharge_summaries"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer,
                          ForeignKey("ipd_admissions.id"),
                          index=True,
                          unique=True)

    demographics = Column(Text, default="")
    medical_history = Column(Text, default="")
    treatment_summary = Column(Text, default="")
    medications = Column(Text, default="")
    follow_up = Column(Text, default="")
    icd10_codes = Column(Text, default="")  # CSV/JSON

    finalized = Column(Boolean, default=False)
    finalized_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)


class IpdDischargeChecklist(Base):
    __tablename__ = "ipd_discharge_checklists"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer,
                          ForeignKey("ipd_admissions.id"),
                          index=True,
                          unique=True)
    financial_clearance = Column(Boolean, default=False)
    financial_cleared_by = Column(Integer,
                                  ForeignKey("users.id"),
                                  nullable=True)
    clinical_clearance = Column(Boolean, default=False)
    clinical_cleared_by = Column(Integer,
                                 ForeignKey("users.id"),
                                 nullable=True)
    delay_reason = Column(Text, default="")
    submitted = Column(Boolean, default=False)
    submitted_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------


class IpdReferral(Base):
    __tablename__ = "ipd_referrals"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    type = Column(String(20), default="internal")  # internal/external
    to_department = Column(String(120), default="")
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    external_org = Column(String(200), default="")
    reason = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(
        String(20),
        default="requested")  # requested/accepted/completed/cancelled


# ---------------------------------------------------------------------
# OT & Anaesthesia
# ---------------------------------------------------------------------


class IpdOtCase(Base):
    __tablename__ = "ipd_ot_cases"
    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    surgery_name = Column(String(200), default="")
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    status = Column(String(20),
                    default="planned")  # planned/unplanned/cancelled/completed
    surgeon_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    anaesthetist_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    staff_tags = Column(Text, default="")  # CSV/JSON
    preop_notes = Column(Text, default="")
    postop_notes = Column(Text, default="")
    instrument_tracking = Column(Text, default="")
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)


class IpdAnaesthesiaRecord(Base):
    __tablename__ = "ipd_anaesthesia_records"
    id = Column(Integer, primary_key=True)
    ot_case_id = Column(Integer, ForeignKey("ipd_ot_cases.id"), index=True)
    pre_assessment = Column(Text, default="")
    anaesthesia_type = Column(
        String(20), default="general")  # local/regional/spinal/general
    intraop_monitoring = Column(Text, default="")  # JSON time-series
    drugs_administered = Column(Text, default="")  # JSON: name, dose, time
    post_status = Column(Text, default="")
