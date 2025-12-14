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
    BigInteger,
)
from sqlalchemy.orm import relationship
from decimal import Decimal, ROUND_HALF_UP
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
    invoice_uid = Column(String(36), unique=True, index=True, nullable=True)
    invoice_number = Column(String(32), unique=True, index=True, nullable=True)

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
    header_discount_percent = Column(Numeric(10, 2), default=0)
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
    patient = relationship("Patient")

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
    pharmacy_sales = relationship(
        "PharmacySale",
        back_populates="billing_invoice",
        lazy="selectin",
    )
    # FILE: app/models/billing.py
    # put this INSIDE class Invoice (indentation must match other methods in class)

    # ---------- Billing math helpers ----------
    @staticmethod
    def _d(v) -> Decimal:
        if v is None:
            return Decimal("0")
        return Decimal(str(v))

    @staticmethod
    def _q2(v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def recalc(self) -> None:
        """
        Recalculate invoice totals from items.

        Counts only items where is_voided=False.

        Item math:
          base = qty * unit_price
          line_discount = discount_amount (if >0) else base * discount_percent/100
          taxable = max(base - line_discount, 0)
          tax = taxable * tax_rate/100
          line_total = taxable + tax

        Header discount:
          - if header_discount_amount > 0 -> use it
          - else if header_discount_percent > 0 -> apply on (gross - line_discounts)
          - capped so net_total never goes negative

        Updates:
          gross_total, discount_total, tax_total, net_total, balance_due
        """
        _d = self._d
        _q2 = self._q2

        gross = Decimal("0")
        line_disc_total = Decimal("0")
        tax_total = Decimal("0")
        net_before_header = Decimal("0")

        items = [
            it for it in (self.items or [])
            if not getattr(it, "is_voided", False)
        ]

        for it in items:
            qty = _d(it.quantity)
            price = _d(it.unit_price)
            base = _q2(qty * price)

            disc_amt = _d(it.discount_amount)
            disc_pct = _d(it.discount_percent)

            # if amount not set but % set, compute
            if disc_amt <= 0 and disc_pct > 0:
                disc_amt = _q2(base * disc_pct / Decimal("100"))

            # cap discount to base
            if disc_amt > base:
                disc_amt = base

            taxable = base - disc_amt
            if taxable < 0:
                taxable = Decimal("0")

            tr = _d(it.tax_rate)
            tax_amt = _q2(taxable * tr / Decimal("100"))
            line_total = _q2(taxable + tax_amt)

            # write back computed values
            it.discount_amount = disc_amt
            it.tax_amount = tax_amt
            it.line_total = line_total

            gross += base
            line_disc_total += disc_amt
            tax_total += tax_amt
            net_before_header += line_total

        gross = _q2(gross)
        line_disc_total = _q2(line_disc_total)
        tax_total = _q2(tax_total)
        net_before_header = _q2(net_before_header)

        # Header discount
        hdr_pct = _d(self.header_discount_percent)
        hdr_amt = _d(self.header_discount_amount)

        gross_after_line_discounts = gross - line_disc_total
        if gross_after_line_discounts < 0:
            gross_after_line_discounts = Decimal("0")

        computed_hdr = Decimal("0")
        if hdr_amt > 0:
            computed_hdr = hdr_amt
        elif hdr_pct > 0:
            computed_hdr = _q2(gross_after_line_discounts * hdr_pct /
                               Decimal("100"))

        # cap header discount so net doesn't go negative
        if computed_hdr > net_before_header:
            computed_hdr = net_before_header

        self.header_discount_amount = _q2(computed_hdr)

        discount_total = _q2(line_disc_total + computed_hdr)
        net_total = _q2(net_before_header - computed_hdr)

        self.gross_total = gross
        self.discount_total = discount_total
        self.tax_total = tax_total
        self.net_total = net_total

        # Balance due = net_total - (amount_paid + advance_adjusted)
        paid = _d(self.amount_paid)
        adv = _d(self.advance_adjusted)
        due = net_total - paid - adv
        if due < 0:
            due = Decimal("0")
        self.balance_due = _q2(due)


class InvoiceItem(Base):
    __tablename__ = "billing_invoice_items"
    __table_args__ = (
        # ✅ Dedupe per-invoice (safe)
        UniqueConstraint(
            "invoice_id",
            "service_type",
            "service_ref_id",
            "is_voided",
            name="uq_billing_item_dedupe_per_invoice",
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
    service_ref_id = Column(BigInteger, nullable=True)

    description = Column(String(300), nullable=False)

    quantity = Column(Numeric(10, 2), default=1)
    unit_price = Column(Numeric(12, 2), default=0)

    # GST / tax in %
    tax_rate = Column(Numeric(10, 2), default=0)

    # Discount % and amount at line level
    discount_percent = Column(Numeric(10, 2), default=0)
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
    is_voided = Column(Boolean, default=False, nullable=False)
    void_reason = Column(String(255), nullable=True)
    voided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    voided_at = Column(DateTime, nullable=True)

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
