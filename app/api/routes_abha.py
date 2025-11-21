# FILE: app/api/routes_abha_demo.py
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


@router.post("/generate")
def abha_generate(
        name: str,
        dob: str,
        mobile: str,
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.update") and not has_perm(
            user, "patients.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # demo only – in real integration you will call ABDM gateway here
    txn_id = f"demo-{int(datetime.utcnow().timestamp())}"
    debug_otp = "123456"

    return {
        "txnId": txn_id,
        "debug_otp": debug_otp,
        "message": "Demo ABHA OTP generated. Use debug_otp only in dev.",
    }


@router.post("/verify-otp")
def abha_verify_otp(
        txnId: str,
        otp: str,
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # demo validation
    if otp != "123456":
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    p = db.query(Patient).get(patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    if not p.abha_number:
        p.abha_number = f"ABHA-{patient_id:06d}"
        db.commit()
        db.refresh(p)

    return {
        "message": "ABHA linked (demo only)",
        "abha_number": p.abha_number,
        "txnId": txnId,
    }
