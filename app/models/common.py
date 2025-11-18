# app/models/common.py
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.base import Base

class FileAttachment(Base):
    __tablename__ = "file_attachments"
    id = Column(Integer, primary_key=True)
    entity = Column(String(32),
                    index=True)  # 'lab_result' | 'ris_report' | 'ot_case'
    entity_id = Column(Integer, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), index=True)
    filename = Column(String(255))
    stored_path = Column(String(512))  # server path
    public_url = Column(String(512))  # e.g. /files/...
    content_type = Column(String(128))
    size_bytes = Column(Integer)
    note = Column(String(255))
    uploaded_by = Column(Integer, ForeignKey("users.id"))
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient")
