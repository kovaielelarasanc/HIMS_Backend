# FILE: app/services/billing_finance.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from typing import Sequence
from fastapi import HTTPException
from sqlalchemy import case as sa_case, func, or_
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.user import User
from app.models.billing import (
    AdvanceType,
    BillingAdvance,
    BillingAdvanceApplication,  # canonical (used by apply_advances_to_case)
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingNumberSeries,
    BillingPayment,
    BillingPaymentAllocation,
    DocStatus,
    NumberDocType,
    NumberResetPeriod,
    PayMode,
    PayerType,
    PaymentDirection,
    PaymentKind,
    ReceiptStatus,
)

# Optional legacy alias (some code referenced BillingAdvanceAllocation)
try:
    from app.models.billing import BillingAdvanceAllocation as _BillingAdvanceAllocation  # type: ignore
except Exception:
    _BillingAdvanceAllocation = None  # fallback to BillingAdvanceApplication

AUTO_APPLY_ADVANCE_ENABLED = False


# -------------------------
# Errors
# -------------------------
class BillingError(RuntimeError):
    pass


class BillingStateError(BillingError):
    pass


from sqlalchemy import or_, exists


def invoice_paid_total(db: Session, invoice_id: int) -> Decimal:
    # ---- safe column getters (handles projects with different column names) ----
    pay_status_col = getattr(BillingPayment, "status", None) or getattr(
        BillingPayment, "receipt_status", None)
    alloc_status_col = getattr(
        BillingPaymentAllocation, "status", None) or getattr(
            BillingPaymentAllocation, "receipt_status", None)
    pay_dir_col = getattr(BillingPayment, "direction", None)  # optional
    pay_kind_col = getattr(BillingPayment, "kind", None)

    # -------------------------
    # 1) Allocation-based paid (NEW / correct model)
    # -------------------------
    q_alloc = (db.query(
        func.coalesce(func.sum(BillingPaymentAllocation.amount), 0)).join(
            BillingPayment,
            BillingPayment.id == BillingPaymentAllocation.payment_id).filter(
                BillingPaymentAllocation.invoice_id == int(invoice_id)))

    # payment must be active
    if pay_status_col is not None:
        q_alloc = q_alloc.filter(
            or_(pay_status_col == ReceiptStatus.ACTIVE,
                pay_status_col.is_(None)))

    # allocation must be active (but allow NULL to avoid excluding rows)
    if alloc_status_col is not None:
        q_alloc = q_alloc.filter(
            or_(alloc_status_col == ReceiptStatus.ACTIVE,
                alloc_status_col.is_(None)))

    # only count RECEIPT + ADVANCE_ADJUSTMENT
    if pay_kind_col is not None:
        q_alloc = q_alloc.filter(
            pay_kind_col.in_(
                [PaymentKind.RECEIPT, PaymentKind.ADVANCE_ADJUSTMENT]))

    # optional: count only IN direction (prevents counting refunds if your system uses OUT)
    if pay_dir_col is not None:
        q_alloc = q_alloc.filter(
            or_(pay_dir_col == PaymentDirection.IN, pay_dir_col.is_(None)))

    alloc_paid = _d(q_alloc.scalar() or 0)

    # -------------------------
    # 2) Legacy direct payments (OLD data): BillingPayment.invoice_id set, but no allocations
    #    (Avoid double counting by excluding payments that already have allocations)
    # -------------------------
    has_alloc = (db.query(BillingPaymentAllocation.id).filter(
        BillingPaymentAllocation.payment_id == BillingPayment.id).exists())

    q_direct = (db.query(func.coalesce(func.sum(
        BillingPayment.amount), 0)).filter(
            BillingPayment.invoice_id == int(invoice_id)).filter(~has_alloc))

    if pay_status_col is not None:
        q_direct = q_direct.filter(
            or_(pay_status_col == ReceiptStatus.ACTIVE,
                pay_status_col.is_(None)))

    if pay_kind_col is not None:
        q_direct = q_direct.filter(
            pay_kind_col.in_(
                [PaymentKind.RECEIPT, PaymentKind.ADVANCE_ADJUSTMENT]))

    if pay_dir_col is not None:
        q_direct = q_direct.filter(
            or_(pay_dir_col == PaymentDirection.IN, pay_dir_col.is_(None)))

    direct_paid = _d(q_direct.scalar() or 0)

    return _d(alloc_paid + direct_paid)


def invoice_outstanding_total(db: Session, invoice_id: int) -> Decimal:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv or inv.status == DocStatus.VOID:
        return Decimal("0.00")

    total = _d(getattr(inv, "grand_total", 0) or 0)
    paid = _d(invoice_paid_total(db, invoice_id=int(invoice_id)))

    out = _d(total - paid)
    if out < 0:
        out = Decimal("0.00")
    return _d(out)


# -------------------------
# Decimal helpers
# -------------------------
Q2 = Decimal("0.01")


def D(x) -> Decimal:
    try:
        return Decimal(str(x or 0)).quantize(Q2, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x if x is not None else 0))
    except Exception:
        return Decimal("0")


def _sum_decimal(v) -> Decimal:
    return D(v)


def _now_local() -> datetime:
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return datetime.now(tz)


def _period_key(reset_period: NumberResetPeriod,
                on_dt: datetime) -> Optional[str]:
    if reset_period == NumberResetPeriod.NONE:
        return None
    if reset_period == NumberResetPeriod.YEAR:
        return on_dt.strftime("%Y")
    if reset_period == NumberResetPeriod.MONTH:
        return on_dt.strftime("%Y-%m")
    return None


def _set_if_has(obj: Any, field: str, value: Any) -> None:
    if hasattr(obj, field):
        setattr(obj, field, value)


# -------------------------
# Active line filter (safe across schemas)
# -------------------------
def _apply_active_line_filter(q):
    if hasattr(BillingInvoiceLine, "is_deleted"):
        q = q.filter(
            or_(BillingInvoiceLine.is_deleted.is_(False),
                BillingInvoiceLine.is_deleted.is_(None)))
    if hasattr(BillingInvoiceLine, "is_active"):
        q = q.filter(
            or_(BillingInvoiceLine.is_active.is_(True),
                BillingInvoiceLine.is_active.is_(None)))
    if hasattr(BillingInvoiceLine, "deleted_at"):
        q = q.filter(BillingInvoiceLine.deleted_at.is_(None))
    if hasattr(BillingInvoiceLine, "voided_at"):
        q = q.filter(BillingInvoiceLine.voided_at.is_(None))
    if hasattr(BillingInvoiceLine, "description"):
        try:
            q = q.filter(~BillingInvoiceLine.description.ilike("%(REMOVED)%"))
        except Exception:
            pass
    return q


# -------------------------
# Receipt numbering (BillingNumberSeries)
# -------------------------
def next_receipt_number(
    db: Session,
    *,
    prefix: str = "RCPT-",
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
    on_dt: Optional[datetime] = None,
) -> str:
    on_dt = on_dt or _now_local()
    pk = _period_key(reset_period, on_dt)

    row = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == NumberDocType.RECEIPT,
        BillingNumberSeries.reset_period == reset_period,
        BillingNumberSeries.prefix == (prefix or ""),
        BillingNumberSeries.is_active.is_(True),
    ).with_for_update().first())

    if not row:
        row = BillingNumberSeries(
            doc_type=NumberDocType.RECEIPT,
            prefix=(prefix or ""),
            reset_period=reset_period,
            padding=int(padding or 6),
            next_number=1,
            last_period_key=pk,
            is_active=True,
        )
        db.add(row)
        db.flush()

    if pk is not None and row.last_period_key != pk:
        row.next_number = 1
        row.last_period_key = pk

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    return f"{row.prefix}{n:0{int(row.padding or padding)}d}"


# -------------------------
# Insurance payer split enforcement
# -------------------------
def recompute_line_payer_split(ln: BillingInvoiceLine) -> None:
    net = _d(getattr(ln, "net_amount", 0))
    appr = _d(getattr(ln, "approved_amount", 0))

    insurer = appr
    if insurer < 0:
        insurer = Decimal("0")
    if insurer > net:
        insurer = net

    if hasattr(ln, "insurer_pay_amount"):
        ln.insurer_pay_amount = insurer

    patient = net - insurer
    if patient < 0:
        patient = Decimal("0")

    ln.patient_pay_amount = patient


def invoice_due_split(db: Session, *, invoice_id: int) -> Dict[str, Decimal]:
    insurer_expr = getattr(BillingInvoiceLine, "insurer_pay_amount",
                           BillingInvoiceLine.approved_amount)

    q = db.query(
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.patient_pay_amount), 0),
        func.coalesce(func.sum(insurer_expr), 0),
    ).filter(BillingInvoiceLine.invoice_id == int(invoice_id))

    q = _apply_active_line_filter(q)
    total, patient_due, insurer_due = q.first() or (0, 0, 0)

    total = _d(total)
    patient_due = min(_d(patient_due), total)
    insurer_due = min(_d(insurer_due), total)

    if (patient_due + insurer_due) > total:
        extra = (patient_due + insurer_due) - total
        insurer_due = max(insurer_due - extra, Decimal("0"))

    return {
        "total": total,
        "patient_due": patient_due,
        "insurer_due": insurer_due
    }


# -------------------------
# Allocation math
# -------------------------
def _payment_sign_expr():
    return sa_case(
        (BillingPayment.direction == PaymentDirection.OUT, -1),
        else_=1,
    )


def allocated_amount_for_invoice_bucket(db: Session, *, invoice_id: int,
                                        bucket: PayerType) -> Decimal:
    sign = _payment_sign_expr()

    alloc_sum = (db.query(
        func.coalesce(func.sum(
            BillingPaymentAllocation.amount * sign), 0)).join(
                BillingPayment, BillingPayment.id
                == BillingPaymentAllocation.payment_id).filter(
                    BillingPaymentAllocation.invoice_id == int(invoice_id),
                    BillingPaymentAllocation.payer_bucket == bucket,
                    BillingPayment.status == ReceiptStatus.ACTIVE,
                ).scalar() or 0)
    alloc_sum = _d(alloc_sum)

    # legacy fallback (payment.invoice_id without allocations)
    subq_has_alloc = db.query(BillingPaymentAllocation.id).filter(
        BillingPaymentAllocation.payment_id == BillingPayment.id).exists()

    legacy_sum = (db.query(
        func.coalesce(func.sum(BillingPayment.amount * sign), 0)).filter(
            BillingPayment.invoice_id == int(invoice_id),
            BillingPayment.payer_type == bucket,
            BillingPayment.status == ReceiptStatus.ACTIVE,
            ~subq_has_alloc,
        ).scalar() or 0)
    legacy_sum = _d(legacy_sum)

    return alloc_sum + legacy_sum


def outstanding_for_invoice_bucket(db: Session, *, invoice_id: int,
                                   bucket: PayerType) -> Decimal:
    due = invoice_due_split(db, invoice_id=invoice_id)
    if bucket == PayerType.PATIENT:
        want = due["patient_due"]
    elif bucket == PayerType.INSURER:
        want = due["insurer_due"]
    else:
        want = due["insurer_due"]

    paid = allocated_amount_for_invoice_bucket(db,
                                               invoice_id=invoice_id,
                                               bucket=bucket)
    out = want - paid
    if out < 0:
        out = Decimal("0")
    return out


# -------------------------
# Case financials (dashboard)
# -------------------------
def case_financials(db: Session, *, case_id: int) -> Dict[str, Any]:
    case = db.get(BillingCase, int(case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    inv_ids = (db.query(BillingInvoice.id).filter(
        BillingInvoice.billing_case_id == int(case_id),
        BillingInvoice.status != DocStatus.VOID,
    ).all())
    inv_ids = [int(x[0]) for x in inv_ids]

    posted_total = (db.query(
        func.coalesce(func.sum(BillingInvoice.grand_total), 0)).filter(
            BillingInvoice.billing_case_id == int(case_id),
            BillingInvoice.status == DocStatus.POSTED,
        ).scalar() or 0)
    posted_total = _d(posted_total)

    patient_due = Decimal("0")
    insurer_due = Decimal("0")
    for iid in inv_ids:
        dct = invoice_due_split(db, invoice_id=iid)
        patient_due += dct["patient_due"]
        insurer_due += dct["insurer_due"]

    patient_paid = Decimal("0")
    insurer_paid = Decimal("0")
    for iid in inv_ids:
        patient_paid += allocated_amount_for_invoice_bucket(
            db, invoice_id=iid, bucket=PayerType.PATIENT)
        insurer_paid += allocated_amount_for_invoice_bucket(
            db, invoice_id=iid, bucket=PayerType.INSURER)

    adv_in = (db.query(func.coalesce(func.sum(
        BillingAdvance.amount), 0)).filter(
            BillingAdvance.billing_case_id == int(case_id),
            BillingAdvance.entry_type == AdvanceType.ADVANCE,
        ).scalar() or 0)
    adv_out = (db.query(func.coalesce(func.sum(
        BillingAdvance.amount), 0)).filter(
            BillingAdvance.billing_case_id == int(case_id),
            BillingAdvance.entry_type == AdvanceType.REFUND,
        ).scalar() or 0)
    adv_in = _d(adv_in)
    adv_out = _d(adv_out)

    adv_applied = (db.query(
        func.coalesce(func.sum(BillingAdvanceApplication.amount),
                      0)).filter(BillingAdvanceApplication.billing_case_id ==
                                 int(case_id)).scalar() or 0)
    adv_applied = _d(adv_applied)

    advance_balance_val = adv_in - adv_out - adv_applied
    if advance_balance_val < 0:
        advance_balance_val = Decimal("0")

    # ✅ IMPORTANT:
    # Outstanding must NOT be reduced by advance balance.
    # Outstanding reduces ONLY when ADVANCE_ADJUSTMENT allocations are created (apply-selected).
    patient_outstanding = patient_due - patient_paid
    if patient_outstanding < 0:
        patient_outstanding = Decimal("0")

    insurer_outstanding = insurer_due - insurer_paid
    if insurer_outstanding < 0:
        insurer_outstanding = Decimal("0")

    return {
        "case_id": int(case_id),
        "posted_total": str(posted_total),
        "due": {
            "patient_due": str(patient_due),
            "insurer_due": str(insurer_due)
        },
        "paid": {
            "patient_paid": str(patient_paid),
            "insurer_paid": str(insurer_paid)
        },

        # ✅ show advance wallet separately (deposit wallet)
        "advances": {
            "advance_in": str(adv_in),
            "advance_refund": str(adv_out),
            "advance_applied":
            str(adv_applied),  # this increases ONLY when you apply-selected
            "advance_balance": str(advance_balance_val),
        },

        # ✅ real outstanding (NO advance subtraction)
        "outstanding": {
            "patient_outstanding": str(patient_outstanding),
            "insurer_outstanding": str(insurer_outstanding),
            "total_outstanding":
            str(patient_outstanding + insurer_outstanding),
        },

        # ✅ optional helper for UI (show as “If you apply advance now…”)
        "what_if": {
            "patient_payable_after_using_all_advance":
            str(max(patient_outstanding - advance_balance_val, Decimal("0")))
        }
    }


# -------------------------
# Invoice paid breakup / due (safe)
# -------------------------
def invoice_paid_breakup(db: Session, invoice_id: int) -> Dict[str, Decimal]:
    pay_alloc = (db.query(
        func.coalesce(func.sum(BillingPaymentAllocation.amount), 0)).filter(
            BillingPaymentAllocation.invoice_id == int(invoice_id), ).scalar()
                 or 0)

    # Advance allocations: prefer BillingAdvanceAllocation if exists, else BillingAdvanceApplication
    if _BillingAdvanceAllocation is not None:
        q = db.query(
            func.coalesce(func.sum(
                _BillingAdvanceAllocation.amount), 0)).filter(
                    _BillingAdvanceAllocation.invoice_id == int(invoice_id))
        if hasattr(_BillingAdvanceAllocation, "status"):
            q = q.filter(
                _BillingAdvanceAllocation.status == ReceiptStatus.ACTIVE)
        adv_alloc = q.scalar() or 0
    else:
        # BillingAdvanceApplication may not have invoice_id in your schema; if it does, use it
        adv_alloc = 0
        if hasattr(BillingAdvanceApplication, "invoice_id"):
            adv_alloc = (db.query(
                func.coalesce(func.sum(BillingAdvanceApplication.amount),
                              0)).filter(BillingAdvanceApplication.invoice_id
                                         == int(invoice_id)).scalar() or 0)

    return {
        "payments": _sum_decimal(pay_alloc),
        "advances": _sum_decimal(adv_alloc)
    }


def invoice_due_amount(db: Session, invoice_id: int) -> Dict[str, Decimal]:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise ValueError("Invoice not found")

    amt = D(getattr(inv, "grand_total", 0))
    paid = invoice_paid_breakup(db, invoice_id)
    paid_total = D(paid["payments"] + paid["advances"])
    due = D(max(Decimal("0"), amt - paid_total))
    return {"amount": amt, "paid": paid_total, "due": due, **paid}


# -------------------------
# Record Payment (ALLOCATES)
# -------------------------
def record_payment(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    mode: PayMode = PayMode.CASH,
    invoice_id: Optional[int] = None,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
    txn_ref: Optional[str] = None,
    notes: Optional[str] = None,
    kind: PaymentKind = PaymentKind.RECEIPT,
    direction: PaymentDirection = PaymentDirection.IN,
    receipt_prefix: str = "RCPT-",
    receipt_reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    allow_on_approved: bool = True,
) -> Dict[str, Any]:
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    amt = _d(amount)
    if amt <= 0:
        raise HTTPException(status_code=400, detail="amount must be > 0")

    bucket = payer_type

    invoices_q = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.status != DocStatus.VOID,
    )

    if invoice_id:
        inv = db.get(BillingInvoice, int(invoice_id))
        if not inv or int(inv.billing_case_id) != int(billing_case_id):
            raise HTTPException(
                status_code=400,
                detail="Selected invoice does not belong to this case")

        if inv.status not in (DocStatus.POSTED, ) and not (
                allow_on_approved and inv.status == DocStatus.APPROVED):
            raise HTTPException(
                status_code=409,
                detail="Payment allowed only for POSTED (or APPROVED) invoices"
            )

        targets = [inv]
    else:
        allowed_status = [DocStatus.POSTED]
        if allow_on_approved:
            allowed_status.append(DocStatus.APPROVED)

        targets = (invoices_q.filter(
            BillingInvoice.status.in_(allowed_status)).order_by(
                func.coalesce(BillingInvoice.posted_at,
                              BillingInvoice.created_at).asc(),
                BillingInvoice.id.asc()).all())

    if not targets:
        raise HTTPException(
            status_code=409,
            detail="No payable invoice found to allocate payment")

    total_out = Decimal("0")
    outs: List[Tuple[BillingInvoice, Decimal]] = []
    for inv in targets:
        out = outstanding_for_invoice_bucket(db,
                                             invoice_id=int(inv.id),
                                             bucket=bucket)
        if out > 0:
            outs.append((inv, out))
            total_out += out

    if total_out <= 0:
        raise HTTPException(
            status_code=409,
            detail="No outstanding amount for this payer bucket")

    if amt > total_out and kind == PaymentKind.RECEIPT:
        raise HTTPException(
            status_code=409,
            detail={
                "message":
                "Payment exceeds outstanding. Collect extra as Advance instead of Payment.",
                "outstanding": str(total_out),
            },
        )

    receipt_no = next_receipt_number(
        db,
        prefix=receipt_prefix,
        reset_period=receipt_reset_period,
        padding=6,
        on_dt=_now_local(),
    )

    pay = BillingPayment(
        billing_case_id=int(billing_case_id),
        invoice_id=int(invoice_id) if invoice_id else None,
        payer_type=payer_type,
        payer_id=payer_id,
        mode=mode,
        amount=amt,
        txn_ref=txn_ref,
        notes=notes,
        kind=kind,
        direction=direction,
        status=ReceiptStatus.ACTIVE,
        receipt_number=receipt_no,
        received_by=getattr(user, "id", None),
        meta_json={
            "bucket":
            payer_type.value
            if hasattr(payer_type, "value") else str(payer_type),
            "kind":
            kind.value if hasattr(kind, "value") else str(kind),
        },
    )
    db.add(pay)
    db.flush()

    remaining = amt
    allocations_out = []

    for inv, out in outs:
        if remaining <= 0:
            break
        take = out if remaining >= out else remaining

        alloc = BillingPaymentAllocation(
            billing_case_id=int(billing_case_id),
            payment_id=int(pay.id),
            invoice_id=int(inv.id),
            payer_bucket=bucket,
            amount=take,
        )
        _set_if_has(alloc, "status", ReceiptStatus.ACTIVE)
        db.add(alloc)

        allocations_out.append({
            "invoice_id": int(inv.id),
            "invoice_number": inv.invoice_number,
            "bucket": bucket.value,
            "amount": str(take)
        })

        remaining -= take

    if remaining > 0:
        raise HTTPException(status_code=500,
                            detail="Allocation error: leftover payment amount")

    db.flush()

    return {
        "payment_id": int(pay.id),
        "receipt_number": pay.receipt_number,
        "amount": str(amt),
        "payer_type": payer_type.value,
        "kind": kind.value,
        "direction": direction.value,
        "allocations": allocations_out,
    }


# -------------------------
# Advance ledger
# -------------------------
def advance_balance(db: Session, *, billing_case_id: int) -> Decimal:
    adv_in = (db.query(func.coalesce(func.sum(
        BillingAdvance.amount), 0)).filter(
            BillingAdvance.billing_case_id == int(billing_case_id),
            BillingAdvance.entry_type == AdvanceType.ADVANCE).scalar() or 0)
    adv_out = (db.query(func.coalesce(func.sum(
        BillingAdvance.amount), 0)).filter(
            BillingAdvance.billing_case_id == int(billing_case_id),
            BillingAdvance.entry_type == AdvanceType.REFUND).scalar() or 0)
    adv_applied = (db.query(
        func.coalesce(func.sum(BillingAdvanceApplication.amount),
                      0)).filter(BillingAdvanceApplication.billing_case_id ==
                                 int(billing_case_id)).scalar() or 0)

    bal = _d(adv_in) - _d(adv_out) - _d(adv_applied)
    if bal < 0:
        bal = Decimal("0")
    return bal


def record_advance(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    entry_type: AdvanceType = AdvanceType.ADVANCE,
    mode: PayMode = PayMode.CASH,
    txn_ref: Optional[str] = None,
    remarks: Optional[str] = None,
) -> BillingAdvance:
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    amt = _d(amount)
    if amt <= 0:
        raise HTTPException(status_code=400, detail="amount must be > 0")

    if entry_type == AdvanceType.REFUND:
        bal = advance_balance(db, billing_case_id=billing_case_id)
        if amt > bal:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Refund exceeds available advance balance",
                    "advance_balance": str(bal)
                },
            )

    adv = BillingAdvance(
        billing_case_id=int(billing_case_id),
        entry_type=entry_type,
        mode=mode,
        amount=amt,
        txn_ref=txn_ref,
        entry_by=getattr(user, "id", None),
        remarks=remarks,
    )
    db.add(adv)
    db.flush()
    return adv


def _enum_name(x) -> str:
    """Return a stable name like 'APPROVED' from Enum/value/str."""
    if x is None:
        return ""
    v = getattr(x, "value", None)
    if v is not None:
        return str(v).upper().strip()
    s = str(x).upper().strip()
    # handle 'DocStatus.APPROVED' etc
    if "." in s:
        s = s.split(".")[-1]
    return s


def _is_patient_invoice(inv: BillingInvoice) -> bool:
    """
    Decide whether this invoice should accept patient money (advance).
    - If invoice_type exists and is not PATIENT -> NOT patient invoice.
    - Else default to patient invoice.
    """
    it = getattr(inv, "invoice_type", None)
    itn = _enum_name(it)
    if itn and itn != "PATIENT":
        return False
    return True


def patient_due_for_invoice(db: Session, *, invoice_id: int) -> Decimal:
    """
    ✅ Patient-due used by BOTH:
    - outstanding list (UI)
    - apply-selected (backend validation)

    Assumption: advances are patient money, so apply only to PATIENT invoices.
    Patient due = invoice_outstanding_total() for PATIENT invoices else 0.
    """
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        return Decimal("0.00")

    if inv.status in (DocStatus.VOID, ):
        return Decimal("0.00")

    if not _is_patient_invoice(inv):
        return Decimal("0.00")

    due = D(invoice_outstanding_total(db, invoice_id=int(inv.id)))
    if due < 0:
        due = Decimal("0.00")
    return due


def list_case_invoice_outstanding(
    db: Session,
    *,
    billing_case_id: int,
    status_names: Sequence[str] = ("APPROVED", "POSTED"),
) -> List[Dict[str, Any]]:
    allowed = []
    for s in status_names:
        s = str(s).upper().strip()
        if s == "APPROVED":
            allowed.append(DocStatus.APPROVED)
        elif s == "POSTED":
            allowed.append(DocStatus.POSTED)

    q = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.status != DocStatus.VOID,
    )
    if allowed:
        q = q.filter(BillingInvoice.status.in_(allowed))

    invoices = q.order_by(
        func.coalesce(BillingInvoice.posted_at,
                      BillingInvoice.created_at).asc(),
        BillingInvoice.id.asc(),
    ).all()

    out: List[Dict[str, Any]] = []
    for inv in invoices:
        # ✅ total due (for debugging)
        total_due = D(invoice_outstanding_total(db, invoice_id=int(inv.id)))
        paid = D(invoice_paid_total(db, invoice_id=int(inv.id)))

        # ✅ patient due (THIS is what Apply uses)
        p_due = D(patient_due_for_invoice(db, invoice_id=int(inv.id)))

        out.append({
            "invoice_id":
            int(inv.id),
            "invoice_number":
            inv.invoice_number,
            "module":
            inv.module,
            "status":
            _enum_name(inv.status),
            "invoice_type":
            _enum_name(getattr(inv, "invoice_type", None)) or None,
            "grand_total":
            str(_d(getattr(inv, "grand_total", 0))),
            "paid_total":
            str(_d(paid)),  # debug
            "total_outstanding":
            str(_d(total_due)),  # debug
            "patient_outstanding":
            str(_d(p_due)),  # ✅ UI checkbox must use THIS
        })
    return out


def apply_advances_to_selected_invoices(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    invoice_ids: List[int],
    apply_amount: Optional[Decimal] = None,
    notes: Optional[str] = None,
    receipt_prefix: str = "RCPT-",
    receipt_reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
) -> Dict[str, Any]:
    if not invoice_ids:
        raise HTTPException(status_code=422, detail="invoice_ids required")

    # Normalize ids
    invoice_ids = [int(x) for x in invoice_ids]

    # ✅ Lock/compute balance
    bal = D(advance_balance(db, billing_case_id=int(billing_case_id)))
    if bal <= 0:
        raise HTTPException(status_code=409,
                            detail="No advance balance to apply")

    # ✅ Load selected invoices and lock them
    inv_rows = (db.query(BillingInvoice).filter(
        BillingInvoice.id.in_(invoice_ids)).with_for_update().all())
    found = {int(x.id) for x in inv_rows}
    missing = [int(x) for x in invoice_ids if int(x) not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Invoice(s) not found",
                "missing": missing
            },
        )

    payable: List[BillingInvoice] = []
    for inv in inv_rows:
        if int(inv.billing_case_id) != int(billing_case_id):
            raise HTTPException(
                status_code=400,
                detail=f"Invoice {inv.id} does not belong to this case",
            )
        if inv.status in (DocStatus.VOID, ):
            raise HTTPException(status_code=409,
                                detail=f"Invoice {inv.id} is VOID")
        if inv.status not in (DocStatus.APPROVED, DocStatus.POSTED):
            raise HTTPException(
                status_code=409,
                detail=
                f"Invoice {inv.id} is not payable (must be APPROVED/POSTED)",
            )
        payable.append(inv)

    # Keep exact UI order
    inv_by_id = {int(x.id): x for x in payable}
    ordered = [inv_by_id[i] for i in invoice_ids]

    # ✅ Compute outstanding only for selected invoices (PATIENT bucket)
    outs: List[Tuple[BillingInvoice, Decimal]] = []
    total_out = Decimal("0.00")

    for inv in ordered:
        out = D(patient_due_for_invoice(db, invoice_id=int(inv.id)))
        if out > 0:
            outs.append((inv, out))
            total_out = D(total_out + out)

    if total_out <= 0:
        # ✅ include debug so you can see why
        dbg = []
        for inv in ordered:
            dbg.append({
                "invoice_id":
                int(inv.id),
                "invoice_type":
                _enum_name(getattr(inv, "invoice_type", None)) or None,
                "status":
                _enum_name(inv.status),
                "patient_due":
                str(_d(patient_due_for_invoice(db, invoice_id=int(inv.id)))),
                "total_due":
                str(_d(invoice_outstanding_total(db, invoice_id=int(inv.id)))),
            })
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Selected invoices have no pending patient due",
                "debug": dbg
            },
        )

    # ✅ Determine apply amount
    if apply_amount is None:
        raise HTTPException(status_code=422,
                            detail="apply_amount is required (manual mode)")
    apply_amt = D(apply_amount)

    if apply_amt <= 0:
        raise HTTPException(status_code=400, detail="apply_amount must be > 0")
    if apply_amt > bal:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Apply amount exceeds advance balance",
                "advance_balance": str(bal)
            },
        )
    if apply_amt > total_out:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Apply amount exceeds selected invoices due",
                "selected_due": str(total_out)
            },
        )

    # ✅ Create payment row representing advance adjustment
    receipt_no = next_receipt_number(
        db,
        prefix=receipt_prefix,
        reset_period=receipt_reset_period,
        padding=6,
        on_dt=_now_local(),
    )

    pay = BillingPayment(
        billing_case_id=int(billing_case_id),
        invoice_id=None,
        payer_type=PayerType.PATIENT,
        payer_id=None,
        mode=PayMode.CASH,
        amount=D(apply_amt),
        txn_ref=None,
        notes=(notes or "Advance applied to selected invoices").strip(),
        kind=PaymentKind.ADVANCE_ADJUSTMENT,
        direction=PaymentDirection.IN,
        status=ReceiptStatus.ACTIVE,
        receipt_number=receipt_no,
        received_by=getattr(user, "id", None),
        meta_json={
            "source": "ADVANCE_WALLET",
            "apply_mode": "SELECTED_INVOICES",
            "invoice_ids": invoice_ids,
        },
    )
    db.add(pay)
    db.flush()

    # ✅ Allocate only to selected invoices (NO rounding leftovers)
    remaining = D(apply_amt)
    allocations_out: List[Dict[str, Any]] = []

    for inv, out in outs:
        if remaining <= 0:
            break
        take = min(D(out), D(remaining))
        take = D(take)
        if take <= 0:
            continue

        alloc = BillingPaymentAllocation(
            billing_case_id=int(billing_case_id),
            payment_id=int(pay.id),
            invoice_id=int(inv.id),
            payer_bucket=PayerType.PATIENT,
            amount=take,
        )
        _set_if_has(alloc, "status", ReceiptStatus.ACTIVE)
        db.add(alloc)

        allocations_out.append({
            "invoice_id": int(inv.id),
            "invoice_number": inv.invoice_number,
            "amount": str(take),
        })

        remaining = D(remaining - take)

    if D(remaining) != Decimal("0.00"):
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Allocation error: leftover amount",
                "leftover": str(remaining)
            },
        )

    # ✅ Consume advances oldest-first into BillingAdvanceApplication (history + balance calc)
    # Order column safe
    order_col = getattr(BillingAdvance, "entry_at", None)
    if order_col is None:
        order_col = getattr(BillingAdvance, "created_at", None)
    if order_col is None:
        order_col = BillingAdvance.id

    advances = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == int(billing_case_id)).order_by(
            order_col.asc(), BillingAdvance.id.asc()).with_for_update().all())

    applied_map: Dict[int, Decimal] = {}
    rows = (db.query(
        BillingAdvanceApplication.advance_id,
        func.coalesce(func.sum(BillingAdvanceApplication.amount), 0),
    ).filter(BillingAdvanceApplication.billing_case_id == int(
        billing_case_id)).group_by(BillingAdvanceApplication.advance_id).all())
    for aid, s in rows:
        applied_map[int(aid)] = D(s)

    need = D(apply_amt)
    consumed_out: List[Dict[str, Any]] = []

    for adv in advances:
        if need <= 0:
            break
        if adv.entry_type != AdvanceType.ADVANCE:
            continue

        already = applied_map.get(int(adv.id), Decimal("0.00"))
        avail = D(D(getattr(adv, "amount", 0)) - D(already))
        if avail <= 0:
            continue

        take = min(avail, need)
        take = D(take)

        app = BillingAdvanceApplication(
            billing_case_id=int(billing_case_id),
            advance_id=int(adv.id),
            payment_id=int(pay.id),
            amount=take,
        )
        db.add(app)

        consumed_out.append({
            "advance_id": int(adv.id),
            "amount": str(take),
            "mode": str(getattr(adv, "mode", "") or ""),
            "txn_ref": getattr(adv, "txn_ref", None),
            "entry_at": getattr(adv, "entry_at", None),
        })

        need = D(need - take)

    if D(need) != Decimal("0.00"):
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Advance consumption error: insufficient advances",
                "leftover": str(need)
            },
        )

    db.flush()

    return {
        "payment_id":
        int(pay.id),
        "receipt_number":
        pay.receipt_number,
        "applied_amount":
        str(D(apply_amt)),
        "selected_due":
        str(D(total_out)),
        "allocations":
        allocations_out,
        "consumed_advances":
        consumed_out,
        "advance_balance_after":
        str(D(advance_balance(db, billing_case_id=billing_case_id))),
    }


def apply_advances_to_case(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    max_apply_amount: Optional[Decimal] = None,
    receipt_prefix: str = "RCPT-",
    receipt_reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
) -> Dict[str, Any]:
    """
    🔒 AUTO APPLY DISABLED (PERMANENT)
    - This function used to auto-apply advances to all case invoices.
    - Some workflows may still call it (post_invoice, etc).
    - Now it returns a safe NO-OP so NOTHING auto applies.
    - Manual apply must be done only via apply_advances_to_selected_invoices().
    """
    if not AUTO_APPLY_ADVANCE_ENABLED:
        bal = advance_balance(db, billing_case_id=int(billing_case_id))
        return {
            "skipped": True,
            "reason": "AUTO_APPLY_DISABLED",
            "payment_id": None,
            "receipt_number": None,
            "applied_amount": "0.00",
            "allocations": [],
            "advance_balance_after": str(_d(bal)),
        }

    # If you ever want to re-enable in future, restore old implementation here.
    raise HTTPException(
        status_code=410,
        detail="Auto-apply is disabled. Use apply-selected endpoint.",
    )
