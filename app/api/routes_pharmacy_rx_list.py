# FILE: app/api/routes_pharmacy_rx_list.py
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.pharmacy_prescription import PharmacyPrescription

router = APIRouter()


def _need_any(user: User, codes: list[str]) -> None:
    """Simple RBAC helper – same idea as other modules."""
    if getattr(user, "is_admin", False):
        return
    roles = getattr(user, "roles", []) or []
    have = {p.code for r in roles for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


def _full_name(p: Patient) -> str:
    parts = [getattr(p, "first_name", None), getattr(p, "last_name", None)]
    name = " ".join([x for x in parts if x]).strip()
    if name:
        return name
    return getattr(p, "name", "") or f"Patient #{p.id}"


class PharmacyRxSummaryOut(BaseModel):
    id: int
    rx_number: Optional[str] = None
    type: Optional[str] = None  # OPD / IPD / OT / COUNTER
    status: Optional[str] = None

    patient_id: int
    patient_uhid: Optional[str] = None
    patient_name: str
    created_at: datetime

    visit_id: Optional[int] = None
    admission_id: Optional[int] = None


@router.get("/rx", response_model=List[PharmacyRxSummaryOut])
def list_pharmacy_rx(
        q: Optional[str] = Query(
            None, description="Search UHID / name / phone / rx no"),
        type: Optional[str] = Query(None,
                                    description="OPD / IPD / OT / COUNTER"),
        status: Optional[str] = Query(
            None, description="Draft / Final / Cancelled etc"),
        date_from: Optional[str] = Query(
            None, description="YYYY-MM-DD on created_at"),
        date_to: Optional[str] = Query(None,
                                       description="YYYY-MM-DD on created_at"),
        limit: int = Query(100, ge=1, le=300),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    List pharmacy prescriptions (Rx) for the Pharmacy Rx console.
    """
    _need_any(user, ["pharmacy.rx.view", "pharmacy.prescriptions.view"])

    q_rx = (db.query(PharmacyPrescription, Patient).join(
        Patient, PharmacyPrescription.patient_id == Patient.id))

    # Optional date filters
    if date_from:
        df = datetime.fromisoformat(date_from + "T00:00:00")
        q_rx = q_rx.filter(PharmacyPrescription.created_at >= df)
    if date_to:
        dt = datetime.fromisoformat(date_to + "T23:59:59")
        q_rx = q_rx.filter(PharmacyPrescription.created_at <= dt)

    # Type (OPD / IPD / OT / COUNTER)
    if type:
        q_rx = q_rx.filter(PharmacyPrescription.type == type.upper())

    # Status (only if your model has this field – if not, this will simply be ignored)
    if status and hasattr(PharmacyPrescription, "status"):
        q_rx = q_rx.filter(PharmacyPrescription.status == status.upper())

    # Basic search on patient & rx number
    if q:
        ql = f"%{q.strip()}%"
        conds = [
            Patient.uhid.ilike(ql),
            Patient.first_name.ilike(ql),
            Patient.last_name.ilike(ql),
            Patient.phone.ilike(ql),
        ]
        if hasattr(PharmacyPrescription, "rx_number"):
            conds.append(PharmacyPrescription.rx_number.ilike(ql))
        q_rx = q_rx.filter(or_(*conds))

    rows = (q_rx.order_by(PharmacyPrescription.created_at.desc(),
                          PharmacyPrescription.id.desc()).limit(limit).all())

    out: list[PharmacyRxSummaryOut] = []
    for rx, patient in rows:
        created_at = getattr(rx, "created_at", None) or datetime.utcnow()
        rx_number = getattr(rx, "rx_number", None)
        rx_type = getattr(rx, "type", None)
        status_val = getattr(rx, "status", None)

        admission_id = getattr(rx, "ipd_admission_id", None) or getattr(
            rx, "admission_id", None)

        out.append(
            PharmacyRxSummaryOut(
                id=rx.id,
                rx_number=rx_number,
                type=rx_type,
                status=status_val,
                patient_id=patient.id,
                patient_uhid=getattr(patient, "uhid", None),
                patient_name=_full_name(patient),
                created_at=created_at,
                visit_id=getattr(rx, "visit_id", None),
                admission_id=admission_id,
            ))

    return out
