from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    DateTime,
    String,
    Text,
    Numeric,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class PatientWalletTxn(Base):
    """
    Patient Wallet Ledger (Advance/Deposit)
    - +amount => deposit received
    - -amount => refund (optional)
    """
    __tablename__ = "patient_wallet_txn"

    id = Column(Integer, primary_key=True, index=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    txn_type = Column(String(20),
                      nullable=False)  # deposit | refund | adjustment
    amount = Column(Numeric(12, 2), nullable=False)

    mode = Column(String(30), nullable=True)  # cash/upi/card/neft/...
    reference_no = Column(String(80), nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # relationships
    patient = relationship("Patient", lazy="joined")


Index("ix_wallet_txn_patient_created", PatientWalletTxn.patient_id,
      PatientWalletTxn.created_at)


class PatientWalletAllocation(Base):
    """
    Wallet usage applied to invoices.
    amount is always positive.
    """
    __tablename__ = "patient_wallet_allocation"

    id = Column(Integer, primary_key=True, index=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    invoice_id = Column(
        Integer,
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    amount = Column(Numeric(12, 2), nullable=False)
    notes = Column(Text, nullable=True)

    allocated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    allocated_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # relationships
    patient = relationship("Patient", lazy="joined")
    invoice = relationship(
        "Invoice", lazy="joined")  # <-- uses Invoice model in billing.py


Index("ix_wallet_alloc_patient_invoice", PatientWalletAllocation.patient_id,
      PatientWalletAllocation.invoice_id)
