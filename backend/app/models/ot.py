from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Numeric, Index
from sqlalchemy.orm import relationship
from app.db.base import Base


class OtOrder(Base):
    """
    Dedicated OT module — not tied to IPD tables.
    Context fields let it link to OPD/IPD.
    """
    __tablename__ = "ot_orders"
    __table_args__ = (Index("ix_ot_orders_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    context_type = Column(String(10), nullable=True)  # opd | ipd
    context_id = Column(Integer,
                        nullable=True)  # visit_id | admission_id | None

    surgery_master_id = Column(Integer,
                               ForeignKey("ot_surgery_masters.id"),
                               nullable=True)
    surgery_code = Column(String(40), nullable=True)  # snapshot of master
    surgery_name = Column(String(200), nullable=False)
    estimated_cost = Column(Numeric(12, 2), nullable=False, default=0)

    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)

    status = Column(
        String(20),
        default="planned")  # planned/scheduled/in_progress/completed/cancelled
    surgeon_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    anaesthetist_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    preop_notes = Column(Text, default="")
    postop_notes = Column(Text, default="")

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    attachments = relationship("OtAttachment",
                               back_populates="order",
                               cascade="all, delete-orphan")


class OtAttachment(Base):
    __tablename__ = "ot_attachments"
    __table_args__ = (Index("ix_ot_att_order", "order_id"), )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer,
                      ForeignKey("ot_orders.id", ondelete="CASCADE"),
                      nullable=False)
    file_url = Column(String(500), nullable=False)
    note = Column(String(255), default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("OtOrder", back_populates="attachments")
