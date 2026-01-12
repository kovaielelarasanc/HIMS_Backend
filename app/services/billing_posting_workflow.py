# FILE: app/services/billing_posting_workflow.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.models.user import User
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInsuranceCase,
    BillingPreauthRequest,
    BillingClaim,
    DocStatus,
    PreauthStatus,
    ClaimStatus,
    InsuranceStatus,
)

from app.services.billing_finance import BillingStateError
from app.services.billing_claims_service import upsert_draft_claim_from_invoice


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _invoice_insurer_due(db: Session, *, invoice_id: int) -> Decimal:
    # Sum insurer_pay_amount from invoice lines
    return _d(
        db.query(
            func.coalesce(func.sum(
                BillingInvoiceLine.insurer_pay_amount), 0)).filter(
                    BillingInvoiceLine.invoice_id == int(invoice_id)).scalar())


def _case_posted_insurer_due_excluding_invoice(
        db: Session, *, case_id: int, exclude_invoice_id: int) -> Decimal:
    # Sum insurer pay for POSTED invoices in this case, excluding current invoice
    q = (db.query(
        func.coalesce(func.sum(
            BillingInvoiceLine.insurer_pay_amount), 0)).join(
                BillingInvoice,
                BillingInvoice.id == BillingInvoiceLine.invoice_id).filter(
                    BillingInvoice.billing_case_id == int(case_id)).filter(
                        BillingInvoice.status == DocStatus.POSTED).filter(
                            BillingInvoice.id != int(exclude_invoice_id)))
    return _d(q.scalar())


def _requires_preauth_for_invoice(db: Session, *, invoice_id: int) -> bool:
    # Requires preauth if any line has requires_preauth=True AND insurer_pay_amount > 0
    cnt = (db.query(func.count(BillingInvoiceLine.id)).filter(
        BillingInvoiceLine.invoice_id == int(invoice_id)).filter(
            BillingInvoiceLine.requires_preauth.is_(True)).filter(
                BillingInvoiceLine.insurer_pay_amount > 0).scalar())
    return bool((cnt or 0) > 0)


def _get_insurance_case_for_case(
        db: Session, *, case_id: int) -> Optional[BillingInsuranceCase]:
    return (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(case_id)).first())


def _latest_approved_or_partial_preauth(
        db: Session, *,
        insurance_case_id: int) -> Optional[BillingPreauthRequest]:
    # Prefer latest record; validation checks status
    pre = (db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.insurance_case_id == int(
            insurance_case_id)).order_by(
                desc(BillingPreauthRequest.created_at),
                desc(BillingPreauthRequest.id)).first())
    if not pre:
        return None
    st = str(_enum_value(pre.status) or "").upper()
    if st in {"APPROVED", "PARTIAL"}:
        return pre
    return None


def assert_preauth_ok_for_post(db: Session, *, inv: BillingInvoice) -> None:
    """
    Block posting when:
      - invoice has any requires_preauth line (with insurer payable)
      - insurer_due (cumulative after posting) exceeds approved limit
      - OR no approved/partial preauth exists
    Limit source:
      - Prefer latest approved/partial preauth approved_amount if > 0
      - Else fallback to BillingInsuranceCase.approved_limit if > 0
    """
    case = db.query(BillingCase).filter(
        BillingCase.id == int(inv.billing_case_id)).first()
    if not case:
        raise BillingStateError("Billing case not found for invoice")

    if not _requires_preauth_for_invoice(db, invoice_id=int(inv.id)):
        return

    ins = _get_insurance_case_for_case(db, case_id=int(case.id))
    if not ins:
        raise BillingStateError(
            "Preauth required, but insurance case not configured.")

    # Current invoice insurer due
    current_due = _invoice_insurer_due(db, invoice_id=int(inv.id))
    if current_due <= 0:
        # If nothing payable by insurer in this invoice, no preauth gate
        return

    # cumulative after posting (real-world)
    posted_due = _case_posted_insurer_due_excluding_invoice(
        db, case_id=int(case.id), exclude_invoice_id=int(inv.id))
    cumulative = posted_due + current_due

    pre = _latest_approved_or_partial_preauth(db,
                                              insurance_case_id=int(ins.id))
    if not pre:
        raise BillingStateError(
            "Preauth required. Cannot POST invoice until preauth is APPROVED/PARTIAL."
        )

    limit = _d(pre.approved_amount) if _d(pre.approved_amount) > 0 else _d(
        ins.approved_limit)

    if limit <= 0:
        raise BillingStateError(
            "Preauth approved limit not set. Cannot POST invoice.")

    if cumulative > limit:
        raise BillingStateError(
            f"Preauth limit exceeded. Approved limit={limit} but insurer payable after posting={cumulative}."
        )

    # Optional: sync insurance_case fields for dashboard clarity
    # status mapping
    try:
        if pre.status == PreauthStatus.APPROVED:
            ins.status = InsuranceStatus.PREAUTH_APPROVED
        elif pre.status == PreauthStatus.PARTIAL:
            ins.status = InsuranceStatus.PREAUTH_PARTIAL
    except Exception:
        pass

    # store best known limit
    if _d(pre.approved_amount) > 0:
        ins.approved_limit = _d(pre.approved_amount)
        ins.approved_at = pre.approved_at or datetime.utcnow()

    db.add(ins)
    db.flush()


def normalize_invoice_line_splits(db: Session, *, invoice_id: int) -> None:
    """
    Optional: recompute line payer split if you have helper function.
    Safe if helper doesn't exist.
    """
    try:
        from app.services.billing_finance import recompute_line_payer_split  # type: ignore
    except Exception:
        recompute_line_payer_split = None  # type: ignore

    if recompute_line_payer_split is None:
        return

    lines = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(invoice_id)).all()
    for ln in lines:
        try:
            recompute_line_payer_split(
                ln)  # sets insurer_pay_amount / patient_pay_amount etc.
        except Exception:
            pass
    db.flush()


def post_invoice_workflow(
        db: Session, *, invoice_id: int,
        user: User) -> Tuple[BillingInvoice, Optional[BillingClaim]]:
    """
    Posting workflow:
      1) Invoice must be APPROVED (or already POSTED)
      2) Normalize payer splits (optional)
      3) Enforce preauth + approved_limit validation (cumulative)
      4) POST invoice
      5) Auto-create/update a DRAFT claim with submission package (invoice + lines)
    """
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise BillingStateError("Invoice not found")

    st = str(_enum_value(inv.status) or "").upper()

    if st == "POSTED":
        # Idempotent: keep claim draft updated
        claim = upsert_draft_claim_from_invoice(db,
                                                invoice_id=int(inv.id),
                                                user=user)
        return inv, claim

    if st != "APPROVED":
        raise BillingStateError("Only APPROVED invoices can be POSTED")

    normalize_invoice_line_splits(db, invoice_id=int(inv.id))

    # preauth hard gate + approved limit validation
    assert_preauth_ok_for_post(db, inv=inv)

    # POST
    inv.status = DocStatus.POSTED
    inv.posted_at = datetime.utcnow()
    inv.posted_by = getattr(user, "id", None)
    db.add(inv)
    db.flush()

    # Auto create claim draft + package (only if insurer_due > 0)
    claim = upsert_draft_claim_from_invoice(db,
                                            invoice_id=int(inv.id),
                                            user=user)

    return inv, claim
