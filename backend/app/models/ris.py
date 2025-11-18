from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from app.db.base import Base

# -------------------------
# Radiology (RIS)
# -------------------------


class RisOrder(Base):
    """
    Radiology order & reporting. Dual sign-off optional.
    Context-aware (opd/ipd).
    """
    __tablename__ = "ris_orders"
    __table_args__ = (Index("ix_ris_orders_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    context_type = Column(String(10), nullable=True)  # opd | ipd
    context_id = Column(Integer, nullable=True)
    ordering_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    test_id = Column(Integer,
                     ForeignKey("radiology_tests.id"),
                     nullable=False,
                     index=True)
    test_name = Column(String(200), nullable=False)
    test_code = Column(String(40), nullable=False)
    modality = Column(String(32), nullable=True)  # XR/CT/MRI/USG (optional)

    status = Column(String(20), default="ordered"
                    )  # ordered/scheduled/scanned/reported/approved/cancelled
    scheduled_at = Column(DateTime, nullable=True)
    scanned_at = Column(DateTime, nullable=True)
    reported_at = Column(DateTime,
                         nullable=True)  # added to support history/timestamps

    report_text = Column(Text, nullable=True)
    primary_signoff_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    secondary_signoff_by = Column(Integer,
                                  ForeignKey("users.id"),
                                  nullable=True)
    approved_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    attachments = relationship("RisAttachment",
                               back_populates="order",
                               cascade="all, delete-orphan")


class RisAttachment(Base):
    __tablename__ = "ris_attachments"
    __table_args__ = (Index("ix_ris_att_order", "order_id"), )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer,
                      ForeignKey("ris_orders.id", ondelete="CASCADE"),
                      nullable=False)
    file_url = Column(String(500), nullable=False)
    note = Column(String(255), default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("RisOrder", back_populates="attachments")
