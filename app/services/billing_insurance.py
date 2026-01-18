# FILE: app/services/billing_insurance.py
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func
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
    CoverageFlag,
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


def _log(db: Session,
         entity_type: str,
         entity_id: int,
         action: str,
         user_id: Optional[int],
         old: Any = None,
         new: Any = None,
         reason: str = ""):
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
    # period key
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

    # reset if period changed
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
        BillingCase.id == billing_case_id).one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return case


def upsert_insurance_case(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingInsuranceCase:
    case = get_or_404_case(db, billing_case_id)

    payload = dict(payload or {})  # ensure dict

    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())

    if not ins:
        ins = BillingInsuranceCase(
            billing_case_id=billing_case_id,
            created_by=user_id,
            status=InsuranceStatus.INITIATED,
            payer_kind=InsurancePayerKind.INSURANCE,
        )
        db.add(ins)
        db.flush()
        _log(db, "BillingInsuranceCase", int(ins.id), "CREATE", user_id)

    # ---- update non-ID fields first ----
    for k in ["policy_no", "member_id", "plan_name", "status"]:
        if k in payload and payload[k] is not None:
            setattr(ins, k, payload[k])

    # payer_kind update (if provided)
    payer_kind_changed = False
    if payload.get("payer_kind") is not None:
        new_pk = payload["payer_kind"]
        if new_pk != ins.payer_kind:
            payer_kind_changed = True
        ins.payer_kind = new_pk

    # ---- helpers ----
    def _u(v: Any) -> Optional[int]:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except Exception:
            return None

    def _case_type() -> str:
        return str(case.default_payer_type or "").strip().upper()

    def _has(*keys: str) -> bool:
        return any(k in payload for k in keys)

    # ✅ treat "not provided" differently from "provided as null"
    has_ins_key = _has("insurance_company_id", "insurance_id")
    has_tpa_key = _has("tpa_id")
    has_corp_key = _has("corporate_id")

    incoming_insurance_company_id = None
    if "insurance_company_id" in payload:
        incoming_insurance_company_id = _u(payload.get("insurance_company_id"))
    elif "insurance_id" in payload:
        incoming_insurance_company_id = _u(payload.get("insurance_id"))

    insurance_company_id = (incoming_insurance_company_id
                            if has_ins_key else _u(ins.insurance_company_id))
    tpa_id = (_u(payload.get("tpa_id")) if has_tpa_key else _u(ins.tpa_id))
    corporate_id = (_u(payload.get("corporate_id"))
                    if has_corp_key else _u(ins.corporate_id))

    pk = ins.payer_kind or InsurancePayerKind.INSURANCE
    ct = _case_type()

    # ---- apply IDs (with safe defaults) ----
    if pk == InsurancePayerKind.CORPORATE:
        # prefer explicit corporate_id; fallback to case defaults
        if corporate_id is None and (payer_kind_changed or has_corp_key):
            if ct in ("CORPORATE", "CREDIT_PLAN"):
                corporate_id = _u(case.default_payer_id)

        ins.corporate_id = corporate_id
        ins.tpa_id = None
        ins.insurance_company_id = None

        # strict validate only when user is actively setting payer_kind/corporate_id
        if (payer_kind_changed or has_corp_key) and not ins.corporate_id:
            raise HTTPException(status_code=400, detail="Select Corporate")

    elif pk == InsurancePayerKind.TPA:
        # TPA required; insurance_company optional
        if tpa_id is None and (payer_kind_changed or has_tpa_key):
            tpa_id = _u(case.default_tpa_id)
            if tpa_id is None and ct == "TPA":
                tpa_id = _u(case.default_payer_id)

        # only fill insurance company default if still None AND user is actively setting
        if insurance_company_id is None and (payer_kind_changed
                                             or has_ins_key):
            if ct in ("PAYER", "INSURER", "INSURANCE"):
                insurance_company_id = _u(case.default_payer_id)

        ins.tpa_id = tpa_id
        ins.insurance_company_id = insurance_company_id
        ins.corporate_id = None

        if (payer_kind_changed or has_tpa_key) and not ins.tpa_id:
            raise HTTPException(status_code=400, detail="Select TPA")

    else:
        # INSURANCE
        if insurance_company_id is None and (payer_kind_changed
                                             or has_ins_key):
            if ct in ("PAYER", "INSURER", "INSURANCE"):
                insurance_company_id = _u(case.default_payer_id)

        ins.insurance_company_id = insurance_company_id
        ins.tpa_id = None
        ins.corporate_id = None

        if (payer_kind_changed
                or has_ins_key) and not ins.insurance_company_id:
            raise HTTPException(status_code=400,
                                detail="Select Insurance Company")

    # approved_limit (optional)
    if payload.get("approved_limit") is not None:
        ins.approved_limit = _d(payload["approved_limit"])
        ins.approved_at = _now()

    db.flush()
    return ins


def list_insurance_lines(db: Session,
                         billing_case_id: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    invoices = (db.query(BillingInvoice).options(
        selectinload(BillingInvoice.lines)).filter(
            BillingInvoice.billing_case_id == billing_case_id).filter(
                BillingInvoice.status != DocStatus.VOID).order_by(
                    BillingInvoice.created_at.asc()).all())

    for inv in invoices:
        for ln in inv.lines:
            net = _d(ln.net_amount)
            insurer = _d(ln.insurer_pay_amount)
            insurer = max(D0, min(insurer, net))
            patient = _d(net - insurer)

            rows.append({
                "invoice_id":
                int(inv.id),
                "invoice_number":
                inv.invoice_number,
                "module":
                inv.module,
                "invoice_status":
                str(inv.status.value if hasattr(inv.status, "value") else inv.
                    status),
                "line_id":
                int(ln.id),
                "description":
                ln.description,
                "service_group":
                str(ln.service_group.value if hasattr(ln.service_group, "value"
                                                      ) else ln.service_group),
                "net_amount":
                net,
                "is_covered":
                ln.is_covered,
                "insurer_pay_amount":
                insurer,
                "patient_pay_amount":
                patient,
                "requires_preauth":
                bool(ln.requires_preauth),
            })
    return rows


def patch_insurance_lines(db: Session, billing_case_id: int,
                          patches: List[Dict[str, Any]],
                          user_id: Optional[int]) -> int:
    case = get_or_404_case(db, billing_case_id)

    line_ids = [int(p["line_id"]) for p in patches if p.get("line_id")]
    if not line_ids:
        return 0

    lines = (db.query(BillingInvoiceLine).join(
        BillingInvoice,
        BillingInvoice.id == BillingInvoiceLine.invoice_id).filter(
            BillingInvoice.billing_case_id == billing_case_id).filter(
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
            net = _d(ln.net_amount)
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


def _recalc_invoice_totals(inv: BillingInvoice) -> None:
    sub_total = D0
    disc = D0
    tax = D0
    grand = D0
    for ln in inv.lines:
        sub_total += _d(ln.line_total)
        disc += _d(ln.discount_amount)
        tax += _d(ln.tax_amount)
        grand += _d(ln.net_amount)

    inv.sub_total = sub_total
    inv.discount_total = disc
    inv.tax_total = tax
    inv.round_off = D0
    inv.grand_total = grand


def _clone_invoice_base(orig: BillingInvoice, new_number: str,
                        invoice_type: InvoiceType, payer_type: PayerType,
                        payer_id: Optional[int]) -> BillingInvoice:
    return BillingInvoice(
        billing_case_id=orig.billing_case_id,
        invoice_number=new_number,
        module=orig.module,
        invoice_type=invoice_type,
        status=DocStatus.DRAFT,
        payer_type=payer_type,
        payer_id=payer_id,
        currency=orig.currency or "INR",
        service_date=orig.service_date,
        meta_json={
            "split_from_invoice_number": orig.invoice_number,
            "split_from_invoice_id": int(orig.id),
        },
    )


D0 = Decimal("0.00")


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
        move those payments + allocations to the new PATIENT invoice, then VOID original
    """

    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
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
    ).filter(BillingInvoice.billing_case_id == billing_case_id).filter(
        BillingInvoice.id.in_(invoice_ids)).filter(
            BillingInvoice.status != DocStatus.VOID).all())
    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    # ✅ determine insurer payer (matches your PayerType enum: PATIENT/INSURER/CORPORATE)
    if ins.payer_kind == InsurancePayerKind.CORPORATE:
        insurer_payer_type = PayerType.CORPORATE
        insurer_payer_id = ins.corporate_id

    elif ins.payer_kind == InsurancePayerKind.TPA:
        insurer_payer_type = PayerType.TPA
        insurer_payer_id = ins.tpa_id

    else:
        insurer_payer_type = PayerType.INSURER
        insurer_payer_id = ins.insurance_company_id

    def _load_invoice_with_lines(inv_id: int) -> BillingInvoice:
        return (db.query(BillingInvoice).options(
            selectinload(BillingInvoice.lines)).filter(
                BillingInvoice.id == int(inv_id)).one())

    def _move_payments_and_allocations(orig_inv: BillingInvoice,
                                       new_invoice_id: int) -> int:
        """
        Move linked payments from orig invoice -> new PATIENT invoice.
        Also moves allocations pointing to orig invoice -> new invoice.
        Your model uses BillingPayment.invoice_id (exists), so no mismatches.
        """
        moved = 0
        for p in list(orig_inv.payments or []):
            # ✅ your BillingPayment has invoice_id
            p.invoice_id = int(new_invoice_id)

            # ✅ move allocations if they were attached to the original invoice
            for a in list(getattr(p, "allocations", []) or []):
                if getattr(a, "invoice_id", None) == int(orig_inv.id):
                    a.invoice_id = int(new_invoice_id)

            # trace
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
        paid_payments = list(orig.payments or [])
        if paid_payments and not allow_paid_split:
            raise HTTPException(
                status_code=400,
                detail=
                (f"Invoice {orig.invoice_number} has payments, cannot split. "
                 "Refund/unapply payments or use allow_paid_split=true"),
            )

        # compute per-line shares
        has_insurer = False
        for ln in orig.lines:
            net = _d(ln.net_amount)
            ins_amt = _d(getattr(ln, "insurer_pay_amount", None))
            ins_amt = max(D0, min(ins_amt, net))
            if ins_amt > 0:
                has_insurer = True
            ln.patient_pay_amount = _d(net - ins_amt)

        # if insurer share exists, ensure payer is configured
        if has_insurer and not insurer_payer_id and insurer_payer_type != PayerType.CORPORATE:
            raise HTTPException(
                status_code=400,
                detail=
                "Insurance/TPA payer not selected in Insurance Case (missing payer_id).",
            )

        # create new invoices
        patient_no = next_doc_number(
            db,
            NumberDocType.INVOICE,
            prefix="PINV",
            reset_period=NumberResetPeriod.YEAR,
        )
        patient_inv = _clone_invoice_base(
            orig,
            patient_no,
            InvoiceType.PATIENT,
            PayerType.PATIENT,
            None,
        )

        insurer_inv = None
        if has_insurer:
            insurer_no = next_doc_number(
                db,
                NumberDocType.INVOICE,
                prefix="IINV",
                reset_period=NumberResetPeriod.YEAR,
            )
            insurer_inv = _clone_invoice_base(
                orig,
                insurer_no,
                InvoiceType.INSURER,
                insurer_payer_type,
                insurer_payer_id,
            )

        db.add(patient_inv)
        if insurer_inv:
            db.add(insurer_inv)
        db.flush()  # assigns ids

        # ✅ move payments (force split)
        moved_payments = 0
        if paid_payments and allow_paid_split:
            moved_payments = _move_payments_and_allocations(
                orig, int(patient_inv.id))
            _log(
                db,
                "BillingInvoice",
                int(orig.id),
                "PAYMENTS_MOVED_TO_PATIENT_INVOICE",
                user_id,
                reason=
                f"moved_payments={moved_payments} to {patient_inv.invoice_number}",
            )

        # create split lines (idempotency safe with INS_SPLIT + source_line_key=bucket)
        for ln in orig.lines:
            net = _d(ln.net_amount)
            if net <= 0:
                continue

            ins_amt = _d(getattr(ln, "insurer_pay_amount", None))
            ins_amt = max(D0, min(ins_amt, net))
            pat_amt = _d(net - ins_amt)

            def make_split_line(amount: Decimal,
                                bucket: str) -> Optional[BillingInvoiceLine]:
                if amount <= 0:
                    return None

                ratio = (amount / net) if net > 0 else D0
                if bucket == "PATIENT":
                    target_invoice_id = int(patient_inv.id)
                else:
                    # bucket == INSURER
                    if not insurer_inv:
                        return None
                    target_invoice_id = int(insurer_inv.id)

                return BillingInvoiceLine(
                    billing_case_id=orig.billing_case_id,
                    invoice_id=target_invoice_id,
                    service_group=ln.service_group,
                    item_type=ln.item_type,
                    item_id=ln.item_id,
                    item_code=ln.item_code,
                    description=ln.description,
                    qty=ln.qty,
                    unit_price=ln.unit_price,
                    discount_percent=ln.discount_percent,
                    discount_amount=_d(_d(ln.discount_amount) * ratio),
                    gst_rate=ln.gst_rate,
                    tax_amount=_d(_d(ln.tax_amount) * ratio),
                    line_total=_d(_d(ln.line_total) * ratio),
                    net_amount=amount,
                    revenue_head_id=ln.revenue_head_id,
                    cost_center_id=ln.cost_center_id,
                    doctor_id=ln.doctor_id,

                    # split trace
                    source_module="INS_SPLIT",
                    source_ref_id=int(ln.id),
                    source_line_key=bucket,
                    is_covered=ln.is_covered,
                    approved_amount=ln.approved_amount,
                    patient_pay_amount=pat_amt if bucket == "PATIENT" else D0,
                    insurer_pay_amount=ins_amt if bucket == "INSURER" else D0,
                    requires_preauth=bool(ln.requires_preauth),
                    is_manual=True,
                    manual_reason=f"Split from {orig.invoice_number}",
                    meta_json={
                        "orig_invoice_number": orig.invoice_number,
                        "orig_invoice_id": int(orig.id),
                        "orig_line_id": int(ln.id),
                        "orig_net_amount": str(net),
                        "bucket": bucket,
                        "bucket_amount": str(amount),
                        "orig_meta": ln.meta_json or None,
                    },
                )

            pl = make_split_line(pat_amt, "PATIENT")
            if pl:
                db.add(pl)

            il = make_split_line(ins_amt, "INSURER")
            if il:
                db.add(il)

        db.flush()

        # recalc totals on new invoices
        p_loaded = _load_invoice_with_lines(int(patient_inv.id))
        _recalc_invoice_totals(p_loaded)

        if insurer_inv:
            i_loaded = _load_invoice_with_lines(int(insurer_inv.id))
            _recalc_invoice_totals(i_loaded)

        db.flush()

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


def create_preauth(db: Session, billing_case_id: int, payload: Dict[str, Any],
                   user_id: Optional[int]) -> BillingPreauthRequest:
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    pr = BillingPreauthRequest(
        insurance_case_id=ins.id,
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
        BillingPreauthRequest.id == preauth_id).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status not in [PreauthStatus.DRAFT]:
        raise HTTPException(status_code=400,
                            detail="Only DRAFT can be submitted")

    pr.status = PreauthStatus.SUBMITTED
    pr.submitted_at = _now()
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == pr.insurance_case_id).one()
    ins.status = InsuranceStatus.PREAUTH_SUBMITTED
    db.flush()

    _log(db, "BillingPreauthRequest", int(pr.id), "SUBMIT", user_id)
    return pr


def preauth_approve(db: Session, preauth_id: int, approved_amount: Decimal,
                    status: PreauthStatus, remarks: str,
                    user_id: Optional[int]) -> BillingPreauthRequest:
    pr = db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.id == preauth_id).one_or_none()
    if not pr:
        raise HTTPException(status_code=404, detail="Preauth not found")
    if pr.status not in [PreauthStatus.SUBMITTED]:
        raise HTTPException(status_code=400,
                            detail="Only SUBMITTED can be decided")

    pr.approved_amount = _d(approved_amount)
    pr.remarks = remarks or pr.remarks
    pr.approved_at = _now()
    pr.status = status
    db.flush()

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == pr.insurance_case_id).one()
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
         reason=f"{status}")
    return pr


def _norm_int_list(v: Any) -> List[int]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            try:
                xi = int(x)
                if xi > 0:
                    out.append(xi)
            except Exception:
                continue
        return list(dict.fromkeys(out))  # unique preserve order
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",")]
        out = []
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
        j.get("insurer_invoice_ids") or j.get("invoice_ids")
        or j.get("invoice_id")  # legacy single
        or j.get("primary_invoice_id")  # newer single
    )
    return _norm_int_list(ids)


def _claim_set_invoice_meta(cl: BillingClaim, invoice_ids: List[int],
                            invoices: List[BillingInvoice]) -> None:
    j = dict(cl.attachments_json or {})

    # new keys
    j["insurer_invoice_ids"] = [int(x) for x in invoice_ids]
    j["insurer_invoice_numbers"] = [i.invoice_number for i in invoices]

    # legacy multi
    j["invoice_ids"] = [int(x) for x in invoice_ids]
    j["invoice_numbers"] = [i.invoice_number for i in invoices]

    # legacy + single
    if invoices:
        j["primary_invoice_id"] = int(invoices[0].id)
        j["primary_invoice_number"] = invoices[0].invoice_number

        # ⚠️ important for old claim-packager
        j["invoice_id"] = int(invoices[0].id)
        j["invoice_number"] = invoices[0].invoice_number

    cl.attachments_json = j or None


def _insurer_payer(
        ins: BillingInsuranceCase) -> Tuple[PayerType, Optional[int]]:
    if ins.payer_kind == InsurancePayerKind.CORPORATE:
        return (PayerType.CORPORATE, int(ins.corporate_id or 0) or None)
    if ins.payer_kind == InsurancePayerKind.TPA:
        return (PayerType.TPA, int(ins.tpa_id or 0) or None)
    return (PayerType.INSURER, int(ins.insurance_company_id or 0) or None)


def _invoice_paid_amount(db: Session, invoice_id: int) -> Decimal:
    """
    Prevent double counting:
    - If allocations exist, trust allocations
    - Else fallback to payments directly on invoice_id
    """
    alloc_sum = db.query(
        func.coalesce(func.sum(BillingPaymentAllocation.amount), 0)).filter(
            BillingPaymentAllocation.invoice_id == int(invoice_id),
            BillingPaymentAllocation.status == ReceiptStatus.ACTIVE,
        ).scalar() or 0

    alloc_sum = _d(alloc_sum)
    if alloc_sum > 0:
        return alloc_sum

    pay_sum = db.query(func.coalesce(func.sum(
        BillingPayment.amount), 0)).filter(
            BillingPayment.invoice_id == int(invoice_id),
            BillingPayment.status == ReceiptStatus.ACTIVE,
            BillingPayment.direction == PaymentDirection.IN,
        ).scalar() or 0

    return _d(pay_sum)


def _allocate_payment_to_invoices(
    db: Session,
    billing_case_id: int,
    payment: BillingPayment,
    invoices: List[BillingInvoice],
    total_amount: Decimal,
) -> List[Dict[str, Any]]:
    """
    Allocate sequentially to invoice outstanding (oldest first).
    Creates BillingPaymentAllocation rows.
    """
    remaining = _d(total_amount)
    out: List[Dict[str, Any]] = []

    for inv in invoices:
        if remaining <= 0:
            break

        paid = _invoice_paid_amount(db, int(inv.id))
        grand = _d(inv.grand_total)
        outstanding = max(D0, _d(grand - paid))
        if outstanding <= 0:
            continue

        amt = min(remaining, outstanding)
        if amt <= 0:
            continue

        a = BillingPaymentAllocation(
            tenant_id=None,
            billing_case_id=int(billing_case_id),
            payment_id=int(payment.id),
            invoice_id=int(inv.id),
            payer_bucket=payment.payer_type,  # who paid
            amount=_d(amt),
            status=ReceiptStatus.ACTIVE,
            allocated_by=None,
        )
        db.add(a)
        db.flush()

        out.append({
            "invoice_id": int(inv.id),
            "invoice_number": inv.invoice_number,
            "allocated": str(_d(amt)),
            "outstanding_before": str(outstanding),
        })

        remaining = _d(remaining - amt)

    return out


def _pick_default_insurer_invoices(
        db: Session, billing_case_id: int) -> List[BillingInvoice]:
    # only claimable insurer invoices
    return (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id)).filter(
            BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).filter(
                    BillingInvoice.status.in_([
                        DocStatus.APPROVED, DocStatus.POSTED
                    ])).order_by(BillingInvoice.created_at.asc()).all())


def _list_case_insurer_invoices(db: Session,
                                billing_case_id: int) -> List[BillingInvoice]:
    return (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == billing_case_id).filter(
            BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).order_by(
                    BillingInvoice.created_at.asc()).all())


def create_claim(
    db: Session,
    billing_case_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
) -> BillingClaim:
    ins = (db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == billing_case_id).one_or_none())
    if not ins:
        raise HTTPException(status_code=400, detail="Insurance case not set")

    invoice_ids = _norm_int_list(payload.get("insurer_invoice_ids"))

    # default claim amount from payload (manual)
    claim_amt = _d(payload.get("claim_amount"))

    invoices: List[BillingInvoice] = []
    if invoice_ids:
        # ✅ strongly recommend: claim should be created from INSURER invoices only
        invoices = (db.query(BillingInvoice).filter(
            BillingInvoice.billing_case_id == billing_case_id).filter(
                BillingInvoice.id.in_(invoice_ids)
            ).filter(BillingInvoice.status != DocStatus.VOID).filter(
                BillingInvoice.invoice_type == InvoiceType.INSURER).order_by(
                    BillingInvoice.created_at.asc()).all())
        if len(invoices) != len(set(invoice_ids)):
            found = {int(i.id) for i in invoices}
            missing = [i for i in invoice_ids if i not in found]
            raise HTTPException(
                status_code=400,
                detail=
                f"Invalid insurer_invoice_ids (missing/invalid): {missing}")

        claim_amt = sum((_d(i.grand_total) for i in invoices), D0)

    # merge attachments_json (keep files) and later store invoice ids also
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

    # ✅ store invoice linkage inside claim.attachments_json (no DB change)
    if invoice_ids:
        _claim_set_invoice_meta(cl, invoice_ids, invoices)
        db.flush()

    _log(db, "BillingClaim", int(cl.id), "CREATE", user_id)
    return cl


def claim_submit(db: Session, claim_id: int,
                 user_id: Optional[int]) -> BillingClaim:
    cl = db.query(BillingClaim).filter(
        BillingClaim.id == int(claim_id)).one_or_none()
    if not cl:
        raise HTTPException(status_code=404, detail="Claim not found")
    if cl.status != ClaimStatus.DRAFT:
        raise HTTPException(status_code=400,
                            detail="Only DRAFT can be submitted")

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(cl.insurance_case_id)).one_or_none()
    if not ins:
        raise HTTPException(status_code=400,
                            detail="Insurance case missing for this claim")

    billing_case_id = int(ins.billing_case_id)

    # 1) read invoice ids from claim meta (supports old/new keys)
    invoice_ids = _claim_get_invoice_ids(cl)

    # 2) if missing, auto-pick all eligible insurer invoices for this case
    invoices: List[BillingInvoice] = []
    if not invoice_ids:
        invoices = _pick_default_insurer_invoices(db, billing_case_id)
        invoice_ids = [int(i.id) for i in invoices]

    # 3) still none => proper 400 (never 500)
    if not invoice_ids:
        raise HTTPException(
            status_code=400,
            detail=
            "No claimable INSURER invoices found. Split invoices first and ensure INSURER invoices are APPROVED/POSTED.",
        )

    # 4) validate invoice ids (ensure they are valid insurer invoices and claimable)
    invoices = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == billing_case_id).filter(
            BillingInvoice.id.in_(invoice_ids)).filter(
                BillingInvoice.status != DocStatus.VOID).filter(
                    BillingInvoice.invoice_type == InvoiceType.INSURER).filter(
                        BillingInvoice.status.in_([
                            DocStatus.APPROVED, DocStatus.POSTED
                        ])).order_by(BillingInvoice.created_at.asc()).all())
    found = {int(i.id) for i in invoices}
    missing = [i for i in invoice_ids if int(i) not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=
            f"Invalid insurer_invoice_ids (missing/not claimable): {missing}",
        )

    # 5) write meta (adds legacy invoice_id too)
    _claim_set_invoice_meta(cl, [int(i.id) for i in invoices], invoices)

    # 6) recompute claim amount from invoices (prevents tampering)
    cl.claim_amount = sum((_d(i.grand_total) for i in invoices), D0)

    # 7) mark submitted
    cl.status = ClaimStatus.SUBMITTED
    cl.submitted_at = _now()
    db.flush()

    # update insurance case status
    ins.status = InsuranceStatus.CLAIM_SUBMITTED
    db.flush()

    _log(db,
         "BillingClaim",
         int(cl.id),
         "SUBMIT",
         user_id,
         reason=f"invoices={list(found)}")
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
    cl = db.query(BillingClaim).filter(
        BillingClaim.id == int(claim_id)).one_or_none()
    if not cl:
        raise HTTPException(status_code=404, detail="Claim not found")

    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.id == int(cl.insurance_case_id)).one_or_none()
    if not ins:
        raise HTTPException(status_code=400,
                            detail="Insurance case missing for this claim")

    # validate transitions (basic)
    if status == ClaimStatus.SETTLED and cl.status not in (
            ClaimStatus.SUBMITTED, ClaimStatus.APPROVED,
            ClaimStatus.UNDER_QUERY):
        raise HTTPException(
            status_code=400,
            detail="Only SUBMITTED/APPROVED/UNDER_QUERY can be SETTLED")

    cl.approved_amount = _d(approved_amount)
    cl.settled_amount = _d(settled_amount)
    cl.remarks = remarks or cl.remarks

    if status == ClaimStatus.UNDER_QUERY:
        cl.status = ClaimStatus.UNDER_QUERY
        ins.status = InsuranceStatus.QUERY

    elif status == ClaimStatus.DENIED:
        cl.status = ClaimStatus.DENIED
        ins.status = InsuranceStatus.DENIED

    elif status == ClaimStatus.APPROVED:
        cl.status = ClaimStatus.APPROVED
        # keep insurance status as CLAIM_SUBMITTED or set a dedicated APPROVED if you want later

    elif status == ClaimStatus.SETTLED:
        cl.status = ClaimStatus.SETTLED
        cl.settled_at = _now()
        ins.status = InsuranceStatus.SETTLED

    else:
        cl.status = status

    db.flush()

    # ✅ If SETTLED: create insurer payment + allocations
    allocations_info: List[Dict[str, Any]] = []
    if cl.status == ClaimStatus.SETTLED and _d(cl.settled_amount) > 0:
        billing_case_id = int(ins.billing_case_id)

        # invoices from claim meta (or default pick)
        invoice_ids = _claim_get_invoice_ids(cl)
        invoices: List[BillingInvoice] = []
        if invoice_ids:
            invoices = (db.query(BillingInvoice).filter(
                BillingInvoice.billing_case_id == billing_case_id).filter(
                    BillingInvoice.id.in_(invoice_ids)).filter(
                        BillingInvoice.status != DocStatus.VOID).filter(
                            BillingInvoice.invoice_type ==
                            InvoiceType.INSURER).order_by(
                                BillingInvoice.created_at.asc()).all())
        else:
            invoices = _pick_default_insurer_invoices(db, billing_case_id)
            invoice_ids = [int(i.id) for i in invoices]

        if not invoices:
            raise HTTPException(
                status_code=400,
                detail="No INSURER invoices found to allocate settlement.")

        # ensure claim meta has invoice keys (legacy & new)
        _claim_set_invoice_meta(cl, [int(i.id) for i in invoices], invoices)
        db.flush()

        payer_type, payer_id = _insurer_payer(ins)
        if not payer_id:
            raise HTTPException(
                status_code=400,
                detail=
                "Insurance/TPA/Corporate payer is missing in Insurance Case.")

        # create receipt number (insurer receipt prefix)
        receipt_no = next_doc_number(
            db,
            NumberDocType.RECEIPT,
            prefix="IRCP",
            reset_period=NumberResetPeriod.YEAR,
        )

        # link payment to primary invoice (still allocate to all)
        primary_invoice_id = int(invoices[0].id)

        pay = BillingPayment(
            billing_case_id=billing_case_id,
            invoice_id=primary_invoice_id,
            payer_type=payer_type,
            payer_id=payer_id,
            mode=PayMode.BANK,  # default (extend later via UI)
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
                "insurer_invoice_numbers":
                [i.invoice_number for i in invoices],
            },
        )
        db.add(pay)
        db.flush()

        allocations_info = _allocate_payment_to_invoices(
            db=db,
            billing_case_id=billing_case_id,
            payment=pay,
            invoices=invoices,
            total_amount=_d(cl.settled_amount),
        )
        db.flush()

        _log(
            db,
            "BillingPayment",
            int(pay.id),
            "CREATE_FROM_CLAIM_SETTLEMENT",
            user_id,
            reason=f"claim_id={int(cl.id)} allocated={len(allocations_info)}",
        )

    _log(
        db,
        "BillingClaim",
        int(cl.id),
        "UPDATE_STATUS",
        user_id,
        reason=f"{cl.status} allocations={len(allocations_info)}",
    )
    return cl
