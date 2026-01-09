# FILE: app/models/ipd.py
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (Column, Integer, String, DateTime, Date, Text,
                        ForeignKey, Boolean, Numeric, UniqueConstraint, Index,
                        func)
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
        "mysql_collate": "utf8mb4_unicode_ci",
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
            "mysql_collate": "utf8mb4_unicode_ci",
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
    beds = relationship("IpdBed",
                        back_populates="room",
                        cascade="all, delete-orphan")


class IpdBed(Base):
    __tablename__ = "ipd_beds"
    __table_args__ = (
        Index("ix_ipd_beds_state", "state"),
        Index("ix_ipd_beds_room_state", "room_id", "state"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer,
                     ForeignKey("ipd_rooms.id"),
                     nullable=False,
                     index=True)
    code = Column(String(30), unique=True, nullable=False)
    state = Column(String(20), default="vacant")
    reserved_until = Column(DateTime, nullable=True)
    note = Column(String(255), default="")

    room = relationship("IpdRoom", back_populates="beds")
    bed_assignments = relationship(
        "IpdBedAssignment",
        back_populates="bed",
        cascade="all, delete-orphan",
    )

    @property
    def ward_name(self) -> str | None:
        return self.room.ward.name if self.room and self.room.ward else None

    @property
    def room_name(self) -> str | None:
        return self.room.number if self.room else None

class IpdBedRate(Base):
    __tablename__ = "ipd_bed_rates"
    __table_args__ = (
        Index(
            "ix_ipd_bed_rates_lookup",
            "room_type",
            "rate_basis",
            "effective_from",
            "effective_to",
            "is_active",
        ),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)
    room_type = Column(String(30), nullable=False, index=True)
    rate_basis = Column(String(10), nullable=False, default="daily", index=True)
    daily_rate = Column(Numeric(12, 2), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True)
    @property
    def room_type_display(self) -> str:
        b = (self.rate_basis or "daily").strip().title()
        return f"{self.room_type} ({b})"



class IpdBedAssignment(Base):
    __tablename__ = "ipd_bed_assignments"
    __table_args__ = (
        Index("ix_ipd_bed_assignments_adm_from", "admission_id", "from_ts"),
        Index("ix_ipd_bed_assignments_adm_to", "admission_id", "to_ts"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    bed_id = Column(Integer, ForeignKey("ipd_beds.id"), index=True)
    from_ts = Column(DateTime, default=datetime.utcnow)
    to_ts = Column(DateTime, nullable=True)
    reason = Column(String(120), default="admission")

    admission = relationship("IpdAdmission", back_populates="bed_assignments")
    bed = relationship("IpdBed", back_populates="bed_assignments")


class IpdPackage(Base):
    __tablename__ = "ipd_packages"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    included = Column(Text, default="")
    excluded = Column(Text, default="")
    charges = Column(Numeric(12, 2), default=0)


# ---------------------------------------------------------------------
# IPD Core Workflow
# ---------------------------------------------------------------------


class IpdAdmission(Base):
    __tablename__ = "ipd_admissions"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)

    admission_code = Column(String(20), unique=True, index=True, nullable=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=True,
    )

    practitioner_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )
    primary_nurse_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )

    admission_type = Column(
        String(20),
        default="planned",  # emergency/planned/daycare
    )
    admitted_at = Column(DateTime, default=datetime.utcnow)
    expected_discharge_at = Column(DateTime, nullable=True)

    package_id = Column(Integer, ForeignKey("ipd_packages.id"), nullable=True)
    payor_type = Column(
        String(20),
        default="cash",  # cash/insurance/tpa/...
    )
    insurer_name = Column(String(120), default="")
    policy_number = Column(String(120), default="")

    preliminary_diagnosis = Column(Text, default="")
    history = Column(Text, default="")
    care_plan = Column(Text, default="")

    current_bed_id = Column(Integer, ForeignKey("ipd_beds.id"), nullable=True)
    status = Column(
        String(20),
        default="admitted",
    )  # admitted/transferred/discharged/lama/dama/disappeared

    abha_shared_at = Column(DateTime, nullable=True)
    billing_locked = Column(Boolean, default=False, nullable=False)
    billing_locked_at = Column(DateTime, nullable=True)
    billing_locked_by = Column(Integer, nullable=True)
    discharge_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # ---- Relationships ----
    current_bed = relationship(
        "IpdBed",
        foreign_keys=[current_bed_id],
    )

    bed_assignments = relationship(
        "IpdBedAssignment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    medications = relationship(
        "IpdMedication",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    shift_handovers = relationship(
        "IpdShiftHandover",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    vitals = relationship(
        "IpdVital",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    intake_outputs = relationship(
        "IpdIntakeOutput",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    rounds = relationship(
        "IpdRound",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    progress_notes = relationship(
        "IpdProgressNote",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    pain_assessments = relationship(
        "IpdPainAssessment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    fall_risk_assessments = relationship(
        "IpdFallRiskAssessment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    pressure_ulcer_assessments = relationship(
        "IpdPressureUlcerAssessment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    nutrition_assessments = relationship(
        "IpdNutritionAssessment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    orders = relationship(
        "IpdOrder",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    medication_orders = relationship(
        "IpdMedicationOrder",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    medication_administrations = relationship(
        "IpdMedicationAdministration",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    transfers = relationship(
        "IpdTransfer",
        back_populates="admission",
        cascade="all, delete-orphan",
    )


   
    dressing_records = relationship("IpdDressingRecord", back_populates="admission", cascade="all, delete-orphan")
    blood_transfusions = relationship("IpdBloodTransfusion", back_populates="admission", cascade="all, delete-orphan")
    restraints = relationship("IpdRestraintRecord", back_populates="admission", cascade="all, delete-orphan")
    isolations = relationship("IpdIsolationPrecaution", back_populates="admission", cascade="all, delete-orphan")
    icu_flows = relationship("IcuFlowSheet", back_populates="admission", cascade="all, delete-orphan")
    nursing_timeline = relationship("IpdNursingTimeline", back_populates="admission", cascade="all, delete-orphan")

    discharge_summary = relationship(
        "IpdDischargeSummary",
        back_populates="admission",
        cascade="all, delete-orphan",
        uselist=False,
    )
    discharge_checklist = relationship(
        "IpdDischargeChecklist",
        back_populates="admission",
        cascade="all, delete-orphan",
        uselist=False,
    )
    discharge_medications = relationship(
        "IpdDischargeMedication",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    feedback_entries = relationship(
        "IpdFeedback",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    feedback = relationship(
        "IpdAdmissionFeedback",
        back_populates="admission",
        cascade="all, delete-orphan",
        uselist=False,
    )

    referrals = relationship(
        "IpdReferral",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    ot_cases = relationship(
        "IpdOtCase",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    assessments = relationship(
        "IpdAssessment",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    dressing_transfusion_entries = relationship(
        "IpdDressingTransfusion",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    drug_chart_meta = relationship(
        "IpdDrugChartMeta",
        back_populates="admission",
        uselist=False,
        cascade="all, delete-orphan",
    )
    iv_fluid_orders = relationship(
        "IpdIvFluidOrder",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    drug_chart_nurse_rows = relationship(
        "IpdDrugChartNurseRow",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    doctor_auth_rows = relationship(
        "IpdDrugChartDoctorAuth",
        back_populates="admission",
        cascade="all, delete-orphan",
    )
    nursing_notes = relationship(
        "IpdNursingNote",
        back_populates="admission",
        cascade="all, delete-orphan",
    )

    @property
    def display_code(self) -> str:
        return self.admission_code or f"IP-{self.id:06d}"


# ✅ REPLACE your old IpdTransfer with this NABH-ready version
class IpdTransfer(Base):
    __tablename__ = "ipd_transfers"
    __table_args__ = (
        Index("ix_ipd_transfers_adm_status", "admission_id", "status"),
        Index("ix_ipd_transfers_requested_at", "requested_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)

    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True, nullable=False)

    # Bed movement
    from_bed_id = Column(Integer, ForeignKey("ipd_beds.id"), nullable=True)
    to_bed_id = Column(Integer, ForeignKey("ipd_beds.id"), nullable=True)

    # ✅ Link to bed assignment rows for traceability (NABH audit)
    from_assignment_id = Column(Integer, ForeignKey("ipd_bed_assignments.id"), nullable=True)
    to_assignment_id = Column(Integer, ForeignKey("ipd_bed_assignments.id"), nullable=True)

    # NABH transfer metadata
    transfer_type = Column(String(30), default="transfer")  # upgrade/downgrade/isolation/transfer
    priority = Column(String(20), default="routine")        # routine/urgent
    status = Column(String(20), default="requested")        # requested/approved/rejected/scheduled/completed/cancelled

    reason = Column(String(255), default="")
    request_note = Column(Text, default="")

    # scheduling/reservation
    scheduled_at = Column(DateTime, nullable=True)
    reserved_until = Column(DateTime, nullable=True)

    # Who/When audit
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approval_note = Column(Text, default="")

    rejected_reason = Column(Text, default="")

    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(Text, default="")

    # Execution timestamps
    vacated_at = Column(DateTime, nullable=True)
    occupied_at = Column(DateTime, nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Optional handover checklist JSON/text (store as JSON string)
    handover_json = Column(Text, default="")

    # --- relationships ---
    admission = relationship("IpdAdmission", lazy="joined")

    from_bed = relationship("IpdBed", foreign_keys=[from_bed_id], lazy="joined")
    to_bed = relationship("IpdBed", foreign_keys=[to_bed_id], lazy="joined")

    from_assignment = relationship("IpdBedAssignment", foreign_keys=[from_assignment_id], lazy="joined")
    to_assignment = relationship("IpdBedAssignment", foreign_keys=[to_assignment_id], lazy="joined")

    requested_by_user = relationship("User", foreign_keys=[requested_by], lazy="joined")
    approved_by_user = relationship("User", foreign_keys=[approved_by], lazy="joined")
    cancelled_by_user = relationship("User", foreign_keys=[cancelled_by], lazy="joined")
    completed_by_user = relationship("User", foreign_keys=[completed_by], lazy="joined")


# ---------------------------------------------------------------------
# Nursing / Clinical
# ---------------------------------------------------------------------
class IpdNursingNote(Base):
    __tablename__ = "ipd_nursing_notes"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)

    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    nurse_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    # ✅ DIRECT LINK TO ONE VITALS ROW (optional)
    linked_vital_id = Column(
        Integer,
        ForeignKey("ipd_vitals.id"),
        nullable=True,
        index=True,
    )

    # wound_status = Column(String(255), nullable=False, default="")
    # oxygen_support = Column(String(255), nullable=False, default="")
    # urine_output = Column(String(255), nullable=False, default="")
    # drains_tubes = Column(String(255), nullable=False, default="")
    # pain_score = Column(String(50), nullable=False, default="")
    other_findings = Column(Text, nullable=False, default="")

    note_type = Column(String(20), nullable=False, default="routine")

    # Shift handover fields
    vital_signs_summary = Column(Text, nullable=False, default="")
    todays_procedures = Column(Text, nullable=False, default="")
    current_condition = Column(Text, nullable=False, default="")
    recent_changes = Column(Text, nullable=False, default="")
    ongoing_treatment = Column(Text, nullable=False, default="")
    watch_next_shift = Column(Text, nullable=False, default="")

    # NABH core text fields
    entry_time = Column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )
    # patient_condition = Column(Text, nullable=False, default="")
    significant_events = Column(Text, nullable=False, default="")
    nursing_interventions = Column(Text, nullable=False, default="")
    response_progress = Column(Text, nullable=False, default="")
    handover_note = Column(Text, nullable=False, default="")

    # Extra audit/flags
    shift = Column(String(20), nullable=True, index=True)
    is_icu = Column(Boolean, nullable=False, default=False)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )
    is_locked = Column(Boolean, nullable=False, default=False)

    # ---------------- Relationships ----------------
    admission = relationship("IpdAdmission", back_populates="nursing_notes")
    nurse = relationship("User")

    # ✅ IMPORTANT:
    # Keep ONLY ONE relationship that uses linked_vital_id.
    # Name it "vitals" to match your Pydantic NursingNoteOut.vitals
    vitals = relationship(
        "IpdVital",
        foreign_keys=[linked_vital_id],
        uselist=False,
        lazy="joined",
    )


class IpdShiftHandover(Base):
    __tablename__ = "ipd_shift_handovers"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    nurse_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    # vital_signs = Column(Text, default="")
    procedure_undergone = Column(Text, default="")
    todays_diagnostics = Column(Text, default="")
    current_condition = Column(Text, default="")
    recent_changes = Column(Text, default="")
    ongoing_treatment = Column(Text, default="")
    possible_changes = Column(Text, default="")
    other_info = Column(Text, default="")

    admission = relationship("IpdAdmission", back_populates="shift_handovers")


class IpdVital(Base):
    __tablename__ = "ipd_vitals"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)

    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    recorded_by = Column(Integer,
                         ForeignKey("users.id", ondelete="RESTRICT"),
                         nullable=False)

    bp_systolic = Column(Integer, nullable=True)
    bp_diastolic = Column(Integer, nullable=True)
    temp_c = Column(Numeric(5, 2), nullable=True)
    rr = Column(Integer, nullable=True)
    spo2 = Column(Integer, nullable=True)
    pulse = Column(Integer, nullable=True)

    admission = relationship("IpdAdmission", back_populates="vitals")
    recorder = relationship("User", foreign_keys=[recorded_by])
    nursing_note = relationship(
        "IpdNursingNote",
        uselist=False,
        primaryjoin="IpdVital.id==IpdNursingNote.linked_vital_id",
        viewonly=True,
    )


class IpdIntakeOutput(Base):
    __tablename__ = "ipd_intake_output"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    recorded_at = Column(DateTime, default=datetime.utcnow)
    recorded_by = Column(Integer, ForeignKey("users.id"))

    # ✅ legacy totals (keep for compatibility / reporting)
    intake_ml = Column(Integer, default=0)
    urine_ml = Column(Integer, default=0)

    # ✅ NEW split fields
    intake_oral_ml = Column(Integer, default=0)
    intake_iv_ml = Column(Integer, default=0)
    intake_blood_ml = Column(Integer, default=0)

    urine_foley_ml = Column(Integer, default=0)
    urine_voided_ml = Column(Integer, default=0)

    drains_ml = Column(Integer, default=0)
    stools_count = Column(Integer, default=0)
    remarks = Column(Text, default="")

    admission = relationship("IpdAdmission", back_populates="intake_outputs")


class IpdRound(Base):
    __tablename__ = "ipd_rounds"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    by_user_id = Column(Integer, ForeignKey("users.id"))
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    admission = relationship("IpdAdmission", back_populates="rounds")


class IpdProgressNote(Base):
    __tablename__ = "ipd_progress_notes"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    by_user_id = Column(Integer, ForeignKey("users.id"))
    observation = Column(Text, default="")
    plan = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    admission = relationship("IpdAdmission", back_populates="progress_notes")


# ---------------------------------------------------------------------
# Risk / Clinical Assessments (Pain, Fall, Pressure, Nutrition)
# ---------------------------------------------------------------------


class IpdPainAssessment(Base):
    __tablename__ = "ipd_pain_assessments"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    recorded_at = Column(DateTime, default=datetime.utcnow)
    scale_type = Column(String(50), default="")
    score = Column(Integer, nullable=True)
    location = Column(String(255), default="")
    character = Column(String(255), default="")
    intervention = Column(Text, default="")
    post_intervention_score = Column(Integer, nullable=True)

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission", back_populates="pain_assessments")


class IpdFallRiskAssessment(Base):
    __tablename__ = "ipd_fall_risk_assessments"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    recorded_at = Column(DateTime, default=datetime.utcnow)
    tool = Column(String(50), default="")
    score = Column(Integer, nullable=True)
    risk_level = Column(String(50), default="")
    precautions = Column(Text, default="")

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission",
                             back_populates="fall_risk_assessments")


class IpdPressureUlcerAssessment(Base):
    __tablename__ = "ipd_pressure_ulcer_assessments"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    recorded_at = Column(DateTime, default=datetime.utcnow)
    tool = Column(String(50), default="")
    score = Column(Integer, nullable=True)
    risk_level = Column(String(50), default="")

    existing_ulcer = Column(Boolean, default=False)
    site = Column(String(255), default="")
    stage = Column(String(50), default="")
    management_plan = Column(Text, default="")

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission",
                             back_populates="pressure_ulcer_assessments")


class IpdNutritionAssessment(Base):
    __tablename__ = "ipd_nutrition_assessments"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    recorded_at = Column(DateTime, default=datetime.utcnow)

    bmi = Column(Numeric(5, 2), nullable=True)
    weight_kg = Column(Numeric(6, 2), nullable=True)
    height_cm = Column(Numeric(6, 2), nullable=True)

    screening_tool = Column(String(50), default="")
    score = Column(Integer, nullable=True)
    risk_level = Column(String(50), default="")
    dietician_referral = Column(Boolean, default=False)

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission",
                             back_populates="nutrition_assessments")


# ---------------------------------------------------------------------
# Generic Orders & Medication / Drug Chart
# ---------------------------------------------------------------------


class IpdOrder(Base):
    __tablename__ = "ipd_orders"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    order_type = Column(
        String(50),
        nullable=False,
    )  # lab / radiology / procedure / diet / nursing / device
    linked_order_id = Column(Integer, nullable=True)
    order_text = Column(Text, default="")
    order_status = Column(
        String(20),
        default="ordered",  # ordered / in_progress / completed / cancelled
    )

    ordered_at = Column(DateTime, default=datetime.utcnow)
    ordered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    performed_at = Column(DateTime, nullable=True)
    performed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission", back_populates="orders")
    ordered_by_user = relationship("User",
                                   foreign_keys=[ordered_by],
                                   lazy="joined",
                                   uselist=False)
    performed_by_user = relationship("User",
                                     foreign_keys=[performed_by],
                                     lazy="joined",
                                     uselist=False)


class IpdMedicationOrder(Base):
    __tablename__ = "ipd_medication_orders"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)

    drug_id = Column(Integer, nullable=True)
    drug_name = Column(String(255), nullable=False)

    # dose and unit (for orders screen)
    dose = Column(Numeric(10, 2), nullable=True)
    dose_unit = Column(String(50), default="")

    route = Column(String(50), default="")
    frequency = Column(String(50), default="")
    duration_days = Column(Integer, nullable=True)

    start_datetime = Column(DateTime, default=datetime.utcnow)
    stop_datetime = Column(DateTime, nullable=True)

    special_instructions = Column(Text, default="")

    # NEW: distinguish regular / sos / stat / premed
    order_type = Column(
        String(20),
        default="regular",  # regular / sos / stat / premed
    )

    order_status = Column(
        String(20),
        default="active",  # active / stopped / completed
    )

    ordered_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission",
                             back_populates="medication_orders")
    administrations = relationship(
        "IpdMedicationAdministration",
        back_populates="order",
        cascade="all, delete-orphan",
    )
    ordered_by_user = relationship("User",
                                   foreign_keys=[ordered_by_id],
                                   lazy="joined",
                                   uselist=False)


class IpdDrugChartMeta(Base):
    """
    Header information for the IPD drug chart for a given admission:
    allergies, diagnosis, anthropometrics, diet advice, etc.
    One row per admission.
    """

    __tablename__ = "ipd_drug_chart_meta"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # Patient clinical summary (some may duplicate admission/patient for snapshot)
    allergic_to = Column(String(255), default="")
    diagnosis = Column(String(255), default="")

    weight_kg = Column(Numeric(6, 2), nullable=True)
    height_cm = Column(Numeric(6, 2), nullable=True)
    blood_group = Column(String(10), default="")
    bsa = Column(Numeric(6, 2), nullable=True)  # Body surface area
    bmi = Column(Numeric(6, 2), nullable=True)

    # Dietary advice
    oral_fluid_per_day_ml = Column(Integer, nullable=True)
    salt_gm_per_day = Column(Numeric(6, 2), nullable=True)
    calorie_per_day_kcal = Column(Integer, nullable=True)
    protein_gm_per_day = Column(Numeric(6, 2), nullable=True)
    diet_remarks = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    admission = relationship("IpdAdmission", back_populates="drug_chart_meta")


class IpdMedicationAdministration(Base):
    """
    Drug Chart / MAR – generated from IpdMedicationOrder.
    """

    __tablename__ = "ipd_medication_administration"

    id = Column(Integer, primary_key=True)

    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    med_order_id = Column(
        Integer,
        ForeignKey("ipd_medication_orders.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    scheduled_datetime = Column(DateTime, nullable=False)
    given_status = Column(
        String(20),
        default="pending",  # pending / given / missed / refused / held
    )
    given_datetime = Column(DateTime, nullable=True)
    given_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    remarks = Column(Text, default="")

    admission = relationship(
        "IpdAdmission",
        back_populates="medication_administrations",
    )
    order = relationship(
        "IpdMedicationOrder",
        back_populates="administrations",
    )
    given_by_user = relationship("User", foreign_keys=[given_by])

    __table_args__ = (Index(
        "ix_ipd_med_admin_by_adm_sched",
        "admission_id",
        "scheduled_datetime",
    ), )


# 


# ---------------------------------------------------------------------
# Discharge
# ---------------------------------------------------------------------


# FILE: app/models/ipd.py
class IpdDischargeSummary(Base):
    __tablename__ = "ipd_discharge_summaries"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        unique=True,
        nullable=False,
    )

    # Core existing fields
    demographics = Column(
        Text, default="")  # will be auto-filled from admission/patient
    medical_history = Column(Text, default="")
    treatment_summary = Column(Text, default="")
    medications = Column(Text, default="")
    follow_up = Column(Text, default="")  # can be enriched from opd_followups
    icd10_codes = Column(Text, default="")

    # A. MUST-HAVE
    final_diagnosis_primary = Column(Text, default="")
    final_diagnosis_secondary = Column(Text, default="")
    hospital_course = Column(Text, default="")
    discharge_condition = Column(String(20), default="stable")
    discharge_type = Column(String(20), default="routine")
    allergies = Column(Text, default="")

    # B. Recommended
    procedures = Column(Text, default="")
    investigations = Column(Text, default="")
    diet_instructions = Column(Text, default="")
    activity_instructions = Column(Text, default="")
    warning_signs = Column(Text, default="")
    referral_details = Column(Text, default="")

    # C. Operational / billing
    insurance_details = Column(Text, default="")
    stay_summary = Column(Text, default="")
    patient_ack_name = Column(String(255), default="")
    patient_ack_datetime = Column(DateTime, nullable=True)

    # D. Doctor & system validation
    prepared_by_name = Column(String(255), default="")
    reviewed_by_name = Column(String(255), default="")
    reviewed_by_regno = Column(String(100), default="")
    discharge_datetime = Column(DateTime, nullable=True)

    # E. Safety & quality
    implants = Column(Text, default="")
    pending_reports = Column(Text, default="")
    patient_education = Column(Text, default="")
    followup_appointment_ref = Column(String(255), default="")

    finalized = Column(Boolean, default=False)
    finalized_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    # Relationships
    admission = relationship(
        "IpdAdmission",
        back_populates="discharge_summary",
    )
    finalized_by_user = relationship("User", foreign_keys=[finalized_by])


class IpdDischargeChecklist(Base):
    __tablename__ = "ipd_discharge_checklists"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        unique=True,
        nullable=False,
    )

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

    admission = relationship(
        "IpdAdmission",
        back_populates="discharge_checklist",
    )
    financial_user = relationship("User", foreign_keys=[financial_cleared_by])
    clinical_user = relationship("User", foreign_keys=[clinical_cleared_by])


class IpdDischargeMedication(Base):
    __tablename__ = "ipd_discharge_medications"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    drug_name = Column(String(255), nullable=False)
    dose = Column(Numeric(10, 2), nullable=True)
    dose_unit = Column(String(50), default="")
    route = Column(String(50), default="")
    frequency = Column(String(50), default="")
    duration_days = Column(Integer, nullable=True)
    advice_text = Column(Text, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    admission = relationship("IpdAdmission",
                             back_populates="discharge_medications")
    created_by = relationship("User")


# ---------------------------------------------------------------------
# IPD Feedback (many) + Admission Feedback (1:1)
# ---------------------------------------------------------------------


class IpdFeedback(Base):
    __tablename__ = "ipd_feedback"

    id = Column(Integer, primary_key=True)
    admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), index=True)

    rating_overall = Column(Integer, nullable=True)
    rating_nursing = Column(Integer, nullable=True)
    rating_doctor = Column(Integer, nullable=True)
    rating_cleanliness = Column(Integer, nullable=True)

    comments = Column(Text, default="")
    collected_at = Column(DateTime, default=datetime.utcnow)
    collected_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    admission = relationship("IpdAdmission", back_populates="feedback_entries")


class IpdAdmissionFeedback(Base):
    __tablename__ = "ipd_admission_feedback"

    id = Column(Integer, primary_key=True, index=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    rating_nursing = Column(Integer, nullable=True)
    rating_doctor = Column(Integer, nullable=True)
    rating_cleanliness = Column(Integer, nullable=True)
    comments = Column(Text, nullable=True)
    suggestions = Column(Text, nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    admission = relationship("IpdAdmission", back_populates="feedback")
    created_by = relationship("User")




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
    status = Column(
        String(20),
        default="planned",  # planned/unplanned/cancelled/completed
    )
    surgeon_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    anaesthetist_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    staff_tags = Column(Text, default="")
    preop_notes = Column(Text, default="")
    postop_notes = Column(Text, default="")
    instrument_tracking = Column(Text, default="")
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)

    admission = relationship("IpdAdmission", back_populates="ot_cases")
    anaesthesia_records = relationship(
        "IpdAnaesthesiaRecord",
        back_populates="ot_case",
        cascade="all, delete-orphan",
    )


class IpdAnaesthesiaRecord(Base):
    __tablename__ = "ipd_anaesthesia_records"

    id = Column(Integer, primary_key=True)
    ot_case_id = Column(Integer, ForeignKey("ipd_ot_cases.id"), index=True)

    pre_assessment = Column(Text, default="")
    anaesthesia_type = Column(
        String(20),
        default="general",  # local/regional/spinal/general
    )
    intraop_monitoring = Column(Text, default="")
    drugs_administered = Column(Text, default="")
    post_status = Column(Text, default="")

    ot_case = relationship("IpdOtCase", back_populates="anaesthesia_records")


# ---------------------------------------------------------------------
# IPD Assessments (Generic)
# ---------------------------------------------------------------------


class IpdAssessment(Base):
    __tablename__ = "ipd_assessments"

    id = Column(Integer, primary_key=True, index=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assessment_type = Column(String(50), nullable=False, default="nursing")
    assessed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    summary = Column(Text, nullable=True)
    plan = Column(Text, nullable=True)
    type = Column(String(50),
                  nullable=False)  # 'pain' / 'fall' / 'pressure' / 'nutrition'
    score = Column(Integer, nullable=True)
    risk_level = Column(String(50), nullable=True)
    details = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    admission = relationship("IpdAdmission", back_populates="assessments")
    created_by = relationship("User")


# ---------------------------------------------------------------------
# IPD Medications (simple list, not MAR)
# ---------------------------------------------------------------------


class IpdMedication(Base):
    __tablename__ = "ipd_medications"

    id = Column(Integer, primary_key=True, index=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    drug_name = Column(String(255), nullable=False)
    route = Column(String(50), nullable=False, default="oral")
    frequency = Column(String(50), nullable=False, default="od")
    dose = Column(String(100), nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    instructions = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    admission = relationship("IpdAdmission", back_populates="medications")
    created_by = relationship("User")


# ---------------------------------------------------------------------
# Dressing / Transfusion Combined Log
# ---------------------------------------------------------------------


class IpdDressingTransfusion(Base):
    __tablename__ = "ipd_dressing_transfusion"

    id = Column(Integer, primary_key=True, index=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    entry_type = Column(String(20), nullable=False,
                        default="dressing")  # dressing | transfusion
    done_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    site = Column(String(255), nullable=True)
    product = Column(String(255), nullable=True)
    volume = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    admission = relationship("IpdAdmission",
                             back_populates="dressing_transfusion_entries")
    created_by = relationship("User")


class IpdIvFluidOrder(Base):
    """
    Intravenous fluids section of the drug chart.
    Each record corresponds to one IV fluid order / bag.
    """

    __tablename__ = "ipd_iv_fluid_orders"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Order details
    ordered_datetime = Column(DateTime, default=datetime.utcnow)
    fluid = Column(String(255), nullable=False)  # e.g. DNS 500 ml
    additive = Column(String(255), default="")  # e.g. KCl 20 mEq
    dose_ml = Column(Numeric(10, 2), nullable=True)  # total volume
    rate_ml_per_hr = Column(Numeric(10, 2), nullable=True)

    doctor_name = Column(String(255), default="")
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Administration details
    start_datetime = Column(DateTime, nullable=True)
    start_nurse_name = Column(String(255), default="")
    start_nurse_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    stop_datetime = Column(DateTime, nullable=True)
    stop_nurse_name = Column(String(255), default="")
    stop_nurse_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    remarks = Column(Text, default="")

    admission = relationship("IpdAdmission", back_populates="iv_fluid_orders")
    doctor = relationship("User", foreign_keys=[doctor_id])
    start_nurse = relationship("User", foreign_keys=[start_nurse_id])
    stop_nurse = relationship("User", foreign_keys=[stop_nurse_id])


class IpdDrugChartNurseRow(Base):
    """
    'Name of the nurse, specimen sign and Emp. no.' block at the bottom
    of the drug chart page.
    """

    __tablename__ = "ipd_drug_chart_nurse_rows"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    serial_no = Column(Integer, nullable=True)  # 1..10 etc
    nurse_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    nurse_name = Column(String(255), nullable=False)
    specimen_sign = Column(String(255), default="")  # simple text / initials
    emp_no = Column(String(50), default="")

    admission = relationship("IpdAdmission",
                             back_populates="drug_chart_nurse_rows")
    nurse = relationship("User", foreign_keys=[nurse_id])


class IpdDrugChartDoctorAuth(Base):
    """
    Doctor's daily authorisation block (signature for a given date).
    """

    __tablename__ = "ipd_drug_chart_doctor_auth"

    id = Column(Integer, primary_key=True)
    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    auth_date = Column(Date, nullable=False)  # one row per date
    doctor_name = Column(String(255), default="")
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    doctor_sign = Column(String(255), default="")  # can store initials / text
    remarks = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)

    admission = relationship("IpdAdmission", back_populates="doctor_auth_rows")
    doctor = relationship("User", foreign_keys=[doctor_id])
