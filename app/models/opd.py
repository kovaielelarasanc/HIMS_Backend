# app/models/opd.py
from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    Time,
    DateTime,
    Boolean,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    Text,
    Index,
    CheckConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.base import Base


class OpdSchedule(Base):
    __tablename__ = "opd_schedules"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_opd_sch_weekday"),
        CheckConstraint("end_time > start_time", name="ck_opd_sch_time"),
        # NEW: ensure one schedule per doctor per weekday (no duplicates)
        UniqueConstraint(
            "doctor_user_id",
            "weekday",
            name="uq_opd_sch_doctor_weekday",
        ),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)
    doctor_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        index=True,
        nullable=False,
    )

    weekday = Column(Integer, nullable=False)  # 0..6
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    slot_minutes = Column(Integer, nullable=False, default=15)
    location = Column(String(120), default="")
    is_active = Column(Boolean, default=True)

    doctor = relationship("User", foreign_keys=[doctor_user_id])


class Appointment(Base):
    __tablename__ = "opd_appointments"
    __table_args__ = (
        UniqueConstraint(
            "doctor_user_id",
            "date",
            "slot_start",
            name="uq_doctor_date_slot",
        ),
        CheckConstraint("slot_end > slot_start", name="ck_appt_slot_time"),
        Index("ix_opd_appt_patient_date", "patient_id", "date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
        index=True,
    )
    doctor_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    date = Column(Date, nullable=False)
    slot_start = Column(Time, nullable=False)
    slot_end = Column(Time, nullable=False)
    purpose = Column(String(200), default="Consultation")
    status = Column(String(30), default="booked")  # booked / checked_in / ...
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_user_id])
    department = relationship("Department", foreign_keys=[department_id])
    # backref from FollowUp via appointment relationship string


class Visit(Base):
    __tablename__ = "opd_visits"
    __table_args__ = (
        UniqueConstraint("episode_id", name="uq_opd_visits_episode"),
        # allows multiple NULLs
        UniqueConstraint("appointment_id", name="uq_opd_visits_appt"),
    )

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(
        Integer,
        ForeignKey("opd_appointments.id"),
        nullable=True,
        index=True,
    )
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
        index=True,
    )
    doctor_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    episode_id = Column(
        String(30),
        nullable=False,
        index=True,  # OP-YYYYMM-XXXX
    )
    visit_at = Column(DateTime, default=datetime.utcnow)

    chief_complaint = Column(String(400), nullable=True)
    symptoms = Column(String(1000), nullable=True)
    soap_subjective = Column(Text, nullable=True)
    soap_objective = Column(Text, nullable=True)
    soap_assessment = Column(Text, nullable=True)
    plan = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    appointment = relationship("Appointment", foreign_keys=[appointment_id])
    patient = relationship("Patient", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_user_id])
    department = relationship("Department", foreign_keys=[department_id])


class Vitals(Base):
    __tablename__ = "opd_vitals"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )
    height_cm = Column(Numeric(6, 2), nullable=True)
    weight_kg = Column(Numeric(6, 2), nullable=True)
    bp_systolic = Column(Integer, nullable=True)
    bp_diastolic = Column(Integer, nullable=True)
    pulse = Column(Integer, nullable=True)
    rr = Column(Integer, nullable=True)
    temp_c = Column(Numeric(4, 1), nullable=True)
    spo2 = Column(Integer, nullable=True)
    notes = Column(String(600), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", foreign_keys=[patient_id])

    appointment_id = Column(
        Integer,
        ForeignKey("opd_appointments.id"),
        nullable=True,
        index=True,
    )
    appointment = relationship("Appointment", foreign_keys=[appointment_id])


class Prescription(Base):
    __tablename__ = "opd_prescriptions"
    __table_args__ = (UniqueConstraint("visit_id", name="uq_rx_visit"), )

    id = Column(Integer, primary_key=True, index=True)
    visit_id = Column(
        Integer,
        ForeignKey("opd_visits.id"),
        nullable=False,
        index=True,
    )
    notes = Column(Text, nullable=True)
    signed_at = Column(DateTime, nullable=True)
    signed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    visit = relationship("Visit", foreign_keys=[visit_id])
    items = relationship("PrescriptionItem", cascade="all, delete-orphan")
    signer = relationship("User", foreign_keys=[signed_by])


class PrescriptionItem(Base):
    __tablename__ = "opd_prescription_items"

    id = Column(Integer, primary_key=True, index=True)
    prescription_id = Column(
        Integer,
        ForeignKey("opd_prescriptions.id"),
        nullable=False,
        index=True,
    )
    drug_name = Column(String(200), nullable=False)
    strength = Column(String(100), nullable=True)
    frequency = Column(String(100), nullable=True)
    duration_days = Column(Integer, default=0)
    quantity = Column(Integer, default=0)
    unit_price = Column(Numeric(10, 2), default=0)


class LabTest(Base):
    """
    LAB MASTER – used by LIS/IPD billing and OPD orders.
    DO NOT REMOVE.
    """
    __tablename__ = "lab_tests"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    price = Column(Numeric(10, 2), default=0)


class RadiologyTest(Base):
    """
    RIS MASTER – used by RIS/IPD billing and OPD orders.
    DO NOT REMOVE.
    """
    __tablename__ = "radiology_tests"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    modality = Column(String(16))
    price = Column(Numeric(10, 2), default=0)
    is_active = Column(Boolean, default=True)


class LabOrder(Base):
    __tablename__ = "opd_lab_orders"
    __table_args__ = (
        UniqueConstraint(
            "visit_id",
            "test_id",
            name="uq_lab_order_visit_test",
        ),
        Index("ix_lab_order_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    visit_id = Column(
        Integer,
        ForeignKey("opd_visits.id"),
        index=True,
        nullable=False,
    )
    test_id = Column(Integer, ForeignKey("lab_tests.id"), nullable=False)
    ordered_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(30), default="ordered")

    visit = relationship("Visit", foreign_keys=[visit_id])
    test = relationship("LabTest", foreign_keys=[test_id])


class RadiologyOrder(Base):
    __tablename__ = "opd_radiology_orders"
    __table_args__ = (
        UniqueConstraint(
            "visit_id",
            "test_id",
            name="uq_ris_order_visit_test",
        ),
        Index("ix_ris_order_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    visit_id = Column(
        Integer,
        ForeignKey("opd_visits.id"),
        index=True,
        nullable=False,
    )
    test_id = Column(Integer, ForeignKey("radiology_tests.id"), nullable=False)
    ordered_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(30), default="ordered")

    visit = relationship("Visit", foreign_keys=[visit_id])
    test = relationship("RadiologyTest", foreign_keys=[test_id])


class Medicine(Base):
    __tablename__ = "medicines"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    form = Column(String(60), nullable=True)  # tablet/syrup/injection etc.
    unit = Column(String(40), nullable=True)  # per tab / per 100ml etc.
    price_per_unit = Column(Numeric(10, 2), default=0)


class DoctorFee(Base):
    """
    Doctor Consultation Fee master – used by OPD billing auto-pricing.
    One active row per doctor (enforced by unique constraint).
    """
    __tablename__ = "doctor_fees"
    __table_args__ = (UniqueConstraint(
        "doctor_user_id",
        name="uq_doctor_fee_doctor",
    ), )

    id = Column(Integer, primary_key=True, index=True)
    doctor_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    base_fee = Column(Numeric(10, 2), nullable=False, default=0)
    followup_fee = Column(Numeric(10, 2), nullable=True)
    currency = Column(String(8), default="INR")
    is_active = Column(Boolean, default=True)
    notes = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    doctor = relationship("User", foreign_keys=[doctor_user_id])


# ---------- NEW: Follow-up tracking ----------
class FollowUp(Base):
    """
    Follow-up request created from a Visit.

    - Initially status = 'waiting' (no slot yet).
    - Waiting-time screen will confirm & assign a real Appointment.
    """

    __tablename__ = "opd_followups"
    __table_args__ = (
        UniqueConstraint(
            "appointment_id",
            name="uq_followup_appointment",
        ),  # at most one follow-up record -> appointment link
    )

    id = Column(Integer, primary_key=True, index=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
        index=True,
    )
    doctor_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    source_visit_id = Column(
        Integer,
        ForeignKey("opd_visits.id"),
        nullable=False,
        index=True,
    )

    # When doctor wants patient to come again (initial target)
    due_date = Column(Date, nullable=False)

    # waiting | scheduled | completed | cancelled
    status = Column(String(30), default="waiting")

    # Once waiting is confirmed -> real appointment
    appointment_id = Column(
        Integer,
        ForeignKey("opd_appointments.id"),
        nullable=True,
        index=True,
    )

    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    patient = relationship("Patient", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_user_id])
    department = relationship("Department", foreign_keys=[department_id])
    source_visit = relationship("Visit", foreign_keys=[source_visit_id])
    appointment = relationship("Appointment", foreign_keys=[appointment_id])
