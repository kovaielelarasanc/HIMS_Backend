from __future__ import annotations

from decimal import Decimal
import zlib
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.billing import Invoice, InvoiceItem
from app.models.lis import LisOrder, LisOrderItem
from app.models.opd import LabTest
from decimal import Decimal
from datetime import date
from sqlalchemy.orm import Session
from app.models.ipd import IpdBedRate, IpdBed, IpdRoom
from app.services.room_type import normalize_room_type

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
    return int(zlib.crc32(key.encode("utf-8")))


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


def recalc_totals(inv: Invoice, db: Session) -> None:
    gross_subtotal = Decimal("0")
    line_discount = Decimal("0")
    tax_total = Decimal("0")

    for it in inv.items:
        if it.is_voided:
            continue
        qty = _d(it.quantity)
        price = _d(it.unit_price)
        base = qty * price

        gross_subtotal += base
        line_discount += _d(it.discount_amount)
        tax_total += _d(it.tax_amount)

    header_disc_amt = _d(inv.header_discount_amount)
    header_disc_pct = _d(inv.header_discount_percent)

    if header_disc_pct and (not header_disc_amt or header_disc_amt == 0):
        header_disc_amt = (gross_subtotal -
                           line_discount) * header_disc_pct / Decimal("100")
        header_disc_amt = header_disc_amt.quantize(Decimal("0.01"))
        inv.header_discount_amount = header_disc_amt

    total_discount = (line_discount + header_disc_amt).quantize(
        Decimal("0.01"))
    net = (gross_subtotal - line_discount - header_disc_amt +
           tax_total).quantize(Decimal("0.01"))

    inv.gross_total = gross_subtotal.quantize(Decimal("0.01"))
    inv.tax_total = tax_total.quantize(Decimal("0.01"))
    inv.discount_total = total_discount
    inv.net_total = net

    paid = Decimal("0")
    for pay in inv.payments:
        paid += _d(pay.amount)
    inv.amount_paid = paid.quantize(Decimal("0.01"))

    adv_used = Decimal("0")
    for adj in inv.advance_adjustments:
        adv_used += _d(adj.amount_applied)
    inv.advance_adjusted = adv_used.quantize(Decimal("0.01"))

    inv.balance_due = (net - paid - adv_used).quantize(Decimal("0.01"))


def ensure_invoice_for_lis(db: Session, *, order: LisOrder,
                           created_by: int | None) -> Invoice:
    """
    Rule:
      - If order has context_type+context_id => invoice context = that (opd/ipd)
      - Else => invoice context = ('lis', order.id)
      - billing_type always 'lab'
    """
    ctx_type = (order.context_type or "").strip() or None
    ctx_id = order.context_id

    if not ctx_type or not ctx_id:
        ctx_type = "lis"
        ctx_id = int(order.id)

    inv = (db.query(Invoice).filter(
        Invoice.patient_id == order.patient_id,
        Invoice.billing_type == "lab",
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
        patient_id=order.patient_id,
        context_type=ctx_type,
        context_id=ctx_id,
        billing_type="lab",
        status="draft",
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


def add_lis_items_to_invoice(db: Session, *, inv: Invoice, order: LisOrder,
                             created_by: int | None) -> int:
    """
    Adds each LisOrderItem as a 'lab' InvoiceItem with unit_price from LabTest.price.
    Idempotent: safe service_ref_id prevents duplicate insertion.
    """
    lis_items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order.id).all()

    created_count = 0
    for li in lis_items:
        test = db.query(LabTest).get(li.test_id)
        price = _d(getattr(test, "price", 0) or 0)

        # ✅ create a unique key that includes invoice context + lis_item id
        ref_key = f"{inv.context_type}:{inv.context_id}:lab:{li.id}"
        safe_ref = _service_ref_id(ref_key)

        exists = (db.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == inv.id,
            InvoiceItem.service_type == "lab",
            InvoiceItem.service_ref_id == safe_ref,
            InvoiceItem.is_voided.is_(False),
        ).first())
        if exists:
            continue

        seq = _next_seq_for_invoice(db, inv.id)

        qty, unit_price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
            Decimal("1"),
            price,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )

        db.add(
            InvoiceItem(
                invoice_id=inv.id,
                seq=seq,
                service_type="lab",
                service_ref_id=safe_ref,
                description=(li.test_name or getattr(test, "name", "")
                             or "Lab Test").strip(),
                quantity=qty,
                unit_price=unit_price,
                tax_rate=tax_rate,
                discount_percent=disc_pct,
                discount_amount=disc_amt,
                tax_amount=tax_amt,
                line_total=line_total,
                is_voided=False,
                created_by=created_by,
            ))
        created_count += 1

    db.flush()
    # refresh totals
    inv = (db.query(Invoice).filter(Invoice.id == inv.id).first())
    # ensure relationships loaded later by caller if needed; totals ok either way
    recalc_totals(inv, db)
    inv.updated_by = created_by
    db.flush()

    return created_count


def bill_lis_order(db: Session, *, order: LisOrder,
                   created_by: int | None) -> Invoice:
    """
    One-shot:
      - find/create invoice
      - add lis items
      - recalc totals
    """
    inv = ensure_invoice_for_lis(db, order=order, created_by=created_by)
    add_lis_items_to_invoice(db, inv=inv, order=order, created_by=created_by)
    db.flush()
    return inv



def _resolve_rate(db: Session, room_type: str, for_date: date) -> Decimal:
    rt = normalize_room_type(room_type)

    r = (
        db.query(IpdBedRate)
        .filter(IpdBedRate.is_active.is_(True))
        .filter(IpdBedRate.room_type == rt)
        .filter(IpdBedRate.effective_from <= for_date)
        .filter((IpdBedRate.effective_to == None) | (IpdBedRate.effective_to >= for_date))  # noqa
        .order_by(IpdBedRate.effective_from.desc())
        .first()
    )
    return (r.daily_rate if r else Decimal("0.00"))

def _get_room_type(db: Session, bed_id: int) -> str:
    bed = db.get(IpdBed, bed_id)
    if not bed or not getattr(bed, "room_id", None):
        return "General"
    room = db.get(IpdRoom, bed.room_id)
    return normalize_room_type(getattr(room, "type", None))