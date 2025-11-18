from __future__ import annotations
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Numeric, Boolean, DateTime,
                        Index, ForeignKey, UniqueConstraint)
from sqlalchemy.orm import relationship
from app.db.base import Base


class Invoice(Base):
    __tablename__ = "billing_invoices"
    __table_args__ = (Index("ix_billing_invoices_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    context_type = Column(String(10), nullable=True)  # opd | ipd
    context_id = Column(Integer, nullable=True)

    status = Column(String(16),
                    default="draft")  # draft/finalized/cancelled/reversed
    gross_total = Column(Numeric(12, 2), default=0)
    tax_total = Column(Numeric(12, 2), default=0)
    net_total = Column(Numeric(12, 2), default=0)
    amount_paid = Column(Numeric(12, 2), default=0)
    balance_due = Column(Numeric(12, 2), default=0)

    finalized_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    items = relationship("InvoiceItem",
                         back_populates="invoice",
                         cascade="all, delete-orphan")
    payments = relationship("Payment",
                            back_populates="invoice",
                            cascade="all, delete-orphan")


class InvoiceItem(Base):
    __tablename__ = "billing_invoice_items"
    __table_args__ = (
        # global dedupe (intentional): prevent same external line twice if not voided
        UniqueConstraint("service_type",
                         "service_ref_id",
                         "is_voided",
                         name="uq_billing_service_unique"),
        Index("ix_billing_items_invoice", "invoice_id"),
        Index("ix_billing_items_service", "service_type", "service_ref_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer,
                        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
                        nullable=False)
    service_type = Column(
        String(32),
        nullable=False)  # lab | radiology | ot | pharmacy | opd_consult | ipd
    service_ref_id = Column(Integer, nullable=False)
    description = Column(String(300), nullable=False)
    quantity = Column(Integer, default=1)
    unit_price = Column(Numeric(12, 2), default=0)
    tax_rate = Column(Numeric(5, 2), default=0)  # %
    tax_amount = Column(Numeric(12, 2), default=0)
    line_total = Column(Numeric(12, 2), default=0)
    is_voided = Column(Boolean, default=False)

    # NEW: keep audit for void action (used by auto_void_items_for_event)
    void_reason = Column(String(255), nullable=True)
    voided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    voided_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="items")


class Payment(Base):
    __tablename__ = "billing_payments"
    __table_args__ = (Index("ix_billing_payments_invoice", "invoice_id"), )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer,
                        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
                        nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    mode = Column(String(16), nullable=False)  # cash/card/upi/credit
    reference_no = Column(String(100), nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="payments")
