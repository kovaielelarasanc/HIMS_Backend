# FILE: app/services/billing_charge_item_service.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
from app.models.charge_item_master import ChargeItemMaster, ChargeItemServiceHeader
from app.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    BillingCase,
    BillingTariffRate,
    ServiceGroup,
    DocStatus,
)
from app.models.billing import BillingNumberSeries
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


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().upper()


# ✅ This was missing (your ImportError fix)
def is_misc_module(module: Optional[str]) -> bool:
    """
    True if invoice module should be treated as MISC.
    Accepts None / "" / "MISC" as misc.
    """
    return _norm(module) in {"", "MISC"}


def parse_service_group(v: Optional[str]) -> ServiceGroup:
    if not v:
        return ServiceGroup.MISC
    s = str(v).strip().upper()
    try:
        return ServiceGroup(s)
    except Exception:
        try:
            return ServiceGroup[s]
        except Exception:
            return ServiceGroup.MISC


# Keep in sync with your router MODULES keys (do NOT introduce new module codes)
_ALLOWED_INVOICE_MODULES = {
    "ADM",
    "ROOM",
    "BLOOD",
    "LAB",
    "DIET",
    "DOC",
    "PHM",
    "PHC",
    "PROC",
    "SCAN",
    "SURG",
    "XRAY",
    "MISC",
}


def _set_if_has(model_obj, attr: str, value):
    """
    Set attribute only if SQLAlchemy model has this column/attr.
    Avoids crashing when your schema differs across installs.
    """
    if hasattr(model_obj.__class__, attr):
        setattr(model_obj, attr, value)


def _status_norm(x) -> str:
    return _norm(str(_enum_value(x or "")))


def _module_norm(x) -> str:
    return _norm(str(x or "")) or "MISC"


def next_invoice_number(
    db: Session,
    *,
    doc_type: str = "INVOICE",
    default_prefix: str = "INV-",
    default_padding: int = 6,
) -> str:
    """
    Generates next invoice number using BillingNumberSeries with row lock.
    If series missing, creates it.
    """
    # If your series table does not have tenant_id/doc_type/prefix/padding/next_number,
    # then update this function accordingly.
    q = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == doc_type, ).with_for_update())
    series = q.first()

    if not series:
        series = BillingNumberSeries(
            doc_type=doc_type,
            prefix=default_prefix,
            padding=default_padding,
            next_number=1,
        )
        # set reset_period only if it exists (some schemas use enum)
        if hasattr(series.__class__, "reset_period"):
            try:
                series.reset_period = getattr(
                    series.__class__,
                    "reset_period").type.enum_class.YEARLY  # type: ignore
            except Exception:
                # if reset_period is string/enum unknown, ignore safely
                pass

        db.add(series)
        db.flush()

    prefix = getattr(series, "prefix", None) or default_prefix
    padding = int(getattr(series, "padding", None) or default_padding)

    n = int(getattr(series, "next_number", None) or 1)
    series.next_number = n + 1

    return f"{prefix}{str(n).zfill(padding)}"


def get_or_create_draft_invoice_for_case_module(
    db: Session,
    *,
    case: BillingCase,
    module: str,
    like_invoice: Optional[BillingInvoice] = None,
    created_by: Optional[int] = None,
) -> BillingInvoice:
    """
    Find existing DRAFT invoice for (case, module), else create one.
    Copies payer fields from like_invoice when possible.
    """
    mod = _module_norm(module)

    existing = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case.id,
        func.upper(BillingInvoice.module) == mod,
    ).order_by(BillingInvoice.id.desc()).first())
    if existing:
        st = _status_norm(getattr(existing, "status", None))
        if st != "DRAFT":
            raise RuntimeError(
                f"Target invoice '{mod}' is {st}; cannot modify")
        return existing

    inv = BillingInvoice(billing_case_id=case.id)

    # Required common fields
    _set_if_has(inv, "module", mod)
    _set_if_has(inv, "status", DocStatus.DRAFT)

    # Copy from like_invoice (best match for payer fields)
    if like_invoice is not None:
        _set_if_has(inv, "invoice_type",
                    getattr(like_invoice, "invoice_type", None))
        _set_if_has(inv, "payer_type", getattr(like_invoice, "payer_type",
                                               None))
        _set_if_has(inv, "payer_id", getattr(like_invoice, "payer_id", None))
        _set_if_has(inv, "tariff_plan_id",
                    getattr(like_invoice, "tariff_plan_id", None))
        _set_if_has(inv, "encounter_type",
                    getattr(like_invoice, "encounter_type", None))
        _set_if_has(inv, "encounter_id",
                    getattr(like_invoice, "encounter_id", None))

    # Copy from case if invoice has these
    _set_if_has(inv, "patient_id", getattr(case, "patient_id", None))

    # Invoice number (if present in model)
    if hasattr(inv.__class__, "invoice_number"):

        inv.invoice_number = next_invoice_number(db)

    # Totals init (safe)
    for f in ("sub_total", "discount_total", "tax_total", "round_off",
              "grand_total"):
        if hasattr(inv.__class__, f):
            setattr(inv, f, D(0))

    _set_if_has(inv, "created_by", created_by)
    if hasattr(inv.__class__, "created_at"):
        _set_if_has(inv, "created_at", datetime.utcnow())

    db.add(inv)
    db.flush()  # get inv.id
    return inv


def resolve_service_group(db: Session,
                          service_header_code: Optional[str]) -> ServiceGroup:
    """
    If a custom service header exists in master, use its mapped service_group.
    Else fallback to parse_service_group() (old behavior).
    """
    code = _norm(service_header_code)
    if not code:
        return ServiceGroup.MISC

    try:
        row = (db.query(ChargeItemServiceHeader).filter(
            func.upper(ChargeItemServiceHeader.code) == code,
            ChargeItemServiceHeader.is_active.is_(True),
        ).first())
        if row and getattr(row, "service_group", None):
            return row.service_group
    except Exception:
        # Table might not exist during migration; fallback safely
        pass

    return parse_service_group(code)


def expected_invoice_module_for_charge_item(ci: ChargeItemMaster) -> str:
    """
    Rules (your requirement):
      - category ADM/DIET/BLOOD => invoice.module same as category
      - category MISC (or others) => if module_header matches predefined modules, use it
                                   else fallback to MISC
    """
    cat = _norm(getattr(ci, "category", None))

    if cat in {"ADM", "DIET", "BLOOD"}:
        return cat

    mh = _norm(getattr(ci, "module_header", None))
    if mh in _ALLOWED_INVOICE_MODULES:
        return mh

    return "MISC"


def require_editable_invoice(inv: BillingInvoice) -> None:
    """
    Editable only when DRAFT (string or Enum safe).
    """
    st = _norm(str(_enum_value(getattr(inv, "status", None) or "")))
    if st != "DRAFT":
        raise ValueError("Invoice locked. Reopen to edit.")


def recalc_invoice_totals(db: Session, inv: BillingInvoice) -> None:
    sums = (db.query(
        func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
        func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
    ).filter(BillingInvoiceLine.invoice_id == inv.id).one())

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

    # Only draft editable
    require_editable_invoice(inv)

    # ✅ safer: handle status whether Enum or str
    stv = _norm(str(_enum_value(getattr(inv, "status", None) or "")))
    if stv in {"POSTED", "VOID"}:
        raise RuntimeError(f"Invoice is {stv}; cannot modify")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise LookupError("Billing case not found")

    ci = db.get(ChargeItemMaster, int(charge_item_id))
    if not ci or not getattr(ci, "is_active", False):
        raise LookupError("Charge item not found / inactive")

    expected_mod = expected_invoice_module_for_charge_item(ci)
    cur_mod = _norm(getattr(inv, "module", None)) or "MISC"

    # ✅ Enforce: charge item must go into correct module invoice
    if cur_mod not in _ALLOWED_INVOICE_MODULES:
        cur_mod = "MISC"

    if cur_mod != expected_mod:
        # Backward-safe auto-fix only when invoice is empty
        line_count = (db.query(func.count(BillingInvoiceLine.id)).filter(
            BillingInvoiceLine.invoice_id == inv.id).scalar() or 0)

        # If wrong module but invoice is still empty draft, fix module automatically
        if line_count == 0 and cur_mod in {"", "MISC"}:
            inv.module = expected_mod
            cur_mod = expected_mod
        else:
            raise PermissionError(
                f"Charge item category '{_norm(getattr(ci, 'category', None)) or 'MISC'}' "
                f"must be billed under '{expected_mod}' invoice. Current invoice module is '{cur_mod}'."
            )

    # Normalize module if blank
    if (getattr(inv, "module", None) or "").strip() == "":
        inv.module = expected_mod

    sg = resolve_service_group(db, getattr(ci, "service_header", None))

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

    # totals
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
