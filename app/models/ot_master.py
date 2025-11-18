from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime, UniqueConstraint, Index, ForeignKey
from app.db.base import Base


class OtSurgeryMaster(Base):
    """
    OT Master — standardized surgeries mapped to codes & default pricing.
    """
    __tablename__ = "ot_surgery_masters"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ot_surgery_code"),
        UniqueConstraint("name", name="uq_ot_surgery_name"),
        Index("ix_ot_surgery_active", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)
    default_cost = Column(Numeric(12, 2), nullable=False, default=0)
    description = Column(String(500), default="")
    active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
