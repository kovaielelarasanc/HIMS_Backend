# backend/app/api/routes_abha.py
import random
from datetime import datetime, timedelta
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient

router = APIRouter()

# simple in-memory OTP txn store (dev only)
TXN: Dict[str, dict] = {}

@router.post("/generate")
def abha_generate(
    name: str,
    dob: str,
    mobile: str,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    # In real life, call ABDM gateway & send OTP to mobile
    txn = f"ABHA{random.randint(100000, 999999)}"
    otp = f"{random.randint(0, 999999):06d}"
    TXN[txn] = {"otp": otp, "expires": datetime.utcnow() + timedelta(minutes=10)}
    return {"txnId": txn, "debug_otp": otp}  # debug_otp ONLY for development

@router.post("/verify-otp")
def abha_verify_otp(
    txnId: str,
    otp: str,
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    entry = TXN.get(txnId)
    if not entry or entry["expires"] < datetime.utcnow() or entry["otp"] != otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    abha = f"ABHA-{random.randint(10**11, 10**12-1)}"
    p.abha_number = abha
    db.commit()
    return {"abha_number": abha}
