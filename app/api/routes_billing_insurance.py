# FILE: app/api/routes_billing_insurance.py
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing Insurance"])

ZERO = Decimal("0.00")


# ---------------------------------------------------------
# helpers
# ---------------------------------------------------------
def _need_any(user: User, codes: List[str]) -> None:
    if bool(getattr(user, "is_admin", False)):
        return
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
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
        "status": _enum_str(pr.status),
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
        "status": _enum_str(cl.status),
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


def _http_400_from_db_error(e: Exception) -> HTTPException:
    msg = str(getattr(e, "orig", e))
    return HTTPException(status_code=400, detail=f"DB validation error: {msg}")


def _db500(detail: str = "Database error") -> HTTPException:
    return HTTPException(status_code=500, detail=detail)


# ---------------------------------------------------------
# Insurance Case
# ---------------------------------------------------------
@router.get("/cases/{billing_case_id}/insurance",
            response_model=InsuranceCaseOut)
def get_insurance(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.view", "billing.insurance.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
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
    try:
        ins = upsert_insurance_case(
            db,
            int(billing_case_id),
            payload.model_dump(exclude_unset=True),
            getattr(user, "id", None),
        )
        db.commit()
        db.refresh(ins)
        return ins
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("put_insurance unexpected error case=%s",
                         billing_case_id)
        raise


# ---------------------------------------------------------
# Insurance Lines
# ---------------------------------------------------------
@router.get("/cases/{billing_case_id}/insurance/lines",
            response_model=List[InsuranceLineRow])
def get_ins_lines(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.view", "billing.insurance.manage"])
    return list_insurance_lines(db, int(billing_case_id))


@router.patch("/cases/{billing_case_id}/insurance/lines")
def patch_ins_lines(
        billing_case_id: int,
        payload: List[InsuranceLinePatch] = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.manage"])
    try:
        n = patch_insurance_lines(
            db,
            int(billing_case_id),
            [p.model_dump(exclude_unset=True) for p in payload],
            getattr(user, "id", None),
        )
        db.commit()
        return {"updated": int(n)}
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("patch_ins_lines unexpected error case=%s",
                         billing_case_id)
        raise


# ---------------------------------------------------------
# Split (Insurance)
# ---------------------------------------------------------
@router.post("/cases/{billing_case_id}/insurance/split")
def split_insurance_invoices(
        billing_case_id: int,
        body: SplitRequest,
        allow_paid_split: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.insurance.manage"])
    try:
        res = split_invoices_for_insurance(
            db,
            billing_case_id=int(billing_case_id),
            invoice_ids=[int(x) for x in (body.invoice_ids or [])],
            user_id=getattr(user, "id", None),
            allow_paid_split=bool(allow_paid_split),
        )
        db.commit()
        return res
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("split_insurance_invoices unexpected error case=%s",
                         billing_case_id)
        raise


# ---------------------------------------------------------
# Preauth
# ---------------------------------------------------------
@router.get("/cases/{billing_case_id}/preauths",
            response_model=List[PreauthOut])
def list_preauths(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.view", "billing.preauth.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
    if not ins:
        return []
    rows = (db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.insurance_case_id == int(ins.id)).order_by(
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
    try:
        pr = create_preauth(
            db,
            int(billing_case_id),
            payload.model_dump(exclude_unset=True),
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, pr, _preauth_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("post_preauth unexpected error case=%s",
                         billing_case_id)
        raise


@router.post("/cases/{billing_case_id}/preauths/{preauth_id}/submit",
             response_model=PreauthOut)
def submit_preauth(
        billing_case_id: int,
        preauth_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    try:
        pr = preauth_submit(db, int(preauth_id), getattr(user, "id", None))
        return _commit_refresh_out(db, pr, _preauth_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("submit_preauth unexpected error preauth_id=%s",
                         preauth_id)
        raise


@router.post("/preauths/{preauth_id}/approve", response_model=PreauthOut)
def approve_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    try:
        pr = preauth_approve(
            db,
            int(preauth_id),
            payload.approved_amount,
            PreauthStatus.APPROVED,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, pr, _preauth_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("approve_preauth unexpected error preauth_id=%s",
                         preauth_id)
        raise


@router.post("/preauths/{preauth_id}/partial", response_model=PreauthOut)
def partial_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    try:
        pr = preauth_approve(
            db,
            int(preauth_id),
            payload.approved_amount,
            PreauthStatus.PARTIAL,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, pr, _preauth_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("partial_preauth unexpected error preauth_id=%s",
                         preauth_id)
        raise


@router.post("/preauths/{preauth_id}/reject", response_model=PreauthOut)
def reject_preauth(
        preauth_id: int,
        payload: PreauthDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.preauth.manage"])
    try:
        pr = preauth_approve(
            db,
            int(preauth_id),
            Decimal("0.00"),
            PreauthStatus.REJECTED,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, pr, _preauth_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("reject_preauth unexpected error preauth_id=%s",
                         preauth_id)
        raise


# ---------------------------------------------------------
# Claims
# ---------------------------------------------------------
@router.get("/cases/{billing_case_id}/claims", response_model=List[ClaimOut])
def list_claims(
        billing_case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.view", "billing.claims.manage"])
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
    if not ins:
        return []
    rows = (db.query(BillingClaim).filter(
        BillingClaim.insurance_case_id == int(ins.id)).order_by(
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
    try:
        cl = create_claim(
            db,
            int(billing_case_id),
            payload.model_dump(exclude_unset=True),
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("post_claim unexpected error case=%s",
                         billing_case_id)
        raise


@router.post("/claims/{claim_id}/submit", response_model=ClaimOut)
def submit_claim(
        claim_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_submit(db, int(claim_id), getattr(user, "id", None))
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.exception("submit_claim DB error claim_id=%s", claim_id)
        raise _db500("Database error during claim submit")
    except Exception:
        db.rollback()
        logger.exception("submit_claim unexpected error claim_id=%s", claim_id)
        raise HTTPException(status_code=500,
                            detail="Submit failed. Check server logs.")


# ✅ NEW: Approve Claim (separate from Settle)
@router.post("/claims/{claim_id}/approve", response_model=ClaimOut)
def approve_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_settle(
            db,
            int(claim_id),
            payload.approved_amount,
            getattr(payload, "settled_amount", ZERO) or ZERO,
            ClaimStatus.APPROVED,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.exception("approve_claim DB error claim_id=%s", claim_id)
        raise _db500("Database error during claim approval")
    except Exception:
        db.rollback()
        logger.exception("approve_claim unexpected error claim_id=%s",
                         claim_id)
        raise HTTPException(status_code=500,
                            detail="Approve failed. Check server logs.")


@router.post("/claims/{claim_id}/settle", response_model=ClaimOut)
def settle_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_settle(
            db,
            int(claim_id),
            payload.approved_amount,
            payload.settled_amount,
            ClaimStatus.SETTLED,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        logger.exception("settle_claim DB error claim_id=%s", claim_id)
        raise _db500("Database error during claim settlement")
    except Exception:
        db.rollback()
        logger.exception("settle_claim unexpected error claim_id=%s", claim_id)
        raise HTTPException(status_code=500,
                            detail="Settle failed. Check server logs.")


@router.post("/claims/{claim_id}/deny", response_model=ClaimOut)
def deny_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_settle(
            db,
            int(claim_id),
            payload.approved_amount,
            payload.settled_amount,
            ClaimStatus.DENIED,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("deny_claim unexpected error claim_id=%s", claim_id)
        raise


@router.post("/claims/{claim_id}/query", response_model=ClaimOut)
def query_claim(
        claim_id: int,
        payload: ClaimDecision,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.claims.manage"])
    try:
        cl = claim_settle(
            db,
            int(claim_id),
            payload.approved_amount,
            payload.settled_amount,
            ClaimStatus.UNDER_QUERY,
            payload.remarks or "",
            getattr(user, "id", None),
        )
        return _commit_refresh_out(db, cl, _claim_out)
    except (IntegrityError, DataError) as e:
        db.rollback()
        raise _http_400_from_db_error(e)
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise _db500()
    except Exception:
        db.rollback()
        logger.exception("query_claim unexpected error claim_id=%s", claim_id)
        raise
