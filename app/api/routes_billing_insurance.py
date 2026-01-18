# FILE: app/api/routes_billing_insurance.py
from __future__ import annotations

from typing import List, Any, Dict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, DataError, SQLAlchemyError

from app.api.deps import get_db, current_user
from app.models.user import User

from app.schemas.billing_insurance import (
    InsuranceCaseUpsert,
    InsuranceCaseOut,
    PreauthCreate,
    PreauthDecision,
    PreauthOut,
    ClaimCreate,
    ClaimDecision,
    ClaimOut,
    InsuranceLineRow,
    InsuranceLinePatch,
    SplitRequest,
)

from app.models.billing import (
    BillingInsuranceCase,
    BillingPreauthRequest,
    BillingClaim,
    PreauthStatus,
    ClaimStatus,
)

from app.services.billing_insurance import (
    upsert_insurance_case,
    list_insurance_lines,
    patch_insurance_lines,
    split_invoices_for_insurance,
    create_preauth,
    preauth_submit,
    preauth_approve,
    create_claim,
    claim_submit,
    claim_settle,
    _ref,
)

router = APIRouter(prefix="/billing", tags=["Billing Insurance"])

ZERO = Decimal("0.00")


def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


def _m(v) -> Decimal:
    try:
        if v is None:
            return ZERO
        return Decimal(str(v))
    except Exception:
        return ZERO


def _enum_str(v: Any) -> Any:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _preauth_out(pr: BillingPreauthRequest) -> Dict[str, Any]:
    return {
        "id": int(pr.id),
        "insurance_case_id": int(pr.insurance_case_id),
        "requested_amount": _m(pr.requested_amount),
        "approved_amount": _m(pr.approved_amount),
        "status": _enum_str(pr.status),  # ✅
        "submitted_at": pr.submitted_at,
        "approved_at": pr.approved_at,
        "remarks": pr.remarks,
        "attachments_json": pr.attachments_json,
        "created_at": pr.created_at,
        "updated_at": pr.updated_at,
        "ref_no": _ref("PA", int(pr.id)),
    }


def _claim_out(cl: BillingClaim) -> Dict[str, Any]:
    return {
        "id": int(cl.id),
        "insurance_case_id": int(cl.insurance_case_id),
        "claim_amount": _m(cl.claim_amount),
        "approved_amount": _m(cl.approved_amount),
        "settled_amount": _m(cl.settled_amount),
        "status": _enum_str(cl.status),  # ✅
        "submitted_at": cl.submitted_at,
        "settled_at": cl.settled_at,
        "remarks": cl.remarks,
        "attachments_json": cl.attachments_json,
        "created_at": cl.created_at,
        "updated_at": cl.updated_at,
        "ref_no": _ref("CL", int(cl.id)),
    }


def _commit_refresh_out(db: Session, obj: Any, out_fn):
    try:
        db.commit()
        db.refresh(obj)
        return out_fn(obj)
    except Exception:
        db.rollback()
        raise


def _flush_refresh_commit(db: Session, obj: Any, out_fn):
    try:
        db.flush()
        db.refresh(obj)
        out = out_fn(obj)
        db.commit()
        return out
    except (IntegrityError, DataError) as e:
        db.rollback()
        msg = str(getattr(e, "orig",
                          e))  # ✅ shows enum/constraint errors clearly
        raise HTTPException(status_code=400,
                            detail=f"DB validation error: {msg}")
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        db.rollback()
        raise


@router.get("/cases/{billing_case_id}/insurance",
            response_model=InsuranceCaseOut)
def get_insurance(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.view", "billing.insurance.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
    if not ins:
        raise HTTPException(status_code=404, detail="Insurance case not found")
    return ins


@router.put("/cases/{billing_case_id}/insurance",
            response_model=InsuranceCaseOut)
def put_insurance(
        billing_case_id: int,
        payload: InsuranceCaseUpsert,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.manage"])
    ins = upsert_insurance_case(
        db,
        billing_case_id,
        payload.model_dump(exclude_unset=True),
        getattr(user, "id", None),
    )
    db.commit()
    return ins


@router.get("/cases/{billing_case_id}/insurance/lines",
            response_model=List[InsuranceLineRow])
def get_ins_lines(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.view", "billing.insurance.manage"])
    return list_insurance_lines(db, billing_case_id)


@router.patch("/cases/{billing_case_id}/insurance/lines")
def patch_ins_lines(
        billing_case_id: int,
        payload: List[InsuranceLinePatch] = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.manage"])
    n = patch_insurance_lines(
        db,
        billing_case_id,
        [p.model_dump(exclude_unset=True) for p in payload],
        getattr(user, "id", None),
    )
    db.commit()
    return {"updated": n}


@router.post("/cases/{billing_case_id}/insurance/split")
def split_insurance_invoices(
        billing_case_id: int,
        body: SplitRequest,
        allow_paid_split: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    res = split_invoices_for_insurance(
        db,
        billing_case_id=billing_case_id,
        invoice_ids=body.invoice_ids,
        user_id=user.id,
        allow_paid_split=allow_paid_split,
    )
    db.commit()
    return res


@router.get("/cases/{billing_case_id}/preauths",
            response_model=List[PreauthOut])
def list_preauths(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.view", "billing.preauth.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
    if not ins:
        return []
    rows = (db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.insurance_case_id == ins.id).order_by(
            BillingPreauthRequest.created_at.desc()).all())
    return [_preauth_out(r) for r in rows]


@router.post("/cases/{billing_case_id}/preauths", response_model=PreauthOut)
def post_preauth(
        billing_case_id: int,
        payload: PreauthCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    pr = create_preauth(db, billing_case_id,
                        payload.model_dump(exclude_unset=True),
                        getattr(user, "id", None))
    return _commit_refresh_out(db, pr, _preauth_out)


@router.post("/cases/{billing_case_id}/preauths/{preauth_id}/submit",
             response_model=PreauthOut)
def submit_preauth(
        billing_case_id: int,
        preauth_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    pr = preauth_submit(db, preauth_id, getattr(user, "id", None))
    return _commit_refresh_out(db, pr, _preauth_out)


@router.post("/preauths/{preauth_id}/approve", response_model=PreauthOut)
def approve_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    pr = preauth_approve(db, preauth_id, payload.approved_amount,
                         PreauthStatus.APPROVED, payload.remarks or "",
                         getattr(user, "id", None))
    return _commit_refresh_out(db, pr, _preauth_out)


@router.post("/preauths/{preauth_id}/partial", response_model=PreauthOut)
def partial_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    pr = preauth_approve(db, preauth_id, payload.approved_amount,
                         PreauthStatus.PARTIAL, payload.remarks or "",
                         getattr(user, "id", None))
    return _commit_refresh_out(db, pr, _preauth_out)


@router.post("/preauths/{preauth_id}/reject", response_model=PreauthOut)
def reject_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    pr = preauth_approve(db, preauth_id, 0, PreauthStatus.REJECTED,
                         payload.remarks or "", getattr(user, "id", None))
    return _commit_refresh_out(db, pr, _preauth_out)


@router.get("/cases/{billing_case_id}/claims", response_model=List[ClaimOut])
def list_claims(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.view", "billing.claims.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
    if not ins:
        return []
    rows = (db.query(BillingClaim).filter(
        BillingClaim.insurance_case_id == ins.id).order_by(
            BillingClaim.created_at.desc()).all())
    return [_claim_out(r) for r in rows]


@router.post("/cases/{billing_case_id}/claims", response_model=ClaimOut)
def post_claim(
        billing_case_id: int,
        payload: ClaimCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    cl = create_claim(db, billing_case_id,
                      payload.model_dump(exclude_unset=True),
                      getattr(user, "id", None))
    return _commit_refresh_out(db, cl, _claim_out)


@router.post("/claims/{claim_id}/submit", response_model=ClaimOut)
def submit_claim(
        claim_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_submit(db, claim_id, getattr(user, "id", None))
        return _commit_refresh_out(db, cl, _claim_out)
    except HTTPException:
        db.rollback()
        raise
    except (KeyError, ValueError, TypeError) as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        db.rollback()
        raise


@router.post("/claims/{claim_id}/settle", response_model=ClaimOut)
def settle_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    cl = claim_settle(db, claim_id, payload.approved_amount,
                      payload.settled_amount, ClaimStatus.SETTLED,
                      payload.remarks or "", getattr(user, "id", None))
    return _commit_refresh_out(db, cl, _claim_out)


@router.post("/claims/{claim_id}/deny", response_model=ClaimOut)
def deny_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    cl = claim_settle(db, claim_id, payload.approved_amount,
                      payload.settled_amount, ClaimStatus.DENIED,
                      payload.remarks or "", getattr(user, "id", None))
    return _commit_refresh_out(db, cl, _claim_out)


@router.post("/claims/{claim_id}/query", response_model=ClaimOut)
def query_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    cl = claim_settle(db, claim_id, payload.approved_amount,
                      payload.settled_amount, ClaimStatus.UNDER_QUERY,
                      payload.remarks or "", getattr(user, "id", None))
    return _commit_refresh_out(db, cl, _claim_out)
