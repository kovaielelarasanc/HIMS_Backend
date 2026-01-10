# FILE: app/services/billing_charge_item_service.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.charge_item_master import ChargeItemMaster
from app.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    BillingCase,
    BillingTariffRate,
    ServiceGroup,
    DocStatus,
)

# ============================================================
# Money helpers
# ============================================================
QTY_Q = Decimal("0.0001")
MONEY_Q = Decimal("0.01")
PCT_Q = Decimal("0.01")


def D(x, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x if x is not None else default))
    except Exception:
        return Decimal(default)


def q_money(x: Decimal) -> Decimal:
    return D(x).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def q_qty(x: Decimal) -> Decimal:
    return D(x).quantize(QTY_Q, rounding=ROUND_HALF_UP)


def q_pct(x: Decimal) -> Decimal:
    return D(x).quantize(PCT_Q, rounding=ROUND_HALF_UP)


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def parse_service_group(v: Optional[str]) -> ServiceGroup:
    if not v:
        return ServiceGroup.MISC
    s = str(v).strip().upper()
    # supports both Enum(value) and Enum[name] shapes
    try:
        return ServiceGroup(s)
    except Exception:
        try:
            return ServiceGroup[s]
        except Exception:
            return ServiceGroup.MISC


def is_misc_module(module: Optional[str]) -> bool:
    m = (module or "").strip().upper()
    return (m == "") or (m == "MISC")


def require_editable_invoice(inv: BillingInvoice) -> None:
    st = str(_enum_value(inv.status) or "")
    if st != "DRAFT":
        raise ValueError(
            "Invoice locked. Reopen to edit.")  # handled in router


def recalc_invoice_totals(db: Session, inv: BillingInvoice) -> None:
    sums = db.query(
        func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
        func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
    ).filter(BillingInvoiceLine.invoice_id == inv.id).one()

    sub_total = q_money(D(sums[0]))
    discount_total = q_money(D(sums[1]))
    tax_total = q_money(D(sums[2]))
    net_sum = q_money(D(sums[3]))

    inv.sub_total = sub_total
    inv.discount_total = discount_total
    inv.tax_total = tax_total

    inv.round_off = q_money(D(getattr(inv, "round_off", 0) or 0))
    inv.grand_total = q_money(net_sum + inv.round_off)


# ============================================================
# Core: add charge item line to invoice (NO commit here)
# ============================================================
def add_charge_item_line_to_invoice(
    db: Session,
    *,
    invoice_id: int,
    charge_item_id: int,
    qty: Decimal,
    unit_price: Optional[Decimal] = None,
    gst_rate: Optional[Decimal] = None,
    discount_percent: Optional[Decimal] = None,
    discount_amount: Optional[Decimal] = None,
    idempotency_key: Optional[str] = None,
    revenue_head_id: Optional[int] = None,
    cost_center_id: Optional[int] = None,
    doctor_id: Optional[int] = None,
    manual_reason: Optional[str] = None,
    created_by: Optional[int] = None,
) -> Tuple[BillingInvoice, BillingInvoiceLine]:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise LookupError("Invoice not found")

    # ✅ STRICT RULE: only MISC invoices can accept CHARGE_ITEM lines
    if not is_misc_module(getattr(inv, "module", None)):
        cur_mod = (getattr(inv, "module", None)
                   or "").strip().upper() or "MISC"
        raise PermissionError(
            f"Charge items can be added only to MISC invoices. Current invoice module is '{cur_mod}'."
        )

    # Keep data clean: if module is null/empty, normalize to MISC (still MISC)
    if (getattr(inv, "module", None) or "").strip() == "":
        inv.module = "MISC"

    # Only draft editable
    require_editable_invoice(inv)

    if inv.status in (DocStatus.POSTED, DocStatus.VOID):
        raise RuntimeError(
            f"Invoice is {_enum_value(inv.status)}; cannot modify")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise LookupError("Billing case not found")

    ci = db.get(ChargeItemMaster, int(charge_item_id))
    if not ci or not getattr(ci, "is_active", False):
        raise LookupError("Charge item not found / inactive")

    sg = parse_service_group(getattr(ci, "service_header", None))

    qty2 = q_qty(D(qty))
    if qty2 <= 0:
        raise ValueError("qty must be > 0")

    # pricing
    up = q_money(D(unit_price)) if unit_price is not None else None
    gr = q_pct(D(gst_rate)) if gst_rate is not None else None

    if up is None or gr is None:
        if getattr(case, "tariff_plan_id", None):
            tr = (db.query(BillingTariffRate).filter(
                BillingTariffRate.tariff_plan_id == case.tariff_plan_id,
                BillingTariffRate.item_type == "CHARGE_ITEM",
                BillingTariffRate.item_id == ci.id,
                BillingTariffRate.is_active.is_(True),
            ).first())
            if tr:
                if up is None:
                    up = q_money(D(getattr(tr, "rate", None)))
                if gr is None:
                    gr = q_pct(D(getattr(tr, "gst_rate", None)))

        if up is None:
            up = q_money(D(getattr(ci, "price", 0)))
        if gr is None:
            gr = q_pct(D(getattr(ci, "gst_rate", 0)))

    if up < 0:
        raise ValueError("unit_price cannot be negative")
    if gr < 0 or gr > 100:
        raise ValueError("gst_rate must be 0..100")

    # Discounts
    disc_pct = q_pct(D(discount_percent))
    disc_amt = q_money(D(discount_amount))

    line_total = q_money(qty2 * up)

    if disc_amt > 0:
        if disc_amt > line_total:
            disc_amt = line_total
        disc_pct = q_pct((disc_amt / line_total *
                          100) if line_total > 0 else 0)
    else:
        if disc_pct < 0 or disc_pct > 100:
            raise ValueError("discount_percent must be 0..100")
        disc_amt = q_money(line_total * disc_pct / 100)

    taxable = q_money(line_total - disc_amt)
    tax_amt = q_money(taxable * gr / 100)
    net_amt = q_money(taxable + tax_amt)

    # idempotency mapping
    source_module = None
    source_ref_id = None
    source_line_key = None
    if idempotency_key:
        key = str(idempotency_key).strip()
        if len(key) > 64:
            raise ValueError("idempotency_key too long (max 64)")
        source_module = "CHARGE_ITEM"
        source_ref_id = int(inv.id)
        source_line_key = key

    line = BillingInvoiceLine(
        billing_case_id=inv.billing_case_id,
        invoice_id=inv.id,
        service_group=sg,
        item_type="CHARGE_ITEM",
        item_id=ci.id,
        item_code=getattr(ci, "code", None),
        description=getattr(ci, "name", None),
        qty=qty2,
        unit_price=up,
        discount_percent=disc_pct,
        discount_amount=disc_amt,
        gst_rate=gr,
        tax_amount=tax_amt,
        line_total=line_total,
        net_amount=net_amt,
        revenue_head_id=revenue_head_id,
        cost_center_id=cost_center_id,
        doctor_id=doctor_id,
        is_manual=True,
        manual_reason=(manual_reason or "CHARGE_ITEM")[:255],
        source_module=source_module,
        source_ref_id=source_ref_id,
        source_line_key=source_line_key,
        created_by=created_by,
    )

    db.add(line)

    # ensure totals include pending line (autoflush usually works, still safe)
    recalc_invoice_totals(db, inv)
    return inv, line


def fetch_idempotent_existing_line(
    db: Session,
    *,
    billing_case_id: int,
    invoice_id: int,
    idempotency_key: str,
) -> Optional[BillingInvoiceLine]:
    return (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.billing_case_id == int(billing_case_id),
        BillingInvoiceLine.source_module == "CHARGE_ITEM",
        BillingInvoiceLine.source_ref_id == int(invoice_id),
        BillingInvoiceLine.source_line_key == str(idempotency_key),
    ).first())
