# FILE: app/services/billing_insurance.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func, and_
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
    NumberDocType,
    NumberResetPeriod,
    DocStatus,
    InvoiceType,
    PayerType,
    InsuranceStatus,
    PreauthStatus,
    ClaimStatus,
    InsurancePayerKind,
    BillingPayment,
    BillingPaymentAllocation,
    PayMode,
    PaymentKind,
    PaymentDirection,
    ReceiptStatus,
)

D0 = Decimal("0.00")
D2 = Decimal("0.01")


# ----------------------------
# helpers
# ----------------------------
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
        )
    )


def _json_text(col, path: str):
    # MySQL JSON safe text compare: JSON_UNQUOTE(JSON_EXTRACT(col, '$.k'))
    return func.JSON_UNQUOTE(func.JSON_EXTRACT(col, path))


def get_or_404_case(db: Session, billing_case_id: int) -> BillingCase:
    case = db.query(BillingCase).filter(BillingCase.id == int(billing_case_id)).one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return case


def next_doc_number(
    db: Session,
    doc_type: NumberDocType,
    prefix: str = "",
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
) -> str:
    """
    Tenant DB safe counter. Uses SELECT FOR UPDATE.
    Your setup is DB-per-tenant, so NO tenant_id is required.
    """
    now = datetime.utcnow()
    if reset_period == NumberResetPeriod.YEAR:
        period_key = f"{now.year}"
    elif reset_period == NumberResetPeriod.MONTH:
        period_key = f"{now.year}-{now.month:02d}"
    else:
        period_key = None

    row = (
        db.query(BillingNumberSeries)
        .filter(
            BillingNumberSeries.doc_type == doc_type,
            BillingNumberSeries.reset_period == reset_period,
            BillingNumberSeries.prefix == (prefix or ""),
            BillingNumberSeries.is_active == True,
        )
        .with_for_update()
        .one_or_none()
    )

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


def _allocation_tenant_id(db: Session, case: Optional[BillingCase]) -> Optional[int]:
    """
    Optional: keep tenant_id NULL by default (your system doesn't need it).
    If you still want it for analytics, you can set db.info['tenant_id'] in get_db().
    """
    try:
        tid = db.info.get("tenant_id") if hasattr(db, "info") else None
        if tid is not None:
            return int(tid)
    except Exception:
        pass
    try:
        tid2 = getattr(case, "tenant_id", None) if case is not None else None
        return int(tid2) if tid2 is not None else None
    except Exception:
        return None


# ----------------------------
# Insurance Case Upsert
# ----------------------------
def upsert_insurance_case(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingInsuranceCase:
    case = get_or_404_case(db, billing_case_id)
    payload = dict(payload or {})

    ins = (
        db.query(BillingInsuranceCase)
        .filter(BillingInsuranceCase.billing_case_id == int(billing_case_id))
        .one_or_none()
    )

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

    # update simple fields
    for k in ["policy_no", "member_id", "plan_name", "status"]:
        if k in payload and payload[k] is not None:
            setattr(ins, k, payload[k])

    payer_kind_changed = False
    if payload.get("payer_kind") is not None:
        new_pk = payload["payer_kind"]
        if new_pk != ins.payer_kind:
            payer_kind_changed = True
        ins.payer_kind = new_pk

    def _u(v: Any) -> Optional[int]:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except Exception:
            return None

    def _case_type() -> str:
        return str(getattr(case, "default_payer_type", "") or "").strip().upper()

    def _has(*keys: str) -> bool:
        return any(k in payload for k in keys)

    has_ins_key = _has("insurance_company_id", "insurance_id")
    has_tpa_key = _has("tpa_id")
    has_corp_key = _has("corporate_id")

    incoming_insurance_company_id = None
    if "insurance_company_id" in payload:
        incoming_insurance_company_id = _u(payload.get("insurance_company_id"))
    elif "insurance_id" in payload:
        incoming_insurance_company_id = _u(payload.get("insurance_id"))

    insurance_company_id = incoming_insurance_company_id if has_ins_key else _u(getattr(ins, "insurance_company_id", None))
    tpa_id = _u(payload.get("tpa_id")) if has_tpa_key else _u(getattr(ins, "tpa_id", None))
    corporate_id = _u(payload.get("corporate_id")) if has_corp_key else _u(getattr(ins, "corporate_id", None))

    pk = ins.payer_kind or InsurancePayerKind.INSURANCE
    ct = _case_type()

    if pk == InsurancePayerKind.CORPORATE:
        if corporate_id is None and (payer_kind_changed or has_corp_key):
            if ct in ("CORPORATE", "CREDIT_PLAN"):
                corporate_id = _u(getattr(case, "default_payer_id", None))

        ins.corporate_id = corporate_id
        ins.tpa_id = None
        ins.insurance_company_id = None

        if (payer_kind_changed or has_corp_key) and not ins.corporate_id:
            raise HTTPException(status_code=400, detail="Select Corporate")

    elif pk == InsurancePayerKind.TPA:
        if tpa_id is None and (payer_kind_changed or has_tpa_key):
            tpa_id = _u(getattr(case, "default_tpa_id", None))
            if tpa_id is None and ct == "TPA":
                tpa_id = _u(getattr(case, "default_payer_id", None))

        if insurance_company_id is None and (payer_kind_changed or has_ins_key):
            if ct in ("PAYER", "INSURER", "INSURANCE"):
                insurance_company_id = _u(getattr(case, "default_payer_id", None))

        ins.tpa_id = tpa_id
        ins.insurance_company_id = insurance_company_id
        ins.corporate_id = None

        if (payer_kind_changed or has_tpa_key) and not ins.tpa_id:
            raise HTTPException(status_code=400, detail="Select TPA")

    else:
        # INSURANCE
        if insurance_company_id is None and (payer_kind_changed or has_ins_key):
            if ct in ("PAYER", "INSURER", "INSURANCE"):
                insurance_company_id = _u(getattr(case, "default_payer_id", None))

        ins.insurance_company_id = insurance_company_id
        ins.tpa_id = None
        ins.corporate_id = None

        if (payer_kind_changed or has_ins_key) and not ins.insurance_company_id:
            raise HTTPException(status_code=400, detail="Select Insurance Company")

    # approved_limit optional
    if payload.get("approved_limit") is not None:
        ins.approved_limit = _d(payload["approved_limit"])
        ins.approved_at = _now()

    db.flush()
    return ins


# ----------------------------
# Lines view + patch
# ----------------------------
def list_insurance_lines(db: Session, billing_case_id: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    invoices = (
        db.query(BillingInvoice)
        .options(selectinload(BillingInvoice.lines))
        .filter(BillingInvoice.billing_case_id == int(billing_case_id))
        .filter(BillingInvoice.status != DocStatus.VOID)
        .order_by(BillingInvoice.created_at.asc())
        .all()
    )

    for inv in invoices:
        for ln in (inv.lines or []):
            net = _d(getattr(ln, "net_amount", 0))
            insurer = _d(getattr(ln, "insurer_pay_amount", 0))
            insurer = max(D0, min(insurer, net))
            patient = _d(net - insurer)

            rows.append(
                {
                    "invoice_id": int(inv.id),
                    "invoice_number": inv.invoice_number,
                    "module": inv.module,
                    "invoice_status": str(inv.status.value if hasattr(inv.status, "value") else inv.status),
                    "line_id": int(ln.id),
                    "description": ln.description,
                    "service_group": str(ln.service_group.value if hasattr(ln.service_group, "value") else ln.service_group),
                    "net_amount": net,
                    "is_covered": getattr(ln, "is_covered", None),
                    "insurer_pay_amount": insurer,
                    "patient_pay_amount": patient,
                    "requires_preauth": bool(getattr(ln, "requires_preauth", False)),
                }
            )
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

    lines = (
        db.query(BillingInvoiceLine)
        .join(BillingInvoice, BillingInvoice.id == BillingInvoiceLine.invoice_id)
        .filter(BillingInvoice.billing_case_id == int(billing_case_id))
        .filter(BillingInvoice.status != DocStatus.VOID)
        .filter(BillingInvoiceLine.id.in_(line_ids))
        .all()
    )
    by_id = {int(l.id): l for l in lines}

    updated = 0
    for p in patches:
        try:
            lid = int(p.get("line_id") or 0)
        except Exception:
            continue
        if lid <= 0:
            continue

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

        updated += 1

    db.flush()
    _log(db, "BillingCase", int(case.id), "INSURANCE_LINES_PATCH", user_id, reason=f"patched={updated}")
    return updated


# ----------------------------
# Split invoices
# ----------------------------
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
        module=orig.module,
        invoice_type=invoice_type,
        status=DocStatus.DRAFT,
        payer_type=payer_type,
        payer_id=payer_id,
        currency=getattr(orig, "currency", None) or "INR",
        service_date=getattr(orig, "service_date", None),
        meta_json={
            "split_from_invoice_number": orig.invoice_number,
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
    """
    Insurance Split:
    - VOID original invoice(s) (so they won't print)
    - Create PATIENT invoice (patient share)
    - Create INSURER invoice (insurer share) if any insurer amount exists
    - If allow_paid_split=True and original invoice has payments:
        move those payments + allocations that point to the ORIGINAL invoice
        to the new PATIENT invoice, then VOID original
    """

    case = get_or_404_case(db, billing_case_id)

    ins = (
        db.query(BillingInsuranceCase)
        .filter(BillingInsuranceCase.billing_case_id == int(billing_case_id))
        .one_or_none()
    )
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set for this billing case")

    if not invoice_ids:
        raise HTTPException(status_code=400, detail="invoice_ids required")

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

    invoices = (
        db.query(BillingInvoice)
        .options(
            selectinload(BillingInvoice.lines),
            selectinload(BillingInvoice.payments).selectinload(BillingPayment.allocations),
        )
        .filter(BillingInvoice.billing_case_id == int(billing_case_id))
        .filter(BillingInvoice.id.in_([int(x) for x in invoice_ids]))
        .filter(BillingInvoice.status != DocStatus.VOID)
        .all()
    )
    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    def _has_existing_split_for(orig_id: int) -> bool:
        if not hasattr(BillingInvoice, "meta_json"):
            return False
        q = (
            db.query(BillingInvoice.id)
            .filter(BillingInvoice.billing_case_id == int(billing_case_id))
            .filter(BillingInvoice.status != DocStatus.VOID)
            .filter(_json_text(BillingInvoice.meta_json, "$.split_from_invoice_id") == str(int(orig_id)))
        )
        return db.query(q.exists()).scalar() is True

    def _load_invoice_with_lines(inv_id: int) -> BillingInvoice:
        return (
            db.query(BillingInvoice)
            .options(selectinload(BillingInvoice.lines))
            .filter(BillingInvoice.id == int(inv_id))
            .one()
        )

    def _move_payments_and_allocations(orig_inv: BillingInvoice, new_invoice_id: int) -> int:
        moved = 0
        for p in list(orig_inv.payments or []):
            # safety: if payment has allocations to other invoices, do not auto-move
            for a in list(getattr(p, "allocations", []) or []):
                inv_id = getattr(a, "invoice_id", None)
                if inv_id is not None and int(inv_id) != int(orig_inv.id):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Invoice {orig_inv.invoice_number} has a payment with allocations across multiple invoices. "
                            "Cannot auto-move payments. Please unapply allocations first."
                        ),
                    )

            p.invoice_id = int(new_invoice_id)

            for a in list(getattr(p, "allocations", []) or []):
                if getattr(a, "invoice_id", None) == int(orig_inv.id):
                    a.invoice_id = int(new_invoice_id)

            if hasattr(p, "meta_json"):
                mj = dict(p.meta_json or {})
                mj["moved_from_invoice_number"] = orig_inv.invoice_number
                mj["moved_from_invoice_id"] = int(orig_inv.id)
                mj["moved_to_invoice_id"] = int(new_invoice_id)
                mj["moved_at"] = _now().isoformat()
                p.meta_json = mj

            moved += 1

        db.flush()
        return moved

    results: List[Dict[str, Any]] = []

    for orig in invoices:
        if _has_existing_split_for(int(orig.id)):
            raise HTTPException(
                status_code=400,
                detail=f"Invoice {orig.invoice_number} already has split invoices (split_from_invoice_id={int(orig.id)}).",
            )

        paid_payments = list(orig.payments or [])
        if paid_payments and not allow_paid_split:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invoice {orig.invoice_number} has payments, cannot split. "
                    "Refund/unapply payments or use allow_paid_split=true"
                ),
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
                detail="Insurance/TPA payer not selected in Insurance Case (missing payer_id).",
            )

        # create new invoices
        patient_no = next_doc_number(db, NumberDocType.INVOICE, prefix="PINV", reset_period=NumberResetPeriod.YEAR)
        patient_inv = _clone_invoice_base(orig, patient_no, InvoiceType.PATIENT, PayerType.PATIENT, None)

        insurer_inv = None
        if has_insurer:
            insurer_no = next_doc_number(db, NumberDocType.INVOICE, prefix="IINV", reset_period=NumberResetPeriod.YEAR)
            insurer_inv = _clone_invoice_base(orig, insurer_no, InvoiceType.INSURER, insurer_payer_type, insurer_payer_id)

        db.add(patient_inv)
        if insurer_inv:
            db.add(insurer_inv)
        db.flush()

        moved_payments = 0
        if paid_payments and allow_paid_split:
            moved_payments = _move_payments_and_allocations(orig, int(patient_inv.id))
            _log(
                db,
                "BillingInvoice",
                int(orig.id),
                "PAYMENTS_MOVED_TO_PATIENT_INVOICE",
                user_id,
                reason=f"moved_payments={moved_payments} to {patient_inv.invoice_number}",
            )

        # create split lines (scale unit_price so recompute remains consistent)
        for ln in (orig.lines or []):
            net = _d(getattr(ln, "net_amount", 0))
            if net <= 0:
                continue

            ins_amt = _d(getattr(ln, "insurer_pay_amount", 0))
            ins_amt = max(D0, min(ins_amt, net))
            pat_amt = _d(net - ins_amt)

            def _line_exists(target_invoice_id: int, src_line_id: int, bucket: str) -> bool:
                q = (
                    db.query(BillingInvoiceLine.id)
                    .filter(BillingInvoiceLine.invoice_id == int(target_invoice_id))
                    .filter(BillingInvoiceLine.source_module == "INS_SPLIT")
                    .filter(BillingInvoiceLine.source_ref_id == int(src_line_id))
                    .filter(BillingInvoiceLine.source_line_key == bucket)
                )
                return db.query(q.exists()).scalar() is True

            def make_split_line(amount: Decimal, bucket: str) -> Optional[BillingInvoiceLine]:
                if amount <= 0:
                    return None

                ratio = (amount / net) if net > 0 else D0

                if bucket == "PATIENT":
                    target_invoice_id = int(patient_inv.id)
                else:
                    if not insurer_inv:
                        return None
                    target_invoice_id = int(insurer_inv.id)

                if _line_exists(target_invoice_id, int(ln.id), bucket):
                    return None

                unit_price = _d(getattr(ln, "unit_price", 0))
                scaled_unit_price = _d(unit_price * ratio) if ratio > 0 else D0

                return BillingInvoiceLine(
                    billing_case_id=orig.billing_case_id,
                    invoice_id=target_invoice_id,
                    service_group=ln.service_group,
                    item_type=ln.item_type,
                    item_id=ln.item_id,
                    item_code=ln.item_code,
                    description=ln.description,
                    qty=ln.qty,
                    unit_price=scaled_unit_price,
                    discount_percent=ln.discount_percent,
                    discount_amount=_d(_d(getattr(ln, "discount_amount", 0)) * ratio),
                    gst_rate=ln.gst_rate,
                    tax_amount=_d(_d(getattr(ln, "tax_amount", 0)) * ratio),
                    line_total=_d(_d(getattr(ln, "line_total", 0)) * ratio),
                    net_amount=amount,
                    revenue_head_id=ln.revenue_head_id,
                    cost_center_id=ln.cost_center_id,
                    doctor_id=ln.doctor_id,
                    # split trace
                    source_module="INS_SPLIT",
                    source_ref_id=int(ln.id),
                    source_line_key=bucket,
                    is_covered=getattr(ln, "is_covered", None),
                    approved_amount=getattr(ln, "approved_amount", None),
                    patient_pay_amount=pat_amt if bucket == "PATIENT" else D0,
                    insurer_pay_amount=ins_amt if bucket == "INSURER" else D0,
                    requires_preauth=bool(getattr(ln, "requires_preauth", False)),
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

            pl = make_split_line(pat_amt, "PATIENT")
            if pl:
                db.add(pl)

            il = make_split_line(ins_amt, "INSURER")
            if il:
                db.add(il)

        db.flush()

        # recalc totals
        p_loaded = _load_invoice_with_lines(int(patient_inv.id))
        _recalc_invoice_totals(p_loaded)

        if insurer_inv:
            i_loaded = _load_invoice_with_lines(int(insurer_inv.id))
            _recalc_invoice_totals(i_loaded)

        db.flush()

        # paid split safety: moved payments must not exceed patient invoice total
        if moved_payments:
            moved_sum = (
                db.query(func.coalesce(func.sum(BillingPayment.amount), 0))
                .filter(BillingPayment.invoice_id == int(patient_inv.id))
                .filter(BillingPayment.status == ReceiptStatus.ACTIVE)
                .filter(BillingPayment.direction == PaymentDirection.IN)
                .scalar()
                or 0
            )
            moved_sum = _d(moved_sum)
            if moved_sum > _d(p_loaded.grand_total):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot split paid invoice {orig.invoice_number}: "
                        f"moved payment {moved_sum} exceeds patient invoice total {_d(p_loaded.grand_total)}. "
                        "Refund/unapply excess payment first."
                    ),
                )

        # VOID original
        orig.status = DocStatus.VOID
        reason = f"Split into PATIENT:{patient_inv.invoice_number}"
        if insurer_inv:
            reason += f" + INSURER:{insurer_inv.invoice_number}"
        if moved_payments:
            reason += f" (moved_payments={moved_payments})"

        orig.void_reason = reason
        orig.voided_by = user_id
        orig.voided_at = _now()

        _log(db, "BillingInvoice", int(orig.id), "VOID_SPLIT", user_id, reason=reason)

        results.append(
            {
                "from": orig.invoice_number,
                "patient_invoice_number": patient_inv.invoice_number,
                "insurer_invoice_number": insurer_inv.invoice_number if insurer_inv else None,
                "moved_payments": moved_payments,
            }
        )

    return {"split": results}


# ----------------------------
# Preauth
# ----------------------------
def create_preauth(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingPreauthRequest:
    ins = (
        db.query(BillingInsuranceCase)
        .filter(BillingInsuranceCase.billing_case_id == int(billing_case_id))
        .one_or_none()
    )
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    req = _d(payload.get("requested_amount"))
    if req <= 0:
        raise HTTPException(status_code=400, detail="requested_amount must be > 0")

    pr = BillingPreauthRequest(
        insurance_case_id=ins.id,
        approved_amount=D0,
        requested_amount=req,
        remarks=payload.get("remarks"),
        attachments_json=payload.get("attachments_json"),
        created_by=user_id,
        status=PreauthStatus.DRAFT,
    )
    db.add(pr)
    db.flush()

    _log(db, "BillingPreauthRequest", int(pr.id), "CREATE", user_id)
    return pr


def preauth_submit(db: Session, preauth_id: int, user_id: Optional[int]) -> BillingPreauthRequest:
    pr = db.query(BillingPreauthRequest).filter(BillingPreauthRequest.id == int(preauth_id)).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status != PreauthStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only DRAFT can be submitted")

    pr.status = PreauthStatus.SUBMITTED
    pr.submitted_at = _now()
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(BillingInsuranceCase.id == int(pr.insurance_case_id)).one()
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
    pr = db.query(BillingPreauthRequest).filter(BillingPreauthRequest.id == int(preauth_id)).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status != PreauthStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only SUBMITTED can be decided")

    aa = _d(approved_amount)
    if status in (PreauthStatus.APPROVED, PreauthStatus.PARTIAL) and aa <= 0:
        raise HTTPException(status_code=400, detail="approved_amount must be > 0 for APPROVED/PARTIAL")
    if status == PreauthStatus.REJECTED:
        aa = D0

    pr.approved_amount = aa
    pr.remarks = (remarks or "").strip() or pr.remarks
    pr.approved_at = _now()
    pr.status = status
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(BillingInsuranceCase.id == int(pr.insurance_case_id)).one()
    ins.approved_limit = _d(pr.approved_amount)
    ins.approved_at = _now()

    if status == PreauthStatus.APPROVED:
        ins.status = InsuranceStatus.PREAUTH_APPROVED
    elif status == PreauthStatus.PARTIAL:
        ins.status = InsuranceStatus.PREAUTH_PARTIAL
    else:
        ins.status = InsuranceStatus.PREAUTH_REJECTED

    db.flush()
    _log(db, "BillingPreauthRequest", int(pr.id), "DECIDE", user_id, reason=f"{status}")
    return pr


# ----------------------------
# Claim helpers
# ----------------------------
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


def _claim_get_invoice_ids(cl: BillingClaim) -> List[int]:
    j = cl.attachments_json or {}
    ids = (
        j.get("insurer_invoice_ids")
        or j.get("invoice_ids")
        or j.get("invoice_id")         # legacy single
        or j.get("primary_invoice_id") # newer single
    )
    return _norm_int_list(ids)


def _claim_set_invoice_meta(cl: BillingClaim, invoice_ids: List[int], invoices: List[BillingInvoice]) -> None:
    j = dict(cl.attachments_json or {})

    j["insurer_invoice_ids"] = [int(x) for x in invoice_ids]
    j["insurer_invoice_numbers"] = [i.invoice_number for i in invoices]

    # legacy multi
    j["invoice_ids"] = [int(x) for x in invoice_ids]
    j["invoice_numbers"] = [i.invoice_number for i in invoices]

    # legacy + single
    if invoices:
        j["primary_invoice_id"] = int(invoices[0].id)
        j["primary_invoice_number"] = invoices[0].invoice_number
        j["invoice_id"] = int(invoices[0].id)
        j["invoice_number"] = invoices[0].invoice_number

    cl.attachments_json = j or None


def _insurer_payer(ins: BillingInsuranceCase) -> Tuple[PayerType, Optional[int]]:
    if ins.payer_kind == InsurancePayerKind.CORPORATE:
        return (PayerType.CORPORATE, int(ins.corporate_id or 0) or None)
    if ins.payer_kind == InsurancePayerKind.TPA:
        return (PayerType.TPA, int(ins.tpa_id or 0) or None)
    return (PayerType.INSURER, int(ins.insurance_company_id or 0) or None)


def _invoice_paid_amount(db: Session, invoice_id: int) -> Decimal:
    """
    Accurate paid:
    - allocations sum (active)
    + legacy payments on invoice_id that have NO allocations at all
    """
    alloc_sum = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.amount), 0))
        .filter(BillingPaymentAllocation.invoice_id == int(invoice_id))
        .filter(BillingPaymentAllocation.status == ReceiptStatus.ACTIVE)
        .scalar()
        or 0
    )
    alloc_sum = _d(alloc_sum)

    # payments with no allocations (legacy)
    pay_sum = (
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0))
        .outerjoin(
            BillingPaymentAllocation,
            and_(
                BillingPaymentAllocation.payment_id == BillingPayment.id,
                BillingPaymentAllocation.status == ReceiptStatus.ACTIVE,
            ),
        )
        .filter(BillingPayment.invoice_id == int(invoice_id))
        .filter(BillingPayment.status == ReceiptStatus.ACTIVE)
        .filter(BillingPayment.direction == PaymentDirection.IN)
        .filter(BillingPaymentAllocation.id.is_(None))
        .scalar()
        or 0
    )
    pay_sum = _d(pay_sum)

    return _d(alloc_sum + pay_sum)


def _allocate_payment_to_invoices(
    db: Session,
    billing_case_id: int,
    payment: BillingPayment,
    invoices: List[BillingInvoice],
    total_amount: Decimal,
    tenant_id: Optional[int],
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

        a = BillingPaymentAllocation(
            tenant_id=tenant_id,  # ok if None
            billing_case_id=int(billing_case_id),
            payment_id=int(payment.id),
            invoice_id=int(inv.id),
            payer_bucket=payment.payer_type,
            amount=_d(amt),
            status=ReceiptStatus.ACTIVE,
            allocated_by=None,
        )
        db.add(a)
        db.flush()

        out.append(
            {
                "invoice_id": int(inv.id),
                "invoice_number": inv.invoice_number,
                "allocated": str(_d(amt)),
                "outstanding_before": str(outstanding),
            }
        )

        remaining = _d(remaining - amt)

    return out


def _pick_default_insurer_invoices(db: Session, billing_case_id: int) -> List[BillingInvoice]:
    return (
        db.query(BillingInvoice)
        .filter(BillingInvoice.billing_case_id == int(billing_case_id))
        .filter(BillingInvoice.status != DocStatus.VOID)
        .filter(BillingInvoice.invoice_type == InvoiceType.INSURER)
        .filter(BillingInvoice.status.in_([DocStatus.APPROVED, DocStatus.POSTED]))
        .order_by(BillingInvoice.created_at.asc())
        .all()
    )


# ----------------------------
# Claim create / submit / settle
# ----------------------------
def create_claim(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingClaim:
    ins = (
        db.query(BillingInsuranceCase)
        .filter(BillingInsuranceCase.billing_case_id == int(billing_case_id))
        .one_or_none()
    )
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    invoice_ids = _norm_int_list(payload.get("insurer_invoice_ids"))
    claim_amt = _d(payload.get("claim_amount"))

    invoices: List[BillingInvoice] = []
    if invoice_ids:
        invoices = (
            db.query(BillingInvoice)
            .filter(BillingInvoice.billing_case_id == int(billing_case_id))
            .filter(BillingInvoice.id.in_(invoice_ids))
            .filter(BillingInvoice.status != DocStatus.VOID)
            .filter(BillingInvoice.invoice_type == InvoiceType.INSURER)
            .order_by(BillingInvoice.created_at.asc())
            .all()
        )
        if len(invoices) != len(set(invoice_ids)):
            found = {int(i.id) for i in invoices}
            missing = [i for i in invoice_ids if i not in found]
            raise HTTPException(status_code=400, detail=f"Invalid insurer_invoice_ids (missing/invalid): {missing}")

        claim_amt = sum((_d(i.grand_total) for i in invoices), D0)

    if claim_amt <= 0:
        raise HTTPException(status_code=400, detail="claim_amount must be > 0 (or select insurer_invoice_ids)")

    attachments = dict(payload.get("attachments_json") or {})

    cl = BillingClaim(
        insurance_case_id=ins.id,
        approved_amount=D0,
        settled_amount=D0,
        claim_amount=claim_amt,
        remarks=payload.get("remarks"),
        attachments_json=attachments or None,
        created_by=user_id,
        status=ClaimStatus.DRAFT,
    )
    db.add(cl)
    db.flush()

    if invoice_ids:
        _claim_set_invoice_meta(cl, invoice_ids, invoices)
        db.flush()

    _log(db, "BillingClaim", int(cl.id), "CREATE", user_id)
    return cl


def claim_submit(db: Session, claim_id: int, user_id: Optional[int]) -> BillingClaim:
    cl = db.query(BillingClaim).filter(BillingClaim.id == int(claim_id)).one_or_none()
    if not cl:
        raise HTTPException(status_code=404, detail="Claim not found")
    if cl.status != ClaimStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only DRAFT can be submitted")

    ins = db.query(BillingInsuranceCase).filter(BillingInsuranceCase.id == int(cl.insurance_case_id)).one_or_none()
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case missing for this claim")

    billing_case_id = int(ins.billing_case_id)

    invoice_ids = _claim_get_invoice_ids(cl)

    invoices: List[BillingInvoice] = []
    if not invoice_ids:
        invoices = _pick_default_insurer_invoices(db, billing_case_id)
        invoice_ids = [int(i.id) for i in invoices]

    if not invoice_ids:
        raise HTTPException(
            status_code=400,
            detail="No claimable INSURER invoices found. Split invoices first and ensure INSURER invoices are APPROVED/POSTED.",
        )

    invoices = (
        db.query(BillingInvoice)
        .filter(BillingInvoice.billing_case_id == int(billing_case_id))
        .filter(BillingInvoice.id.in_(invoice_ids))
        .filter(BillingInvoice.status != DocStatus.VOID)
        .filter(BillingInvoice.invoice_type == InvoiceType.INSURER)
        .filter(BillingInvoice.status.in_([DocStatus.APPROVED, DocStatus.POSTED]))
        .order_by(BillingInvoice.created_at.asc())
        .all()
    )
    found = {int(i.id) for i in invoices}
    missing = [i for i in invoice_ids if int(i) not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Invalid insurer_invoice_ids (missing/not claimable): {missing}")

    # prevent overlap with other active claims (python-level)
    other_claims = (
        db.query(BillingClaim)
        .filter(BillingClaim.insurance_case_id == int(ins.id))
        .filter(BillingClaim.id != int(cl.id))
        .filter(BillingClaim.status.in_([ClaimStatus.SUBMITTED, ClaimStatus.APPROVED, ClaimStatus.UNDER_QUERY]))
        .all()
    )
    current_set = set(found)
    for oc in other_claims:
        oset = set(_claim_get_invoice_ids(oc))
        overlap = sorted(list(current_set.intersection(oset)))
        if overlap:
            raise HTTPException(
                status_code=400,
                detail=f"Some insurer invoices are already linked to another active claim (claim_id={int(oc.id)}): {overlap}",
            )

    _claim_set_invoice_meta(cl, [int(i.id) for i in invoices], invoices)
    cl.claim_amount = sum((_d(i.grand_total) for i in invoices), D0)

    cl.status = ClaimStatus.SUBMITTED
    cl.submitted_at = _now()
    db.flush()

    ins.status = InsuranceStatus.CLAIM_SUBMITTED
    db.flush()

    _log(db, "BillingClaim", int(cl.id), "SUBMIT", user_id, reason=f"invoices={sorted(list(found))}")
    return cl


def claim_settle(
    db: Session,
    claim_id: int,
    approved_amount: Decimal,
    settled_amount: Decimal,
    status: ClaimStatus,
    remarks: str,
    user_id: Optional[int],
) -> BillingClaim:
    cl = db.query(BillingClaim).filter(BillingClaim.id == int(claim_id)).one_or_none()
    if not cl:
        raise HTTPException(status_code=404, detail="Claim not found")

    ins = db.query(BillingInsuranceCase).filter(BillingInsuranceCase.id == int(cl.insurance_case_id)).one_or_none()
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case missing for this claim")

    case = get_or_404_case(db, int(ins.billing_case_id))
    alloc_tenant_id = _allocation_tenant_id(db, case)

    # transition guard
    if status == ClaimStatus.SETTLED and cl.status not in (ClaimStatus.SUBMITTED, ClaimStatus.APPROVED, ClaimStatus.UNDER_QUERY):
        raise HTTPException(status_code=400, detail="Only SUBMITTED/APPROVED/UNDER_QUERY can be SETTLED")

    aa = _d(approved_amount)
    sa = _d(settled_amount)

    if status in (ClaimStatus.DENIED, ClaimStatus.UNDER_QUERY) and sa != D0:
        raise HTTPException(status_code=400, detail="settled_amount must be 0 for DENIED/UNDER_QUERY")

    if status == ClaimStatus.SETTLED and sa <= 0:
        raise HTTPException(status_code=400, detail="settled_amount must be > 0 for SETTLED")

    if aa and sa and sa > aa:
        raise HTTPException(status_code=400, detail="settled_amount cannot exceed approved_amount")

    cl.approved_amount = aa
    cl.settled_amount = sa
    cl.remarks = (remarks or "").strip() or cl.remarks

    if status == ClaimStatus.UNDER_QUERY:
        cl.status = ClaimStatus.UNDER_QUERY
        ins.status = InsuranceStatus.QUERY
    elif status == ClaimStatus.DENIED:
        cl.status = ClaimStatus.DENIED
        ins.status = InsuranceStatus.DENIED
    elif status == ClaimStatus.APPROVED:
        cl.status = ClaimStatus.APPROVED
    elif status == ClaimStatus.SETTLED:
        cl.status = ClaimStatus.SETTLED
        cl.settled_at = _now()
        ins.status = InsuranceStatus.SETTLED
    else:
        cl.status = status

    db.flush()

    allocations_info: List[Dict[str, Any]] = []

    if cl.status == ClaimStatus.SETTLED and _d(cl.settled_amount) > 0:
        billing_case_id = int(ins.billing_case_id)

        invoice_ids = _claim_get_invoice_ids(cl)
        invoices: List[BillingInvoice] = []
        if invoice_ids:
            invoices = (
                db.query(BillingInvoice)
                .filter(BillingInvoice.billing_case_id == int(billing_case_id))
                .filter(BillingInvoice.id.in_(invoice_ids))
                .filter(BillingInvoice.status != DocStatus.VOID)
                .filter(BillingInvoice.invoice_type == InvoiceType.INSURER)
                .order_by(BillingInvoice.created_at.asc())
                .all()
            )
        else:
            invoices = _pick_default_insurer_invoices(db, billing_case_id)
            invoice_ids = [int(i.id) for i in invoices]

        if not invoices:
            raise HTTPException(status_code=400, detail="No INSURER invoices found to allocate settlement.")

        # ensure claim has proper meta (legacy + new keys)
        _claim_set_invoice_meta(cl, [int(i.id) for i in invoices], invoices)
        db.flush()

        payer_type, payer_id = _insurer_payer(ins)
        if not payer_id:
            raise HTTPException(status_code=400, detail="Insurance/TPA/Corporate payer is missing in Insurance Case.")

        # block overpayment (no credit/advance creation here)
        total_outstanding = D0
        for inv in invoices:
            paid = _invoice_paid_amount(db, int(inv.id))
            grand = _d(getattr(inv, "grand_total", 0))
            total_outstanding += max(D0, _d(grand - paid))
        if _d(cl.settled_amount) > _d(total_outstanding):
            raise HTTPException(
                status_code=400,
                detail=f"Settlement amount { _d(cl.settled_amount) } exceeds total outstanding { _d(total_outstanding) }. Adjust settled_amount.",
            )

        # idempotency: reuse existing payment for this claim if already created
        pay = None
        if hasattr(BillingPayment, "meta_json"):
            pay = (
                db.query(BillingPayment)
                .filter(BillingPayment.billing_case_id == int(billing_case_id))
                .filter(BillingPayment.status == ReceiptStatus.ACTIVE)
                .filter(BillingPayment.kind == PaymentKind.RECEIPT)
                .filter(BillingPayment.direction == PaymentDirection.IN)
                .filter(_json_text(BillingPayment.meta_json, "$.source") == "CLAIM_SETTLEMENT")
                .filter(_json_text(BillingPayment.meta_json, "$.claim_id") == str(int(cl.id)))
                .one_or_none()
            )

        if not pay:
            receipt_no = next_doc_number(db, NumberDocType.RECEIPT, prefix="IRCP", reset_period=NumberResetPeriod.YEAR)
            primary_invoice_id = int(invoices[0].id)

            pay = BillingPayment(
                billing_case_id=billing_case_id,
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
                    "source": "CLAIM_SETTLEMENT",
                    "claim_id": int(cl.id),
                    "claim_ref_no": _ref("CL", int(cl.id)),
                    "insurer_invoice_ids": [int(i.id) for i in invoices],
                    "insurer_invoice_numbers": [i.invoice_number for i in invoices],
                },
            )
            db.add(pay)
            db.flush()

            _log(
                db,
                "BillingPayment",
                int(pay.id),
                "CREATE_FROM_CLAIM_SETTLEMENT",
                user_id,
                reason=f"claim_id={int(cl.id)}",
            )

        # allocate only if allocations not already present
        alloc_cnt = (
            db.query(func.count(BillingPaymentAllocation.id))
            .filter(BillingPaymentAllocation.payment_id == int(pay.id))
            .filter(BillingPaymentAllocation.status == ReceiptStatus.ACTIVE)
            .scalar()
            or 0
        )
        if int(alloc_cnt) == 0:
            allocations_info = _allocate_payment_to_invoices(
                db=db,
                billing_case_id=billing_case_id,
                payment=pay,
                invoices=invoices,
                total_amount=_d(cl.settled_amount),
                tenant_id=alloc_tenant_id,
            )
            db.flush()

            _log(
                db,
                "BillingPayment",
                int(pay.id),
                "ALLOCATED_TO_INVOICES",
                user_id,
                reason=f"allocations={len(allocations_info)}",
            )
        else:
            allocations_info = [{"note": "allocations already exist", "count": int(alloc_cnt)}]

    _log(
        db,
        "BillingClaim",
        int(cl.id),
        "UPDATE_STATUS",
        user_id,
        reason=f"{cl.status} allocations={len(allocations_info)}",
    )
    return cl
