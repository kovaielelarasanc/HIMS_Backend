# FILE: app/models/billing.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Numeric,
    Boolean,
    DateTime,
    Index,
    ForeignKey,
    UniqueConstraint,
    Text,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class BillingProvider(Base):
    """
    Credit provider / TPA / Corporate billing entity
    (used when billing to an organization instead of direct cash patient).
    """
    __tablename__ = "billing_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(199), nullable=False, unique=True)
    code = Column(String(50), nullable=True, unique=True)
    provider_type = Column(
        String(50), nullable=True)  # insurance | tpa | corporate | other
    contact_person = Column(String(100), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(255), nullable=True)
    address = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)

    invoices = relationship("Invoice", back_populates="provider")


class Invoice(Base):
    """
    Core invoice table used for:
    - OP Billing
    - IP Billing
    - Pharmacy
    - Laboratory
    - Radiology
    - Any other billing types

    Use:
    - context_type + context_id: to link to OP visit / IP admission / order
    - billing_type: for UI filter (op_billing, ip_billing, pharmacy, lab, radiology, general)
    """

    __tablename__ = "billing_invoices"
    __table_args__ = (Index("ix_billing_invoices_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)

    # Optional invoice number visible in print (INV-000001 etc.)
    invoice_number = Column(String(30), unique=True, index=True, nullable=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )

    # Module context
    # opd | ipd | pharmacy | lab | radiology | other
    context_type = Column(String(20), nullable=True)
    # Visit / admission / order id
    context_id = Column(Integer, nullable=True)

    # Logical billing category for UI
    # op_billing | ip_billing | pharmacy | lab | radiology | general
    billing_type = Column(String(20), nullable=True)

    # Credit provider (TPA, corporate, insurance)
    provider_id = Column(Integer,
                         ForeignKey("billing_providers.id"),
                         nullable=True)

    # Treating consultant (normally a doctor user)
    consultant_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Optional visit number visible to user
    visit_no = Column(String(50), nullable=True)

    # Free text
    remarks = Column(Text, nullable=True)

    status = Column(
        String(16),
        default="draft")  # draft | finalized | cancelled | reversed

    # Totals
    # gross_total = sum(qty * unit_price) before discount & tax
    gross_total = Column(Numeric(12, 2), default=0)

    # All discounts: line level + header level
    discount_total = Column(Numeric(12, 2), default=0)

    # Total tax over all items
    tax_total = Column(Numeric(12, 2), default=0)

    # Final amount for this invoice after discount & taxes,
    # but before patient advances are adjusted
    # (note: advances adjusted stored separately)
    net_total = Column(Numeric(12, 2), default=0)

    # Money actually paid against this invoice (all payment rows)
    amount_paid = Column(Numeric(12, 2), default=0)

    # Remaining due AFTER applying advances & payments
    balance_due = Column(Numeric(12, 2), default=0)

    # Header-level discount applied on total
    header_discount_percent = Column(Numeric(5, 2), default=0)
    header_discount_amount = Column(Numeric(12, 2), default=0)

    discount_remarks = Column(String(255), nullable=True)
    discount_authorized_by = Column(Integer,
                                    ForeignKey("users.id"),
                                    nullable=True)

    # Snapshot of patient outstanding before this invoice was finalized
    # (for "Previous Balance" on print)
    previous_balance_snapshot = Column(Numeric(12, 2), default=0)

    # How much patient advance has been used to reduce this invoice
    advance_adjusted = Column(Numeric(12, 2), default=0)

    finalized_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    items = relationship(
        "InvoiceItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceItem.seq",
    )
    payments = relationship(
        "Payment",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )
    provider = relationship("BillingProvider", back_populates="invoices")
    advance_adjustments = relationship(
        "AdvanceAdjustment",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class InvoiceItem(Base):
    """
    Per line item in invoice.
    Supports:
    - Service-type items (OP consult, IP, Lab, Radiology, Pharmacy, OT, etc.)
    - Manual lines (e.g., "Dressing charges")
    """

    __tablename__ = "billing_invoice_items"
    __table_args__ = (
        # Global dedupe (optional): prevent same external line twice if not voided
        UniqueConstraint(
            "service_type",
            "service_ref_id",
            "is_voided",
            name="uq_billing_service_unique",
        ),
        Index("ix_billing_items_invoice", "invoice_id"),
        Index("ix_billing_items_service", "service_type", "service_ref_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(
        Integer,
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )

    # S.no order for UI/print
    seq = Column(Integer, default=1)

    # lab | radiology | ot | pharmacy | opd_consult | ipd | manual | other
    service_type = Column(String(32), nullable=False)
    service_ref_id = Column(Integer, nullable=False, default=0)
    description = Column(String(300), nullable=False)

    quantity = Column(Integer, default=1)
    unit_price = Column(Numeric(12, 2), default=0)

    # GST / tax in %
    tax_rate = Column(Numeric(5, 2), default=0)

    # Discount % and amount at line level
    discount_percent = Column(Numeric(5, 2), default=0)
    discount_amount = Column(Numeric(12, 2), default=0)

    # Calculated tax amount in currency
    tax_amount = Column(Numeric(12, 2), default=0)

    # Final total for this line =
    # (qty * unit_price - discount_amount) + tax_amount
    line_total = Column(Numeric(12, 2), default=0)

    is_voided = Column(Boolean, default=False)

    # Audit for void action
    void_reason = Column(String(255), nullable=True)
    voided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    voided_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    invoice = relationship("Invoice", back_populates="items")


class Payment(Base):
    """
    Payments tagged to invoice.
    Supports multiple modes (split payments):
    - cash
    - card
    - upi
    - credit (credit provider)
    - cheque
    - neft/rtgs
    - wallet/other
    """

    __tablename__ = "billing_payments"
    __table_args__ = (Index("ix_billing_payments_invoice", "invoice_id"), )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(
        Integer,
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )

    amount = Column(Numeric(12, 2), nullable=False)
    mode = Column(String(32), nullable=False)
    reference_no = Column(String(100), nullable=True)
    notes = Column(String(255), nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="payments")


class Advance(Base):
    """
    Patient advance payments (especially IP advance).
    These are NOT tied to a specific invoice directly.
    Instead, they are adjusted later using AdvanceAdjustment rows.
    """

    __tablename__ = "billing_advances"
    __table_args__ = (Index("ix_billing_advances_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(
        Integer,
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )

    # Usually 'ipd' or 'opd', and admission / visit id
    context_type = Column(String(20), nullable=True)
    context_id = Column(Integer, nullable=True)

    amount = Column(Numeric(12, 2), nullable=False)
    balance_remaining = Column(Numeric(12, 2), nullable=False)

    mode = Column(String(32), nullable=False)  # cash/card/upi/other
    reference_no = Column(String(100), nullable=True)
    remarks = Column(String(255), nullable=True)

    received_at = Column(DateTime, default=datetime.utcnow)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    adjustments = relationship(
        "AdvanceAdjustment",
        back_populates="advance",
        cascade="all, delete-orphan",
    )


class AdvanceAdjustment(Base):
    """
    Many-to-many between invoices and advance payments.
    Represents how much from a particular advance got applied to a particular invoice.
    """

    __tablename__ = "billing_advance_adjustments"
    __table_args__ = (
        Index("ix_billing_adv_adj_invoice", "invoice_id"),
        Index("ix_billing_adv_adj_advance", "advance_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    advance_id = Column(
        Integer,
        ForeignKey("billing_advances.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_id = Column(
        Integer,
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        nullable=False,
    )

    amount_applied = Column(Numeric(12, 2), nullable=False)
    applied_at = Column(DateTime, default=datetime.utcnow)

    advance = relationship("Advance", back_populates="adjustments")
    invoice = relationship("Invoice", back_populates="advance_adjustments")
