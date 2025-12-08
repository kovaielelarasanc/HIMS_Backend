# backend/app/models/doctor.py
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Numeric
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, nullable=False)

    user_id = Column(Integer,
                     ForeignKey("users.id"),
                     unique=True,
                     nullable=False)
    department_id = Column(Integer,
                           ForeignKey("departments.id"),
                           nullable=True)

    speciality = Column(String(255), nullable=True)
    qualification = Column(String(255), nullable=True)
    registration_no = Column(String(100), nullable=True)

    consultation_fee = Column(Numeric(10, 2), nullable=True)

    is_opd = Column(Boolean, default=True)
    is_ipd = Column(Boolean, default=True)
    is_teleconsult = Column(Boolean, default=False)

    is_active = Column(Boolean, default=True, index=True)

    # Relationships
    user = relationship("User", back_populates="doctor_profile")
    department = relationship("Department")
