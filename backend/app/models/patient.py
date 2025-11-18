from sqlalchemy import Column, Integer, String, Boolean, Date, ForeignKey, DateTime, Text, func
from sqlalchemy.orm import relationship
from datetime import datetime
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

    # 👉 make these unique + indexed
    phone = Column(String(20), unique=True, index=True, nullable=True)
    email = Column(String(191), unique=True, index=True, nullable=True)

    aadhar_last4 = Column(String(4), nullable=True)
    first_name = Column(String(120), nullable=False)
    last_name = Column(String(120), nullable=True)
    gender = Column(String(16), nullable=False)
    dob = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime,
                        onupdate=func.now(),
                        server_default=func.now())

    addresses = relationship("PatientAddress",
                             cascade="all, delete-orphan",
                             back_populates="patient")
    documents = relationship("PatientDocument", cascade="all, delete-orphan")
    consents = relationship("PatientConsent", cascade="all, delete-orphan")


class PatientAddress(Base):
    __tablename__ = "patient_addresses"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"))
    type = Column(String(20))  # current/permanent
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
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class PatientConsent(Base):
    __tablename__ = "patient_consents"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    type = Column(String(32), nullable=False)
    text = Column(String(2000), nullable=False)
    captured_at = Column(DateTime, server_default=func.now(), nullable=False)

    patient = relationship("Patient", back_populates="consents")
