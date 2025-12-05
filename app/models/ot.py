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
)
from sqlalchemy.orm import relationship

from app.db.base import Base

# ============================================================
#  OT MASTERS
# ============================================================


class OtSpeciality(Base):
    """
    Master for OT Specialities / Types (General, Ortho, Neuro, Cardiac, etc.)
    """
    __tablename__ = "ot_specialities"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

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

    theatres = relationship("OtTheatre", back_populates="speciality")


class OtEquipmentMaster(Base):
    """
    Master list of OT equipments (for checklists).
    Example: Anaesthesia Machine, Defibrillator, Suction, OT Table, Cautery.
    """
    __tablename__ = "ot_equipment_master"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    category = Column(String(100),
                      nullable=True)  # e.g. "Anaesthesia", "Monitoring"
    description = Column(Text, nullable=True)
    is_critical = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)


class OtEnvironmentSetting(Base):
    """
    Optional master for environment limits per theatre.
    (Temp, humidity, pressure recommended ranges).
    """
    __tablename__ = "ot_environment_settings"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    theatre_id = Column(Integer, ForeignKey("ot_theatres.id"), nullable=False)

    min_temperature_c = Column(Numeric(4, 1), nullable=True)
    max_temperature_c = Column(Numeric(4, 1), nullable=True)
    min_humidity_percent = Column(Numeric(4, 1), nullable=True)
    max_humidity_percent = Column(Numeric(4, 1), nullable=True)
    min_pressure_diff_pa = Column(Numeric(6, 2), nullable=True)
    max_pressure_diff_pa = Column(Numeric(6, 2), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    theatre = relationship("OtTheatre", back_populates="environment_setting")


class OtTheatre(Base):
    """
    Master for Operation Theatres.
    """
    __tablename__ = "ot_theatres"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    location = Column(String(255), nullable=True)

    speciality_id = Column(Integer,
                           ForeignKey("ot_specialities.id"),
                           nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    # Relationships
    speciality = relationship("OtSpeciality", back_populates="theatres")
    schedules = relationship(
        "OtSchedule",
        back_populates="theatre",
        cascade="all, delete-orphan",
    )
    environment_setting = relationship(
        "OtEnvironmentSetting",
        back_populates="theatre",
        uselist=False,
        cascade="all, delete-orphan",
    )
    environment_logs = relationship(
        "OtEnvironmentLog",
        back_populates="theatre",
        cascade="all, delete-orphan",
    )
    equipment_checklists = relationship(
        "OtEquipmentDailyChecklist",
        back_populates="theatre",
        cascade="all, delete-orphan",
    )
    cleaning_logs = relationship(
        "OtCleaningLog",
        back_populates="theatre",
        cascade="all, delete-orphan",
    )


# ============================================================
#  CORE TRANSACTIONS: SCHEDULE & CASE
# ============================================================


class OtSchedule(Base):
    """
    OT booking / scheduling per patient & surgeon.
    One schedule = one planned OT case.
    """
    __tablename__ = "ot_schedules"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True, index=True)

    theatre_id = Column(Integer, ForeignKey("ot_theatres.id"), nullable=False)
    date = Column(Date, nullable=False)

    planned_start_time = Column(Time, nullable=False)
    planned_end_time = Column(Time, nullable=True)

    # Patient / IP Admission (FKs to your existing tables)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    admission_id = Column(Integer,
                          ForeignKey("ipd_admissions.id"),
                          nullable=True,
                          index=True)

    # Surgeon / Anaesthetist user IDs (FKs to users table)
    surgeon_user_id = Column(Integer,
                             ForeignKey("users.id"),
                             nullable=False,
                             index=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id"),
                                  nullable=True,
                                  index=True)

    procedure_name = Column(String(255), nullable=False)
    side = Column(String(50), nullable=True)  # Left / Right / Bilateral / NA
    priority = Column(String(50), nullable=False, default="Elective")
    status = Column(
        String(50),
        nullable=False,
        default="planned",
    )  # planned / in_progress / completed / cancelled

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)

    # Relationships
    theatre = relationship("OtTheatre", back_populates="schedules")
    patient = relationship("Patient")
    admission = relationship("IpdAdmission")
    surgeon = relationship("User", foreign_keys=[surgeon_user_id])
    anaesthetist = relationship("User", foreign_keys=[anaesthetist_user_id])

    case = relationship(
        "OtCase",
        back_populates="schedule",
        uselist=False,
    )

    @property
    def case_id(self) -> int | None:
        """
        Virtual field so Pydantic can expose schedule.case_id
        without needing a real DB column.
        """
        return self.case.id if self.case else None


class OtCase(Base):
    """
    Central entity for one surgery.
    All checklists, notes, anaesthesia, etc. attach to this.
    """
    __tablename__ = "ot_cases"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer,
                         ForeignKey("ot_schedules.id"),
                         nullable=False,
                         unique=True)

    # Clinical identifiers
    preop_diagnosis = Column(Text, nullable=True)
    postop_diagnosis = Column(Text, nullable=True)
    final_procedure_name = Column(String(255), nullable=True)

    speciality_id = Column(Integer,
                           ForeignKey("ot_specialities.id"),
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

    schedule = relationship("OtSchedule", back_populates="case")
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
    )


# ============================================================
#  CLINICAL RECORDS LINKED TO OT CASE
# ============================================================


class PreAnaesthesiaEvaluation(Base):
    """
    Pre-Anaesthetic Evaluation (PAE) form.
    """
    __tablename__ = "ot_pre_anaesthesia_evaluations"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id"),
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
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    nurse_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    data = Column(
        JSON, nullable=False)  # flexible structure: {field: {value, remark}}
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="preop_checklist")
    nurse = relationship("User")


class SurgicalSafetyChecklist(Base):
    """
    WHO Surgical Safety Checklist – three phases:
    SIGN IN, TIME OUT, SIGN OUT.
    Each phase stored as JSON.
    """
    __tablename__ = "ot_safety_checklists"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)

    sign_in_data = Column(JSON, nullable=True)
    sign_in_done_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sign_in_time = Column(DateTime, nullable=True)

    time_out_data = Column(JSON, nullable=True)
    time_out_done_by_id = Column(Integer,
                                 ForeignKey("users.id"),
                                 nullable=True)
    time_out_time = Column(DateTime, nullable=True)

    sign_out_data = Column(JSON, nullable=True)
    sign_out_done_by_id = Column(Integer,
                                 ForeignKey("users.id"),
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
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    anaesthetist_user_id = Column(Integer,
                                  ForeignKey("users.id"),
                                  nullable=False)

    preop_vitals = Column(JSON, nullable=True)  # baseline vitals snapshot
    plan = Column(Text, nullable=True)  # GA/Spinal/Epidural etc.
    airway_plan = Column(Text, nullable=True)
    intraop_summary = Column(Text, nullable=True)
    complications = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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


class AnaesthesiaVitalLog(Base):
    """
    Time-series vitals during anaesthesia.
    """
    __tablename__ = "ot_anaesthesia_vitals"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer,
                       ForeignKey("ot_anaesthesia_records.id"),
                       nullable=False)

    time = Column(DateTime, nullable=False)
    bp_systolic = Column(Integer, nullable=True)
    bp_diastolic = Column(Integer, nullable=True)
    pulse = Column(Integer, nullable=True)
    spo2 = Column(Integer, nullable=True)
    rr = Column(Integer, nullable=True)
    etco2 = Column(Numeric(5, 2), nullable=True)
    temperature = Column(Numeric(4, 1), nullable=True)
    comments = Column(String(255), nullable=True)

    record = relationship("AnaesthesiaRecord", back_populates="vitals")


class AnaesthesiaDrugLog(Base):
    """
    Intra-op drug administration log.
    """
    __tablename__ = "ot_anaesthesia_drugs"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer,
                       ForeignKey("ot_anaesthesia_records.id"),
                       nullable=False)

    time = Column(DateTime, nullable=False)
    drug_name = Column(String(255), nullable=False)
    dose = Column(String(50), nullable=True)
    route = Column(String(50), nullable=True)
    remarks = Column(String(255), nullable=True)

    record = relationship("AnaesthesiaRecord", back_populates="drugs")


class OtNursingRecord(Base):
    """
    Intra-operative nursing care record.
    """
    __tablename__ = "ot_nursing_records"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    primary_nurse_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    positioning = Column(String(255), nullable=True)
    skin_prep_details = Column(Text, nullable=True)
    catheter_details = Column(Text, nullable=True)
    drains_details = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="nursing_record")
    primary_nurse = relationship("User")


class OtSpongeInstrumentCount(Base):
    """
    Sponge & instrument count record.
    """
    __tablename__ = "ot_sponge_instrument_counts"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)

    initial_count_data = Column(JSON, nullable=True)
    final_count_data = Column(JSON, nullable=True)
    discrepancy = Column(Boolean, default=False, nullable=False)
    discrepancy_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="counts_record")


class OtImplantRecord(Base):
    """
    Implants / prosthesis used in surgery.
    """
    __tablename__ = "ot_implant_records"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("ot_cases.id"), nullable=False)

    implant_name = Column(String(255), nullable=False)
    size = Column(String(50), nullable=True)
    batch_no = Column(String(100), nullable=True)
    lot_no = Column(String(100), nullable=True)
    manufacturer = Column(String(255), nullable=True)
    expiry_date = Column(Date, nullable=True)
    inventory_item_id = Column(Integer,
                               nullable=True)  # link to inventory if needed

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="implant_records")


class OperationNote(Base):
    """
    Surgeon’s Operation Notes.
    """
    __tablename__ = "ot_operation_notes"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    surgeon_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

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

    case = relationship("OtCase", back_populates="operation_note")
    surgeon = relationship("User")


class OtBloodTransfusionRecord(Base):
    """
    OT-side blood / blood component transfusion details.
    """
    __tablename__ = "ot_blood_transfusion_records"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("ot_cases.id"), nullable=False)

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
    """
    __tablename__ = "ot_pacu_records"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer,
                     ForeignKey("ot_cases.id"),
                     nullable=False,
                     unique=True)
    nurse_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    admission_time = Column(DateTime, nullable=True)
    discharge_time = Column(DateTime, nullable=True)
    pain_scores = Column(JSON, nullable=True)  # {time: score}
    vitals = Column(JSON, nullable=True)  # time-series or summary
    complications = Column(Text, nullable=True)
    disposition = Column(String(100), nullable=True)  # ward / ICU / home etc.

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("OtCase", back_populates="pacu_record")
    nurse = relationship("User")


# ============================================================
#  OT ADMIN / STATUTORY LOGS
#  (OT Register & Utilization will be REPORTS from OtCase + OtSchedule)
# ============================================================


class OtEquipmentDailyChecklist(Base):
    """
    Daily equipment checklist per OT theatre (defib, suction, etc.).
    `data` holds checklist items mapped from OtEquipmentMaster.
    """
    __tablename__ = "ot_equipment_checklists"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    theatre_id = Column(Integer, ForeignKey("ot_theatres.id"), nullable=False)
    date = Column(Date, nullable=False)
    shift = Column(String(50),
                   nullable=True)  # Morning / Evening / Night, etc.

    checked_by_user_id = Column(Integer,
                                ForeignKey("users.id"),
                                nullable=False)
    data = Column(
        JSON,
        nullable=False)  # {equipment_id or code: {ok: bool, remark: str}}

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    theatre = relationship("OtTheatre", back_populates="equipment_checklists")
    checked_by = relationship("User")


class OtCleaningLog(Base):
    """
    OT cleaning / sterility log (between cases and daily).
    """
    __tablename__ = "ot_cleaning_logs"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    theatre_id = Column(Integer, ForeignKey("ot_theatres.id"), nullable=False)
    date = Column(Date, nullable=False)
    session = Column(String(50),
                     nullable=True)  # pre-list / between-cases / end-of-day
    case_id = Column(Integer, ForeignKey("ot_cases.id"), nullable=True)

    method = Column(String(255),
                    nullable=True)  # e.g. mopping, fumigation, UV, etc.
    done_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    remarks = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    theatre = relationship("OtTheatre", back_populates="cleaning_logs")
    case = relationship("OtCase", back_populates="cleaning_logs")
    done_by = relationship("User")


class OtEnvironmentLog(Base):
    """
    Temperature, humidity, and pressure differential logs per OT.
    """
    __tablename__ = "ot_environment_logs"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    id = Column(Integer, primary_key=True)
    theatre_id = Column(Integer, ForeignKey("ot_theatres.id"), nullable=False)
    date = Column(Date, nullable=False)
    time = Column(Time, nullable=False)

    temperature_c = Column(Numeric(4, 1), nullable=True)
    humidity_percent = Column(Numeric(4, 1), nullable=True)
    pressure_diff_pa = Column(Numeric(6, 2), nullable=True)

    logged_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    theatre = relationship("OtTheatre", back_populates="environment_logs")
    logged_by = relationship("User")
