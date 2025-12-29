# FILE: app/api/routes_ipd_referrals.py
from __future__ import annotations

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, current_user as auth_current_user
from app.models.ipd import IpdAdmission
from app.models.user import User
from app.models.ipd_referral import IpdReferral, IpdReferralEvent
from app.schemas.ipd_referral import (
    ReferralCreate,
    ReferralOut,
    ReferralDecision,
    ReferralRespond,
    ReferralCancel,
)
from app.utils.resp import ok, err

def has_perm(user: UserModel, code: str) -> bool:
    """
    Simple RBAC helper. Admins bypass check.
    """
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if p.code == code:
                return True
    return False


router = APIRouter(prefix="/ipd", tags=["IPD - Referrals"])


# ---------------- helpers ----------------

def _can(user: User, codes: List[str]) -> bool:
    return any(has_perm(user, c) for c in codes)

def _get_adm(db: Session, admission_id: int) -> Optional[IpdAdmission]:
    return db.query(IpdAdmission).filter(IpdAdmission.id == admission_id).first()

def _get_ref(db: Session, admission_id: int, ref_id: int) -> Optional[IpdReferral]:
    return (
        db.query(IpdReferral)
        .options(selectinload(IpdReferral.events))
        .filter(IpdReferral.admission_id == admission_id, IpdReferral.id == ref_id)
        .first()
    )

def _add_event(db: Session, ref: IpdReferral, event_type: str, by_user_id: Optional[int], note: str = "", meta=None):
    db.add(
        IpdReferralEvent(
            referral_id=ref.id,
            event_type=event_type,
            event_at=datetime.utcnow(),
            by_user_id=by_user_id,
            note=(note or "").strip(),
            meta=meta,
        )
    )

def _validate(payload: ReferralCreate) -> Optional[str]:
    # reason mandatory
    if not (payload.reason or "").strip():
        return "Referral reason is required."

    # internal vs external rules
    if payload.ref_type == "internal":
        # service referral can be handled by to_service
        if payload.category == "service":
            if not (payload.to_service or "").strip():
                return "Service referral requires to_service (e.g., dietician/physio/wound_care)."
        else:
            if not (payload.to_user_id or payload.to_department_id or (payload.to_department or "").strip()):
                return "Internal referral requires to_user_id or to_department_id (or to_department)."

    if payload.ref_type == "external":
        if not (payload.external_org or "").strip():
            return "External referral requires external_org."
        # transfer should have clinical summary
        if payload.category == "transfer" and not (payload.clinical_summary or "").strip():
            return "External transfer requires clinical_summary."

    # category/care_mode consistency (soft guard)
    if payload.category == "transfer" and payload.care_mode != "transfer":
        return "For category=transfer, care_mode must be transfer."

    return None


# ---------------- endpoints ----------------

@router.post("/admissions/{admission_id}/referrals")
def create_referral(
    admission_id: int,
    payload: ReferralCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.create", "ipd.manage", "ipd.doctor", "ipd.nursing"]):
        return err("Not permitted", 403)

    adm = _get_adm(db, admission_id)
    if not adm:
        return err("Admission not found", 404)

    msg = _validate(payload)
    if msg:
        return err(msg, 422)

    ref = IpdReferral(
        admission_id=admission_id,

        ref_type=payload.ref_type,
        category=payload.category,
        care_mode=payload.care_mode,
        priority=payload.priority,
        status="requested",

        requested_by_user_id=getattr(user, "id", None),

        to_department_id=payload.to_department_id,
        to_user_id=payload.to_user_id,
        to_department=(payload.to_department or ""),
        to_service=(payload.to_service or ""),

        external_org=(payload.external_org or ""),
        external_contact_name=(payload.external_contact_name or ""),
        external_contact_phone=(payload.external_contact_phone or ""),
        external_address=(payload.external_address or ""),

        reason=(payload.reason or "").strip(),
        clinical_summary=(payload.clinical_summary or ""),
        attachments=[a.dict() for a in (payload.attachments or [])] or None,
    )

    db.add(ref)
    db.flush()  # get ref.id

    _add_event(
        db,
        ref,
        event_type="requested",
        by_user_id=getattr(user, "id", None),
        note=ref.reason,
        meta={
            "ref_type": ref.ref_type,
            "category": ref.category,
            "care_mode": ref.care_mode,
            "priority": ref.priority,
        },
    )

    db.commit()

    ref = _get_ref(db, admission_id, ref.id)
    return ok(ReferralOut.from_orm(ref), 201)


@router.get("/admissions/{admission_id}/referrals")
def list_referrals(
    admission_id: int,
    status: Optional[str] = Query(None),
    ref_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.view", "ipd.view", "ipd.manage", "ipd.doctor", "ipd.nursing"]):
        return err("Not permitted", 403)

    q = (
        db.query(IpdReferral)
        .options(selectinload(IpdReferral.events))
        .filter(IpdReferral.admission_id == admission_id)
        .order_by(IpdReferral.id.desc())
    )
    if status:
        q = q.filter(IpdReferral.status == status)
    if ref_type:
        q = q.filter(IpdReferral.ref_type == ref_type)
    if category:
        q = q.filter(IpdReferral.category == category)
    if priority:
        q = q.filter(IpdReferral.priority == priority)

    rows = q.limit(limit).all()
    return ok([ReferralOut.from_orm(r) for r in rows])


@router.get("/admissions/{admission_id}/referrals/{ref_id}")
def get_referral(
    admission_id: int,
    ref_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.view", "ipd.view", "ipd.manage", "ipd.doctor", "ipd.nursing"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    return ok(ReferralOut.from_orm(ref))


@router.post("/admissions/{admission_id}/referrals/{ref_id}/accept")
def accept_referral(
    admission_id: int,
    ref_id: int,
    payload: ReferralDecision,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.accept", "ipd.manage", "ipd.doctor"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    if ref.status != "requested":
        return err(f"Cannot accept referral in '{ref.status}' status.", 409)

    ref.status = "accepted"
    ref.accepted_at = datetime.utcnow()
    ref.accepted_by_user_id = getattr(user, "id", None)

    _add_event(db, ref, "accepted", getattr(user, "id", None), note=(payload.note or ""))

    db.commit()
    ref = _get_ref(db, admission_id, ref_id)
    return ok(ReferralOut.from_orm(ref))


@router.post("/admissions/{admission_id}/referrals/{ref_id}/decline")
def decline_referral(
    admission_id: int,
    ref_id: int,
    payload: ReferralDecision,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.decline", "ipd.manage", "ipd.doctor"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    if ref.status != "requested":
        return err(f"Cannot decline referral in '{ref.status}' status.", 409)

    note = (payload.note or "").strip()
    if not note:
        return err("Decline reason is required.", 422)

    ref.status = "declined"
    ref.decline_reason = note

    _add_event(db, ref, "declined", getattr(user, "id", None), note=note)

    db.commit()
    ref = _get_ref(db, admission_id, ref_id)
    return ok(ReferralOut.from_orm(ref))


@router.post("/admissions/{admission_id}/referrals/{ref_id}/respond")
def respond_referral(
    admission_id: int,
    ref_id: int,
    payload: ReferralRespond,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.respond", "ipd.manage", "ipd.doctor"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    if ref.status not in ("accepted", "requested"):
        return err(f"Cannot respond referral in '{ref.status}' status.", 409)

    ref.status = "responded"
    ref.responded_at = datetime.utcnow()
    ref.responded_by_user_id = getattr(user, "id", None)
    ref.response_note = payload.response_note.strip()

    _add_event(db, ref, "responded", getattr(user, "id", None), note=ref.response_note)

    db.commit()
    ref = _get_ref(db, admission_id, ref_id)
    return ok(ReferralOut.from_orm(ref))


@router.post("/admissions/{admission_id}/referrals/{ref_id}/close")
def close_referral(
    admission_id: int,
    ref_id: int,
    payload: ReferralDecision,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.close", "ipd.manage", "ipd.doctor", "ipd.nursing"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    if ref.status not in ("responded", "accepted"):
        return err(f"Cannot close referral in '{ref.status}' status.", 409)

    ref.status = "closed"
    ref.closed_at = datetime.utcnow()

    _add_event(db, ref, "closed", getattr(user, "id", None), note=(payload.note or ""))

    db.commit()
    ref = _get_ref(db, admission_id, ref_id)
    return ok(ReferralOut.from_orm(ref))


@router.post("/admissions/{admission_id}/referrals/{ref_id}/cancel")
def cancel_referral(
    admission_id: int,
    ref_id: int,
    payload: ReferralCancel,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not _can(user, ["ipd.referrals.cancel", "ipd.manage", "ipd.doctor", "ipd.nursing"]):
        return err("Not permitted", 403)

    ref = _get_ref(db, admission_id, ref_id)
    if not ref:
        return err("Referral not found", 404)

    if ref.status in ("closed", "cancelled"):
        return err(f"Cannot cancel referral in '{ref.status}' status.", 409)

    ref.status = "cancelled"
    ref.cancelled_at = datetime.utcnow()
    ref.cancel_reason = payload.reason.strip()

    _add_event(db, ref, "cancelled", getattr(user, "id", None), note=ref.cancel_reason)

    db.commit()
    ref = _get_ref(db, admission_id, ref_id)
    return ok(ReferralOut.from_orm(ref))
