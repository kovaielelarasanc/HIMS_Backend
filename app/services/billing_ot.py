# FILE: app/services/billing_ot.py
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta
import uuid
import zlib

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.models.billing import Invoice, InvoiceItem
from app.models.ot import OtCase, OtSchedule, OtScheduleProcedure, OtProcedure
from app.models.ipd import IpdBed, IpdBedRate


# -----------------------------
# helpers
# -----------------------------
def _d(x) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")


def _new_invoice_uid() -> str:
    return str(uuid.uuid4())


def _new_invoice_number() -> str:
    return f"INV-{uuid.uuid4().hex[:8].upper()}"


def _service_ref_id(key: str) -> int:
    """
    ✅ IMPORTANT FIX:
    crc32 returns 0..4,294,967,295 (can overflow signed INT).
    So we force into safe signed-int range (<= 2,147,483,647).
    """
    return (zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF)


def _next_seq_for_invoice(db: Session, invoice_id: int) -> int:
    max_seq = db.query(func.max(InvoiceItem.seq)).filter(
        InvoiceItem.invoice_id == invoice_id).scalar()
    return (max_seq or 0) + 1


def _compute_line(qty, price, disc_pct, disc_amt, tax_rate):
    qty = _d(qty)
    price = _d(price)
    base = qty * price

    disc_pct = _d(disc_pct)
    disc_amt = _d(disc_amt)

    if disc_pct and (not disc_amt or disc_amt == 0):
        disc_amt = (base * disc_pct / Decimal("100")).quantize(Decimal("0.01"))
    elif disc_amt and (not disc_pct or disc_pct == 0) and base:
        disc_pct = (disc_amt * Decimal("100") / base).quantize(Decimal("0.01"))

    taxable = base - disc_amt
    tax_rate = _d(tax_rate)
    tax_amt = (taxable * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
    line_total = (taxable + tax_amt).quantize(Decimal("0.01"))

    return qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total


def recalc_totals(inv: Invoice) -> None:
    gross = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")

    for it in (inv.items or []):
        if it.is_voided:
            continue
        gross += _d(it.quantity) * _d(it.unit_price)
        disc += _d(it.discount_amount)
        tax += _d(it.tax_amount)

    header_disc_amt = _d(getattr(inv, "header_discount_amount", 0))
    inv.gross_total = gross.quantize(Decimal("0.01"))
    inv.discount_total = (disc + header_disc_amt).quantize(Decimal("0.01"))
    inv.tax_total = tax.quantize(Decimal("0.01"))
    inv.net_total = (gross - disc - header_disc_amt + tax).quantize(
        Decimal("0.01"))

    paid = Decimal("0")
    for p in (inv.payments or []):
        paid += _d(p.amount)
    inv.amount_paid = paid.quantize(Decimal("0.01"))

    adv = Decimal("0")
    for a in (inv.advance_adjustments or []):
        adv += _d(a.amount_applied)
    inv.advance_adjusted = adv.quantize(Decimal("0.01"))

    inv.balance_due = (inv.net_total - inv.amount_paid -
                       inv.advance_adjusted).quantize(Decimal("0.01"))


def _ensure_ot_invoice(db: Session, *, patient_id: int, case_id: int,
                       created_by: int | None) -> Invoice:
    ctx_type = "ot_case"
    ctx_id = int(case_id)

    inv = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id,
        Invoice.billing_type == "ot",
        Invoice.context_type == ctx_type,
        Invoice.context_id == ctx_id,
        Invoice.status != "cancelled",
    ).order_by(Invoice.id.desc()).first())

    if inv:
        if not inv.invoice_uid:
            inv.invoice_uid = _new_invoice_uid()
        if not inv.invoice_number:
            inv.invoice_number = _new_invoice_number()
        db.flush()
        return inv

    inv = Invoice(
        invoice_uid=_new_invoice_uid(),
        invoice_number=_new_invoice_number(),
        patient_id=patient_id,
        context_type=ctx_type,
        context_id=ctx_id,
        billing_type="ot",
        status="draft",
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


def _hours_between(start: datetime, end: datetime) -> Decimal:
    secs = Decimal(str((end - start).total_seconds()))
    hrs = (secs / Decimal("3600")).quantize(Decimal("0.01"))
    return hrs if hrs > 0 else Decimal("0")


def _get_billable_window(case: OtCase, sched: OtSchedule):
    # ✅ prefer actual surgery time
    if case.actual_start_time and case.actual_end_time:
        return case.actual_start_time, case.actual_end_time

    # fallback to planned time if actual missing
    if sched and sched.date and sched.planned_start_time:
        start = datetime.combine(sched.date, sched.planned_start_time)
        if sched.planned_end_time:
            end = datetime.combine(sched.date, sched.planned_end_time)
        else:
            end = start + timedelta(hours=1)
        return start, end

    return None, None


def _resolve_ot_bed_hourly_rate(db: Session,
                                *,
                                bed: IpdBed,
                                on_date=None) -> Decimal:
    """
    Uses IpdBed.room -> IpdRoom.room_type / type as room_type.
    Picks active effective bed rate and converts to hourly (daily/24).
    """
    room = getattr(bed, "room", None)
    room_type = (getattr(room, "room_type", None)
                 or getattr(room, "type", None) or "General")
    day = on_date or datetime.utcnow().date()

    r = (
        db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
            IpdBedRate.room_type == room_type).filter(
                IpdBedRate.effective_from <= day).filter(
                    (IpdBedRate.effective_to == None) |
                    (IpdBedRate.effective_to >= day))  # noqa
        .order_by(IpdBedRate.effective_from.desc()).first())
    if not r:
        return Decimal("0")

    daily = _d(getattr(r, "daily_rate", 0))
    if daily <= 0:
        return Decimal("0")

    return (daily / Decimal("24")).quantize(Decimal("0.01"))


# -----------------------------
# MAIN: create OT invoice items
# -----------------------------
def create_ot_invoice_items_for_case(db: Session, *, case_id: int,
                                     user_id: int | None) -> Invoice:
    """
    ✅ Idempotent OT billing:
      - Ensures OT invoice for context (ot_case, case_id)
      - Adds procedure charges (hours * rate_per_hour)
      - Adds OT bed charges (hours * hourly bed rate)
    """
    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.procedures).joinedload(OtScheduleProcedure.procedure),
        joinedload(OtCase.schedule).joinedload(OtSchedule.primary_procedure),
        joinedload(OtCase.schedule).joinedload(OtSchedule.ot_bed).joinedload(
            IpdBed.room),
    ).filter(OtCase.id == case_id).first())
    if not case:
        raise Exception("OT case not found")

    sched = case.schedule
    if not sched or not sched.patient_id:
        raise Exception("OT case not linked to schedule/patient")

    start, end = _get_billable_window(case, sched)
    if not start or not end or end <= start:
        raise Exception("OT timings missing/invalid, cannot bill")

    hours = _hours_between(start, end)
    if hours <= 0:
        raise Exception("OT duration is zero, cannot bill")

    inv = _ensure_ot_invoice(db,
                             patient_id=sched.patient_id,
                             case_id=case.id,
                             created_by=user_id)

    # existing active items (idempotent)
    existing = set(
        int(x[0]) for x in db.query(InvoiceItem.service_ref_id).filter(
            InvoiceItem.invoice_id == inv.id,
            InvoiceItem.is_voided.is_(False),
            InvoiceItem.service_ref_id.isnot(None),
        ).all() if x[0] is not None)

    # --------------------------------------
    # 1) PROCEDURE CHARGES
    # --------------------------------------
    proc_links = list(getattr(sched, "procedures", []) or [])

    # ✅ fallback: if no link rows exist, bill primary_procedure (since you store primary_procedure_id)
    procedures_to_bill: list[OtProcedure] = []
    if proc_links:
        for link in proc_links:
            p = getattr(link, "procedure", None)
            if p:
                procedures_to_bill.append(p)
    else:
        if getattr(sched, "primary_procedure", None):
            procedures_to_bill.append(sched.primary_procedure)

    if not procedures_to_bill:
        raise Exception(
            "No OT procedures linked (and no primary procedure). Cannot bill.")

    for proc in procedures_to_bill:
        rate = _d(getattr(proc, "rate_per_hour", 0))
        if rate <= 0:
            continue

        ref_key = f"otcase:{case.id}:proc:{proc.id}"
        safe_ref = _service_ref_id(ref_key)
        if safe_ref in existing:
            continue

        seq = _next_seq_for_invoice(db, inv.id)
        desc = f"OT Procedure — {proc.name} ({float(hours):.2f} hr) — Case #{case.id}"

        qty, unit_price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
            hours, rate, Decimal("0"), Decimal("0"), Decimal("0"))

        db.add(
            InvoiceItem(
                invoice_id=inv.id,
                seq=seq,
                service_type="ot_procedure",
                service_ref_id=safe_ref,
                description=desc,
                quantity=qty,
                unit_price=unit_price,
                tax_rate=tax_rate,
                discount_percent=disc_pct,
                discount_amount=disc_amt,
                tax_amount=tax_amt,
                line_total=line_total,
                is_voided=False,
                created_by=user_id,
            ))
        existing.add(safe_ref)

    # --------------------------------------
    # 2) OT BED CHARGES
    # --------------------------------------
    if sched.ot_bed_id and sched.ot_bed:
        hourly_rate = _resolve_ot_bed_hourly_rate(db,
                                                  bed=sched.ot_bed,
                                                  on_date=start.date())
        if hourly_rate > 0:
            ref_key = f"otcase:{case.id}:ot_bed:{sched.ot_bed_id}"
            safe_ref = _service_ref_id(ref_key)

            if safe_ref not in existing:
                seq = _next_seq_for_invoice(db, inv.id)
                desc = f"OT Bed Charges — OT Bed #{sched.ot_bed_id} ({float(hours):.2f} hr) — Case #{case.id}"

                qty, unit_price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
                    hours, hourly_rate, Decimal("0"), Decimal("0"),
                    Decimal("0"))

                db.add(
                    InvoiceItem(
                        invoice_id=inv.id,
                        seq=seq,
                        service_type="ot_bed",
                        service_ref_id=safe_ref,
                        description=desc,
                        quantity=qty,
                        unit_price=unit_price,
                        tax_rate=tax_rate,
                        discount_percent=disc_pct,
                        discount_amount=disc_amt,
                        tax_amount=tax_amt,
                        line_total=line_total,
                        is_voided=False,
                        created_by=user_id,
                    ))
                existing.add(safe_ref)

    db.flush()

    # reload invoice for totals
    inv = (db.query(Invoice).options(
        joinedload(Invoice.items),
        joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments),
    ).get(inv.id))

    recalc_totals(inv)
    inv.updated_by = user_id
    db.flush()
    return inv
