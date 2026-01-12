# app/services/billing_math.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict


def D(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def money2(x) -> Decimal:
    return D(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_line_amounts(qty, unit_price, discount_amount,
                         gst_rate) -> Dict[str, Decimal]:
    qty = D(qty)
    unit_price = D(unit_price)
    discount_amount = D(discount_amount)
    gst_rate = D(gst_rate)

    if qty < 0:
        qty = Decimal("0")
    if unit_price < 0:
        unit_price = Decimal("0")
    if discount_amount < 0:
        discount_amount = Decimal("0")
    if gst_rate < 0:
        gst_rate = Decimal("0")

    line_total = qty * unit_price
    taxable = max(Decimal("0"), line_total - discount_amount)
    tax_amount = (taxable * gst_rate / Decimal("100"))

    net_amount = taxable + tax_amount

    return {
        "line_total": money2(line_total),
        "discount_amount": money2(discount_amount),
        "tax_amount": money2(tax_amount),
        "net_amount": money2(net_amount),
    }
