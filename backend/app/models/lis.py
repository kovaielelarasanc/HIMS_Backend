from __future__ import annotations
from datetime import datetime
from sqlalchemy import (Column, Integer, String, DateTime, Boolean, ForeignKey,
                        Numeric, Text, UniqueConstraint, Index)
from sqlalchemy.orm import relationship
from app.db.base import Base

# -------------------------
# Laboratory (LIS)
# -------------------------
# Reuses OPD master: app.models.opd.LabTest (code maps to NABL code).


class LisOrder(Base):
    """
    LIS order container (one order per draw/batch).
    Context-aware so it can be linked from OPD Visit or IPD Admission,
    but does not hard-depend on those tables.
    """
    __tablename__ = "lis_orders"
    __table_args__ = (Index("ix_lis_orders_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    context_type = Column(String(10), nullable=True)  # opd | ipd
    context_id = Column(Integer, nullable=True)
    ordering_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    priority = Column(String(16), default="routine")  # routine | stat
    status = Column(
        String(20), default="ordered"
    )  # draft/ordered/collected/in_progress/validated/reported/cancelled

    collected_at = Column(DateTime, nullable=True)
    reported_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("LisOrderItem",
                         back_populates="order",
                         cascade="all, delete-orphan")


class LisOrderItem(Base):
    """
    Individual test line item.
    Links to OPD LabTest master (NABL mapping via LabTest.code).
    """
    __tablename__ = "lis_order_items"
    __table_args__ = (
        Index("ix_lis_items_order", "order_id"),
        Index("ix_lis_items_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer,
                      ForeignKey("lis_orders.id", ondelete="CASCADE"),
                      nullable=False)
    test_id = Column(Integer,
                     ForeignKey("lab_tests.id"),
                     nullable=False,
                     index=True)

    test_name = Column(String(200), nullable=False)
    test_code = Column(String(40),
                       nullable=False)  # NABL code (from LabTest.code)
    unit = Column(String(32), nullable=True)
    normal_range = Column(String(128), nullable=True)
    specimen_type = Column(String(64), nullable=True)

    sample_barcode = Column(String(64), nullable=True)
    status = Column(
        String(20), default="ordered"
    )  # ordered/collected/in_progress/validated/reported/cancelled

    result_value = Column(String(255), nullable=True)
    is_critical = Column(Boolean, default=False)
    result_at = Column(DateTime, nullable=True)

    validated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("LisOrder", back_populates="items")
    attachments = relationship("LisAttachment",
                               back_populates="item",
                               cascade="all, delete-orphan")


class LisAttachment(Base):
    __tablename__ = "lis_attachments"
    __table_args__ = (Index("ix_lis_att_item", "order_item_id"), )

    id = Column(Integer, primary_key=True, index=True)
    order_item_id = Column(Integer,
                           ForeignKey("lis_order_items.id",
                                      ondelete="CASCADE"),
                           nullable=False)
    file_url = Column(String(500), nullable=False)
    note = Column(String(255), default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("LisOrderItem", back_populates="attachments")
