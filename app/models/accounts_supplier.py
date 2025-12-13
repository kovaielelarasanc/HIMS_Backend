# app/models/accounts_supplier.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, String, Date, DateTime, ForeignKey,
    Numeric, Boolean, UniqueConstraint, Index, Text
)
from sqlalchemy.orm import relationship
from app.db.base import Base


class SupplierInvoice(Base):
    __tablename__ = "acc_supplier_invoices"
    __table_args__ = (
        UniqueConstraint("grn_id", name="uq_acc_supplier_invoices_grn"),
        UniqueConstraint("supplier_id", "invoice_number", name="uq_acc_supplier_invoices_supplier_invoice_no"),
        Index("ix_acc_supplier_invoices_supplier_date", "supplier_id", "invoice_date"),
    )

    id = Column(Integer, primary_key=True)

    grn_id = Column(Integer, ForeignKey("inv_grns.id"), nullable=False, index=True)

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    location_id = Column(Integer, ForeignKey("inv_locations.id"), nullable=True, index=True)

    grn_number = Column(String(50), nullable=False, index=True)

    invoice_number = Column(String(100), nullable=False, default="")
    invoice_date = Column(Date, nullable=True)

    due_date = Column(Date, nullable=True)
    currency = Column(String(8), nullable=False, default="INR")

    invoice_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))

    paid_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    outstanding_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))

    status = Column(String(20), nullable=False, default="UNPAID")

    is_overdue = Column(Boolean, nullable=False, default=False)
    last_payment_date = Column(Date, nullable=True)

    notes = Column(Text, default="")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = relationship("Supplier", backref="ledger_invoices")
    location = relationship("InventoryLocation")
    grn = relationship("GRN")
    allocations = relationship(
        "SupplierPaymentAllocation",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class SupplierPayment(Base):
    __tablename__ = "acc_supplier_payments"
    __table_args__ = (
        Index("ix_acc_supplier_payments_supplier_date", "supplier_id", "payment_date"),
    )

    id = Column(Integer, primary_key=True)

    supplier_id = Column(Integer, ForeignKey("inv_suppliers.id"), nullable=False, index=True)
    payment_date = Column(Date, nullable=False, default=date.today)

    payment_method = Column(String(30), nullable=False, default="CASH")
    reference_no = Column(String(100), nullable=True)

    amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    allocated_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    advance_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))

    remarks = Column(Text, default="")

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    supplier = relationship("Supplier", backref="supplier_payments")
    created_by = relationship("User", backref="supplier_payments_created")

    allocations = relationship(
        "SupplierPaymentAllocation",
        back_populates="payment",
        cascade="all, delete-orphan",
    )


class SupplierPaymentAllocation(Base):
    __tablename__ = "acc_supplier_payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="uq_acc_supplier_alloc_payment_invoice"),
        Index("ix_acc_supplier_alloc_invoice", "invoice_id"),
    )

    id = Column(Integer, primary_key=True)

    payment_id = Column(Integer, ForeignKey("acc_supplier_payments.id"), nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("acc_supplier_invoices.id"), nullable=False, index=True)

    amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    payment = relationship("SupplierPayment", back_populates="allocations")
    invoice = relationship("SupplierInvoice", back_populates="allocations")
