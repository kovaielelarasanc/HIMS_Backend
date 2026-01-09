# FILE: app/models/ot.py
from __future__ import annotations

from datetime import datetime, date, time

from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    Time,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    Numeric,
    JSON,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.models.ot_master import OtDeviceMaster

MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}

# ============================================================
#  OT MASTERS
# ============================================================


class OtSpeciality(Base):
    """
    Master for OT Specialities / Types (General, Ortho, Neuro, Cardiac, etc.)
    """
    __tablename__ = "ot_specialities"
    __table_args__ = (
        Index("ix_ot_specialities_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    procedures = relationship(
        "OtProcedure",
        back_populates="speciality",
        cascade="all, delete-orphan",
    )


class OtProcedure(Base):
    """
    Master list of OT Procedures:
    - used for scheduling & billing

    NEW REQUIREMENT:
    - Procedure fixed cost split-up:
      base_cost + anesthesia_cost + surgeon_cost + petitory_cost(optional) + asst_doctor_cost(optional)

    Note:
    - We KEEP old rate_per_hour/default_duration_min for backward compatibility.
    """
    __tablename__ = "ot_procedures"
    __table_args__ = (
        Index("ix_ot_procedures_active", "is_active"),
        Index("ix_ot_procedures_speciality", "speciality_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)

    speciality_id = Column(Integer,
                           ForeignKey("ot_specialities.id"),
                           nullable=True)

    default_duration_min = Column(Integer, nullable=True)
    rate_per_hour = Column(Numeric(10, 2), nullable=True)
    description = Column(Text, nullable=True)

    # ✅ NEW: Fixed cost split-up
    base_cost = Column(Numeric(12, 2), nullable=False, default=0)
    anesthesia_cost = Column(Numeric(12, 2), nullable=False, default=0)
    surgeon_cost = Column(Numeric(12, 2), nullable=False, default=0)
    petitory_cost = Column(Numeric(12, 2), nullable=False, default=0)
    asst_doctor_cost = Column(Numeric(12, 2), nullable=False, default=0)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    speciality = relationship("OtSpeciality", back_populates="procedures")

    schedule_links = relationship(
        "OtScheduleProcedure",
        back_populates="procedure",
        cascade="all, delete-orphan",
    )

    @property
    def total_fixed_cost(self):
        return ((self.base_cost or 0) + (self.anesthesia_cost or 0) +
                (self.surgeon_cost or 0) + (self.petitory_cost or 0) +
                (self.asst_doctor_cost or 0))


class OtEquipmentMaster(Base):
    """
    Master list of OT equipments (for checklists).
    Example: Anaesthesia Machine, Defibrillator, Suction, OT Table, Cautery.
    """
    __tablename__ = "ot_equipment_master"
    __table_args__ = (
        Index("ix_ot_equipment_active", "is_active"),
        Index("ix_ot_equipment_category", "category"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    category = Column(String(100),
                      nullable=True)  # "Anaesthesia", "Monitoring"
    description = Column(Text, nullable=True)
    is_critical = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)


# ============================================================
#  CORE TRANSACTIONS: SCHEDULE & CASE
# ============================================================


class OtScheduleProcedure(Base):
    """
    Link a schedule to one or more procedures.
    We still keep primary_procedure_id on OtSchedule for quick access.
    """
    __tablename__ = "ot_schedule_procedures"
    __table_args__ = (
        UniqueConstraint("schedule_id",
                         "procedure_id",
                         name="uq_ot_sched_proc"),
        Index("ix_ot_sched_proc_schedule", "schedule_id"),
        Index("ix_ot_sched_proc_procedure", "procedure_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    schedule_id = Column(Integer,
                         ForeignKey("ot_schedules.id", ondelete="CASCADE"),
                         nullable=False)
    procedure_id = Column(Integer,
                          ForeignKey("ot_procedures.id", ondelete="RESTRICT"),
                          nullable=False)
    is_primary = Column(Boolean, default=False, nullable=False)

    schedule = relationship("OtSchedule", back_populates="procedures")
    procedure = relationship("OtProcedure", back_populates="schedule_links")


class OtSchedule(Base):
    """
    OT Schedule – OT THEATER based (NO IPD bed master as OT location).
    Date + planned times are treated as IST-local schedule slots.

    DB stores created_at/updated_at in UTC (datetime.utcnow()) as naive.
    """
    __tablename__ = "ot_schedules"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_schedules_case_id"),
        Index("ix_ot_schedules_date", "date"),
        Index("ix_ot_schedules_status", "status"),
        Index("ix_ot_schedules_theater_date", "ot_theater_id", "date"),
        Index("ix_ot_schedules_admission", "admission_id"),
        Index("ix_ot_schedules_patient", "patient_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    # Planned date & time
    date = Column(Date, nullable=False)
    planned_start_time = Column(Time, nullable=False)
    planned_end_time = Column(Time, nullable=True)

    # Patient / Admission context
    patient_id = Column(Integer,
                        ForeignKey("patients.id", ondelete="SET NULL"),
                        nullable=True)
    admission_id = Column(Integer,
                          ForeignKey("ipd_admissions.id", ondelete="SET NULL"),
                          nullable=True)

    # ✅ OT THEATER (matches your DB: ot_theater_masters)
    ot_theater_id = Column(Integer,
                           ForeignKey("ot_theater_masters.id",
                                      ondelete="SET NULL"),
                           nullable=True)

    # Staff
    surgeon_user_id = Column(Integer,
                             ForeignKey("users.id", ondelete="SET NULL"),
                             nullable=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id", ondelete="SET NULL"),
                                  nullable=True)

    # Optional doctors
    petitory_user_id = Column(Integer,
                              ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)
    asst_doctor_user_id = Column(Integer,
                                 ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)

    # Procedure details
    procedure_name = Column(String(255), nullable=True)
    side = Column(String(50), nullable=True)
    priority = Column(String(50), default="Elective", nullable=False)
    notes = Column(Text, nullable=True)

    status = Column(String(50), default="planned", nullable=False)

    # Link to OT Case
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="SET NULL"),
                     nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    # Primary procedure from master
    primary_procedure_id = Column(Integer,
                                  ForeignKey("ot_procedures.id",
                                             ondelete="SET NULL"),
                                  nullable=True)

    # Relationships
    patient = relationship("Patient")
    admission = relationship("IpdAdmission")

    # ✅ MUST match your master class name: OtTheaterMaster
    theater = relationship("OtTheaterMaster",
                           foreign_keys=[ot_theater_id],
                           lazy="joined")

    surgeon = relationship("User", foreign_keys=[surgeon_user_id])
    anaesthetist = relationship("User", foreign_keys=[anaesthetist_user_id])
    petitory = relationship("User", foreign_keys=[petitory_user_id])
    asst_doctor = relationship("User", foreign_keys=[asst_doctor_user_id])

    primary_procedure = relationship("OtProcedure",
                                     foreign_keys=[primary_procedure_id])

    procedures = relationship(
        "OtScheduleProcedure",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )

    case = relationship(
        "OtCase",
        back_populates="schedule",
        foreign_keys=[case_id],
        uselist=False,
    )


class OtCase(Base):
    """
    Central entity for one surgery.
    All checklists, notes, anaesthesia, etc. attach to this.
    """
    __tablename__ = "ot_cases"
    __table_args__ = (
        Index("ix_ot_cases_created_at", "created_at"),
        Index("ix_ot_cases_speciality", "speciality_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)

    # Clinical identifiers
    preop_diagnosis = Column(Text, nullable=True)
    postop_diagnosis = Column(Text, nullable=True)
    final_procedure_name = Column(String(255), nullable=True)

    speciality_id = Column(Integer,
                           ForeignKey("ot_specialities.id",
                                      ondelete="SET NULL"),
                           nullable=True)

    # Actual timings
    actual_start_time = Column(DateTime, nullable=True)
    actual_end_time = Column(DateTime, nullable=True)

    outcome = Column(String(50),
                     nullable=True)  # completed / abandoned / converted
    icu_required = Column(Boolean, default=False, nullable=False)
    immediate_postop_condition = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    # ✅ case -> schedule (1:1)
    schedule = relationship("OtSchedule", back_populates="case", uselist=False)

    speciality = relationship("OtSpeciality")

    # Clinical linked records (one-to-one or one-to-many)
    preanaesthesia = relationship(
        "PreAnaesthesiaEvaluation",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    preop_checklist = relationship(
        "PreOpChecklist",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    safety_checklist = relationship(
        "SurgicalSafetyChecklist",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    anaesthesia_record = relationship(
        "AnaesthesiaRecord",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    nursing_record = relationship(
        "OtNursingRecord",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    counts_record = relationship(
        "OtSpongeInstrumentCount",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    implant_records = relationship(
        "OtImplantRecord",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    operation_note = relationship(
        "OperationNote",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    blood_records = relationship(
        "OtBloodTransfusionRecord",
        back_populates="case",
        cascade="all, delete-orphan",
    )
    pacu_record = relationship(
        "PacuRecord",
        back_populates="case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    cleaning_logs = relationship(
        "OtCleaningLog",
        back_populates="case",
        cascade="all, delete-orphan",
    )

    @property
    def schedule_id(self) -> int | None:
        return self.schedule.id if self.schedule else None


# ============================================================
#  CLINICAL RECORDS LINKED TO OT CASE
# ============================================================


class PreAnaesthesiaEvaluation(Base):
    """
    Pre-Anaesthetic Evaluation (PAE) form.
    """
    __tablename__ = "ot_pre_anaesthesia_evaluations"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_pae_case"),
        Index("ix_ot_pae_anaesthetist", "anaesthetist_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id", ondelete="RESTRICT"),
                                  nullable=False)

    asa_grade = Column(String(10), nullable=False)  # ASA I–V
    comorbidities = Column(Text, nullable=True)
    airway_assessment = Column(Text, nullable=True)
    allergies = Column(Text, nullable=True)
    previous_anaesthesia_issues = Column(Text, nullable=True)
    plan = Column(Text, nullable=True)
    risk_explanation = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="preanaesthesia")
    anaesthetist = relationship("User")


class PreOpChecklist(Base):
    """
    Pre-Operative Checklist (nursing).
    JSON holds all checkbox items, remarks etc.
    """
    __tablename__ = "ot_preop_checklists"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_preop_case"),
        Index("ix_ot_preop_nurse", "nurse_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)
    nurse_user_id = Column(Integer,
                           ForeignKey("users.id", ondelete="RESTRICT"),
                           nullable=False)

    data = Column(JSON, nullable=False)  # {field: {value, remark}}
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="preop_checklist")
    nurse = relationship("User")


class SurgicalSafetyChecklist(Base):
    """
    WHO Surgical Safety Checklist – SIGN IN, TIME OUT, SIGN OUT.
    Each phase stored as JSON.
    """
    __tablename__ = "ot_safety_checklists"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_safety_case"),
        Index("ix_ot_safety_signin_by", "sign_in_done_by_id"),
        Index("ix_ot_safety_timeout_by", "time_out_done_by_id"),
        Index("ix_ot_safety_signout_by", "sign_out_done_by_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)

    sign_in_data = Column(JSON, nullable=True)
    sign_in_done_by_id = Column(Integer,
                                ForeignKey("users.id", ondelete="SET NULL"),
                                nullable=True)
    sign_in_time = Column(DateTime, nullable=True)

    time_out_data = Column(JSON, nullable=True)
    time_out_done_by_id = Column(Integer,
                                 ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)
    time_out_time = Column(DateTime, nullable=True)

    sign_out_data = Column(JSON, nullable=True)
    sign_out_done_by_id = Column(Integer,
                                 ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)
    sign_out_time = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="safety_checklist")
    sign_in_done_by = relationship("User", foreign_keys=[sign_in_done_by_id])
    time_out_done_by = relationship("User", foreign_keys=[time_out_done_by_id])
    sign_out_done_by = relationship("User", foreign_keys=[sign_out_done_by_id])


class AnaesthesiaRecord(Base):
    """
    Main Anaesthetic Record header.
    Real-time vitals and drug logs are in child tables.
    """
    __tablename__ = "ot_anaesthesia_records"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_anaesthesia_case"),
        Index("ix_ot_anaesthesia_anaesthetist", "anaesthetist_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id", ondelete="RESTRICT"),
                                  nullable=False)

    preop_vitals = Column(JSON, nullable=True)  # baseline vitals snapshot
    plan = Column(Text, nullable=True)  # GA/Spinal/Epidural etc.
    airway_plan = Column(Text, nullable=True)
    intraop_summary = Column(Text, nullable=True)
    complications = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)  # ✅ NEW

    case = relationship("OtCase", back_populates="anaesthesia_record")
    anaesthetist = relationship("User")

    vitals = relationship(
        "AnaesthesiaVitalLog",
        back_populates="record",
        cascade="all, delete-orphan",
    )
    drugs = relationship(
        "AnaesthesiaDrugLog",
        back_populates="record",
        cascade="all, delete-orphan",
    )
    devices = relationship("AnaesthesiaDeviceUse",
                           back_populates="record",
                           cascade="all, delete-orphan")


class AnaesthesiaDeviceUse(Base):
    """
    Used OT devices during anaesthesia.
    Billing can read this table to auto-add invoice items.
    """
    __tablename__ = "ot_anaesthesia_device_uses"
    __table_args__ = (
        UniqueConstraint("record_id", "device_id", name="uq_ot_anaes_device"),
        Index("ix_ot_anaes_device_record", "record_id"),
        Index("ix_ot_anaes_device_device", "device_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer,
                       ForeignKey("ot_anaesthesia_records.id",
                                  ondelete="CASCADE"),
                       nullable=False)
    device_id = Column(Integer,
                       ForeignKey("ot_device_masters.id", ondelete="RESTRICT"),
                       nullable=False)

    qty = Column(Integer, nullable=False, default=1)
    notes = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    record = relationship("AnaesthesiaRecord", back_populates="devices")
    device = relationship("OtDeviceMaster", lazy="joined")


class AnaesthesiaVitalLog(Base):
    """
    Time-series vitals during anaesthesia.
    """
    __tablename__ = "ot_anaesthesia_vitals"
    __table_args__ = (
        Index("ix_ot_an_vitals_record_time", "record_id", "time"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer,
                       ForeignKey("ot_anaesthesia_records.id",
                                  ondelete="CASCADE"),
                       nullable=False,
                       index=True)

    time = Column(DateTime, nullable=False)

    bp_systolic = Column(Integer, nullable=True)
    bp_diastolic = Column(Integer, nullable=True)
    pulse = Column(Integer, nullable=True)
    spo2 = Column(Integer, nullable=True)
    rr = Column(Integer, nullable=True)
    etco2 = Column(Numeric(5, 2), nullable=True)
    temperature = Column(Numeric(4, 1), nullable=True)
    comments = Column(String(255), nullable=True)

    ventilation_mode = Column(
        String(20), nullable=True)  # Spont/Assist/Control/Manual/Ventilator
    peak_airway_pressure = Column(Numeric(5, 2), nullable=True)  # cmH2O
    cvp_pcwp = Column(Numeric(5, 2), nullable=True)  # CVP / PCWP
    st_segment = Column(String(20), nullable=True)  # Normal/Depression/...
    urine_output_ml = Column(Integer, nullable=True)
    blood_loss_ml = Column(Integer, nullable=True)

    oxygen_fio2 = Column(String(50), nullable=True)
    n2o = Column(String(50), nullable=True)
    air = Column(String(50), nullable=True)
    agent = Column(String(50), nullable=True)
    iv_fluids = Column(String(100), nullable=True)

    record = relationship("AnaesthesiaRecord", back_populates="vitals")


class AnaesthesiaDrugLog(Base):
    """
    Intra-op drug administration log.
    """
    __tablename__ = "ot_anaesthesia_drugs"
    __table_args__ = (
        Index("ix_ot_an_drugs_record_time", "record_id", "time"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer,
                       ForeignKey("ot_anaesthesia_records.id",
                                  ondelete="CASCADE"),
                       nullable=False,
                       index=True)

    time = Column(DateTime, nullable=False)
    drug_name = Column(String(255), nullable=False)
    dose = Column(String(50), nullable=True)
    route = Column(String(50), nullable=True)
    remarks = Column(String(255), nullable=True)

    record = relationship("AnaesthesiaRecord", back_populates="drugs")


class OtNursingRecord(Base):
    """
    Intra-operative nursing care record – aligned with UI NursingTab.
    """
    __tablename__ = "ot_nursing_records"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_nursing_case"),
        Index("ix_ot_nursing_primary_nurse", "primary_nurse_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)
    primary_nurse_id = Column(Integer,
                              ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)

    scrub_nurse_name = Column(String(255), nullable=True)
    circulating_nurse_name = Column(String(255), nullable=True)

    positioning = Column(String(255), nullable=True)
    skin_prep = Column(String(255), nullable=True)
    catheterisation = Column(String(255), nullable=True)
    diathermy_plate_site = Column(String(255), nullable=True)

    counts_initial_done = Column(Boolean, nullable=False, default=False)
    counts_closure_done = Column(Boolean, nullable=False, default=False)

    antibiotics_time = Column(Time, nullable=True)

    warming_measures = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    case = relationship("OtCase", back_populates="nursing_record")
    primary_nurse = relationship("User")


MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class OtCaseInstrumentCountLine(Base):
    __tablename__ = "ot_case_instrument_count_lines"
    __table_args__ = (
        UniqueConstraint("case_id",
                         "instrument_id",
                         name="uq_ot_case_instrument_line"),
        Index("ix_ot_case_instrument_case", "case_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)

    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False)

    instrument_id = Column(Integer,
                           ForeignKey("ot_instrument_masters.id"),
                           nullable=True)
    instrument_code = Column(String(40), nullable=False, default="")
    instrument_name = Column(String(200), nullable=False, default="")
    uom = Column(String(30), nullable=False, default="Nos")

    initial_qty = Column(Integer, nullable=False, default=0)
    added_qty = Column(Integer, nullable=False, default=0)
    final_qty = Column(Integer, nullable=False, default=0)

    remarks = Column(String(500), default="")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class OtSpongeInstrumentCount(Base):
    """
    Sponge & instrument count record.
    """
    __tablename__ = "ot_sponge_instrument_counts"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_counts_case"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)

    initial_count_data = Column(JSON, nullable=True)
    final_count_data = Column(JSON, nullable=True)
    discrepancy = Column(Boolean, default=False, nullable=False)
    discrepancy_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=True)

    case = relationship("OtCase", back_populates="counts_record")


class OtImplantRecord(Base):
    """
    Implants / prosthesis used in surgery.
    """
    __tablename__ = "ot_implant_records"
    __table_args__ = (
        Index("ix_ot_implants_case", "case_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     index=True)

    implant_name = Column(String(255), nullable=False)
    size = Column(String(50), nullable=True)
    batch_no = Column(String(100), nullable=True)
    lot_no = Column(String(100), nullable=True)
    manufacturer = Column(String(255), nullable=True)
    expiry_date = Column(Date, nullable=True)
    inventory_item_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="implant_records")


class OperationNote(Base):
    """
    Surgeon’s Operation Notes.
    """
    __tablename__ = "ot_operation_notes"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_opnote_case"),
        Index("ix_ot_opnote_surgeon", "surgeon_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     unique=True)
    surgeon_user_id = Column(Integer,
                             ForeignKey("users.id", ondelete="RESTRICT"),
                             nullable=False)

    preop_diagnosis = Column(Text, nullable=True)
    postop_diagnosis = Column(Text, nullable=True)
    indication = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)
    procedure_steps = Column(Text, nullable=True)
    blood_loss_ml = Column(Integer, nullable=True)
    complications = Column(Text, nullable=True)
    drains_details = Column(Text, nullable=True)
    postop_instructions = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    case = relationship("OtCase", back_populates="operation_note")
    surgeon = relationship("User")


class OtBloodTransfusionRecord(Base):
    """
    OT-side blood / blood component transfusion details.
    """
    __tablename__ = "ot_blood_transfusion_records"
    __table_args__ = (
        Index("ix_ot_blood_case", "case_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="CASCADE"),
                     nullable=False,
                     index=True)

    component = Column(String(50),
                       nullable=False)  # PRBC / FFP / Platelet / etc.
    units = Column(Integer, nullable=True)
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    reaction = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="blood_records")





class PacuRecord(Base):
    """
    Post-Anaesthesia Care Unit (Recovery) record.
    Matches: POST OPERATIVE RECOVERY RECORD sheet
    """
    __tablename__ = "ot_pacu_records"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_ot_pacu_case"),
        Index("ix_ot_pacu_nurse", "nurse_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    case_id = Column(
        Integer,
        ForeignKey("ot_cases.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    nurse_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ---- Times (Sheet: Time to RECOVERY / Time to WARD/ICU)
    time_to_recovery = Column(String(5), nullable=True)  # "HH:MM"
    time_to_ward_icu = Column(String(5), nullable=True)  # "HH:MM"
    disposition = Column(String(50), nullable=True)  # Ward / ICU / Home etc.

    # ---- Sheet checkbox groups (stored as string arrays)
    # Anaesthesia type is fetched from Anaesthesia record for PDF header,
    # but you may store PACU-selected types if you want (optional).
    anaesthesia_methods = Column(
        JSON,
        nullable=True)  # ["GA/MAC", "Spinal/Epidural", "Nerve/Plexus Block"]
    airway_support = Column(
        JSON, nullable=True
    )  # ["None", "Face Mask/Airway", "LMA", "Intubated", "O2"]
    monitoring = Column(JSON, nullable=True)  # ["SpO2", "NIBP", "ECG", "CVP"]

    # ---- Sheet right column items
    post_op_charts = Column(
        JSON, nullable=True
    )  # ["Diabetic Chart", "I.V. Fluids", "Analgesia", "PCA Chart"]
    tubes_drains = Column(
        JSON, nullable=True
    )  # ["Wound Drains", "Urinary Catheter", "NG Tube", "Irrigation"]

    # ---- Vitals / Chart entries (time-series)
    # Each row example:
    # {"time":"10:15","spo2":"98","hr":"86","bp":"120/80","cvp":"8","rbs":"142","remarks":"Stable"}
    vitals_log = Column(JSON, nullable=True)

    # ---- Notes / Instructions (Sheet bottom)
    post_op_instructions = Column(Text, nullable=True)
    iv_fluids_orders = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=True)

    case = relationship("OtCase", back_populates="pacu_record")
    nurse = relationship("User")


# ============================================================
#  OT ADMIN / STATUTORY LOGS
#  (OT Register & Utilization will be REPORTS from OtCase + OtSchedule)
# ============================================================


class OtEquipmentDailyChecklist(Base):
    """
    Daily equipment checklist per OT location (tied to IPD bed master).
    `data` holds checklist items mapped from OtEquipmentMaster.
    """
    __tablename__ = "ot_equipment_checklists"
    __table_args__ = (
        Index("ix_ot_eq_chk_ot_bed_date", "ot_bed_id", "date"),
        Index("ix_ot_eq_chk_checked_by", "checked_by_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    # ✅ OT location bed
    ot_bed_id = Column(Integer,
                       ForeignKey("ipd_beds.id", ondelete="SET NULL"),
                       nullable=True,
                       index=True)

    date = Column(Date, nullable=False)
    shift = Column(String(50),
                   nullable=True)  # Morning / Evening / Night, etc.

    checked_by_user_id = Column(Integer,
                                ForeignKey("users.id", ondelete="RESTRICT"),
                                nullable=False)
    data = Column(
        JSON, nullable=False)  # {equipment_id/code: {ok: bool, remark: str}}

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ot_bed = relationship("IpdBed", foreign_keys=[ot_bed_id], lazy="joined")
    checked_by = relationship("User")


class OtCleaningLog(Base):
    """
    OT cleaning / sterility log (between cases and daily).
    Tied to OT location bed (shared master) and optionally a case.
    """
    __tablename__ = "ot_cleaning_logs"
    __table_args__ = (
        Index("ix_ot_clean_ot_bed_date", "ot_bed_id", "date"),
        Index("ix_ot_clean_case", "case_id"),
        Index("ix_ot_clean_done_by", "done_by_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    ot_bed_id = Column(Integer,
                       ForeignKey("ipd_beds.id", ondelete="SET NULL"),
                       nullable=True,
                       index=True)
    date = Column(Date, nullable=False)
    session = Column(String(50),
                     nullable=True)  # pre-list / between-cases / EOD

    case_id = Column(Integer,
                     ForeignKey("ot_cases.id", ondelete="SET NULL"),
                     nullable=True,
                     index=True)

    method = Column(String(255),
                    nullable=True)  # mopping, fumigation, UV, etc.
    done_by_user_id = Column(Integer,
                             ForeignKey("users.id", ondelete="RESTRICT"),
                             nullable=False)
    remarks = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ot_bed = relationship("IpdBed", foreign_keys=[ot_bed_id], lazy="joined")
    case = relationship("OtCase", back_populates="cleaning_logs")
    done_by = relationship("User")


class OtEnvironmentLog(Base):
    """
    Temperature, humidity, and pressure differential logs per OT location.
    """
    __tablename__ = "ot_environment_logs"
    __table_args__ = (
        Index("ix_ot_env_ot_bed_date_time", "ot_bed_id", "date", "time"),
        Index("ix_ot_env_logged_by", "logged_by_user_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True)

    ot_bed_id = Column(Integer,
                       ForeignKey("ipd_beds.id", ondelete="SET NULL"),
                       nullable=True,
                       index=True)
    date = Column(Date, nullable=False)
    time = Column(Time, nullable=False)

    temperature_c = Column(Numeric(4, 1), nullable=True)
    humidity_percent = Column(Numeric(4, 1), nullable=True)
    pressure_diff_pa = Column(Numeric(6, 2), nullable=True)

    logged_by_user_id = Column(Integer,
                               ForeignKey("users.id", ondelete="RESTRICT"),
                               nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ot_bed = relationship("IpdBed", foreign_keys=[ot_bed_id], lazy="joined")
    logged_by = relationship("User")
