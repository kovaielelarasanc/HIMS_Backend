# app/services/ipd_billing.py
from __future__ import annotations
import os, zlib
from datetime import datetime, date, time, timedelta
from typing import List, Tuple
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceItem
from app.models.ipd import IpdAdmission, IpdBedAssignment, IpdBed, IpdRoom, IpdBedRate
from app.models.lis import LisOrder, LisOrderItem
from app.models.opd import LabTest, RadiologyTest
from app.models.ris import RisOrder

BILLING_AUTO_IPD_ON_DISCHARGE = os.getenv(
    "BILLING_AUTO_IPD_ON_DISCHARGE", "false").lower() in {"1", "true", "yes"}
DEFAULT_TAX = float(os.getenv("BILLING_DEFAULT_TAX", "0") or 0)


# ---- helpers reused from billing_auto ----
def _recompute(inv: Invoice):
    gross = 0.0
    tax = 0.0
    for it in inv.items:
        if it.is_voided:
            continue
        gross += float(it.unit_price) * int(it.quantity or 1)
        tax += float(it.tax_amount or 0)
    inv.gross_total = gross
    inv.tax_total = tax
    inv.net_total = gross + tax
    inv.balance_due = float(inv.net_total) - float(inv.amount_paid or 0)


def _find_or_create_draft_invoice(db: Session, *, patient_id: int,
                                  context_id: int,
                                  user_id: int | None) -> Invoice:
    inv = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id, Invoice.context_type == "ipd",
        Invoice.context_id == context_id,
        Invoice.status == "draft").order_by(Invoice.id.desc()).first())
    if inv:
        return inv
    inv = Invoice(patient_id=patient_id,
                  context_type="ipd",
                  context_id=context_id,
                  status="draft",
                  created_by=user_id)
    db.add(inv)
    db.flush()
    return inv


def _ensure_item(db: Session,
                 *,
                 inv: Invoice,
                 service_type: str,
                 service_ref_id: int,
                 description: str,
                 unit_price: float,
                 qty: int = 1,
                 tax_rate: float = DEFAULT_TAX,
                 user_id: int | None = None) -> InvoiceItem | None:
    exists = (db.query(InvoiceItem).filter(
        InvoiceItem.service_type == service_type,
        InvoiceItem.service_ref_id == service_ref_id,
        InvoiceItem.is_voided.is_(False)).first())
    if exists:
        return None
    tr = round(unit_price * qty * (tax_rate / 100.0), 2)
    lt = round(unit_price * qty + tr, 2)
    line = InvoiceItem(invoice_id=inv.id,
                       service_type=service_type,
                       service_ref_id=service_ref_id,
                       description=description,
                       quantity=qty,
                       unit_price=unit_price,
                       tax_rate=tax_rate,
                       tax_amount=tr,
                       line_total=lt,
                       created_by=user_id)
    db.add(line)
    _recompute(inv)
    return line


# ---- IPD bed-day computation ----


def _resolve_rate(db: Session, room_type: str, for_date: date) -> float | None:
    r = (
        db.query(IpdBedRate).filter(
            IpdBedRate.is_active.is_(True), IpdBedRate.room_type == room_type,
            IpdBedRate.effective_from <= for_date).filter(
                (IpdBedRate.effective_to == None) |
                (IpdBedRate.effective_to >= for_date))  # noqa: E711
        .order_by(IpdBedRate.effective_from.desc()).first())
    return float(r.daily_rate) if r else None


def _bed_days_for_window(db: Session, admission_id: int, from_date: date,
                         to_date: date) -> List[tuple[date, int, str, float]]:
    """Return [(d, assignment_id, room_type, rate)] for each calendar day."""
    assigns = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == admission_id).order_by(
            IpdBedAssignment.from_ts.asc()).all())
    out: list[tuple[date, int, str, float]] = []
    cursor = from_date
    while cursor <= to_date:
        eod = datetime.combine(cursor, time.max)
        active = None
        for a in assigns:
            started = a.from_ts <= eod
            ended_ok = (a.to_ts is None) or (a.to_ts >= datetime.combine(
                cursor, time.min))
            if started and ended_ok:
                active = a
        if active:
            bed = db.query(IpdBed).get(active.bed_id)
            room = db.query(IpdRoom).get(bed.room_id) if bed else None
            room_type = (room.type if room else "General")
            rate = _resolve_rate(db, room_type, cursor) or 0.0
            out.append((cursor, active.id, room_type, rate))
        cursor += timedelta(days=1)
    return out


def _bed_ref_id(assignment_id: int, d: date) -> int:
    # 32-bit unique id per (assignment_id, date) using CRC32
    key = f"bed:{assignment_id}:{d.isoformat()}".encode()
    return zlib.crc32(key)


# ---- unbilled clinical items tied to THIS admission only ----


def _add_unbilled_lis_ris_for_admission(db: Session, inv: Invoice,
                                        admission_id: int, patient_id: int,
                                        user_id: int | None):
    # LIS items linked to this admission
    lis_items = (db.query(LisOrderItem).join(
        LisOrder, LisOrderItem.order_id == LisOrder.id).filter(
            LisOrder.patient_id == patient_id, LisOrder.context_type == "ipd",
            LisOrder.context_id == admission_id,
            LisOrderItem.status.in_(["validated", "reported"])).all())
    for it in lis_items:
        # price from LabTest
        mt = db.query(LabTest).get(it.test_id)
        price = float(getattr(mt, "price", 0) or 0)
        desc = f"Lab: {it.test_name} ({it.test_code})"
        _ensure_item(db,
                     inv=inv,
                     service_type="lab",
                     service_ref_id=it.id,
                     description=desc,
                     unit_price=price,
                     qty=1,
                     user_id=user_id)

    # RIS orders linked to this admission
    ris_orders = (db.query(RisOrder).filter(
        RisOrder.patient_id == patient_id, RisOrder.context_type == "ipd",
        RisOrder.context_id == admission_id,
        RisOrder.status.in_(["reported", "approved"])).all())
    for ro in ris_orders:
        mt = db.query(RadiologyTest).get(ro.test_id)
        price = float(getattr(mt, "price", 0) or 0)
        desc = f"Radiology: {ro.test_name} ({ro.test_code})"
        _ensure_item(db,
                     inv=inv,
                     service_type="radiology",
                     service_ref_id=ro.id,
                     description=desc,
                     unit_price=price,
                     qty=1,
                     user_id=user_id)


def auto_finalize_ipd_on_discharge(db: Session,
                                   *,
                                   admission_id: int,
                                   user_id: int | None = None):
    """
    On discharge, generate/complete a SINGLE consolidated IPD invoice:
      - All bed-days in stay window
      - Any unbilled LIS/RIS for this admission
      - (OT/Pharmacy can be added similarly if you wire them to admission)
    Then finalize.
    """
    if not BILLING_AUTO_IPD_ON_DISCHARGE:
        return

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm or adm.status != "discharged":
        return

    patient_id = adm.patient_id
    # Compute stay window (calendar days) — customize if you exclude admission/discharge day
    start_d = adm.admitted_at.date()
    end_d = (adm.discharge_at.date() if getattr(adm, "discharge_at", None) else
             datetime.utcnow().date())

    inv = _find_or_create_draft_invoice(db,
                                        patient_id=patient_id,
                                        context_id=admission_id,
                                        user_id=user_id)

    # Bed-days
    for (d, assign_id, room_type,
         rate) in _bed_days_for_window(db, admission_id, start_d, end_d):
        ref_id = _bed_ref_id(assign_id, d)
        desc = f"Bed charge — {d.isoformat()} — {room_type}"
        _ensure_item(db,
                     inv=inv,
                     service_type="ipd",
                     service_ref_id=ref_id,
                     description=desc,
                     unit_price=rate,
                     qty=1,
                     user_id=user_id)

    # Clinical services for this admission
    _add_unbilled_lis_ris_for_admission(db, inv, admission_id, patient_id,
                                        user_id)

    # Finalize
    if inv.status == "draft" and any(not i.is_voided for i in inv.items):
        inv.status = "finalized"
        inv.finalized_at = datetime.utcnow()
        _recompute(inv)
        db.flush()
