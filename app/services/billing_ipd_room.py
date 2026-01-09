from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ipd import IpdAdmission, IpdBed, IpdRoom, IpdBedAssignment, IpdBedRate
from app.models.billing import (
    BillingCase,
    BillingCaseLink,
    BillingInvoice,
    BillingInvoiceLine,
    EncounterType,
    BillingCaseStatus,
    PayerMode,
    InvoiceType,
    PayerType,
    DocStatus,
    ServiceGroup,
    CoverageFlag,
)
from app.services.id_gen import next_billing_case_number, next_invoice_number

IST = timezone(timedelta(hours=5, minutes=30))


# -------------------------
# small helpers
# -------------------------
def _tenant_id_from_user(user) -> Optional[int]:
    return getattr(user, "tenant_id", None) or getattr(user, "hospital_id",
                                                       None)


def _d(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else 0))
    except Exception:
        return Decimal("0")


def _aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    DB may return naive UTC.
    Treat naive as UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    u = _aware_utc(dt)
    return u.astimezone(IST) if u else None


def _ist_day_bounds(d: date) -> Tuple[datetime, datetime]:
    """
    Returns IST-aware [start, end] for the given date.
    """
    start = datetime.combine(d, time.min).replace(tzinfo=IST)
    end = datetime.combine(d, time.max).replace(tzinfo=IST)
    return start, end


def _safe_add_case_link(db: Session, billing_case_id: int, entity_type: str,
                        entity_id: int) -> None:
    if not entity_id:
        return
    exists = (db.query(BillingCaseLink.id).filter(
        BillingCaseLink.billing_case_id == billing_case_id,
        BillingCaseLink.entity_type == entity_type,
        BillingCaseLink.entity_id == int(entity_id),
    ).first())
    if exists:
        return
    db.add(
        BillingCaseLink(
            billing_case_id=billing_case_id,
            entity_type=entity_type,
            entity_id=int(entity_id),
        ))


def _recalc_invoice_totals(db: Session, invoice_id: int) -> None:
    row = (db.query(
        func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
        func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
    ).filter(BillingInvoiceLine.invoice_id == invoice_id).first())
    sub_total, discount_total, tax_total, net_total = row or (0, 0, 0, 0)

    inv = db.query(BillingInvoice).get(invoice_id)
    if not inv:
        return

    inv.sub_total = sub_total
    inv.discount_total = discount_total
    inv.tax_total = tax_total
    inv.round_off = 0
    inv.grand_total = (net_total or 0)


def _sg_room() -> ServiceGroup:
    return ServiceGroup.ROOM


def _enc_ip() -> Any:
    if hasattr(EncounterType, "IP"):
        return EncounterType.IP
    if hasattr(EncounterType, "IPD"):
        return EncounterType.IPD
    return next(iter(EncounterType))


# -------------------------
# room type normalization (match your masters roughly)
# -------------------------
def _norm_room_type(x: Optional[str]) -> str:
    s = (x or "General").strip()
    if not s:
        return "General"
    key = " ".join(s.lower().split())
    if "nicu" in key:
        return "NICU"
    if "picu" in key:
        return "PICU"
    if "icu" in key:
        return "ICU"
    if "hdu" in key:
        return "HDU"
    if "deluxe" in key:
        return "Deluxe"
    if "semi" in key and "private" in key:
        return "Semi Private"
    if "private" in key:
        return "Private"
    if "general" in key:
        return "General"
    if "isolation" in key:
        return "Isolation"
    return s.title()


def _resolve_daily_rate(db: Session, room_type: str,
                        for_date: date) -> Optional[Decimal]:
    rt = _norm_room_type(room_type)
    r = (db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
        IpdBedRate.room_type == rt).filter(
            IpdBedRate.effective_from <= for_date).filter(
                (IpdBedRate.effective_to.is_(None))
                | (IpdBedRate.effective_to >= for_date)).order_by(
                    IpdBedRate.effective_from.desc()).first())
    return _d(r.daily_rate) if r else None


@dataclass
class _DayCharge:
    day: date
    assignment_id: int
    bed_id: Optional[int]
    bed_code: Optional[str]
    room_type: str
    rate: Decimal
    missing_rate: bool


def _compute_daily_charges_ist(
    db: Session,
    admission_id: int,
    from_date: date,
    to_date: date,
) -> List[_DayCharge]:
    """
    One row per IST day: determine active bed assignment for that day,
    then resolve rate by room_type + date.
    Mirrors your preview logic, but uses IST day boundaries safely.
    """
    assigns = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id).order_by(
            IpdBedAssignment.from_ts.asc()).all())

    out: List[_DayCharge] = []
    cursor = from_date
    while cursor <= to_date:
        day_start_ist, day_end_ist = _ist_day_bounds(cursor)

        active: Optional[IpdBedAssignment] = None
        for a in assigns:
            a_start = _to_ist(a.from_ts)
            a_end = _to_ist(a.to_ts) if a.to_ts else None
            if not a_start:
                continue

            start_ok = a_start <= day_end_ist
            end_ok = (a_end is None) or (a_end >= day_start_ist)
            if start_ok and end_ok:
                active = a  # keep last overlap => latest started

        if not active:
            cursor += timedelta(days=1)
            continue

        bed = db.query(IpdBed).get(active.bed_id) if active.bed_id else None
        room = db.query(IpdRoom).get(
            bed.room_id) if bed and bed.room_id else None
        room_type = _norm_room_type(getattr(room, "type", None) or "General")

        rate = _resolve_daily_rate(db, room_type, cursor)
        missing = rate is None
        rate_val = rate if rate is not None else Decimal("0")

        out.append(
            _DayCharge(
                day=cursor,
                assignment_id=int(active.id),
                bed_id=int(bed.id) if bed else None,
                bed_code=getattr(bed, "code", None) if bed else None,
                room_type=room_type,
                rate=rate_val,
                missing_rate=missing,
            ))

        cursor += timedelta(days=1)

    return out


# -------------------------
# public API: sync room charges
# -------------------------
def sync_ipd_room_charges(
    db: Session,
    *,
    admission_id: int,
    upto_dt: datetime,
    user,
    gst_rate: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """
    ✅ New billing module sync:
    - Creates/ensures BillingCase (Encounter=IP, encounter_id=admission_id)
    - Ensures PATIENT invoice
    - Upserts one room line per IST day (idempotent)
    - Updates amounts if room/rate changes
    """

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise ValueError("Admission not found")

    tenant_id = _tenant_id_from_user(user)
    patient_id = int(adm.patient_id)

    # date range in IST
    admitted_ist = _to_ist(adm.admitted_at) or datetime.now(IST)
    upto_ist = _to_ist(upto_dt) or datetime.now(IST)

    from_date = admitted_ist.date()
    to_date = upto_ist.date()

    # GST default (typically 0 for room; keep optional)
    gst = _d(gst_rate) if gst_rate is not None else Decimal("0")

    # 1) Ensure BillingCase
    case = (db.query(BillingCase).filter(
        BillingCase.encounter_type == _enc_ip(),
        BillingCase.encounter_id == int(admission_id),
    ).first())
    if not case:
        case = BillingCase(
            tenant_id=tenant_id,
            patient_id=patient_id,
            encounter_type=_enc_ip(),
            encounter_id=int(admission_id),
            case_number="TEMP",
            status=BillingCaseStatus.OPEN,
            payer_mode=PayerMode.SELF,
            tariff_plan_id=getattr(adm, "tariff_plan_id", None),
            created_by=getattr(user, "id", None),
            updated_by=getattr(user, "id", None),
        )
        db.add(case)
        db.flush()
        case.case_number = next_billing_case_number(
            db,
            tenant_id=tenant_id,
            encounter_type="IP",
            on_date=upto_dt,
            padding=6,
        )

    _safe_add_case_link(db, int(case.id), "IPD_ADMISSION", int(admission_id))

    # 2) Ensure main PATIENT invoice
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case.id,
        BillingInvoice.invoice_type == InvoiceType.PATIENT,
        BillingInvoice.payer_type == PayerType.PATIENT,
    ).order_by(BillingInvoice.id.asc()).first())
    if not inv:
        inv = BillingInvoice(
            billing_case_id=case.id,
            invoice_number="TEMP",
            invoice_type=InvoiceType.PATIENT,
            status=DocStatus.DRAFT,
            payer_type=PayerType.PATIENT,
            payer_id=None,
            currency="INR",
            created_by=getattr(user, "id", None),
            updated_by=getattr(user, "id", None),
        )
        db.add(inv)
        db.flush()
        inv.invoice_number = next_invoice_number(
            db,
            tenant_id=tenant_id,
            encounter_type="IP",
            on_date=upto_dt,
            padding=6,
        )

    # 3) Compute daily charges using assignments + rates
    days = _compute_daily_charges_ist(db, admission_id, from_date, to_date)

    missing_days = sum(1 for d in days if d.missing_rate)
    missing_room_types = sorted({d.room_type for d in days if d.missing_rate})

    # 4) Upsert per day
    for d in days:
        key = f"ROOM:{d.day.isoformat()}"  # idempotent key per day

        line = (db.query(BillingInvoiceLine).filter(
            BillingInvoiceLine.billing_case_id == case.id,
            BillingInvoiceLine.source_module == "IPD",
            BillingInvoiceLine.source_ref_id == int(admission_id),
            BillingInvoiceLine.source_line_key == key,
        ).first())

        qty = Decimal("1")
        unit_price = _d(d.rate)
        line_total = qty * unit_price
        discount_amount = Decimal("0")
        taxable = line_total - discount_amount
        tax_amount = (taxable * gst /
                      Decimal("100")) if gst > 0 else Decimal("0")
        net_amount = taxable + tax_amount

        desc = f"IP Room Charge ({d.room_type}) - {d.day.isoformat()}"
        if d.bed_code:
            desc += f" [Bed: {d.bed_code}]"
        if d.missing_rate:
            desc += " (MISSING RATE)"

        payload = dict(
            billing_case_id=case.id,
            invoice_id=inv.id,
            service_group=ServiceGroup.ROOM,
            item_type="IPD_ROOM",
            item_id=d.bed_id,
            item_code=None,
            description=desc,
            qty=qty,
            unit_price=unit_price,
            discount_percent=Decimal("0"),
            discount_amount=discount_amount,
            gst_rate=gst,
            tax_amount=tax_amount,
            line_total=line_total,
            net_amount=net_amount,
            revenue_head_id=None,
            cost_center_id=None,
            doctor_id=None,
            source_module="IPD",
            source_ref_id=int(admission_id),
            source_line_key=key,
            is_covered=CoverageFlag.NO,
            approved_amount=Decimal("0"),
            patient_pay_amount=net_amount,
            requires_preauth=False,
            is_manual=False,
            created_by=getattr(user, "id", None),
        )

        if line:
            for k, v in payload.items():
                setattr(line, k, v)
        else:
            db.add(BillingInvoiceLine(**payload))

    # 5) Optional cleanup: remove room lines AFTER to_date (if discharge reduced)
    # (We parse the key "ROOM:YYYY-MM-DD" in python)
    all_room_lines = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.billing_case_id == case.id,
        BillingInvoiceLine.source_module == "IPD",
        BillingInvoiceLine.source_ref_id == int(admission_id),
    ).all())
    for ln in all_room_lines:
        k = getattr(ln, "source_line_key", "") or ""
        if not k.startswith("ROOM:"):
            continue
        try:
            day_str = k.split("ROOM:", 1)[1].strip()
            ln_day = date.fromisoformat(day_str)
            if ln_day > to_date:
                db.delete(ln)
        except Exception:
            # ignore malformed keys
            continue

    db.flush()
    _recalc_invoice_totals(db, int(inv.id))
    case.status = BillingCaseStatus.READY_FOR_POST

    return {
        "billing_case_id": int(case.id),
        "invoice_id": int(inv.id),
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "days_billed": len(days),
        "missing_rate_days": int(missing_days),
        "missing_room_types": missing_room_types,
    }
