from __future__ import annotations

from decimal import Decimal
import zlib
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.billing import Invoice, InvoiceItem
from app.models.ris import RisOrder

# IMPORTANT: your master is RadiologyTest table
# In your project you wrote it is in opd models:
from app.models.opd import RadiologyTest


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
    gross = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")

    for it in inv.items:
        if it.is_voided:
            continue
        qty = _d(it.quantity)
        price = _d(it.unit_price)
        gross += qty * price
        disc += _d(it.discount_amount)
        tax += _d(it.tax_amount)

    header_disc_amt = _d(inv.header_discount_amount)
    header_disc_pct = _d(inv.header_discount_percent)

    if header_disc_pct and (not header_disc_amt or header_disc_amt == 0):
        header_disc_amt = (gross - disc) * header_disc_pct / Decimal("100")
        header_disc_amt = header_disc_amt.quantize(Decimal("0.01"))
        inv.header_discount_amount = header_disc_amt

    inv.gross_total = gross.quantize(Decimal("0.01"))
    inv.discount_total = (disc + header_disc_amt).quantize(Decimal("0.01"))
    inv.tax_total = tax.quantize(Decimal("0.01"))
    inv.net_total = (gross - disc - header_disc_amt + tax).quantize(
        Decimal("0.01"))

    paid = Decimal("0")
    for p in inv.payments:
        paid += _d(p.amount)
    inv.amount_paid = paid.quantize(Decimal("0.01"))

    adv = Decimal("0")
    for a in inv.advance_adjustments:
        adv += _d(a.amount_applied)
    inv.advance_adjusted = adv.quantize(Decimal("0.01"))

    inv.balance_due = (inv.net_total - inv.amount_paid -
                       inv.advance_adjusted).quantize(Decimal("0.01"))


def ensure_invoice_for_ris(db: Session, *, order: RisOrder,
                           created_by: int | None) -> Invoice:
    """
    billing_type = 'radiology'
    context:
      - if order.context_type + context_id exists -> use it (opd/ipd)
      - else fallback -> ('ris', order.id)
    """
    ctx_type = (order.context_type or "").strip() or None
    ctx_id = order.context_id

    if not ctx_type or not ctx_id:
        ctx_type = "ris"
        ctx_id = int(order.id)

    inv = (db.query(Invoice).filter(
        Invoice.patient_id == order.patient_id,
        Invoice.billing_type == "radiology",
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
        billing_type="radiology",
        status="draft",
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


def bill_ris_order(db: Session, *, order: RisOrder,
                   created_by: int | None) -> Invoice:
    """
    One-shot:
      - find/create invoice
      - add ONE invoice item for the RIS order
      - idempotent safe_ref prevents duplicates
    """
    inv = ensure_invoice_for_ris(db, order=order, created_by=created_by)

    # price from master
    test = db.query(RadiologyTest).get(order.test_id)
    price = _d(getattr(test, "price", 0) or 0)

    # ✅ global safe id (unique per invoice context + ris order)
    ref_key = f"{inv.context_type}:{inv.context_id}:radiology:{order.id}"
    safe_ref = _service_ref_id(ref_key)

    exists = (db.query(InvoiceItem).filter(
        InvoiceItem.invoice_id == inv.id,
        InvoiceItem.service_type == "radiology",
        InvoiceItem.service_ref_id == safe_ref,
        InvoiceItem.is_voided.is_(False),
    ).first())
    if not exists:
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
                service_type="radiology",
                service_ref_id=safe_ref,
                description=(order.test_name or "Radiology Test").strip(),
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

    db.flush()
    # totals
    inv = db.query(Invoice).get(inv.id)
    recalc_totals(inv, db)
    inv.updated_by = created_by
    db.flush()

    return inv
