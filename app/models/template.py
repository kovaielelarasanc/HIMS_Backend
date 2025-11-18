from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from app.db.base import Base


class DocumentTemplate(Base):
    __tablename__ = "document_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    code = Column(String(80), unique=True, nullable=False, index=True)
    category = Column(String(40), default="report")  # report | consent
    subcategory = Column(String(80), nullable=True)
    description = Column(String(400), nullable=True)
    html = Column(Text, default="")
    css = Column(Text, default="")
    placeholders = Column(JSON, default={})
    is_active = Column(Boolean, default=True)
    version = Column(Integer, default=1)
    created_by = Column(Integer, nullable=True)
    updated_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    revisions = relationship("TemplateRevision",
                             cascade="all, delete-orphan",
                             back_populates="template")


class TemplateRevision(Base):
    __tablename__ = "template_revisions"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer,
                         ForeignKey("document_templates.id"),
                         nullable=False,
                         index=True)
    version = Column(Integer, default=1)
    html = Column(Text, default="")
    css = Column(Text, default="")
    placeholders = Column(JSON, default={})
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    template = relationship("DocumentTemplate", back_populates="revisions")


class PatientConsentTemp(Base):
    __tablename__ = "patient_consents_temp"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    template_id = Column(Integer,
                         ForeignKey("document_templates.id"),
                         nullable=False,
                         index=True)
    data = Column(JSON, default={})
    html_rendered = Column(Text, default="")
    pdf_path = Column(String(400), default="")
    status = Column(String(20), default="draft")  # draft | finalized
    signed_by = Column(Integer, nullable=True)
    witness_name = Column(String(160), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
