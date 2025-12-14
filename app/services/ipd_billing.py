# FILE: app/services/ipd_billing.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.ipd import IpdAdmission, IpdBedAssignment, IpdBed, IpdRoom, IpdBedRate, IpdDischargeSummary
from app.models.billing import Invoice

from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceItem
from app.models.ipd import IpdAdmission

# ... keep your existing imports + functions (compute_ipd_bed_charges_daily, ensure_invoice_for_context, etc.) ...
# -------------------------
# Helpers / Rules
# -------------------------


def _date_range_inclusive(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def _resolve_rate(db: Session, room_type: str, for_date: date) -> Decimal:
    rt = _normalize_room_type(room_type)

    r = (
        db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
            IpdBedRate.room_type == rt).filter(
                IpdBedRate.effective_from <= for_date).filter(
                    (IpdBedRate.effective_to == None) |
                    (IpdBedRate.effective_to >= for_date))  # noqa
        .order_by(IpdBedRate.effective_from.desc()).first())
    return (r.daily_rate if r else Decimal("0.00"))


def _normalize_room_type(x: Optional[str]) -> str:
    s = (x or "General").strip()
    # choose ONE normalization style for whole product:
    return s.title()  # "deluxe" -> "Deluxe", "DELUXE" -> "Deluxe"


def _get_room_type(db: Session, bed_id: int) -> str:
    bed = db.get(IpdBed, bed_id)
    if not bed or not getattr(bed, "room_id", None):
        return "General"
    room = db.get(IpdRoom, bed.room_id)
    return _normalize_room_type(getattr(room, "type", None))


def _get_admission_or_404(db: Session, admission_id: int) -> IpdAdmission:
    adm = db.get(IpdAdmission, admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    return adm


def get_ipd_discharge_ts(db: Session, admission_id: int) -> Optional[datetime]:
    """
    Single source for discharge date/time used for billing end.
    Priority:
      1) DischargeSummary.discharge_datetime
      2) Admission.discharge_at
      3) None
    """
    ds = (db.query(IpdDischargeSummary).filter(
        IpdDischargeSummary.admission_id == admission_id).first())
    if ds and ds.discharge_datetime:
        return ds.discharge_datetime

    adm = db.get(IpdAdmission, admission_id)
    if adm and getattr(adm, "discharge_at", None):
        return adm.discharge_at

    return None


def fetch_assignments(db: Session,
                      admission_id: int) -> List[IpdBedAssignment]:
    return (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id).order_by(
            IpdBedAssignment.from_ts.asc()).all())


def active_assignment_for_day(assigns: List[IpdBedAssignment],
                              day: date) -> Optional[IpdBedAssignment]:
    """
    Rule (Per-calendar-day):
    pick the assignment active at END OF DAY (23:59:59).
    This avoids double billing on transfer days.
    """
    eod = datetime.combine(day, time.max)
    active = None
    for a in assigns:
        start_ok = a.from_ts <= eod
        end_ok = (a.to_ts is None) or (a.to_ts >= datetime.combine(
            day, time.min))
        if start_ok and end_ok:
            active = a
    return active


def compute_ipd_bed_charges_daily(
    db: Session,
    admission_id: int,
    from_date: date,
    to_date: date,
) -> Dict[str, Any]:
    if to_date < from_date:
        raise HTTPException(400, "to_date must be >= from_date")

    _get_admission_or_404(db, admission_id)
    assigns = fetch_assignments(db, admission_id)

    days = []
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

        days.append({
            "date": d.isoformat(),
            "assignment_id": a.id,
            "bed_id": a.bed_id,
            "room_type": room_type,
            "rate": float(rate),  # keep API friendly
        })

    return {
        "admission_id": admission_id,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "days": days,
        "missing_rate_days": missing_rate_days,
        "total_amount": float(total.quantize(Decimal("0.01"))),
    }


# -------------------------
# Invoice helpers
# -------------------------


def ensure_invoice_for_context(
    db: Session,
    patient_id: int,
    billing_type: str,
    context_type: str,
    context_id: int,
) -> Invoice:
    """
    Idempotent: return existing invoice for this IPD admission context, else create.
    (Assumes Invoice has columns: patient_id, billing_type, context_type, context_id)
    """
    inv = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id,
        Invoice.billing_type == billing_type,
        Invoice.context_type == context_type,
        Invoice.context_id == context_id,
    ).first())
    if inv:
        return inv

    inv = Invoice(
        patient_id=patient_id,
        billing_type=billing_type,
        context_type=context_type,
        context_id=context_id,
        status="draft",
    )
    db.add(inv)
    db.flush()
    return inv


def auto_finalize_ipd_on_discharge(db: Session, admission_id: int,
                                   user_id: int) -> Dict[str, Any]:
    """
    Called on discharge finalization:
    - calculate bed charges (daily rule)
    - update invoice with totals + store breakdown safely
    """
    adm = _get_admission_or_404(db, admission_id)

    discharge_ts = get_ipd_discharge_ts(db, admission_id) or datetime.utcnow()
    from_date = (adm.admitted_at or datetime.utcnow()).date()
    to_date = discharge_ts.date()

    breakdown = compute_ipd_bed_charges_daily(db, admission_id, from_date,
                                              to_date)

    inv = ensure_invoice_for_context(
        db=db,
        patient_id=adm.patient_id,
        billing_type="ip_billing",
        context_type="ipd",
        context_id=adm.id,
    )

    # ✅ Safe storage method (doesn't depend on InvoiceItem table)
    # Use any JSON/text field you already have (examples: inv.meta_json, inv.notes_json, inv.extra)
    # Adjust the field name to match your Invoice model.
    if hasattr(inv, "meta_json"):
        meta = getattr(inv, "meta_json") or {}
        meta["bed_charges"] = breakdown
        inv.meta_json = meta
    elif hasattr(inv, "notes"):
        inv.notes = (inv.notes or "") + f"\n[BedCharges] {breakdown}"
    else:
        # last fallback: attach as attribute (won't persist unless column exists)
        setattr(inv, "bed_charges_breakdown", breakdown)

    # Update totals (adjust field names as per your Invoice model)
    if hasattr(inv, "total_amount"):
        inv.total_amount = breakdown["total_amount"]
    if hasattr(inv, "status"):
        inv.status = "final"  # or "posted" based on your billing workflow

    if hasattr(inv, "updated_by"):
        inv.updated_by = user_id
    if hasattr(inv, "updated_at"):
        inv.updated_at = datetime.utcnow()

    db.add(inv)
    db.flush()

    return {"invoice_id": getattr(inv, "id", None), "bed_charges": breakdown}


# ADD inside: FILE: app/services/ipd_billing.py


def _bed_item_ref(admission_id: int, day: date) -> int:
    # admission_id * 10^8 + YYYYMMDD (fixed 8 digits)
    ymd = int(day.strftime("%Y%m%d"))
    return admission_id * 100_000_000 + ymd


def _calc_line_total(qty: Decimal, unit_price: Decimal,
                     discount_amount: Decimal,
                     tax_rate: Decimal) -> Dict[str, Decimal]:
    """
    Calculates tax_amount + line_total for InvoiceItem.
    tax is computed on (qty*unit_price - discount_amount).
    """
    base = (qty * unit_price) - discount_amount
    if base < 0:
        base = Decimal("0")
    tax_amount = (base * tax_rate / Decimal("100")).quantize(Decimal("0.01"))
    line_total = (base + tax_amount).quantize(Decimal("0.01"))
    return {"tax_amount": tax_amount, "line_total": line_total}


def apply_ipd_bed_charges_to_invoice(
    db: Session,
    admission_id: int,
    upto_date: date,
    user_id: Optional[int] = None,
    tax_rate: float = 0.0,
    invoice_id: Optional[int] = None,
    skip_if_already_billed: bool = False,
) -> Dict[str, Any]:
    """
    Create/Update InvoiceItem rows for IPD bed charges up to upto_date.

    - One item per calendar day
    - Idempotent (safe to run multiple times)
    - Voids extra bed-charge items beyond upto_date
    """
    adm: IpdAdmission = db.get(IpdAdmission, admission_id)
    if not adm:
        raise ValueError("Admission not found")

    from_date = (adm.admitted_at or datetime.utcnow()).date()
    to_date = upto_date

    breakdown = compute_ipd_bed_charges_daily(db, admission_id, from_date,
                                              to_date)
    days = breakdown.get("days") or []

    # Resolve invoice
    if invoice_id:
        inv = db.get(Invoice, invoice_id)
        if not inv:
            raise ValueError("Invoice not found")
    else:
        inv = ensure_invoice_for_context(
            db=db,
            patient_id=adm.patient_id,
            billing_type="ip_billing",
            context_type="ipd",
            context_id=adm.id,
        )

    # Existing bed items for this admission (active only)
    existing_items = (db.query(InvoiceItem).filter(
        InvoiceItem.invoice_id == inv.id,
        InvoiceItem.service_type == "ipd_bed",
        InvoiceItem.is_voided.is_(False),
        (InvoiceItem.service_ref_id // 100_000_000) == admission_id,
    ).all())
    existing_by_ref = {int(i.service_ref_id or 0): i for i in existing_items}

    tax_rate_dec = Decimal(str(tax_rate or 0))
    created = 0
    updated = 0
    skipped = 0

    # We'll append new items after max seq
    max_seq = (db.query(
        InvoiceItem.seq).filter(InvoiceItem.invoice_id == inv.id).order_by(
            InvoiceItem.seq.desc()).first())
    next_seq = int(max_seq[0]) + 1 if max_seq and max_seq[0] else 1

    # Upsert day items
    for row in days:
        day = date.fromisoformat(row["date"])
        rate = Decimal(str(row.get("rate") or 0))
        room_type = row.get("room_type") or "General"

        ref = _bed_item_ref(admission_id, day)

        if skip_if_already_billed and ref in existing_by_ref:
            skipped += 1
            continue

        desc = f"Bed Charges ({room_type}) - {day.strftime('%d-%m-%Y')}"
        qty = Decimal("1")
        disc_amt = Decimal("0")
        disc_pct = Decimal("0")

        calced = _calc_line_total(qty, rate, disc_amt, tax_rate_dec)

        item = existing_by_ref.get(ref)

        if item:
            # Update existing
            item.description = desc
            item.quantity = qty
            item.unit_price = rate
            item.tax_rate = tax_rate_dec
            item.discount_percent = disc_pct
            item.discount_amount = disc_amt
            item.tax_amount = calced["tax_amount"]
            item.line_total = calced["line_total"]
            if user_id is not None:
                item.updated_by = user_id
            updated += 1
        else:
            # Create new
            item = InvoiceItem(
                invoice_id=inv.id,
                seq=next_seq,
                service_type="ipd_bed",
                service_ref_id=ref,
                description=desc,
                quantity=qty,
                unit_price=rate,
                tax_rate=tax_rate_dec,
                discount_percent=disc_pct,
                discount_amount=disc_amt,
                tax_amount=calced["tax_amount"],
                line_total=calced["line_total"],
                created_by=user_id,
                updated_by=user_id,
                is_voided=False,
            )
            db.add(item)
            next_seq += 1
            created += 1

    # Void any existing bed items AFTER upto_date (if discharge date changed later/earlier)
    upto_ymd = int(upto_date.strftime("%Y%m%d"))
    for item in existing_items:
        ref = int(item.service_ref_id or 0)
        ymd = ref % 100_000_000
        if ymd > upto_ymd:
            item.is_voided = True
            item.void_reason = "Auto-void: beyond discharge date"
            item.voided_at = datetime.utcnow()
            if user_id is not None:
                item.voided_by = user_id

    db.flush()

    return {
        "invoice_id": inv.id,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "bed_total": breakdown.get("total_amount", 0),
        "missing_rate_days": breakdown.get("missing_rate_days", 0),
    }
