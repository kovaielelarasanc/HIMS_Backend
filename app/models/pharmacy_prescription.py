# FILE: app/models/pharmacy_prescription.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from sqlalchemy import Column, BigInteger, ForeignKey
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Date,
    Boolean,
    Numeric,
    ForeignKey,
    func,
)
from sqlalchemy.orm import relationship, backref

from app.db.base import Base


class PharmacyPrescription(Base):
    """
    OPD / IPD / COUNTER / GENERAL prescription header.

    type:
      - OPD      -> linked to visit_id
      - IPD      -> linked to ipd_admission_id
      - COUNTER  -> counter / OTC sale (patient_id optional)
      - GENERAL  -> template, etc.
    """

    __tablename__ = "pharmacy_prescriptions"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    prescription_number = Column(String(64),
                                 unique=True,
                                 index=True,
                                 nullable=False)

    type = Column(String(16), nullable=False)  # OPD / IPD / COUNTER / GENERAL

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    # IMPORTANT: class name is Visit (from app/models/opd.py)
    visit_id = Column(Integer, ForeignKey("opd_visits.id"), nullable=True)
    ipd_admission_id = Column(Integer,
                              ForeignKey("ipd_admissions.id"),
                              nullable=True)

    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=True)
    doctor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    status = Column(String(32), nullable=False, default="DRAFT")
    notes = Column(Text, nullable=True)

    signed_at = Column(DateTime, nullable=True)
    signed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    cancel_reason = Column(Text, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # --- Relationships ---
    lines = relationship(
        "PharmacyPrescriptionLine",
        back_populates="prescription",
        cascade="all, delete-orphan",
    )

    patient = relationship("Patient")
    # 🔥 FIX: use Visit, not OpdVisit
    visit = relationship("Visit")
    ipd_admission = relationship("IpdAdmission")

    doctor = relationship("User", foreign_keys=[doctor_user_id])
    signed_by = relationship("User", foreign_keys=[signed_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])
    created_by = relationship("User", foreign_keys=[created_by_id])


class PharmacyPrescriptionLine(Base):
    """
    Individual medicine / consumable lines under PharmacyPrescription.
    """

    __tablename__ = "pharmacy_prescription_lines"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    prescription_id = Column(
        Integer,
        ForeignKey("pharmacy_prescriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False)

    # Quantities
    requested_qty = Column(Numeric(14, 3), nullable=False, default=0)
    dispensed_qty = Column(Numeric(14, 3), nullable=False, default=0)
    status = Column(
        String(32), nullable=False,
        default="WAITING")  # WAITING / PARTIAL / DISPENSED / CANCELLED

    # Dosing
    dose_text = Column(String(64), nullable=True)  # "1 tab", "5 ml"
    frequency_code = Column(String(32), nullable=True)  # BD, TDS, 1-0-1
    times_per_day = Column(Integer, nullable=True)
    duration_days = Column(Integer, nullable=True)
    route = Column(String(32), nullable=True)  # oral, IV, IM, topical, etc.
    timing = Column(String(32), nullable=True)  # AF, BF, HS
    instructions = Column(Text, nullable=True)

    # IPD extras (for daily routine)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    schedule_pattern = Column(String(64), nullable=True)  # "M,N,HS" or times
    is_prn = Column(Boolean, nullable=False, default=False)
    is_stat = Column(Boolean, nullable=False, default=False)
    order_status = Column(
        String(32),
        nullable=True)  # ACTIVE / DISCONTINUED / COMPLETED (optional)

    # Stock snapshot at prescription time
    available_qty_snapshot = Column(Numeric(14, 3), nullable=True)
    is_out_of_stock = Column(Boolean, nullable=False, default=False)

    # Display snapshots (optional but useful so UI does not break if item changes)
    item_name = Column(String(255), nullable=True)
    item_form = Column(String(64), nullable=True)
    item_strength = Column(String(64), nullable=True)
    item_type = Column(String(32), nullable=True)  # drug / consumable
    # ✅ NEW: batch lock + snapshots for UI
    batch_id = Column(Integer,
                      ForeignKey("inv_item_batches.id"),
                      nullable=True, index=True)
    batch_no_snapshot = Column(String(100), nullable=True)
    expiry_date_snapshot = Column(Date, nullable=True)

    created_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    batch = relationship("ItemBatch", foreign_keys=[batch_id])

    prescription = relationship("PharmacyPrescription", back_populates="lines")
    item = relationship("InventoryItem")


class PharmacySale(Base):
    """
    Pharmacy Invoice / internal charge.

    context_type:
      - OPD     : normal OPD pharmacy invoice
      - IPD     : IPD pharmacy charges (can be pulled into final bill)
      - COUNTER : walk-in / OPD counter invoice
    """

    __tablename__ = "pharmacy_sales"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    bill_number = Column(String(64), unique=True, index=True, nullable=False)

    prescription_id = Column(Integer,
                             ForeignKey("pharmacy_prescriptions.id"),
                             nullable=True)

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    visit_id = Column(Integer, ForeignKey("opd_visits.id"), nullable=True)
    ipd_admission_id = Column(Integer,
                              ForeignKey("ipd_admissions.id"),
                              nullable=True)

    location_id = Column(Integer,
                         ForeignKey("inv_locations.id"),
                         nullable=True)

    context_type = Column(String(16), nullable=False)  # OPD / IPD / COUNTER
    bill_datetime = Column(DateTime, nullable=False, server_default=func.now())

    # Amount fields
    gross_amount = Column(Numeric(14, 2), nullable=False, default=0)
    total_tax = Column(Numeric(14, 2), nullable=False, default=0)
    discount_amount_total = Column(Numeric(14, 2), nullable=False, default=0)
    net_amount = Column(Numeric(14, 2), nullable=False, default=0)
    rounding_adjustment = Column(Numeric(14, 2), nullable=False, default=0)

    invoice_status = Column(String(16), nullable=False,
                            default="DRAFT")  # DRAFT / FINALIZED / CANCELLED
    payment_status = Column(String(16), nullable=False,
                            default="UNPAID")  # UNPAID / PARTIALLY_PAID / PAID

    cancel_reason = Column(Text, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    prescription = relationship("PharmacyPrescription")
    patient = relationship("Patient")
    # 🔥 FIX: use Visit, not OpdVisit
    visit = relationship("Visit")
    ipd_admission = relationship("IpdAdmission")
    location = relationship("InventoryLocation")
    billing_invoice_id = Column(
        BigInteger,  # ✅ match BillingInvoice.id (BigInteger)
        ForeignKey("billing_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    billing_invoice = relationship(
        "BillingInvoice",
        back_populates="pharmacy_sales",
        lazy="joined",
    )
    items = relationship("PharmacySaleItem",
                         back_populates="sale",
                         cascade="all, delete-orphan")
    payments = relationship("PharmacyPayment",
                            back_populates="sale",
                            cascade="all, delete-orphan")

    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])
    created_by = relationship("User", foreign_keys=[created_by_id])


class PharmacySaleItem(Base):
    """
    Line items for PharmacySale.
    """

    __tablename__ = "pharmacy_sale_items"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer,
                     ForeignKey("pharmacy_sales.id", ondelete="CASCADE"),
                     nullable=False)
    rx_line_id = Column(
        Integer,
        ForeignKey("pharmacy_prescription_lines.id", ondelete="SET NULL"),
        nullable=True,
    )

    item_id = Column(Integer, ForeignKey("inv_items.id"), nullable=False)
    batch_id = Column(Integer,
                      ForeignKey("inv_item_batches.id"),
                      nullable=True)

    item_name = Column(String(255), nullable=False)
    batch_no = Column(String(64), nullable=True)
    expiry_date = Column(Date, nullable=True)

    quantity = Column(Numeric(14, 3), nullable=False, default=0)
    unit_price = Column(Numeric(14, 2), nullable=False, default=0)
    tax_percent = Column(Numeric(5, 2), nullable=False, default=0)
    line_amount = Column(Numeric(14, 2), nullable=False,
                         default=0)  # before tax
    tax_amount = Column(Numeric(14, 2), nullable=False, default=0)
    discount_amount = Column(Numeric(14, 2), nullable=False, default=0)
    total_amount = Column(Numeric(14, 2), nullable=False,
                          default=0)  # after tax

    # Optional link to stock_txn if you want 1:1 mapping
    stock_txn_id = Column(Integer,
                          ForeignKey("inv_stock_txns.id"),
                          nullable=True)

    sale = relationship("PharmacySale", back_populates="items")
    rx_line = relationship("PharmacyPrescriptionLine")
    item = relationship("InventoryItem")
    batch = relationship("ItemBatch")
    stock_txn = relationship("StockTransaction")


class PharmacyPayment(Base):
    """
    Payments against PharmacySale.
    """

    __tablename__ = "pharmacy_payments"

    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    sale_id = Column(Integer,
                     ForeignKey("pharmacy_sales.id", ondelete="CASCADE"),
                     nullable=False)

    amount = Column(Numeric(14, 2), nullable=False)
    mode = Column(String(16),
                  nullable=False)  # CASH / CARD / UPI / NEFT / OTHER
    reference = Column(String(128), nullable=True)
    paid_on = Column(DateTime, nullable=False, server_default=func.now())
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        index=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    sale = relationship("PharmacySale", back_populates="payments")
    created_by = relationship("User", foreign_keys=[created_by_id])
