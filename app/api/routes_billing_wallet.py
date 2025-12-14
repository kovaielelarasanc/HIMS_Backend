from __future__ import annotations

from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.api.deps import get_db, current_user
from app.models.user import User as UserModel

from app.models.billing_wallet import PatientWalletTxn, PatientWalletAllocation
from app.schemas.billing_wallet import (
    WalletDepositIn,
    WalletTxnOut,
    WalletSummaryOut,
)
from app.services.billing_wallet import get_wallet_totals

router = APIRouter(prefix="/billing/wallet", tags=["Billing Wallet"])


@router.post("/deposit", response_model=WalletTxnOut)
def add_deposit(
        body: WalletDepositIn,
        db: Session = Depends(get_db),
        user: UserModel = Depends(current_user),
):
    amt = Decimal(body.amount)
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    txn = PatientWalletTxn(
        patient_id=body.patient_id,
        txn_type="deposit",
        amount=amt,
        mode=body.mode or "cash",
        reference_no=body.reference_no,
        notes=body.notes,
        created_by=getattr(user, "id", None),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


@router.get("/summary", response_model=WalletSummaryOut)
def wallet_summary(
        patient_id: int = Query(..., gt=0),
        db: Session = Depends(get_db),
        user: UserModel = Depends(current_user),
):
    totals = get_wallet_totals(db, patient_id)

    txns = (db.query(PatientWalletTxn).filter(
        PatientWalletTxn.patient_id == patient_id).order_by(
            desc(PatientWalletTxn.created_at)).limit(20).all())

    allocs = (db.query(PatientWalletAllocation).filter(
        PatientWalletAllocation.patient_id == patient_id).order_by(
            desc(PatientWalletAllocation.allocated_at)).limit(20).all())

    return WalletSummaryOut(
        patient_id=patient_id,
        total_deposit=totals["total_deposit"],
        total_allocated=totals["total_allocated"],
        total_refund=totals["total_refund"],
        available_balance=totals["available_balance"],
        recent_txns=txns,
        recent_allocations=allocs,
    )
