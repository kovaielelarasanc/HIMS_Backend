from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Numeric,
    ForeignKey,
    Boolean,
    Index,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

Qty = Numeric(14, 4)

MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class InvPatientConsumption(Base):
    """
    Header doc for "Ward Patient Usage" / "Patient Consumption".
    Stores encounter here (NOT in StockTransaction) so Billing can sync cleanly.
    """
    __tablename__ = "inv_patient_consumptions"
    __table_args__ = (
        UniqueConstraint("consumption_number", name="uq_inv_patient_consumptions_no"),
        Index("ix_inv_pc_patient_time", "patient_id", "posted_at"),
        Index("ix_inv_pc_loc_time", "location_id", "posted_at"),
        Index("ix_inv_pc_enc", "encounter_type", "encounter_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    consumption_number = Column(String(64), nullable=False, index=True)
    posted_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)

    # ✅ store encounter here (not in stock_txn)
    encounter_type = Column(String(16), nullable=True, index=True)  # OP/IP/OT/ER
    encounter_id = Column(BigInteger, nullable=True, index=True)

    # backward/optional
    visit_id = Column(Integer, nullable=True, index=True)
    doctor_id = Column(Integer, nullable=True, index=True)

    notes = Column(Text, nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # billing sync info (optional but very useful)
    billing_case_id = Column(BigInteger, ForeignKey("billing_cases.id"), nullable=True, index=True)
    billing_invoice_ids_json = Column(JSON, nullable=True)

    is_cancelled = Column(Boolean, nullable=False, default=False)
    cancel_reason = Column(String(255), nullable=False, default="")
    cancelled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now(), index=True)

    # relationships
    location = relationship("InventoryLocation", foreign_keys=[location_id])
    patient = relationship("Patient", foreign_keys=[patient_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    lines = relationship("InvPatientConsumptionLine", back_populates="consumption", cascade="all, delete-orphan")


class InvPatientConsumptionLine(Base):
    __tablename__ = "inv_patient_consumption_lines"
    __table_args__ = (
        Index("ix_inv_pcl_cons", "consumption_id"),
        Index("ix_inv_pcl_item", "item_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    consumption_id = Column(Integer, ForeignKey("inv_patient_consumptions.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    requested_qty = Column(Qty, nullable=False, default=Decimal("0"))
    remark = Column(String(255), nullable=False, default="")

    consumption = relationship("InvPatientConsumption", back_populates="lines")
    item = relationship("InventoryItem", foreign_keys=[item_id])

    allocations = relationship("InvPatientConsumptionAllocation", back_populates="line", cascade="all, delete-orphan")


class InvPatientConsumptionAllocation(Base):
    __tablename__ = "inv_patient_consumption_allocations"
    __table_args__ = (
        Index("ix_inv_pca_line", "line_id"),
        Index("ix_inv_pca_batch", "batch_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    line_id = Column(Integer, ForeignKey("inv_patient_consumption_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)
    qty = Column(Qty, nullable=False, default=Decimal("0"))

    line = relationship("InvPatientConsumptionLine", back_populates="allocations")
    batch = relationship("ItemBatch", foreign_keys=[batch_id])
