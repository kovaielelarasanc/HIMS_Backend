from decimal import Decimal


def recalc_invoice_totals(inv):
    # assumes inv.items already loaded
    gross = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")
    net = Decimal("0")

    for it in inv.items:
        qty = Decimal(str(it.quantity or 0))
        unit = Decimal(str(it.unit_price or 0))
        d_amt = Decimal(str(it.discount_amount or 0))
        t_amt = Decimal(str(it.tax_amount or 0))
        line = (qty * unit - d_amt) + t_amt

        gross += (qty * unit)
        disc += d_amt
        tax += t_amt
        net += line

    # header discount (optional)
    hdr_disc_amt = Decimal(str(inv.header_discount_amount or 0))
    net = max(Decimal("0"), net - hdr_disc_amt)
    disc += hdr_disc_amt

    inv.gross_total = gross
    inv.discount_total = disc
    inv.tax_total = tax
    inv.net_total = net

    # balance_due depends on payments + advances
    paid = Decimal(str(inv.amount_paid or 0))
    adv = Decimal(str(inv.advance_adjusted or 0))
    inv.balance_due = max(Decimal("0"), net - paid - adv)

    return inv
