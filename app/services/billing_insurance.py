# FILE: app/services/billing_insurance.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, DataError, SQLAlchemyError
from fastapi import HTTPException

from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingInsuranceCase,
    BillingPreauthRequest,
    BillingClaim,
    BillingAuditLog,
    BillingNumberSeries,
    BillingPayment,
    BillingPaymentAllocation,
    NumberDocType,
    NumberResetPeriod,
    DocStatus,
    InvoiceType,
    PayerType,
    InsuranceStatus,
    PreauthStatus,
    ClaimStatus,
    InsurancePayerKind,
    PayMode,
    PaymentKind,
    PaymentDirection,
    ReceiptStatus,
)

logger = logging.getLogger(__name__)

D0 = Decimal("0.00")
D2 = Decimal("0.01")


def _d(v) -> Decimal:
    try:
        return Decimal(str(v or 0)).quantize(D2)
    except Exception:
        return D0


def _now() -> datetime:
    return datetime.utcnow()


def _ref(prefix: str, idv: int) -> str:
    # no DB change needed; still not showing raw id directly
    return f"{prefix}{idv:08d}"


def _enum(v: Any) -> Any:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _log(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    user_id: Optional[int],
    old: Any = None,
    new: Any = None,
    reason: str = "",
):
    db.add(
        BillingAuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            user_id=user_id,
            old_json=old,
            new_json=new,
            reason=reason or None,
        ))


# ============================================================
# Number Series (Tenant DB => NO tenant_id usage needed)
# ============================================================
def next_doc_number(
    db: Session,
    doc_type: NumberDocType,
    prefix: str = "",
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
) -> str:
    """
    Tenant DB safe counter. Uses SELECT FOR UPDATE.
    """
    now = datetime.utcnow()
    if reset_period == NumberResetPeriod.YEAR:
        period_key = f"{now.year}"
    elif reset_period == NumberResetPeriod.MONTH:
        period_key = f"{now.year}-{now.month:02d}"
    else:
        period_key = None

    row = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == doc_type,
        BillingNumberSeries.reset_period == reset_period,
        BillingNumberSeries.prefix == (prefix or ""),
        BillingNumberSeries.is_active == True,
    ).with_for_update().one_or_none())

    if not row:
        row = BillingNumberSeries(
            doc_type=doc_type,
            reset_period=reset_period,
            prefix=prefix or "",
            padding=padding,
            next_number=1,
            last_period_key=period_key,
            is_active=True,
        )
        db.add(row)
        db.flush()

    if reset_period != NumberResetPeriod.NONE and row.last_period_key != period_key:
        row.next_number = 1
        row.last_period_key = period_key

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    num = str(n).zfill(int(row.padding or padding))
    return f"{row.prefix}{num}"


def get_or_404_case(db: Session, billing_case_id: int) -> BillingCase:
    case = db.query(BillingCase).filter(
        BillingCase.id == int(billing_case_id)).one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return case


# ============================================================
# Insurance Case Upsert (your existing logic kept)
# ============================================================
def upsert_insurance_case(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingInsuranceCase:
    case = get_or_404_case(db, billing_case_id)
    payload = dict(payload or {})

    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())

    if not ins:
        ins = BillingInsuranceCase(
            billing_case_id=int(billing_case_id),
            created_by=user_id,
            status=InsuranceStatus.INITIATED,
            payer_kind=InsurancePayerKind.INSURANCE,
        )
        db.add(ins)
        db.flush()
        _log(db, "BillingInsuranceCase", int(ins.id), "CREATE", user_id)

    # non-ID fields
    for k in ["policy_no", "member_id", "plan_name", "status"]:
        if k in payload and payload[k] is not None:
            setattr(ins, k, payload[k])

    # payer_kind
    if payload.get("payer_kind") is not None:
        ins.payer_kind = payload["payer_kind"]

    def _u(v: Any) -> Optional[int]:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except Exception:
            return None

    pk = ins.payer_kind or InsurancePayerKind.INSURANCE

    insurance_company_id = _u(payload.get(
        "insurance_company_id")) if "insurance_company_id" in payload else _u(
            getattr(ins, "insurance_company_id", None))
    tpa_id = _u(payload.get("tpa_id")) if "tpa_id" in payload else _u(
        getattr(ins, "tpa_id", None))
    corporate_id = _u(
        payload.get("corporate_id")) if "corporate_id" in payload else _u(
            getattr(ins, "corporate_id", None))

    if pk == InsurancePayerKind.CORPORATE:
        ins.corporate_id = corporate_id
        ins.tpa_id = None
        ins.insurance_company_id = None
        if not ins.corporate_id:
            # fallback if you want from case defaults:
            if str(case.default_payer_type
                   or "").upper() in ("CORPORATE",
                                      "CREDIT_PLAN") and case.default_payer_id:
                ins.corporate_id = int(case.default_payer_id)
        if not ins.corporate_id:
            raise HTTPException(status_code=400, detail="Select Corporate")

    elif pk == InsurancePayerKind.TPA:
        ins.tpa_id = tpa_id
        ins.insurance_company_id = insurance_company_id
        ins.corporate_id = None
        if not ins.tpa_id:
            if getattr(case, "default_tpa_id", None):
                ins.tpa_id = int(case.default_tpa_id)
        if not ins.tpa_id:
            raise HTTPException(status_code=400, detail="Select TPA")

    else:
        ins.insurance_company_id = insurance_company_id
        ins.tpa_id = None
        ins.corporate_id = None
        if not ins.insurance_company_id:
            if str(case.default_payer_type
                   or "").upper() in ("PAYER", "INSURER",
                                      "INSURANCE") and case.default_payer_id:
                ins.insurance_company_id = int(case.default_payer_id)
        if not ins.insurance_company_id:
            raise HTTPException(status_code=400,
                                detail="Select Insurance Company")

    if payload.get("approved_limit") is not None:
        ins.approved_limit = _d(payload["approved_limit"])
        ins.approved_at = _now()

    db.flush()
    return ins


# ============================================================
# Lines (Insurance Mapping)
# ============================================================
def list_insurance_lines(db: Session,
                         billing_case_id: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    invoices = (db.query(BillingInvoice).options(
        selectinload(BillingInvoice.lines)).filter(
            BillingInvoice.billing_case_id == int(billing_case_id)).filter(
                BillingInvoice.status != DocStatus.VOID).order_by(
                    BillingInvoice.created_at.asc()).all())

    for inv in invoices:
        for ln in (inv.lines or []):
            net = _d(getattr(ln, "net_amount", 0))
            insurer = _d(getattr(ln, "insurer_pay_amount", 0))
            insurer = max(D0, min(insurer, net))
            patient = _d(net - insurer)

            rows.append({
                "invoice_id":
                int(inv.id),
                "invoice_number":
                getattr(inv, "invoice_number", None),
                "module":
                getattr(inv, "module", None),
                "invoice_status":
                _enum(getattr(inv, "status", None)),
                "line_id":
                int(ln.id),
                "description":
                getattr(ln, "description", None),
                "service_group":
                _enum(getattr(ln, "service_group", None)),
                "net_amount":
                net,
                "is_covered":
                getattr(ln, "is_covered", None),
                "insurer_pay_amount":
                insurer,
                "patient_pay_amount":
                patient,
                "requires_preauth":
                bool(getattr(ln, "requires_preauth", False)),
            })
    return rows


def patch_insurance_lines(
    db: Session,
    billing_case_id: int,
    patches: List[Dict[str, Any]],
    user_id: Optional[int],
) -> int:
    case = get_or_404_case(db, billing_case_id)

    line_ids = [int(p["line_id"]) for p in patches if p.get("line_id")]
    if not line_ids:
        return 0

    lines = (db.query(BillingInvoiceLine).join(
        BillingInvoice,
        BillingInvoice.id == BillingInvoiceLine.invoice_id).filter(
            BillingInvoice.billing_case_id == int(billing_case_id)).filter(
                BillingInvoice.status != DocStatus.VOID).filter(
                    BillingInvoiceLine.id.in_(line_ids)).all())
    by_id = {int(l.id): l for l in lines}

    updated = 0
    for p in patches:
        lid = int(p["line_id"])
        ln = by_id.get(lid)
        if not ln:
            continue

        if p.get("is_covered") is not None:
            ln.is_covered = p["is_covered"]

        if p.get("requires_preauth") is not None:
            ln.requires_preauth = bool(p["requires_preauth"])

        if p.get("insurer_pay_amount") is not None:
            net = _d(getattr(ln, "net_amount", 0))
            insurer = _d(p["insurer_pay_amount"])
            insurer = max(D0, min(insurer, net))
            ln.insurer_pay_amount = insurer
            ln.patient_pay_amount = _d(net - insurer)

        db.flush()
        updated += 1

    _log(db,
         "BillingCase",
         int(case.id),
         "INSURANCE_LINES_PATCH",
         user_id,
         reason=f"patched={updated}")
    return updated


# ============================================================
# Invoice Split (patient + insurer)
# ============================================================
def _recalc_invoice_totals(inv: BillingInvoice) -> None:
    sub_total = D0
    disc = D0
    tax = D0
    grand = D0
    for ln in (inv.lines or []):
        sub_total += _d(getattr(ln, "line_total", 0))
        disc += _d(getattr(ln, "discount_amount", 0))
        tax += _d(getattr(ln, "tax_amount", 0))
        grand += _d(getattr(ln, "net_amount", 0))

    inv.sub_total = sub_total
    inv.discount_total = disc
    inv.tax_total = tax
    inv.round_off = D0
    inv.grand_total = grand


def _clone_invoice_base(
    orig: BillingInvoice,
    new_number: str,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: Optional[int],
) -> BillingInvoice:
    return BillingInvoice(
        billing_case_id=orig.billing_case_id,
        invoice_number=new_number,
        module=getattr(orig, "module", None),
        invoice_type=invoice_type,
        status=DocStatus.DRAFT,
        payer_type=payer_type,
        payer_id=payer_id,
        currency=getattr(orig, "currency", None) or "INR",
        service_date=getattr(orig, "service_date", None),
        meta_json={
            "split_from_invoice_number": getattr(orig, "invoice_number", None),
            "split_from_invoice_id": int(orig.id),
        },
    )


def split_invoices_for_insurance(
    db: Session,
    billing_case_id: int,
    invoice_ids: List[int],
    user_id: Optional[int],
    allow_paid_split: bool = False,
) -> Dict[str, Any]:
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
    if not ins:
        raise HTTPException(
            status_code=400,
            detail="Insurance case not set for this billing case")

    if not invoice_ids:
        raise HTTPException(status_code=400, detail="invoice_ids required")

    invoices = (db.query(BillingInvoice).options(
        selectinload(BillingInvoice.lines),
        selectinload(BillingInvoice.payments).selectinload(
            BillingPayment.allocations),
    ).filter(BillingInvoice.billing_case_id == int(billing_case_id)).filter(
        BillingInvoice.id.in_(invoice_ids)).filter(
            BillingInvoice.status != DocStatus.VOID).all())
    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    # insurer payer
    if ins.payer_kind == InsurancePayerKind.CORPORATE:
        insurer_payer_type = PayerType.CORPORATE
        insurer_payer_id = ins.corporate_id
    elif ins.payer_kind == InsurancePayerKind.TPA:
        insurer_payer_type = PayerType.TPA
        insurer_payer_id = ins.tpa_id
    else:
        insurer_payer_type = PayerType.INSURER
        insurer_payer_id = ins.insurance_company_id

    def _load_invoice(inv_id: int) -> BillingInvoice:
        return db.query(BillingInvoice).options(
            selectinload(BillingInvoice.lines)).filter(
                BillingInvoice.id == int(inv_id)).one()

    def _move_payments(orig_inv: BillingInvoice, new_invoice_id: int) -> int:
        moved = 0
        for p in list(getattr(orig_inv, "payments", []) or []):
            p.invoice_id = int(new_invoice_id)
            for a in list(getattr(p, "allocations", []) or []):
                if getattr(a, "invoice_id", None) == int(orig_inv.id):
                    a.invoice_id = int(new_invoice_id)
            moved += 1
        db.flush()
        return moved

    results: List[Dict[str, Any]] = []

    for orig in invoices:
        paid_payments = list(getattr(orig, "payments", []) or [])
        if paid_payments and not allow_paid_split:
            raise HTTPException(
                status_code=400,
                detail=
                f"Invoice {orig.invoice_number} has payments, cannot split. Refund/unapply payments or use allow_paid_split=true",
            )

        # compute shares
        has_insurer = False
        for ln in (orig.lines or []):
            net = _d(getattr(ln, "net_amount", 0))
            ins_amt = _d(getattr(ln, "insurer_pay_amount", 0))
            ins_amt = max(D0, min(ins_amt, net))
            if ins_amt > 0:
                has_insurer = True
            ln.patient_pay_amount = _d(net - ins_amt)

        if has_insurer and not insurer_payer_id and insurer_payer_type != PayerType.CORPORATE:
            raise HTTPException(
                status_code=400,
                detail="Insurance/TPA payer not selected (missing payer_id).")

        patient_no = next_doc_number(db,
                                     NumberDocType.INVOICE,
                                     prefix="PINV",
                                     reset_period=NumberResetPeriod.YEAR)
        patient_inv = _clone_invoice_base(orig, patient_no,
                                          InvoiceType.PATIENT,
                                          PayerType.PATIENT, None)

        insurer_inv = None
        if has_insurer:
            insurer_no = next_doc_number(db,
                                         NumberDocType.INVOICE,
                                         prefix="IINV",
                                         reset_period=NumberResetPeriod.YEAR)
            insurer_inv = _clone_invoice_base(orig, insurer_no,
                                              InvoiceType.INSURER,
                                              insurer_payer_type,
                                              insurer_payer_id)

        db.add(patient_inv)
        if insurer_inv:
            db.add(insurer_inv)
        db.flush()

        moved_payments = 0
        if paid_payments and allow_paid_split:
            moved_payments = _move_payments(orig, int(patient_inv.id))
            _log(db,
                 "BillingInvoice",
                 int(orig.id),
                 "PAYMENTS_MOVED_TO_PATIENT",
                 user_id,
                 reason=f"moved={moved_payments}")

        for ln in (orig.lines or []):
            net = _d(getattr(ln, "net_amount", 0))
            if net <= 0:
                continue

            ins_amt = _d(getattr(ln, "insurer_pay_amount", 0))
            ins_amt = max(D0, min(ins_amt, net))
            pat_amt = _d(net - ins_amt)

            def _mk(amount: Decimal,
                    bucket: str) -> Optional[BillingInvoiceLine]:
                if amount <= 0:
                    return None
                ratio = (amount / net) if net > 0 else D0
                target_id = int(patient_inv.id) if bucket == "PATIENT" else (
                    int(insurer_inv.id) if insurer_inv else None)
                if not target_id:
                    return None

                return BillingInvoiceLine(
                    billing_case_id=orig.billing_case_id,
                    invoice_id=target_id,
                    service_group=getattr(ln, "service_group", None),
                    item_type=getattr(ln, "item_type", None),
                    item_id=getattr(ln, "item_id", None),
                    item_code=getattr(ln, "item_code", None),
                    description=getattr(ln, "description", None),
                    qty=getattr(ln, "qty", None),
                    unit_price=getattr(ln, "unit_price", None),
                    discount_percent=getattr(ln, "discount_percent", None),
                    discount_amount=_d(
                        _d(getattr(ln, "discount_amount", 0)) * ratio),
                    gst_rate=getattr(ln, "gst_rate", None),
                    tax_amount=_d(_d(getattr(ln, "tax_amount", 0)) * ratio),
                    line_total=_d(_d(getattr(ln, "line_total", 0)) * ratio),
                    net_amount=amount,
                    revenue_head_id=getattr(ln, "revenue_head_id", None),
                    cost_center_id=getattr(ln, "cost_center_id", None),
                    doctor_id=getattr(ln, "doctor_id", None),
                    source_module="INS_SPLIT",
                    source_ref_id=int(ln.id),
                    source_line_key=bucket,
                    is_covered=getattr(ln, "is_covered", None),
                    approved_amount=getattr(ln, "approved_amount", None),
                    patient_pay_amount=pat_amt if bucket == "PATIENT" else D0,
                    insurer_pay_amount=ins_amt if bucket == "INSURER" else D0,
                    requires_preauth=bool(
                        getattr(ln, "requires_preauth", False)),
                    is_manual=True,
                    manual_reason=f"Split from {orig.invoice_number}",
                    meta_json={
                        "orig_invoice_number": orig.invoice_number,
                        "orig_invoice_id": int(orig.id),
                        "orig_line_id": int(ln.id),
                        "orig_net_amount": str(net),
                        "bucket": bucket,
                        "bucket_amount": str(amount),
                        "orig_meta": getattr(ln, "meta_json", None) or None,
                    },
                )

            pl = _mk(pat_amt, "PATIENT")
            if pl:
                db.add(pl)
            il = _mk(ins_amt, "INSURER")
            if il:
                db.add(il)

        db.flush()

        p_loaded = _load_invoice(int(patient_inv.id))
        _recalc_invoice_totals(p_loaded)
        if insurer_inv:
            i_loaded = _load_invoice(int(insurer_inv.id))
            _recalc_invoice_totals(i_loaded)
        db.flush()

        orig.status = DocStatus.VOID
        reason = f"Split into PATIENT:{patient_inv.invoice_number}"
        if insurer_inv:
            reason += f" + INSURER:{insurer_inv.invoice_number}"
        if moved_payments:
            reason += f" (moved_payments={moved_payments})"

        if hasattr(orig, "void_reason"):
            orig.void_reason = reason
        if hasattr(orig, "voided_by"):
            orig.voided_by = user_id
        if hasattr(orig, "voided_at"):
            orig.voided_at = _now()

        _log(db,
             "BillingInvoice",
             int(orig.id),
             "VOID_SPLIT",
             user_id,
             reason=reason)

        results.append({
            "from":
            orig.invoice_number,
            "patient_invoice_number":
            patient_inv.invoice_number,
            "insurer_invoice_number":
            insurer_inv.invoice_number if insurer_inv else None,
            "moved_payments":
            moved_payments,
        })

    return {"split": results}


# ============================================================
# Preauth
# ============================================================
def create_preauth(db: Session, billing_case_id: int, payload: Dict[str, Any],
                   user_id: Optional[int]) -> BillingPreauthRequest:
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    pr = BillingPreauthRequest(
        insurance_case_id=int(ins.id),
        approved_amount=D0,
        requested_amount=_d(payload.get("requested_amount")),
        remarks=payload.get("remarks"),
        attachments_json=payload.get("attachments_json"),
        created_by=user_id,
        status=PreauthStatus.DRAFT,
    )
    db.add(pr)
    db.flush()
    _log(db, "BillingPreauthRequest", int(pr.id), "CREATE", user_id)
    return pr


def preauth_submit(db: Session, preauth_id: int,
                   user_id: Optional[int]) -> BillingPreauthRequest:
    pr = db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.id == int(preauth_id)).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status != PreauthStatus.DRAFT:
        raise HTTPException(status_code=400,
                            detail="Only DRAFT can be submitted")

    pr.status = PreauthStatus.SUBMITTED
    pr.submitted_at = _now()
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(pr.insurance_case_id)).one()
    ins.status = InsuranceStatus.PREAUTH_SUBMITTED
    db.flush()

    _log(db, "BillingPreauthRequest", int(pr.id), "SUBMIT", user_id)
    return pr


def preauth_approve(
    db: Session,
    preauth_id: int,
    approved_amount: Decimal,
    status: PreauthStatus,
    remarks: str,
    user_id: Optional[int],
) -> BillingPreauthRequest:
    pr = db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.id == int(preauth_id)).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status != PreauthStatus.SUBMITTED:
        raise HTTPException(status_code=400,
                            detail="Only SUBMITTED can be decided")

    pr.approved_amount = _d(approved_amount)
    pr.remarks = remarks or pr.remarks
    pr.approved_at = _now()
    pr.status = status
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(pr.insurance_case_id)).one()
    ins.approved_limit = _d(pr.approved_amount)
    ins.approved_at = _now()
    if status == PreauthStatus.APPROVED:
        ins.status = InsuranceStatus.PREAUTH_APPROVED
    elif status == PreauthStatus.PARTIAL:
        ins.status = InsuranceStatus.PREAUTH_PARTIAL
    else:
        ins.status = InsuranceStatus.PREAUTH_REJECTED
    db.flush()

    _log(db,
         "BillingPreauthRequest",
         int(pr.id),
         "DECIDE",
         user_id,
         reason=str(status))
    return pr


# ============================================================
# Claims (THIS IS WHERE YOUR 500 IS COMING FROM)
# Fix: Always store invoice_id (single) + invoice_ids (list)
# ============================================================
def _norm_int_list(v: Any) -> List[int]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        out: List[int] = []
        for x in v:
            try:
                xi = int(x)
                if xi > 0:
                    out.append(xi)
            except Exception:
                continue
        # unique preserve order
        return list(dict.fromkeys(out))
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",")]
        out: List[int] = []
        for p in parts:
            try:
                xi = int(p)
                if xi > 0:
                    out.append(xi)
            except Exception:
                continue
        return list(dict.fromkeys(out))
    try:
        xi = int(v)
        return [xi] if xi > 0 else []
    except Exception:
        return []


def _claim_meta_dict(cl: BillingClaim) -> Dict[str, Any]:
    j = getattr(cl, "attachments_json", None) or {}
    if not isinstance(j, dict):
        return {}
    return dict(j)


def _claim_get_invoice_ids(cl: BillingClaim) -> List[int]:
    j = _claim_meta_dict(cl)
    ids = (
        j.get("insurer_invoice_ids") or j.get("invoice_ids")
        or j.get("_claim_invoice_ids") or j.get("invoice_id")  # legacy single
        or j.get("primary_invoice_id")  # newer single
    )
    return _norm_int_list(ids)


def _claim_set_invoice_meta(cl: BillingClaim, invoice_ids: List[int],
                            invoices: List[BillingInvoice]) -> None:
    j = _claim_meta_dict(cl)

    invoice_ids = [int(x) for x in _norm_int_list(invoice_ids)]
    nums = [getattr(i, "invoice_number", None) for i in invoices]

    j["insurer_invoice_ids"] = invoice_ids
    j["insurer_invoice_numbers"] = nums

    # legacy multi
    j["invoice_ids"] = invoice_ids
    j["invoice_numbers"] = nums

    # legacy single (THIS PREVENTS: "invoice_id missing")
    if invoices:
        j["primary_invoice_id"] = int(invoices[0].id)
        j["primary_invoice_number"] = getattr(invoices[0], "invoice_number",
                                              None)

        j["invoice_id"] = int(invoices[0].id)
        j["invoice_number"] = getattr(invoices[0], "invoice_number", None)

    j["_claim_invoice_ids"] = invoice_ids

    cl.attachments_json = j or None


def _infer_claim_invoices(db: Session,
                          billing_case_id: int) -> List[BillingInvoice]:
    """
    User-friendly:
    - allow INSURER invoices in DRAFT/APPROVED/POSTED (not VOID)
    """
    return (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id)).filter(
            BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).order_by(
                    BillingInvoice.created_at.asc()).all())


def create_claim(db: Session, billing_case_id: int, payload: Dict[str, Any],
                 user_id: Optional[int]) -> BillingClaim:
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(
            billing_case_id)).one_or_none())
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    payload = dict(payload or {})
    invoice_ids = _norm_int_list(
        payload.get("insurer_invoice_ids") or payload.get("invoice_ids")
        or payload.get("invoice_id"))

    # If UI didn't send invoice_ids, infer insurer invoices (after split)
    invoices: List[BillingInvoice] = []
    if invoice_ids:
        invoices = (db.query(BillingInvoice).filter(
            BillingInvoice.billing_case_id == int(billing_case_id)).filter(
                BillingInvoice.id.in_(invoice_ids)
            ).filter(BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).order_by(
                    BillingInvoice.created_at.asc()).all())
        found = {int(i.id) for i in invoices}
        missing = [i for i in invoice_ids if int(i) not in found]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid insurer_invoice_ids: {missing}")
    else:
        invoices = _infer_claim_invoices(db, int(billing_case_id))
        invoice_ids = [int(i.id) for i in invoices]

    # amount: prefer computed from invoices if present; else payload claim_amount
    if invoices:
        claim_amt = sum((_d(getattr(i, "grand_total", 0)) for i in invoices),
                        D0)
    else:
        claim_amt = _d(payload.get("claim_amount"))

    attachments = payload.get("attachments_json") or {}
    if not isinstance(attachments, dict):
        attachments = {}

        # after invoice_ids parsed...
    all_selected = []
    if invoice_ids:
        all_selected = (db.query(BillingInvoice).filter(
            BillingInvoice.billing_case_id == int(billing_case_id)).filter(
                BillingInvoice.id.in_(invoice_ids)).filter(
                    BillingInvoice.status != DocStatus.VOID).order_by(
                        BillingInvoice.created_at.asc()).all())
        # keep ONLY insurer invoices; ignore patient invoices silently
        invoices = [
            i for i in all_selected
            if getattr(i, "invoice_type", None) == InvoiceType.INSURER
        ]
    else:
        invoices = _infer_claim_invoices(db, int(billing_case_id))
        invoice_ids = [int(i.id) for i in invoices]

    if not invoices:
        # user-friendly
        raise HTTPException(
            status_code=400,
            detail=
            "No INSURER invoices found. Do Step 3 Split first (Generate Two Invoices).",
        )

    cl = BillingClaim(
        insurance_case_id=int(ins.id),
        claim_amount=_d(claim_amt),
        approved_amount=D0,
        settled_amount=D0,
        remarks=payload.get("remarks"),
        attachments_json=attachments or None,
        created_by=user_id,
        status=ClaimStatus.DRAFT,
    )
    db.add(cl)
    db.flush()

    # ✅ CRITICAL: store invoice_id + invoice_ids now (prevents submit 500)
    if invoices:
        _claim_set_invoice_meta(cl, invoice_ids, invoices)
        db.flush()

    logger.info(
        "create_claim: case=%s claim=%s invoice_ids=%s amount=%s",
        int(billing_case_id),
        int(cl.id),
        invoice_ids,
        str(_d(claim_amt)),
    )

    _log(db,
         "BillingClaim",
         int(cl.id),
         "CREATE",
         user_id,
         reason=f"invoices={invoice_ids}")
    return cl


def claim_approve(
    db: Session,
    claim_id: int,
    approved_amount: Decimal,
    remarks: str,
    user_id: Optional[int],
) -> BillingClaim:
    return claim_settle(
        db=db,
        claim_id=claim_id,
        approved_amount=approved_amount,
        settled_amount=D0,
        status=ClaimStatus.APPROVED,
        remarks=remarks,
        user_id=user_id,
    )


def _meta_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    return {}


def _extract_invoice_ids(meta: Dict[str, Any]) -> List[int]:
    ids: List[int] = []
    for k in ("insurer_invoice_ids", "invoice_ids", "invoices"):
        vv = meta.get(k)
        if isinstance(vv, list):
            for x in vv:
                try:
                    xi = int(x)
                    if xi:
                        ids.append(xi)
                except Exception:
                    pass

    # legacy single key
    if not ids:
        v1 = meta.get("invoice_id")
        if v1 is not None:
            try:
                ids = [int(v1)]
            except Exception:
                ids = []

    # unique
    out = []
    seen = set()
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def claim_submit(db: Session, claim_id: int,
                 user_id: Optional[int]) -> "BillingClaim":
    """
    ✅ Fix:
    - never assumes claim has invoice_id
    - ensures attachments_json has invoice_id + insurer_invoice_ids + invoice_numbers
    - converts missing-invoice case to clean 400 (not 500)
    """
    try:
        cl = db.query(BillingClaim).filter(
            BillingClaim.id == int(claim_id)).one_or_none()
        if not cl:
            raise HTTPException(status_code=404, detail="Claim not found")

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(
                cl.insurance_case_id)).one_or_none()
        if not ins:
            raise HTTPException(status_code=400,
                                detail="Insurance case missing for this claim")

        # allowed transitions
        st = str(getattr(cl, "status", "") or "").upper()
        if st not in ("DRAFT", "UNDER_QUERY"):
            raise HTTPException(
                status_code=400,
                detail=
                f"Only DRAFT/UNDER_QUERY can be submitted (current: {st})")

        billing_case_id = int(ins.billing_case_id)

        meta = _meta_dict(getattr(cl, "attachments_json", None))
        invoice_ids = _extract_invoice_ids(meta)

        # if none in meta → infer insurer invoices from case
        invoices: List[BillingInvoice] = []
        if invoice_ids:
            invoices = (db.query(BillingInvoice).filter(
                BillingInvoice.billing_case_id == billing_case_id).filter(
                    BillingInvoice.id.in_(invoice_ids)).filter(
                        BillingInvoice.status != DocStatus.VOID).filter(
                            BillingInvoice.invoice_type ==
                            InvoiceType.INSURER).order_by(
                                BillingInvoice.created_at.asc()).all())

        if not invoices:
            invoices = (db.query(BillingInvoice).filter(
                BillingInvoice.billing_case_id == billing_case_id
            ).filter(BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).order_by(
                    BillingInvoice.created_at.asc()).all())
            invoice_ids = [int(i.id) for i in invoices]

        if not invoices:
            raise HTTPException(
                status_code=400,
                detail=
                "No INSURER invoices found. Run Step 3 Split first (Patient + Insurer invoices).",
            )

        primary = invoices[0]
        primary_invoice_id = int(primary.id)
        primary_invoice_no = getattr(primary, "invoice_number", None)

        insurer_invoice_numbers = [
            getattr(i, "invoice_number", None) for i in invoices
        ]

        # ✅ write BOTH new + legacy keys so older code never breaks
        meta["invoice_id"] = primary_invoice_id
        meta["invoice_number"] = primary_invoice_no
        meta["invoice_ids"] = invoice_ids
        meta["insurer_invoice_ids"] = invoice_ids
        meta["insurer_invoice_numbers"] = insurer_invoice_numbers

        cl.attachments_json = meta

        cl.status = ClaimStatus.SUBMITTED
        cl.submitted_at = _now()

        # update insurance case status (optional but useful)
        try:
            ins.status = InsuranceStatus.SUBMITTED
        except Exception:
            pass

        db.flush()
        logger.info(
            "claim_submit: claim=%s case=%s invoice_ids=%s primary_invoice_id=%s",
            int(cl.id), billing_case_id, invoice_ids, primary_invoice_id)
        return cl

    except HTTPException:
        raise
    except SQLAlchemyError:
        logger.exception("claim_submit DB error claim_id=%s", claim_id)
        raise HTTPException(status_code=500,
                            detail="Database error during claim submit")
    except Exception as e:
        logger.exception("claim_submit unexpected error claim_id=%s", claim_id)
        raise HTTPException(
            status_code=500,
            detail=f"Claim submit failed: {e.__class__.__name__}")


def _insurer_payer(
        ins: BillingInsuranceCase) -> Tuple[PayerType, Optional[int]]:
    if ins.payer_kind == InsurancePayerKind.CORPORATE:
        return (PayerType.CORPORATE, int(ins.corporate_id or 0) or None)
    if ins.payer_kind == InsurancePayerKind.TPA:
        return (PayerType.TPA, int(ins.tpa_id or 0) or None)
    return (PayerType.INSURER, int(ins.insurance_company_id or 0) or None)


def _invoice_paid_amount(db: Session, invoice_id: int) -> Decimal:
    alloc_sum = (db.query(
        func.coalesce(func.sum(BillingPaymentAllocation.amount), 0)).filter(
            BillingPaymentAllocation.invoice_id == int(invoice_id),
            BillingPaymentAllocation.status == ReceiptStatus.ACTIVE,
        ).scalar() or 0)
    alloc_sum = _d(alloc_sum)
    if alloc_sum > 0:
        return alloc_sum

    pay_sum = (db.query(func.coalesce(func.sum(
        BillingPayment.amount), 0)).filter(
            BillingPayment.invoice_id == int(invoice_id),
            BillingPayment.status == ReceiptStatus.ACTIVE,
            BillingPayment.direction == PaymentDirection.IN,
        ).scalar() or 0)
    return _d(pay_sum)


def _allocate_payment_to_invoices(
    db: Session,
    billing_case_id: int,
    payment: BillingPayment,
    invoices: List[BillingInvoice],
    total_amount: Decimal,
) -> List[Dict[str, Any]]:
    remaining = _d(total_amount)
    out: List[Dict[str, Any]] = []

    for inv in invoices:
        if remaining <= 0:
            break

        paid = _invoice_paid_amount(db, int(inv.id))
        grand = _d(getattr(inv, "grand_total", 0))
        outstanding = max(D0, _d(grand - paid))
        if outstanding <= 0:
            continue

        amt = min(remaining, outstanding)
        if amt <= 0:
            continue

        kwargs = dict(
            billing_case_id=int(billing_case_id),
            payment_id=int(payment.id),
            invoice_id=int(inv.id),
            payer_bucket=getattr(payment, "payer_type", PayerType.PATIENT),
            amount=_d(amt),
            status=ReceiptStatus.ACTIVE,
            allocated_by=None,
        )
        # tenant_id optional in your setup
        if hasattr(BillingPaymentAllocation, "tenant_id"):
            kwargs["tenant_id"] = None

        a = BillingPaymentAllocation(**kwargs)
        db.add(a)
        db.flush()

        out.append({
            "invoice_id": int(inv.id),
            "invoice_number": getattr(inv, "invoice_number", None),
            "allocated": str(_d(amt)),
            "outstanding_before": str(outstanding),
        })

        remaining = _d(remaining - amt)

    return out


def claim_settle(
    db: Session,
    claim_id: int,
    approved_amount: Decimal,
    settled_amount: Decimal,
    status: ClaimStatus,
    remarks: str,
    user_id: Optional[int],
) -> BillingClaim:
    """
    Robust claim workflow:

    DRAFT -> (submit)
    SUBMITTED -> APPROVED -> SETTLED
             -> UNDER_QUERY
             -> DENIED

    Fixes:
    - Validates approved/settled before writing
    - Ensures invoice_id + invoice_ids exist in claim.attachments_json
    - Creates settlement receipt + allocations safely (idempotent)
    """
    try:
        cl = (db.query(BillingClaim).filter(
            BillingClaim.id == int(claim_id)).one_or_none())
        if not cl:
            raise HTTPException(status_code=404, detail="Claim not found")

        ins = (db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(
                cl.insurance_case_id)).one_or_none())
        if not ins:
            raise HTTPException(status_code=400,
                                detail="Insurance case missing for this claim")

        # -----------------------------
        # ✅ Amount + transition validation (BEFORE applying values)
        # -----------------------------
        cur = getattr(cl, "status", None)

        # idempotent: if already settled and requested settle again, just return (no duplicate receipt)
        if status == ClaimStatus.SETTLED and cur == ClaimStatus.SETTLED:
            if remarks:
                cl.remarks = remarks
                db.flush()
            logger.info("claim_settle(idempotent): claim=%s already SETTLED",
                        int(cl.id))
            return cl

        allowed_from = {
            ClaimStatus.UNDER_QUERY: {
                ClaimStatus.SUBMITTED, ClaimStatus.APPROVED,
                ClaimStatus.UNDER_QUERY
            },
            ClaimStatus.APPROVED: {
                ClaimStatus.SUBMITTED, ClaimStatus.UNDER_QUERY,
                ClaimStatus.APPROVED
            },
            ClaimStatus.DENIED: {
                ClaimStatus.SUBMITTED, ClaimStatus.UNDER_QUERY,
                ClaimStatus.APPROVED, ClaimStatus.DENIED
            },
            ClaimStatus.SETTLED: {
                ClaimStatus.SUBMITTED, ClaimStatus.APPROVED,
                ClaimStatus.UNDER_QUERY
            },
        }

        if status in allowed_from and cur not in allowed_from[status]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid transition: {str(cur)} -> {str(status)}")

        claim_amt = _d(getattr(cl, "claim_amount", 0))
        appr = _d(approved_amount)
        sett = _d(settled_amount)

        # Normalize negatives
        if appr < 0:
            raise HTTPException(status_code=400,
                                detail="Approved amount cannot be negative")
        if sett < 0:
            raise HTTPException(status_code=400,
                                detail="Settled amount cannot be negative")

        if status in (ClaimStatus.APPROVED, ClaimStatus.SETTLED):
            if appr <= 0:
                raise HTTPException(status_code=400,
                                    detail="Approved amount must be > 0")
            if appr > claim_amt:
                raise HTTPException(
                    status_code=400,
                    detail="Approved amount cannot exceed claim amount")

        if status == ClaimStatus.SETTLED:
            if sett <= 0:
                raise HTTPException(status_code=400,
                                    detail="Settled amount must be > 0")

            # settled <= approved (best practice)
            if appr > 0 and sett > appr:
                raise HTTPException(
                    status_code=400,
                    detail="Settled amount cannot exceed approved amount")

            # also protect with claim amt (double safety)
            if sett > claim_amt:
                raise HTTPException(
                    status_code=400,
                    detail="Settled amount cannot exceed claim amount")

        if status in (ClaimStatus.DENIED, ClaimStatus.UNDER_QUERY):
            # keep financials clean
            if status == ClaimStatus.DENIED:
                appr = D0
                sett = D0

        # -----------------------------
        # ✅ Apply values after validation
        # -----------------------------
        if status in (ClaimStatus.APPROVED, ClaimStatus.SETTLED,
                      ClaimStatus.DENIED):
            cl.approved_amount = appr

        if status == ClaimStatus.SETTLED:
            cl.settled_amount = sett
        elif status == ClaimStatus.DENIED:
            cl.settled_amount = D0
        # for UNDER_QUERY we do NOT overwrite settled amount

        if remarks:
            cl.remarks = remarks

        # status change + insurance status
        if status == ClaimStatus.UNDER_QUERY:
            cl.status = ClaimStatus.UNDER_QUERY
            ins.status = InsuranceStatus.QUERY

        elif status == ClaimStatus.DENIED:
            cl.status = ClaimStatus.DENIED
            ins.status = InsuranceStatus.DENIED

        elif status == ClaimStatus.APPROVED:
            cl.status = ClaimStatus.APPROVED
            # optional enum safety:
            if hasattr(InsuranceStatus, "CLAIM_APPROVED"):
                ins.status = InsuranceStatus.CLAIM_APPROVED
            else:
                ins.status = getattr(ins, "status",
                                     None) or InsuranceStatus.CLAIM_SUBMITTED

        elif status == ClaimStatus.SETTLED:
            cl.status = ClaimStatus.SETTLED
            cl.settled_at = _now()
            ins.status = InsuranceStatus.SETTLED

        else:
            cl.status = status

        db.flush()

        allocations_info: List[Dict[str, Any]] = []

        # -----------------------------
        # ✅ Settlement receipt + allocations
        # -----------------------------
        if cl.status == ClaimStatus.SETTLED and _d(cl.settled_amount) > 0:
            billing_case_id = int(ins.billing_case_id)

            invoice_ids = _claim_get_invoice_ids(cl)
            invoices: List[BillingInvoice] = []

            if invoice_ids:
                invoices = (db.query(BillingInvoice).filter(
                    BillingInvoice.billing_case_id ==
                    int(billing_case_id)).filter(
                        BillingInvoice.id.in_(invoice_ids)).filter(
                            BillingInvoice.status != DocStatus.VOID).filter(
                                BillingInvoice.invoice_type ==
                                InvoiceType.INSURER).order_by(
                                    BillingInvoice.created_at.asc()).all())

            if not invoices:
                invoices = _infer_claim_invoices(db, int(billing_case_id))
                invoice_ids = [int(i.id) for i in invoices]

            if not invoices:
                raise HTTPException(
                    status_code=400,
                    detail=
                    "No INSURER invoices found to allocate settlement. Do Step 3 Split first.",
                )

            # Ensure legacy meta keys exist (invoice_id, invoice_ids, etc.)
            _claim_set_invoice_meta(cl, invoice_ids, invoices)
            db.flush()

            payer_type, payer_id = _insurer_payer(ins)
            if not payer_id:
                raise HTTPException(
                    status_code=400,
                    detail=
                    "Insurance/TPA/Corporate payer missing in Insurance Case.",
                )

            # idempotency (don't create receipt twice)
            aj = _claim_meta_dict(cl)
            existing_payment_id = aj.get("settlement_payment_id")
            if existing_payment_id:
                logger.info(
                    "claim_settle: claim=%s already has settlement_payment_id=%s",
                    int(cl.id), existing_payment_id)
                _log(db,
                     "BillingClaim",
                     int(cl.id),
                     "SETTLE_SKIPPED_DUPLICATE",
                     user_id,
                     reason=f"payment_id={existing_payment_id}")
                return cl

            receipt_no = next_doc_number(
                db,
                NumberDocType.RECEIPT,
                prefix="IRCP",
                reset_period=NumberResetPeriod.YEAR,
            )

            primary_invoice_id = int(invoices[0].id)
            pay = BillingPayment(
                billing_case_id=int(billing_case_id),
                invoice_id=primary_invoice_id,
                payer_type=payer_type,
                payer_id=payer_id,
                mode=PayMode.BANK,
                amount=_d(cl.settled_amount),
                txn_ref=None,
                received_at=_now(),
                received_by=user_id,
                receipt_number=receipt_no,
                kind=PaymentKind.RECEIPT,
                direction=PaymentDirection.IN,
                status=ReceiptStatus.ACTIVE,
                meta_json={
                    "source":
                    "CLAIM_SETTLEMENT",
                    "claim_id":
                    int(cl.id),
                    "claim_ref_no":
                    _ref("CL", int(cl.id)),
                    "insurer_invoice_ids": [int(i.id) for i in invoices],
                    "insurer_invoice_numbers":
                    [getattr(i, "invoice_number", None) for i in invoices],
                },
            )
            db.add(pay)
            db.flush()

            allocations_info = _allocate_payment_to_invoices(
                db=db,
                billing_case_id=int(billing_case_id),
                payment=pay,
                invoices=invoices,
                total_amount=_d(cl.settled_amount),
            )
            db.flush()

            # store settlement payment id (idempotency key)
            aj = _claim_meta_dict(cl)
            aj["settlement_payment_id"] = int(pay.id)
            cl.attachments_json = aj
            db.flush()

            _log(
                db,
                "BillingPayment",
                int(pay.id),
                "CREATE_FROM_CLAIM_SETTLEMENT",
                user_id,
                reason=
                f"claim_id={int(cl.id)} allocated={len(allocations_info)}",
            )

        _log(
            db,
            "BillingClaim",
            int(cl.id),
            "UPDATE_STATUS",
            user_id,
            reason=f"{cl.status} allocations={len(allocations_info)}",
        )

        logger.info(
            "claim_settle: claim=%s status=%s approved=%s settled=%s allocations=%s",
            int(cl.id), str(cl.status),
            str(_d(getattr(cl, "approved_amount", 0))),
            str(_d(getattr(cl, "settled_amount", 0))), len(allocations_info))

        return cl

    except HTTPException:
        raise
    except (IntegrityError, DataError) as e:
        logger.exception("claim_settle DB validation error claim_id=%s",
                         claim_id)
        raise HTTPException(
            status_code=400,
            detail=f"DB validation error: {str(getattr(e, 'orig', e))}",
        )
    except SQLAlchemyError as e:
        logger.exception("claim_settle DB error claim_id=%s", claim_id)
        raise HTTPException(
            status_code=500,
            detail=
            f"Database error while settling claim: {e.__class__.__name__}",
        )
    except Exception as e:
        logger.exception("claim_settle unexpected error claim_id=%s", claim_id)
        raise HTTPException(
            status_code=500,
            detail=f"Claim settle failed: {e.__class__.__name__}: {str(e)}",
        )
