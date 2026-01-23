# FILE: app/models/patient.py
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    func,
    ForeignKey,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    uhid = Column(String(32), index=True, nullable=False)
    abha_number = Column(String(32), index=True, nullable=True)

    # prefix (Mr, Ms, Mrs, etc.)
    prefix = Column(String(16), nullable=True)

    # unique identifiers
    phone = Column(String(20), unique=True, index=True, nullable=True)
    email = Column(String(191), unique=True, index=True, nullable=True)

    # aadhar_last4 = Column(String(4), nullable=True)

    # core demographics
    first_name = Column(String(120), nullable=False)
    last_name = Column(String(120), nullable=True)
    gender = Column(String(16), nullable=False)
    dob = Column(Date, nullable=True)
    blood_group = Column(String(8), nullable=True)

    marital_status = Column(String(32), nullable=True)
    is_pregnant = Column(Boolean, nullable=False, default=False)
    rch_id = Column(String(32), index=True, nullable=True)
    # reference / marketing
    ref_source = Column(
        String(32),
        nullable=True,
    )  # doctor / google / social_media / ads / other
    ref_doctor_id = Column(Integer, nullable=True)
    ref_details = Column(String(255), nullable=True)

    # ID proof
    id_proof_type = Column(String(64), nullable=True)
    id_proof_no = Column(String(64), nullable=True)

    # guardian
    guardian_name = Column(String(120), nullable=True)
    guardian_phone = Column(String(20), nullable=True)
    guardian_relation = Column(String(64), nullable=True)

    # additional info
    # Value should match one of the codes/names configured in PatientType master
    patient_type = Column(String(32),
                          nullable=True)  # e.g. EMERGENCY / OPD / IPD / etc.
    tag = Column(String(64), nullable=True)
    religion = Column(String(64), nullable=True)
    occupation = Column(String(64), nullable=True)

    file_number = Column(String(64), nullable=True)
    file_location = Column(String(64), nullable=True)

    # credit / insurance (kept as plain IDs to avoid FK dependency complexity)
    credit_type = Column(String(32), nullable=True)
    credit_payer_id = Column(Integer, nullable=True)
    credit_tpa_id = Column(Integer, nullable=True)
    credit_plan_id = Column(Integer, nullable=True)

    principal_member_name = Column(String(120), nullable=True)
    principal_member_address = Column(String(255), nullable=True)

    policy_number = Column(String(64), nullable=True)
    policy_name = Column(String(120), nullable=True)

    # grouping
    family_id = Column(Integer, nullable=True)

    # status
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        onupdate=func.now(),
        server_default=func.now(),
    )

    # relationships
    addresses = relationship(
        "PatientAddress",
        cascade="all, delete-orphan",
        back_populates="patient",
        order_by="PatientAddress.id.desc()",  # ✅ latest first
        lazy="selectin",  # ✅ better loading
    )
    documents = relationship("PatientDocument", cascade="all, delete-orphan")
    consents = relationship(
        "PatientConsent",
        cascade="all, delete-orphan",
        back_populates="patient",
    )


class PatientAddress(Base):
    __tablename__ = "patient_addresses"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"))
    type = Column(String(20))  # current/permanent/office/other
    line1 = Column(String(191))
    line2 = Column(String(191))
    city = Column(String(120))
    state = Column(String(120))
    pincode = Column(String(20))
    country = Column(String(120), default="India")

    patient = relationship("Patient", back_populates="addresses")


class PatientDocument(Base):
    __tablename__ = "patient_documents"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"))
    type = Column(String(50))
    filename = Column(String(191))
    mime = Column(String(50))
    size = Column(Integer)
    storage_path = Column(String(255))
    uploaded_by = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())


class PatientConsent(Base):
    __tablename__ = "patient_consents"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        index=True,
        nullable=False,
    )
    type = Column(String(32), nullable=False)
    text = Column(String(2000), nullable=False)
    captured_at = Column(DateTime, server_default=func.now(), nullable=False)

    patient = relationship("Patient", back_populates="consents")


class PatientType(Base):
    """
    Patient Type master (Emergency, OPD, IPD, Health Checkup, etc.)
    Used as reference during patient registration & filters.
    """
    __tablename__ = "patient_types"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(64), unique=True, nullable=False)
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime,
                        server_default=func.now(),
                        onupdate=func.now())
