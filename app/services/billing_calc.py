# FILE: app/services/billing_calc.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.orm import Session

from app.models.billing import BillingInvoice, BillingInvoiceLine

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")


def _D(v: Any) -> Decimal:
    try:
        return Decimal(str(v if v is not None else "0"))
    except Exception:
        return Decimal("0")

def D(x) -> Decimal:
    # always convert safely (avoid float)
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))

def _q2(x: Decimal) -> Decimal:
    return _D(x).quantize(Q2, rounding=ROUND_HALF_UP)

def q2(x) -> Decimal:
    return D(x).quantize(Q2, rounding=ROUND_HALF_UP)

def q4(x) -> Decimal:
    return D(x).quantize(Q4, rounding=ROUND_HALF_UP)


def normalize_gst_rate(gst_rate) -> Decimal:
    """
    Accepts:
      - 5     (already percent)
      - 5.00  (already percent)
      - 0.05  (stored fraction for 5%)  ✅ your current DB issue
    Returns percent in [0..100] with 2 decimals.
    """
    r = q2(gst_rate)
    if r > 0 and r < 1:
        r = q2(r * Decimal("100"))
    if r < 0 or r > 100:
        raise ValueError(f"Invalid gst_rate: {r}")
    return r


def recompute_invoice_line(line) -> dict:
    """
    Sets:
      line_total (gross) = qty * unit_price
      discount_amount
      tax_amount          ✅ your missing/wrong value
      net_amount          = (gross - discount) + tax
    """
    qty = q4(getattr(line, "qty", 0))
    unit = q2(getattr(line, "unit_price", 0))
    gross = q2(qty * unit)

    disc_amt = q2(getattr(line, "discount_amount", 0))
    disc_pct = D(getattr(line, "discount_percent", 0))

    # if discount_amount not given but percent given
    if disc_amt <= 0 and disc_pct > 0:
        disc_amt = q2(gross * disc_pct / Decimal("100"))

    # clamp
    if disc_amt > gross:
        disc_amt = gross
    if disc_amt < 0:
        disc_amt = Decimal("0.00")

    taxable = q2(gross - disc_amt)

    gst = normalize_gst_rate(getattr(line, "gst_rate", 0))
    tax = q2(taxable * gst / Decimal("100"))

    # persist
    line.qty = qty
    line.unit_price = unit
    line.gst_rate = gst              # ✅ ensures DB becomes 5.00 instead of 0.05 going forward
    line.line_total = gross
    line.discount_amount = disc_amt
    line.tax_amount = tax            # ✅ FIX
    line.net_amount = q2(taxable + tax)

    return {
        "gross": gross,
        "discount": disc_amt,
        "taxable": taxable,
        "gst_rate": gst,
        "tax": tax,
        "net": line.net_amount,
    }



def recompute_invoice(inv) -> dict:
    """
    Uses invoice.lines and updates:
      sub_total, discount_total, tax_total, grand_total (and round_off if you want)
    """
    sub = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")
    net = Decimal("0")

    lines = getattr(inv, "lines", None) or []
    for ln in lines:
        recompute_invoice_line(ln)
        sub += D(ln.line_total)
        disc += D(ln.discount_amount)
        tax += D(ln.tax_amount)
        net += D(ln.net_amount)

    inv.sub_total = q2(sub)
    inv.discount_total = q2(disc)
    inv.tax_total = q2(tax)

    # If you don't do rounding, keep this 0
    inv.round_off = q2(getattr(inv, "round_off", 0) or 0)

    # Grand total should match sum(net_amount) (+ round_off if you apply)
    inv.grand_total = q2(net + D(inv.round_off))

    return {
        "sub_total": inv.sub_total,
        "discount_total": inv.discount_total,
        "tax_total": inv.tax_total,
        "round_off": inv.round_off,
        "grand_total": inv.grand_total,
    }






def line_is_deleted(ln: BillingInvoiceLine) -> bool:
    """
    Robust "deleted/tombstoned" detector compatible with:
      - is_deleted / is_active / deleted_at / voided_at / status
      - meta_json["deleted"] == True OR meta_json["deleted"] is dict (deleted_info)
      - fallback: qty <= 0 and '(REMOVED)' in description
    """
    # column conventions
    try:
        if hasattr(ln, "is_deleted") and bool(getattr(ln,
                                                      "is_deleted")) is True:
            return True
        if hasattr(ln, "is_active") and getattr(ln, "is_active") is False:
            return True
        if hasattr(ln, "deleted_at") and getattr(ln, "deleted_at",
                                                 None) is not None:
            return True
        if hasattr(ln, "voided_at") and getattr(ln, "voided_at",
                                                None) is not None:
            return True
        if hasattr(ln, "status"):
            st = getattr(ln, "status", None)
            s = st if isinstance(st, str) else (getattr(
                st, "value", None) or getattr(st, "name", None) or str(st))
            if str(s).upper() in {
                    "VOID", "CANCELLED", "CANCELED", "DELETED", "REMOVED"
            }:
                return True
    except Exception:
        pass

    # meta_json conventions
    meta = getattr(ln, "meta_json", None) or {}
    if isinstance(meta, dict):
        dv = meta.get("deleted")
        # allow bool OR dict
        if dv is True or isinstance(dv, dict):
            return True
        if meta.get("deleted_flag") is True:
            return True

    # fallback tombstone marker
    try:
        qty = _D(getattr(ln, "qty", 0))
        desc = (getattr(ln, "description", "") or "")
        if qty <= 0 and "(REMOVED)" in desc:
            return True
    except Exception:
        pass

    return False


def recompute_line_amounts(ln: BillingInvoiceLine) -> BillingInvoiceLine:
    qty = _D(getattr(ln, "qty", 0))
    unit = _D(getattr(ln, "unit_price", 0))
    gross = _q2(qty * unit)

    disc_pct = _D(getattr(ln, "discount_percent", 0))
    disc_amt = _D(getattr(ln, "discount_amount", 0))

    if disc_amt <= 0 and disc_pct > 0:
        disc_amt = _q2(gross * disc_pct / Decimal("100"))

    if disc_amt < 0:
        disc_amt = Decimal("0")
    if disc_amt > gross:
        disc_amt = gross

    taxable = gross - disc_amt

    gst = _D(getattr(ln, "gst_rate", 0))
    tax_amt = _q2(taxable * gst / Decimal("100"))

    ln.line_total = gross
    ln.discount_amount = _q2(disc_amt)
    ln.tax_amount = _q2(tax_amt)
    ln.net_amount = _q2(taxable + tax_amt)
    return ln


def recompute_invoice_totals(db: Session, invoice_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise RuntimeError("Invoice not found")

    rows = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(invoice_id)).order_by(
            BillingInvoiceLine.id.asc()).all())

    sub = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")
    grand = Decimal("0")

    for ln in rows:
        if line_is_deleted(ln):
            continue

        recompute_line_amounts(ln)

        sub += _D(getattr(ln, "line_total", 0))
        disc += _D(getattr(ln, "discount_amount", 0))
        tax += _D(getattr(ln, "tax_amount", 0))
        grand += _D(getattr(ln, "net_amount", 0))

    inv.sub_total = _q2(sub)
    inv.discount_total = _q2(disc)
    inv.tax_total = _q2(tax)
    inv.round_off = _q2(_D(getattr(inv, "round_off", 0)))
    inv.grand_total = _q2(grand + _D(getattr(inv, "round_off", 0)))

    db.add(inv)
    return inv
