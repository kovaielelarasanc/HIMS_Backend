# FILE: app/api/routes_ipd_billing.py
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.ipd import IpdAdmission

from app.models.billing import DocStatus

from app.services.ipd_billing import (
    ensure_ipd_invoice,
    ensure_ipd_pharmacy_invoice,
    ensure_ipd_ot_invoice,
    add_bed_charges_to_ipd_invoice,
    sync_lis_ris_to_ipd_invoice,
)

router = APIRouter(prefix="/ipd", tags=["IPD – Billing"])


def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


def _get_adm(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return adm


@router.get("/admissions/{admission_id}/billing/invoice")
def get_or_create_ipd_invoice(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "billing.view", "ipd.manage"])
    adm = _get_adm(db, admission_id)

    inv = ensure_ipd_invoice(
        db,
        admission_id=adm.id,
        patient_id=adm.patient_id,
        user_id=getattr(user, "id", None),
    )
    db.commit()
    return {
        "invoice_id": inv.id,
        "billing_case_id": inv.billing_case_id,
        "module": inv.module,
        "status": inv.status,
        "invoice_number": inv.invoice_number,
    }


@router.get("/admissions/{admission_id}/billing/invoice/pharmacy")
def get_or_create_ipd_pharmacy_invoice(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "billing.view", "ipd.manage"])
    adm = _get_adm(db, admission_id)

    inv = ensure_ipd_pharmacy_invoice(
        db,
        admission_id=adm.id,
        patient_id=adm.patient_id,
        user_id=getattr(user, "id", None),
    )
    db.commit()
    return {
        "invoice_id": inv.id,
        "billing_case_id": inv.billing_case_id,
        "module": inv.module,
        "status": inv.status,
        "invoice_number": inv.invoice_number,
    }


@router.get("/admissions/{admission_id}/billing/invoice/ot")
def get_or_create_ipd_ot_invoice(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["ipd.view", "billing.view", "ipd.manage"])
    adm = _get_adm(db, admission_id)

    inv = ensure_ipd_ot_invoice(
        db,
        admission_id=adm.id,
        patient_id=adm.patient_id,
        user_id=getattr(user, "id", None),
    )
    db.commit()
    return {
        "invoice_id": inv.id,
        "billing_case_id": inv.billing_case_id,
        "module": inv.module,
        "status": inv.status,
        "invoice_number": inv.invoice_number,
    }


@router.post("/admissions/{admission_id}/billing/sync")
def sync_ipd_invoice_services(
        admission_id: int,
        only_final_status: bool = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["billing.manage", "ipd.manage"])
    _get_adm(db, admission_id)

    inv = sync_lis_ris_to_ipd_invoice(
        db,
        admission_id=admission_id,
        user_id=getattr(user, "id", None),
        only_final_status=only_final_status,
    )
    db.commit()
    return {
        "message": "Synced LIS/RIS to IPD invoice",
        "invoice_id": inv.id,
        "status": inv.status
    }


@router.post("/admissions/{admission_id}/billing/bed-charges")
def post_bed_charges(
        admission_id: int,
        from_date: date | None = None,
        to_date: date | None = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["billing.manage", "ipd.manage"])
    _get_adm(db, admission_id)

    inv = add_bed_charges_to_ipd_invoice(
        db,
        admission_id=admission_id,
        user_id=getattr(user, "id", None),
        from_date=from_date,
        to_date=to_date,
    )

    # block if posted (service already blocks) - just extra clarity for UI
    if inv.status == DocStatus.POSTED:
        raise HTTPException(
            400, "Invoice is POSTED; cannot auto-post bed/room charges")

    db.commit()
    return {
        "message": "Room charges posted",
        "invoice_id": inv.id,
        "status": inv.status
    }
