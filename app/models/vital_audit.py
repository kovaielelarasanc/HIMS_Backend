from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.db.base import Base

def utcnow() -> datetime:
    return datetime.utcnow()

class VitalEventAudit(Base):
    __tablename__ = "vital_event_audits"
    id = Column(Integer, primary_key=True)

    entity_type = Column(String(40), nullable=False)  # "ipd_newborn_resuscitation"
    entity_id = Column(Integer, nullable=False, index=True)

    action = Column(String(40), nullable=False)  # create/update/verify/finalize/print/void
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    ip_addr = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)

    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)

    actor = relationship("User", lazy="joined")

    __table_args__ = (
        Index("ix_vital_audit_entity", "entity_type", "entity_id"),
    )
