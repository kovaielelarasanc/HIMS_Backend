# FILE: app/services/ipd_billing.py
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models.ipd import (
    IpdAdmission,
    IpdBedAssignment,
    IpdBed,
    IpdRoom,
    IpdBedRate,
    IpdDischargeSummary,
)

from app.models.billing import (
    BillingCase,
    BillingCaseStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingNumberSeries,
    EncounterType,
    InvoiceType,
    DocStatus,
    PayerType,
    ServiceGroup,
    NumberDocType,
    NumberResetPeriod,
)

# =========================================================
# Recursion / re-entrancy guards (production safety)
# =========================================================
_IN_SYNC_IPD = ContextVar("IN_SYNC_IPD", default=False)
_IN_RECALC = ContextVar("IN_RECALC", default=False)

# =========================================================
# Time helpers
# =========================================================
IST_OFFSET = timedelta(hours=5, minutes=30)


def now_utc_naive() -> datetime:
    return datetime.utcnow()


def today_local() -> date:
    # treat as IST for business prefix/period
    return (datetime.utcnow() + IST_OFFSET).date()


# =========================================================
# Date range helper
# =========================================================
def _date_range_inclusive(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


# =========================================================
# Room type / rate helpers
# =========================================================
def _normalize_room_type(x: Optional[str]) -> str:
    s = (x or "General").strip()
    return s.title()


def _get_room_type(db: Session, bed_id: int) -> str:
    bed = db.get(IpdBed, bed_id)
    if not bed or not getattr(bed, "room_id", None):
        return "General"
    room = db.get(IpdRoom, bed.room_id)
    return _normalize_room_type(getattr(room, "type", None))


def _resolve_rate(db: Session, room_type: str, for_date: date) -> Decimal:
    rt = _normalize_room_type(room_type)
    r = (
        db.query(IpdBedRate)
        .filter(IpdBedRate.is_active.is_(True))
        .filter(IpdBedRate.room_type == rt)
        .filter(IpdBedRate.effective_from <= for_date)
        .filter(or_(IpdBedRate.effective_to.is_(None), IpdBedRate.effective_to >= for_date))
        .order_by(IpdBedRate.effective_from.desc())
        .first()
    )
    return Decimal(str(r.daily_rate)) if r and r.daily_rate is not None else Decimal("0.00")


def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.get(IpdAdmission, admission_id)
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found")
    return adm


# =========================================================
# Discharge timestamp used for billing end
# =========================================================
def get_ipd_discharge_ts(db: Session, admission_id: int) -> Optional[datetime]:
    """
    Priority:
      1) DischargeSummary.discharge_datetime
      2) Admission.discharge_at
      3) None
    """
    ds = (
        db.query(IpdDischargeSummary)
        .filter(IpdDischargeSummary.admission_id == admission_id)
        .first()
    )
    if ds and getattr(ds, "discharge_datetime", None):
        return ds.discharge_datetime

    adm = db.get(IpdAdmission, admission_id)
    if adm and getattr(adm, "discharge_at", None):
        return adm.discharge_at

    return None


# =========================================================
# Bed assignment helpers
# =========================================================
def fetch_assignments(db: Session, admission_id: int) -> List[IpdBedAssignment]:
    return (
        db.query(IpdBedAssignment)
        .filter(IpdBedAssignment.admission_id == admission_id)
        .order_by(IpdBedAssignment.from_ts.asc())
        .all()
    )


def active_assignment_for_day(assigns: List[IpdBedAssignment], day: date) -> Optional[IpdBedAssignment]:
    """
    Rule (Per-calendar-day): pick assignment active at END OF DAY (23:59:59)
    to avoid double billing on transfer days.
    """
    eod = datetime.combine(day, time.max)
    active = None
    for a in assigns:
        start_ok = a.from_ts <= eod
        end_ok = (a.to_ts is None) or (a.to_ts >= datetime.combine(day, time.min))
        if start_ok and end_ok:
            active = a
    return active


# =========================================================
# Breakdown (daily)
# =========================================================
def _compute_ipd_room_breakdown_daily(
    db: Session,
    admission_id: int,
    from_date: date,
    to_date: date,
) -> Dict[str, Any]:
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be >= from_date")

    _get_admission_or_404(db, admission_id)
    assigns = fetch_assignments(db, admission_id)

    days: List[Dict[str, Any]] = []
    total = Decimal("0.00")
    missing_rate_days = 0

    for d in _date_range_inclusive(from_date, to_date):
        a = active_assignment_for_day(assigns, d)
        if not a:
            continue

        room_type = _get_room_type(db, a.bed_id)
        rate = _resolve_rate(db, room_type, d)

        if rate <= 0:
            missing_rate_days += 1

        total += rate

        days.append(
            {
                "date": d.isoformat(),
                "assignment_id": a.id,
                "bed_id": a.bed_id,
                "room_type": room_type,
                "rate": float(rate),
            }
        )

    return {
        "admission_id": admission_id,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "days": days,
        "missing_rate_days": missing_rate_days,
        "total_amount": float(total.quantize(Decimal("0.01"))),
    }


# =========================================================
# Compatibility wrapper
# =========================================================
def compute_ipd_room_charges_daily(
    db: Session,
    admission_id: int,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    upto_date: Optional[date] = None,
    upto_dt: Optional[datetime] = None,
    user_id: Optional[int] = None,
    tax_rate: float = 0.0,
    gst_rate: Optional[float] = None,
    invoice_id: Optional[int] = None,
    skip_if_already_billed: bool = False,
    **_kwargs,
) -> Dict[str, Any]:
    """
    - If called with (from_date, to_date) and no invoice intent -> returns breakdown.
    - If called with invoice_id/upto_date/etc -> syncs charges and returns sync summary.
    """
    _ = skip_if_already_billed  # keep param for backward compatibility

    wants_breakdown = (
        from_date is not None
        and to_date is not None
        and invoice_id is None
        and upto_date is None
        and upto_dt is None
    )
    if wants_breakdown:
        return _compute_ipd_room_breakdown_daily(db, admission_id, from_date, to_date)

    effective_gst = gst_rate if gst_rate is not None else tax_rate

    return sync_ipd_room_charges(
        db=db,
        admission_id=admission_id,
        upto_date=upto_date,
        upto_dt=upto_dt,
        user_id=user_id,
        gst_rate=float(effective_gst or 0.0),
        invoice_id=invoice_id,
        from_date=from_date,
        to_date=to_date,
        range_only=bool(from_date or to_date),
    )


# =========================================================
# Number series
# =========================================================
def _period_key(reset_period: NumberResetPeriod, on_date: date) -> Optional[str]:
    if reset_period == NumberResetPeriod.NONE:
        return None
    if reset_period == NumberResetPeriod.YEAR:
        return f"{on_date.year:04d}"
    if reset_period == NumberResetPeriod.MONTH:
        return f"{on_date.year:04d}-{on_date.month:02d}"
    return None


def _next_doc_number(
    db: Session,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod,
    padding: int = 6,
    on_date: Optional[date] = None,
) -> str:
    d = on_date or today_local()
    pk = _period_key(reset_period, d)

    row = (
        db.query(BillingNumberSeries)
        .filter(BillingNumberSeries.doc_type == doc_type)
        .filter(BillingNumberSeries.reset_period == reset_period)
        .filter(BillingNumberSeries.prefix == prefix)
        .with_for_update()
        .first()
    )

    if not row:
        row = BillingNumberSeries(
            doc_type=doc_type,
            prefix=prefix,
            reset_period=reset_period,
            padding=padding,
            next_number=1,
            last_period_key=pk,
            is_active=True,
        )
        db.add(row)
        db.flush()

    if reset_period != NumberResetPeriod.NONE and row.last_period_key != pk:
        row.last_period_key = pk
        row.next_number = 1

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    return f"{prefix}{str(n).zfill(int(row.padding or padding))}"


def _case_prefix_ipd(on_date: Optional[date] = None) -> str:
    d = on_date or today_local()
    return f"IP-{d.strftime('%y')}-"


def _invoice_prefix_ipd(module: str, on_date: Optional[date] = None) -> str:
    d = on_date or today_local()
    m = (module or "IPD").upper()
    return f"INV-{m}-{d.strftime('%y%m')}-"


# =========================================================
# Case + Invoice ensure
# =========================================================
def ensure_ipd_billing_case(
    db: Session,
    admission_id: int,
    patient_id: int,
    user_id: Optional[int] = None,
) -> BillingCase:
    case = (
        db.query(BillingCase)
        .filter(BillingCase.encounter_type == EncounterType.IP)
        .filter(BillingCase.encounter_id == admission_id)
        .first()
    )
    if case:
        return case

    case_no = _next_doc_number(
        db,
        doc_type=NumberDocType.CASE,
        prefix=_case_prefix_ipd(),
        reset_period=NumberResetPeriod.YEAR,
        padding=6,
    )

    case = BillingCase(
        patient_id=patient_id,
        encounter_type=EncounterType.IP,
        encounter_id=admission_id,
        case_number=case_no,
        status=BillingCaseStatus.OPEN,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(case)
    db.flush()
    return case


def ensure_invoice_for_case(
    db: Session,
    billing_case: BillingCase,
    module: str,
    invoice_type: InvoiceType = InvoiceType.PATIENT,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> BillingInvoice:
    mod = (module or "").upper()

    inv = (
        db.query(BillingInvoice)
        .filter(BillingInvoice.billing_case_id == billing_case.id)
        .filter(BillingInvoice.module == mod)
        .filter(BillingInvoice.status != DocStatus.VOID)
        .order_by(BillingInvoice.id.desc())
        .first()
    )
    if inv:
        return inv

    inv_no = _next_doc_number(
        db,
        doc_type=NumberDocType.INVOICE,
        prefix=_invoice_prefix_ipd(mod),
        reset_period=NumberResetPeriod.MONTH,
        padding=6,
    )

    inv = BillingInvoice(
        billing_case_id=billing_case.id,
        invoice_number=inv_no,
        module=mod,
        invoice_type=invoice_type,
        status=DocStatus.DRAFT,
        payer_type=payer_type,
        payer_id=payer_id if payer_id is not None else billing_case.patient_id,
        currency="INR",
        created_by=user_id,
        updated_by=user_id,
        service_date=now_utc_naive(),
        meta_json={},
    )
    db.add(inv)
    db.flush()
    return inv


# =========================================================
# Invoice totals (PURE + guarded)
# =========================================================
def recalc_invoice_totals(db: Session, invoice_id: int) -> None:
    if _IN_RECALC.get():
        return

    token = _IN_RECALC.set(True)
    try:
        inv = db.get(BillingInvoice, invoice_id)
        if not inv:
            return

        q = db.query(BillingInvoiceLine).filter(BillingInvoiceLine.invoice_id == invoice_id)
        if hasattr(BillingInvoiceLine, "is_deleted"):
            q = q.filter(BillingInvoiceLine.is_deleted.is_(False))

        lines = q.all()

        sub_total = Decimal("0.00")
        discount_total = Decimal("0.00")
        tax_total = Decimal("0.00")
        grand_total = Decimal("0.00")

        for ln in lines:
            qty = Decimal(str(getattr(ln, "qty", 0) or 0))
            unit = Decimal(str(getattr(ln, "unit_price", 0) or 0))
            disc = Decimal(str(getattr(ln, "discount_amount", 0) or 0))
            tax = Decimal(str(getattr(ln, "tax_amount", 0) or 0))
            net = Decimal(str(getattr(ln, "net_amount", 0) or 0))

            sub_total += (qty * unit)
            discount_total += disc
            tax_total += tax
            grand_total += net

        inv.sub_total = sub_total.quantize(Decimal("0.01"))
        inv.discount_total = discount_total.quantize(Decimal("0.01"))
        inv.tax_total = tax_total.quantize(Decimal("0.01"))
        if hasattr(inv, "round_off"):
            inv.round_off = Decimal("0.00")
        inv.grand_total = grand_total.quantize(Decimal("0.01"))

        db.add(inv)
        db.flush()
    finally:
        _IN_RECALC.reset(token)


# =========================================================
# Line calculation
# =========================================================
def _calc_line(qty: Decimal, unit_price: Decimal, discount_amount: Decimal, gst_rate: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
    base = (qty * unit_price) - discount_amount
    if base < 0:
        base = Decimal("0.00")

    tax = (base * gst_rate / Decimal("100")).quantize(Decimal("0.01"))
    net = (base + tax).quantize(Decimal("0.01"))
    return (base.quantize(Decimal("0.01")), tax, net)


# =========================================================
# IPD Room charges sync
# =========================================================
def sync_ipd_room_charges(
    db: Session,
    admission_id: int,
    upto_date: Optional[date] = None,
    upto_dt: Optional[datetime] = None,
    user_id: Optional[int] = None,
    gst_rate: float = 0.0,
    invoice_id: Optional[int] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    range_only: bool = False,
) -> Dict[str, Any]:
    if _IN_SYNC_IPD.get():
        # prevent accidental re-entry loops
        return {
            "billing_case_id": None,
            "invoice_id": invoice_id,
            "created": 0,
            "updated": 0,
            "deleted_future": 0,
            "missing_rate_days": 0,
            "room_total": 0,
            "from_date": (from_date.isoformat() if from_date else None),
            "to_date": (to_date.isoformat() if to_date else None),
        }

    token = _IN_SYNC_IPD.set(True)
    try:
        adm = _get_admission_or_404(db, admission_id)

        # determine end date
        if to_date:
            end = to_date
        elif upto_date:
            end = upto_date
        elif upto_dt:
            end = upto_dt.date()
        else:
            dis = get_ipd_discharge_ts(db, admission_id)
            end = dis.date() if dis else today_local()

        # determine start date
        start_default = (adm.admitted_at or now_utc_naive()).date()
        start = from_date or start_default

        if end < start:
            raise HTTPException(status_code=400, detail="End date cannot be before start date")

        breakdown = _compute_ipd_room_breakdown_daily(db, admission_id, start, end)
        days = breakdown.get("days") or []

        # resolve case + invoice
        case = ensure_ipd_billing_case(db, admission_id=admission_id, patient_id=adm.patient_id, user_id=user_id)

        if invoice_id:
            inv = db.get(BillingInvoice, invoice_id)
            if not inv:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if inv.billing_case_id != case.id:
                raise HTTPException(status_code=400, detail="Invoice does not belong to this IPD billing case")
        else:
            inv = ensure_invoice_for_case(
                db,
                billing_case=case,
                module="ROOM",
                invoice_type=InvoiceType.PATIENT,
                payer_type=PayerType.PATIENT,
                payer_id=adm.patient_id,
                user_id=user_id,
            )

        if inv.status == DocStatus.POSTED:
            raise HTTPException(
                status_code=400,
                detail="Invoice is POSTED; cannot auto-sync room charges. Use edit-request / credit-debit note flow.",
            )
        if inv.status == DocStatus.VOID:
            raise HTTPException(status_code=400, detail="Invoice is VOID; cannot sync.")

        gst = Decimal(str(gst_rate or 0))

        # existing lines for this admission
        existing = (
            db.query(BillingInvoiceLine)
            .filter(BillingInvoiceLine.invoice_id == inv.id)
            .filter(BillingInvoiceLine.source_module == "IPD_ROOM")
            .filter(BillingInvoiceLine.source_ref_id == admission_id)
            .all()
        )
        by_key = {str(x.source_line_key or ""): x for x in existing}

        created = 0
        updated = 0

        for row in days:
            d = date.fromisoformat(row["date"])
            room_type = row.get("room_type") or "General"
            rate = Decimal(str(row.get("rate") or 0))

            line_key = f"ROOM:{d.isoformat()}"
            desc = f"Observation / Bed Charges ({room_type}) - {d.strftime('%d-%m-%Y')}"

            qty = Decimal("1.0000")
            disc_amt = Decimal("0.00")
            disc_pct = Decimal("0.00")

            _base, tax, net = _calc_line(qty, rate, disc_amt, gst)

            ln = by_key.get(line_key)
            if ln:
                ln.description = desc
                ln.service_group = ServiceGroup.ROOM
                ln.item_type = "ROOM"
                ln.item_id = None
                ln.item_code = room_type

                ln.qty = qty
                ln.unit_price = rate
                ln.discount_percent = disc_pct
                ln.discount_amount = disc_amt
                ln.gst_rate = gst
                ln.tax_amount = tax
                ln.line_total = net
                ln.net_amount = net
                updated += 1
            else:
                ln = BillingInvoiceLine(
                    billing_case_id=case.id,
                    invoice_id=inv.id,
                    service_group=ServiceGroup.ROOM,
                    item_type="ROOM",
                    item_id=None,
                    item_code=room_type,
                    description=desc,
                    qty=qty,
                    unit_price=rate,
                    discount_percent=disc_pct,
                    discount_amount=disc_amt,
                    gst_rate=gst,
                    tax_amount=tax,
                    line_total=net,
                    net_amount=net,
                    source_module="IPD_ROOM",
                    source_ref_id=admission_id,
                    source_line_key=line_key,
                    is_manual=False,
                    created_by=user_id,
                )
                db.add(ln)
                created += 1

        # delete future room lines beyond end date (only for full sync)
        did_delete = 0
        full_sync = (from_date is None) and (to_date is None) and (not range_only)
        if full_sync:
            end_key = f"ROOM:{end.isoformat()}"
            for ln in existing:
                k = str(ln.source_line_key or "")
                if k.startswith("ROOM:") and k > end_key:
                    db.delete(ln)
                    did_delete += 1

        # update invoice meta + totals
        meta = inv.meta_json or {}
        meta["ipd_room_breakdown"] = breakdown
        meta["ipd_room_last_sync_at"] = now_utc_naive().isoformat()
        meta["ipd_room_sync_range"] = {"from": start.isoformat(), "to": end.isoformat()}
        inv.meta_json = meta

        inv.updated_by = user_id
        inv.service_date = datetime.combine(end, time(0, 0, 0))
        db.add(inv)
        db.flush()

        recalc_invoice_totals(db, inv.id)

        return {
            "billing_case_id": case.id,
            "invoice_id": inv.id,
            "created": created,
            "updated": updated,
            "deleted_future": did_delete,
            "missing_rate_days": breakdown.get("missing_rate_days", 0),
            "room_total": breakdown.get("total_amount", 0),
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
        }
    finally:
        _IN_SYNC_IPD.reset(token)


# =========================================================
# Adapter used by routes (FIXED: no recursion)
# =========================================================
def ensure_invoice_for_context(
    db: Session,
    patient_id: int,
    billing_type: str,
    context_type: str,
    context_id: int,
    user_id: Optional[int] = None,
    module: Optional[str] = None,
) -> BillingInvoice:
    """
    Route adapter.
    For IPD discharge/room billing: module should be "ROOM" (default).
    """
    bt = (billing_type or "").strip().lower()
    ct = (context_type or "").strip().lower()

    if bt not in ("ip_billing", "ip", "ipd"):
        raise HTTPException(status_code=400, detail=f"Unsupported billing_type: {billing_type}")

    if ct not in ("ipd", "admission", "ip_admission"):
        raise HTTPException(status_code=400, detail=f"Unsupported context_type: {context_type}")

    adm = _get_admission_or_404(db, context_id)

    mod = (module or "ROOM").upper()

    case = ensure_ipd_billing_case(
        db=db,
        admission_id=adm.id,
        patient_id=patient_id,
        user_id=user_id,
    )

    inv = ensure_invoice_for_case(
        db=db,
        billing_case=case,
        module=mod,
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=adm.patient_id,
        user_id=user_id,
    )
    return inv


# =========================================================
# Backward-compatible wrappers
# =========================================================
def ensure_ipd_invoice(db: Session, admission_id: int, patient_id: int, user_id: Optional[int] = None) -> BillingInvoice:
    case = ensure_ipd_billing_case(db, admission_id=admission_id, patient_id=patient_id, user_id=user_id)
    return ensure_invoice_for_case(
        db,
        billing_case=case,
        module="IPD",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=patient_id,
        user_id=user_id,
    )


def ensure_ipd_pharmacy_invoice(db: Session, admission_id: int, patient_id: int, user_id: Optional[int] = None) -> BillingInvoice:
    case = ensure_ipd_billing_case(db, admission_id=admission_id, patient_id=patient_id, user_id=user_id)
    return ensure_invoice_for_case(
        db,
        billing_case=case,
        module="PHARM",
        invoice_type=InvoiceType.PHARMACY,
        payer_type=PayerType.PATIENT,
        payer_id=patient_id,
        user_id=user_id,
    )


def ensure_ipd_ot_invoice(db: Session, admission_id: int, patient_id: int, user_id: Optional[int] = None) -> BillingInvoice:
    case = ensure_ipd_billing_case(db, admission_id=admission_id, patient_id=patient_id, user_id=user_id)
    return ensure_invoice_for_case(
        db,
        billing_case=case,
        module="OT",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=patient_id,
        user_id=user_id,
    )


def add_bed_charges_to_ipd_invoice(
    db: Session,
    admission_id: int,
    user_id: Optional[int] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> BillingInvoice:
    res = sync_ipd_room_charges(
        db=db,
        admission_id=admission_id,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        range_only=bool(from_date or to_date),
    )
    inv = db.get(BillingInvoice, res["invoice_id"])
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found after sync")
    return inv


def sync_lis_ris_to_ipd_invoice(
    db: Session,
    admission_id: int,
    user_id: Optional[int] = None,
    only_final_status: bool = True,
) -> BillingInvoice:
    """
    Placeholder wrapper: implement LIS/RIS → BillingInvoiceLine with idempotency later.
    """
    _ = only_final_status
    adm = _get_admission_or_404(db, admission_id)
    inv = ensure_ipd_invoice(db, admission_id=adm.id, patient_id=adm.patient_id, user_id=user_id)
    inv.updated_by = user_id
    db.add(inv)
    db.flush()
    return inv
