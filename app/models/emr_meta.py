# FILE: app/models/emr_meta.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, UniqueConstraint, Index
)
from app.db.base import Base


class EmrClinicalPhase(Base):
    __tablename__ = "emr_clinical_phases"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)  # INTAKE, HISTORY...
    label = Column(String(80), nullable=False)
    hint = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=1000)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EmrTemplatePreset(Base):
    __tablename__ = "emr_template_presets"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)  # SOAP, DISCHARGE...
    label = Column(String(120), nullable=False)
    description = Column(String(255), nullable=True)

    dept_code = Column(String(32), nullable=True)          # NULL => global
    record_type_code = Column(String(32), nullable=True)   # NULL => global

    sections_json = Column(Text, nullable=False)           # JSON list of section codes
    schema_json = Column(Text, nullable=True)              # optional JSON

    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=1000)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_preset_scope", "dept_code", "record_type_code", "is_active"),
    )


class EmrSectionLibrary(Base):
    __tablename__ = "emr_section_library"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    label = Column(String(120), nullable=False)

    dept_code = Column(String(32), nullable=True)          # NULL => global
    record_type_code = Column(String(32), nullable=True)   # NULL => global
    phase_code = Column(String(32), nullable=True)         # references phase.code logically

    group = Column(String(32), nullable=True)              # SYSTEM, CUSTOM, NURSING...
    keywords = Column(String(255), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=1000)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_section_scope", "dept_code", "record_type_code", "is_active"),
        Index("ix_section_search", "label", "code"),
    )


class EmrDepartmentTone(Base):
    __tablename__ = "emr_department_tones"

    id = Column(Integer, primary_key=True)
    dept_code = Column(String(32), nullable=False, unique=True)

    # Tailwind token strings (same shape as your current deptTone output)
    bar = Column(String(160), nullable=False)
    chip = Column(String(160), nullable=False)
    glow = Column(String(160), nullable=False)
    btn = Column(String(80), nullable=False)

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("dept_code", name="uq_dept_tone_dept_code"),
    )
