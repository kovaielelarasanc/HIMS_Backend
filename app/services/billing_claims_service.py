# FILE: app/services/billing_claims_service.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.models.user import User
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInsuranceCase,
    BillingClaim,
    DocStatus,
    ClaimStatus,
    InsuranceStatus,
)
from app.services.billing_finance import BillingStateError


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _safe_iso(dt):
    return dt.isoformat() if dt else None


def _get_case(db: Session, case_id: int) -> BillingCase:
    c = db.query(BillingCase).filter(BillingCase.id == int(case_id)).first()
    if not c:
        raise BillingStateError("Billing case not found")
    return c


def _get_invoice(db: Session, invoice_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise BillingStateError("Invoice not found")
    return inv


def _get_insurance_case_for_case(db: Session,
                                 case_id: int) -> BillingInsuranceCase:
    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(case_id)).first()
    if not ins:
        raise BillingStateError(
            "Insurance case not configured for this billing case.")
    return ins


def _invoice_due_totals(db: Session, invoice_id: int) -> Dict[str, Decimal]:
    insurer = _d(
        db.query(
            func.coalesce(func.sum(
                BillingInvoiceLine.insurer_pay_amount), 0)).filter(
                    BillingInvoiceLine.invoice_id == int(invoice_id)).scalar())
    patient = _d(
        db.query(
            func.coalesce(func.sum(
                BillingInvoiceLine.patient_pay_amount), 0)).filter(
                    BillingInvoiceLine.invoice_id == int(invoice_id)).scalar())
    net = _d(
        db.query(func.coalesce(func.sum(
            BillingInvoiceLine.net_amount), 0)).filter(
                BillingInvoiceLine.invoice_id == int(invoice_id)).scalar())
    return {"insurer_due": insurer, "patient_due": patient, "net_total": net}


def build_claim_package_for_invoice(db: Session, *,
                                    invoice_id: int) -> Dict[str, Any]:
    inv = _get_invoice(db, invoice_id)
    case = _get_case(db, int(inv.billing_case_id))

    totals = _invoice_due_totals(db, invoice_id=int(inv.id))

    lines = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
            BillingInvoiceLine.id.asc()).all())

    def line_to_dict(ln: BillingInvoiceLine) -> Dict[str, Any]:
        return {
            "id":
            int(ln.id),
            "service_group":
            _enum_value(ln.service_group),
            "item_type":
            ln.item_type,
            "item_id":
            ln.item_id,
            "item_code":
            ln.item_code,
            "description":
            ln.description,
            "qty":
            str(_d(ln.qty)),
            "unit_price":
            str(_d(ln.unit_price)),
            "discount_percent":
            str(_d(ln.discount_percent)),
            "discount_amount":
            str(_d(ln.discount_amount)),
            "gst_rate":
            str(_d(ln.gst_rate)),
            "tax_amount":
            str(_d(ln.tax_amount)),
            "line_total":
            str(_d(ln.line_total)),
            "net_amount":
            str(_d(ln.net_amount)),
            "is_covered":
            _enum_value(ln.is_covered),
            "approved_amount":
            str(_d(ln.approved_amount)),
            "requires_preauth":
            bool(ln.requires_preauth),
            "insurer_pay_amount":
            str(_d(ln.insurer_pay_amount)),
            "patient_pay_amount":
            str(_d(ln.patient_pay_amount)),
            "source_module":
            ln.source_module,
            "source_ref_id":
            int(ln.source_ref_id) if ln.source_ref_id is not None else None,
            "source_line_key":
            ln.source_line_key,
            "is_manual":
            bool(ln.is_manual),
            "manual_reason":
            ln.manual_reason,
            "created_at":
            _safe_iso(getattr(ln, "created_at", None)),
        }

    pkg = {
        "version": 1,
        "generated_at": datetime.utcnow().isoformat(),
        "case": {
            "billing_case_id": int(case.id),
            "case_number": case.case_number,
            "patient_id": int(case.patient_id),
            "encounter_type": _enum_value(case.encounter_type),
            "encounter_id": int(case.encounter_id),
            "payer_mode": _enum_value(case.payer_mode),
        },
        "invoice": {
            "invoice_id": int(inv.id),
            "invoice_number": inv.invoice_number,
            "module": (inv.module or "MISC"),
            "invoice_type": _enum_value(inv.invoice_type),
            "status": _enum_value(inv.status),
            "currency": inv.currency,
            "sub_total": str(_d(inv.sub_total)),
            "discount_total": str(_d(inv.discount_total)),
            "tax_total": str(_d(inv.tax_total)),
            "round_off": str(_d(inv.round_off)),
            "grand_total": str(_d(inv.grand_total)),
            "approved_at": _safe_iso(inv.approved_at),
            "posted_at": _safe_iso(inv.posted_at),
            "service_date": _safe_iso(inv.service_date),
        },
        "totals": {
            "net_total": str(totals["net_total"]),
            "insurer_due": str(totals["insurer_due"]),
            "patient_due": str(totals["patient_due"]),
        },
        "lines": [line_to_dict(x) for x in lines],
    }
    return pkg


def _find_claim_for_invoice(db: Session, *, insurance_case_id: int,
                            invoice_id: int) -> Optional[BillingClaim]:
    """
    BillingClaim has no invoice_id column.
    We store invoice_id inside attachments_json. So we scan claims in this insurance_case.
    """
    claims = (db.query(BillingClaim).filter(
        BillingClaim.insurance_case_id == int(insurance_case_id)).order_by(
            desc(BillingClaim.created_at), desc(BillingClaim.id)).all())

    for c in claims:
        aj = c.attachments_json or {}
        if isinstance(aj, dict):
            inv_id = None
            inv = aj.get("invoice") if isinstance(aj.get("invoice"),
                                                  dict) else None
            if inv and "invoice_id" in inv:
                inv_id = inv.get("invoice_id")
            elif "invoice_id" in aj:
                inv_id = aj.get("invoice_id")
            if inv_id is not None and int(inv_id) == int(invoice_id):
                return c
    return None


def upsert_draft_claim_from_invoice(db: Session, *, invoice_id: int,
                                    user: User) -> Optional[BillingClaim]:
    """
    Create/update a DRAFT claim for the invoice using insurer_due.
    Stores submission package in attachments_json.
    - Returns None if insurer_due <= 0
    """
    inv = _get_invoice(db, invoice_id)
    case = _get_case(db, int(inv.billing_case_id))
    ins = _get_insurance_case_for_case(db, int(case.id))

    totals = _invoice_due_totals(db, invoice_id=int(inv.id))
    insurer_due = totals["insurer_due"]

    if insurer_due <= 0:
        return None

    # Find existing claim draft for this invoice (via attachments_json invoice_id)
    claim = _find_claim_for_invoice(db,
                                    insurance_case_id=int(ins.id),
                                    invoice_id=int(inv.id))

    if claim:
        st = str(_enum_value(claim.status) or "").upper()
        # Allow updating only if still DRAFT (safe real-world)
        if st != "DRAFT":
            return claim
    else:
        claim = BillingClaim(
            insurance_case_id=int(ins.id),
            status=ClaimStatus.DRAFT,
            created_by=getattr(user, "id", None),
        )
        db.add(claim)
        db.flush()

    # Amounts
    claim.claim_amount = insurer_due

    # Keep approved/settled at 0 unless already set
    if _d(claim.approved_amount) == 0:
        claim.approved_amount = Decimal("0")
    if _d(claim.settled_amount) == 0:
        claim.settled_amount = Decimal("0")

    # Package JSON
    pkg = build_claim_package_for_invoice(db, invoice_id=int(inv.id))
    claim.attachments_json = pkg

    # Keep insurance case status informational
    try:
        if ins.status in {
                InsuranceStatus.INITIATED, InsuranceStatus.PREAUTH_APPROVED,
                InsuranceStatus.PREAUTH_PARTIAL
        }:
            # still pre-claim; don't force CLAIM_SUBMITTED here
            db.add(ins)
    except Exception:
        pass

    db.add(claim)

    # Also store claim_id into invoice.meta_json for fast UI linking
    try:
        mj = inv.meta_json or {}
        if not isinstance(mj, dict):
            mj = {}
        mj["claim_id"] = int(claim.id)
        mj["claim_status"] = _enum_value(claim.status)
        inv.meta_json = mj
        db.add(inv)
    except Exception:
        pass

    db.flush()
    return claim


# -----------------------------
# Lifecycle transitions
# -----------------------------
def _require_status(claim: BillingClaim, allowed: set[str], msg: str):
    st = str(_enum_value(claim.status) or "").upper()
    if st not in allowed:
        raise BillingStateError(msg)


def get_claim(db: Session, claim_id: int) -> BillingClaim:
    c = db.query(BillingClaim).filter(BillingClaim.id == int(claim_id)).first()
    if not c:
        raise BillingStateError("Claim not found")
    return c


def claim_submit(db: Session,
                 *,
                 claim_id: int,
                 user: User,
                 remarks: str = "") -> BillingClaim:
    claim = get_claim(db, claim_id)
    _require_status(claim, {"DRAFT"}, "Only DRAFT claims can be SUBMITTED")

    # Ensure invoice is POSTED (from package)
    pkg = claim.attachments_json or {}
    inv_pkg = (pkg.get("invoice") or {}) if isinstance(pkg, dict) else {}
    invoice_id = inv_pkg.get("invoice_id") or pkg.get("invoice_id")

    if not invoice_id:
        raise BillingStateError("Claim package missing invoice_id")

    inv_db = _get_invoice(db, int(invoice_id))
    if str(_enum_value(inv_db.status) or "").upper() != "POSTED":
        raise BillingStateError("Claim can be submitted only after invoice is POSTED.")

    claim.status = ClaimStatus.SUBMITTED
    claim.submitted_at = datetime.utcnow()
    if remarks:
        claim.remarks = remarks

    # Update insurance case status
    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(claim.insurance_case_id)).first()
    if ins:
        ins.status = InsuranceStatus.CLAIM_SUBMITTED
        db.add(ins)

    db.add(claim)
    db.flush()
    return claim


def claim_acknowledge(db: Session,
                      *,
                      claim_id: int,
                      user: User,
                      remarks: str = "") -> BillingClaim:
    """
    Your ClaimStatus enum has no ACKNOWLEDGED.
    So we store acknowledgement timestamp inside attachments_json.lifecycle.
    Status remains SUBMITTED.
    """
    claim = get_claim(db, claim_id)
    _require_status(claim, {"SUBMITTED"},
                    "Only SUBMITTED claims can be ACKNOWLEDGED")

    pkg = claim.attachments_json if isinstance(claim.attachments_json,
                                               dict) else {}
    lifecycle = pkg.get("lifecycle") if isinstance(pkg.get("lifecycle"),
                                                   dict) else {}
    lifecycle["acknowledged_at"] = datetime.utcnow().isoformat()
    lifecycle["acknowledged_by"] = int(getattr(user, "id", 0) or 0)
    pkg["lifecycle"] = lifecycle
    claim.attachments_json = pkg

    if remarks:
        claim.remarks = remarks

    db.add(claim)
    db.flush()
    return claim


def claim_approve(db: Session,
                  *,
                  claim_id: int,
                  user: User,
                  approved_amount: Decimal,
                  remarks: str = "") -> BillingClaim:
    claim = get_claim(db, claim_id)
    _require_status(claim, {"SUBMITTED", "UNDER_QUERY"},
                    "Only SUBMITTED/UNDER_QUERY claims can be APPROVED")

    claim.status = ClaimStatus.APPROVED
    claim.approved_amount = _d(approved_amount)
    if remarks:
        claim.remarks = remarks

    db.add(claim)
    db.flush()
    return claim


def claim_settle(db: Session,
                 *,
                 claim_id: int,
                 user: User,
                 settled_amount: Decimal,
                 remarks: str = "") -> BillingClaim:
    claim = get_claim(db, claim_id)
    _require_status(claim, {"APPROVED"}, "Only APPROVED claims can be SETTLED")

    claim.status = ClaimStatus.SETTLED
    claim.settled_amount = _d(settled_amount)
    claim.settled_at = datetime.utcnow()
    if remarks:
        claim.remarks = remarks

    # Update insurance case status
    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(claim.insurance_case_id)).first()
    if ins:
        ins.status = InsuranceStatus.SETTLED
        db.add(ins)

    db.add(claim)
    db.flush()
    return claim


def claim_to_dict(c: BillingClaim) -> Dict[str, Any]:
    pkg = c.attachments_json if isinstance(c.attachments_json, dict) else {}
    inv = pkg.get("invoice") if isinstance(pkg.get("invoice"), dict) else {}
    case = pkg.get("case") if isinstance(pkg.get("case"), dict) else {}

    return {
        "id": int(c.id),
        "insurance_case_id": int(c.insurance_case_id),
        "status": _enum_value(c.status),
        "claim_amount": str(_d(c.claim_amount)),
        "approved_amount": str(_d(c.approved_amount)),
        "settled_amount": str(_d(c.settled_amount)),
        "submitted_at": _safe_iso(c.submitted_at),
        "settled_at": _safe_iso(c.settled_at),
        "remarks": c.remarks,
        "invoice": {
            "invoice_id": inv.get("invoice_id"),
            "invoice_number": inv.get("invoice_number"),
            "module": inv.get("module"),
            "status": inv.get("status"),
        },
        "case": {
            "billing_case_id": case.get("billing_case_id"),
            "case_number": case.get("case_number"),
            "patient_id": case.get("patient_id"),
            "encounter_type": case.get("encounter_type"),
            "encounter_id": case.get("encounter_id"),
        },
        "package": pkg,  # full submission package
    }
