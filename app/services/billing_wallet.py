from __future__ import annotations

from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.billing_wallet import PatientWalletTxn, PatientWalletAllocation


def get_wallet_totals(db: Session, patient_id: int) -> dict:
    dep = db.query(func.coalesce(
        func.sum(PatientWalletTxn.amount),
        0)).filter(PatientWalletTxn.patient_id == patient_id).scalar()

    allocated = db.query(
        func.coalesce(func.sum(PatientWalletAllocation.amount), 0)).filter(
            PatientWalletAllocation.patient_id == patient_id).scalar()

    refund = db.query(
        func.coalesce(func.sum(PatientWalletTxn.amount),
                      0)).filter(PatientWalletTxn.patient_id == patient_id,
                                 PatientWalletTxn.amount < 0).scalar()

    dep = Decimal(dep or 0)
    allocated = Decimal(allocated or 0)
    refund = Decimal(refund or 0)

    available = dep - allocated  # refund already included as negative in dep

    return {
        "total_deposit": dep,
        "total_allocated": allocated,
        "total_refund": abs(refund),
        "available_balance": available,
    }
