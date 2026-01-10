from __future__ import annotations

import enum
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime, Date, Numeric,
    ForeignKey, Enum, Boolean, Index, UniqueConstraint
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


class IndentStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
    ISSUED = "ISSUED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class IssueStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"


class IndentPriority(str, enum.Enum):
    ROUTINE = "ROUTINE"
    STAT = "STAT"


class InvIndent(Base):
    """
    Ward/OT raises indent -> Store/Pharmacy approves -> Issue document is generated.
    NABH-friendly: full audit (who/when/why), partial issue supported.
    """
    __tablename__ = "inv_indents"
    __table_args__ = (
        UniqueConstraint("indent_number", name="uq_inv_indents_indent_number"),
        Index("ix_inv_indents_status_date", "status", "indent_date"),
        Index("ix_inv_indents_from_to", "from_location_id", "to_location_id"),
        Index("ix_inv_indents_patient", "patient_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    indent_number = Column(String(64), nullable=False, index=True)

    indent_date = Column(Date, nullable=False, default=date.today)
    priority = Column(Enum(IndentPriority, name="inv_indent_priority"), nullable=False, default=IndentPriority.ROUTINE)

    # stock moves FROM -> TO (issue will deduct from "from" and deliver to "to")
    from_location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    to_location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    # Optional clinical linkage (for OT/ward indent tied to patient/encounter)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    visit_id = Column(Integer, ForeignKey("opd_visits.id"), nullable=True, index=True)
    ipd_admission_id = Column(Integer, ForeignKey("ipd_admissions.id"), nullable=True, index=True)

    # Optional generic encounter link (future)
    encounter_type = Column(String(16), nullable=True)   # OP/IP/OT/ER
    encounter_id = Column(BigInteger, nullable=True)

    status = Column(Enum(IndentStatus, name="inv_indent_status"), nullable=False, default=IndentStatus.DRAFT)

    notes = Column(Text, nullable=False, default="")
    cancel_reason = Column(String(255), nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    submitted_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    submitted_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now(), index=True)

    from_location = relationship("InventoryLocation", foreign_keys=[from_location_id])
    to_location = relationship("InventoryLocation", foreign_keys=[to_location_id])

    patient = relationship("Patient")
    visit = relationship("Visit")
    ipd_admission = relationship("IpdAdmission")

    created_by = relationship("User", foreign_keys=[created_by_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    items = relationship("InvIndentItem", back_populates="indent", cascade="all, delete-orphan")

    issues = relationship("InvIssue", back_populates="indent")


class InvIndentItem(Base):
    __tablename__ = "inv_indent_items"
    __table_args__ = (
        Index("ix_inv_indent_items_indent", "indent_id"),
        Index("ix_inv_indent_items_item", "item_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    indent_id = Column(Integer, ForeignKey("inv_indents.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)

    requested_qty = Column(Qty, nullable=False, default=Decimal("0"))
    approved_qty = Column(Qty, nullable=False, default=Decimal("0"))
    issued_qty = Column(Qty, nullable=False, default=Decimal("0"))

    is_stat = Column(Boolean, nullable=False, default=False)
    remarks = Column(String(255), nullable=False, default="")

    indent = relationship("InvIndent", back_populates="items")
    item = relationship("InventoryItem")


class InvIssue(Base):
    """
    Actual issue document for an indent. Posting creates StockTransaction rows.
    """
    __tablename__ = "inv_issues"
    __table_args__ = (
        UniqueConstraint("issue_number", name="uq_inv_issues_issue_number"),
        Index("ix_inv_issues_status_date", "status", "issue_date"),
        Index("ix_inv_issues_from_to", "from_location_id", "to_location_id"),
        Index("ix_inv_issues_indent", "indent_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    issue_number = Column(String(64), nullable=False, index=True)

    issue_date = Column(Date, nullable=False, default=date.today)

    indent_id = Column(Integer, ForeignKey("inv_indents.id"), nullable=True, index=True)

    from_location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)
    to_location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=False, index=True)

    status = Column(Enum(IssueStatus, name="inv_issue_status"), nullable=False, default=IssueStatus.DRAFT)

    notes = Column(Text, nullable=False, default="")
    cancel_reason = Column(String(255), nullable=False, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    posted_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    posted_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now(), index=True)

    indent = relationship("InvIndent", back_populates="issues")

    from_location = relationship("InventoryLocation", foreign_keys=[from_location_id])
    to_location = relationship("InventoryLocation", foreign_keys=[to_location_id])

    created_by = relationship("User", foreign_keys=[created_by_id])
    posted_by = relationship("User", foreign_keys=[posted_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    items = relationship("InvIssueItem", back_populates="issue", cascade="all, delete-orphan")


class InvIssueItem(Base):
    __tablename__ = "inv_issue_items"
    __table_args__ = (
        Index("ix_inv_issue_items_issue", "issue_id"),
        Index("ix_inv_issue_items_item", "item_id"),
        Index("ix_inv_issue_items_batch", "batch_id"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("inv_issues.id", ondelete="CASCADE"), nullable=False, index=True)

    indent_item_id = Column(Integer, ForeignKey("inv_indent_items.id"), nullable=True, index=True)

    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("inv_item_batches.id"), nullable=True, index=True)

    issued_qty = Column(Qty, nullable=False, default=Decimal("0"))

    # Optional: map to stock transaction created when posting
    stock_txn_id = Column(Integer, ForeignKey("inv_stock_txns.id"), nullable=True, index=True)

    remarks = Column(String(255), nullable=False, default="")

    issue = relationship("InvIssue", back_populates="items")
    indent_item = relationship("InvIndentItem")
    item = relationship("InventoryItem")
    batch = relationship("ItemBatch")
    stock_txn = relationship("StockTransaction")



