# File: app/services/billing_service.py
from __future__ import annotations

import inspect
import secrets
from datetime import datetime, timezone, date as dt_date
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP


from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing import (
    BillingAdvance,
    BillingCase,
    BillingCaseStatus,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPayment,
    BillingTariffRate,
    CoverageFlag,
    DocStatus,
    EncounterType,
    InvoiceType,
    NumberResetPeriod,
    PayMode,
    PayerMode,
    PayerType,
    ServiceGroup,
    AdvanceType,
    BillingAuditLog,
    BillingClaim,
    PaymentKind,
    PaymentDirection,
)
from app.models.user import User
from app.models.opd import Appointment, Visit

from app.services.id_gen import next_billing_case_number, next_invoice_number
from app.services.billing_posting_workflow import post_invoice_workflow

# v2 finance services (real allocations + receipts)
from app.services.billing_finance import (
    record_payment as record_payment_v2,
    record_advance as record_advance_v2,
    apply_advances_to_case as apply_advances_to_case_v2,
    case_financials as case_financials_v2,
    BillingError as FinanceBillingError,
    BillingStateError as FinanceBillingStateError,
)

# ============================================================
# Backward-compatible: upsert_auto_line (used by billing_particulars.py)
# ============================================================

from decimal import Decimal
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.billing import BillingInvoice, BillingInvoiceLine, DocStatus, ServiceGroup

# If these already exist in your billing_service.py, DO NOT redefine them.
# Keep only one definition in the file.
try:
    BillingError
except NameError:

    class BillingError(Exception):
        pass


try:
    BillingStateError
except NameError:

    class BillingStateError(Exception):
        pass


TWOPLACES = Decimal("0.01")


def _d(x) -> Decimal:
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _m(x) -> Decimal:
    # money normalize
    return _d(x).quantize(TWOPLACES)


def _deep_merge(a: Any, b: Any) -> Any:
    """
    Deep merge dicts: values in b override/extend a.
    Non-dicts are replaced.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return b
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_if_has(obj, field: str, value):
    if hasattr(obj, field):
        setattr(obj, field, value)


def _compute_amounts(
    *,
    qty: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal,
    discount_percent: Decimal,
    discount_amount: Decimal,
):
    qty = _d(qty)
    unit_price = _d(unit_price)
    gst_rate = _d(gst_rate)
    discount_percent = _d(discount_percent)
    discount_amount = _d(discount_amount)

    if qty < 0:
        qty = Decimal("0")
    if unit_price < 0:
        unit_price = Decimal("0")
    if gst_rate < 0:
        gst_rate = Decimal("0")
    if discount_percent < 0:
        discount_percent = Decimal("0")
    if discount_amount < 0:
        discount_amount = Decimal("0")

    gross = qty * unit_price

    # If explicit discount_amount is provided, prefer it, else compute from percent
    if discount_amount > 0:
        disc = discount_amount
    else:
        disc = (gross * discount_percent) / Decimal("100")

    if disc > gross:
        disc = gross

    taxable = gross - disc
    tax = (taxable * gst_rate) / Decimal("100")
    total = taxable + tax

    return {
        "gross": _m(gross),
        "discount": _m(disc),
        "taxable": _m(taxable),
        "tax": _m(tax),
        "total": _m(total),
    }


def _recalc_invoice_totals(db: Session, invoice_id: int) -> None:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        return

    # Sum all lines for this invoice
    lines = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(invoice_id)).all()

    gross = Decimal("0")
    disc = Decimal("0")
    tax = Decimal("0")
    total = Decimal("0")

    for ln in lines:
        # Be flexible with field names across versions
        g = _d(
            getattr(ln, "gross_amount", None)
            or getattr(ln, "sub_total", None) or getattr(ln, "amount", None))
        d = _d(getattr(ln, "discount_amount", None))
        t = _d(
            getattr(ln, "tax_amount", None) or getattr(ln, "gst_amount", None))
        tt = _d(
            getattr(ln, "line_total", None)
            or getattr(ln, "total_amount", None)
            or getattr(ln, "net_amount", None))

        # If totals aren't stored, recompute from qty/unit_price
        if tt <= 0:
            calc = _compute_amounts(
                qty=_d(getattr(ln, "qty", 0)),
                unit_price=_d(
                    getattr(ln, "unit_price", None)
                    or getattr(ln, "rate", None)),
                gst_rate=_d(
                    getattr(ln, "gst_rate", None)
                    or getattr(ln, "tax_rate", None)),
                discount_percent=_d(getattr(ln, "discount_percent", 0)),
                discount_amount=_d(getattr(ln, "discount_amount", 0)),
            )
            g, d, t, tt = calc["gross"], calc["discount"], calc["tax"], calc[
                "total"]

        gross += g
        disc += d
        tax += t
        total += tt

    # Map to your invoice column names (your model uses these)
    _set_if_has(inv, "sub_total", _m(gross))
    _set_if_has(inv, "discount_total", _m(disc))
    _set_if_has(inv, "tax_total", _m(tax))

    # round_off if present
    if hasattr(inv, "round_off"):
        rounded = _m(total)
        ro = rounded - _m(total)
        _set_if_has(inv, "round_off", _m(ro))
        _set_if_has(inv, "grand_total", rounded)
    else:
        _set_if_has(inv, "grand_total", _m(total))

    if hasattr(inv, "updated_at"):
        inv.updated_at = datetime.utcnow()

    db.add(inv)


def upsert_auto_line(
    db: Session,
    *,
    invoice_id: int,
    billing_case_id: int,
    user: Any,
    service_group: ServiceGroup,
    item_type: str,
    item_id: Optional[int],
    item_code: Optional[str],
    description: str,
    qty: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal = Decimal("0"),
    discount_percent: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    source_module: str,
    source_ref_id: int,
    source_line_key: str,
    service_date: Optional[datetime] = None,
    doctor_id: Optional[int] = None,
    meta_patch: Optional[Dict[str, Any]] = None,
):
    """
    Idempotent create/update for system-generated lines.
    Keyed by (source_module, source_ref_id, source_line_key).
    Works with your BillingInvoiceLine idempotency constraint.
    """

    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if getattr(inv, "status",
               None) not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError("Invoice is not editable (POSTED/VOID).")

    key = (source_line_key or "").strip()
    if not key:
        raise BillingError("source_line_key is required for auto lines")
    if len(key) > 64:
        key = key[:64]

    # Find existing line by idempotency keys
    line = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.source_module == source_module,
        BillingInvoiceLine.source_ref_id == int(source_ref_id),
        BillingInvoiceLine.source_line_key == key,
    ).first())

    if line is None:
        line = BillingInvoiceLine()
        _set_if_has(line, "invoice_id", int(invoice_id))
        _set_if_has(line, "billing_case_id", int(billing_case_id))

        # idempotency keys
        _set_if_has(line, "source_module", source_module)
        _set_if_has(line, "source_ref_id", int(source_ref_id))
        _set_if_has(line, "source_line_key", key)

    else:
        # If line exists but belongs to another invoice, be safe:
        existing_invoice_id = int(getattr(line, "invoice_id", 0) or 0)
        if existing_invoice_id and existing_invoice_id != int(invoice_id):
            old_inv = db.get(BillingInvoice, existing_invoice_id)
            # allow moving only if old invoice is editable
            if old_inv and getattr(
                    old_inv, "status",
                    None) in (DocStatus.DRAFT, DocStatus.APPROVED):
                _set_if_has(line, "invoice_id", int(invoice_id))
            else:
                raise BillingStateError(
                    "Auto line already exists on a POSTED/VOID invoice. "
                    "Pass a unique line_key suffix to create a new line.")

    # Core fields
    _set_if_has(line, "service_group", service_group)
    _set_if_has(line, "item_type", item_type)
    _set_if_has(line, "item_id", int(item_id) if item_id is not None else None)
    _set_if_has(line, "item_code", item_code)
    _set_if_has(line, "description", description)

    _set_if_has(line, "qty", _d(qty))
    if hasattr(line, "unit_price"):
        line.unit_price = _d(unit_price)
    elif hasattr(line, "rate"):
        line.rate = _d(unit_price)

    _set_if_has(line, "gst_rate", _d(gst_rate))
    _set_if_has(line, "discount_percent", _d(discount_percent))
    _set_if_has(line, "discount_amount", _d(discount_amount))

    if doctor_id is not None:
        _set_if_has(line, "doctor_id", int(doctor_id))

    if service_date is not None:
        _set_if_has(line, "service_date", service_date)

    # Meta merge
    if meta_patch:
        if hasattr(line, "meta"):
            base = getattr(line, "meta", None) or {}
            line.meta = _deep_merge(base, meta_patch)
        elif hasattr(line, "meta_json"):
            base = getattr(line, "meta_json", None) or {}
            line.meta_json = _deep_merge(base, meta_patch)

    # Amounts
    calc = _compute_amounts(
        qty=_d(qty),
        unit_price=_d(unit_price),
        gst_rate=_d(gst_rate),
        discount_percent=_d(discount_percent),
        discount_amount=_d(discount_amount),
    )

    _set_if_has(line, "gross_amount", calc["gross"])
    _set_if_has(line, "tax_amount", calc["tax"])
    _set_if_has(line, "line_total", calc["total"])
    _set_if_has(line, "net_amount", calc["total"])  # some schemas
    _set_if_has(line, "total_amount", calc["total"])  # some schemas

    if hasattr(line, "updated_at"):
        line.updated_at = datetime.utcnow()

    db.add(line)
    db.flush()  # to get line.id

    # Update invoice totals
    _recalc_invoice_totals(db, int(invoice_id))
    db.flush()

    return line


# ============================================================
# Errors (keep compatible, but backed by finance errors)
# ============================================================
class BillingError(FinanceBillingError):
    pass


class BillingStateError(FinanceBillingStateError):
    pass


# ============================================================
# Small helpers
# ============================================================
def _enum_value(v: Any) -> Any:
    return getattr(v, "value", v)


def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x if x is not None else 0))
    except Exception:
        return Decimal("0")


def _dec_s(x: Decimal) -> str:
    try:
        return format(_d(x), "f")
    except Exception:
        return "0"


def _set_if_has(obj: Any, field: str, value: Any) -> None:
    if hasattr(obj, field):
        setattr(obj, field, value)

from typing import Any, Dict
from sqlalchemy.orm.attributes import flag_modified

def _deep_merge_with_remove(dst: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge with remove semantics:
    - value None or "" => remove key
    - dict => deep merge
    - otherwise overwrite
    """
    for k, v in (patch or {}).items():
        if v is None or (isinstance(v, str) and v.strip() == ""):
            dst.pop(k, None)
            continue

        if isinstance(v, dict):
            cur = dst.get(k)
            if not isinstance(cur, dict):
                cur = {}
            dst[k] = _deep_merge_with_remove(cur, v)
        else:
            dst[k] = v
    return dst

def _merge_meta(obj: Any, patch: Dict[str, Any]) -> None:
    """
    Safe meta_json merge:
    - Supports removing keys (None/"")
    - Supports nested dict merges
    - Ensures SQLAlchemy persists JSON updates
    """
    if not hasattr(obj, "meta_json"):
        return

    current = getattr(obj, "meta_json", None)
    if not isinstance(current, dict):
        current = {}

    merged = _deep_merge_with_remove(dict(current), patch or {})
    setattr(obj, "meta_json", merged if merged else None)

    # ✅ force persist even if JSON isn't MutableDict
    try:
        flag_modified(obj, "meta_json")
    except Exception:
        pass


def _now_local() -> datetime:
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return datetime.now(timezone.utc).astimezone(tz)


def _utcnow_naive() -> datetime:
    return datetime.utcnow()


def _to_local_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return dt.astimezone(tz).replace(tzinfo=None)


def _require_draft(inv: BillingInvoice):
    if str(_enum_value(getattr(inv, "status", ""))) != "DRAFT":
        raise HTTPException(status_code=409,
                            detail="Invoice locked. Reopen to edit.")


# ============================================================
# ✅ ACTIVE-LINE FILTER (fix for "deleted line still counted")
# ============================================================
def _apply_active_line_filter(q):
    if hasattr(BillingInvoiceLine, "is_deleted"):
        q = q.filter(
            or_(BillingInvoiceLine.is_deleted.is_(False),
                BillingInvoiceLine.is_deleted.is_(None)))
    if hasattr(BillingInvoiceLine, "is_active"):
        q = q.filter(
            or_(BillingInvoiceLine.is_active.is_(True),
                BillingInvoiceLine.is_active.is_(None)))
    if hasattr(BillingInvoiceLine, "deleted_at"):
        q = q.filter(BillingInvoiceLine.deleted_at.is_(None))
    if hasattr(BillingInvoiceLine, "voided_at"):
        q = q.filter(BillingInvoiceLine.voided_at.is_(None))

    if hasattr(BillingInvoiceLine, "status"):
        try:
            bad = ["VOID", "CANCELLED", "CANCELED", "DELETED", "REMOVED"]
            q = q.filter(~BillingInvoiceLine.status.in_(bad))
        except Exception:
            pass

    # fallback tombstone marker (works even if no is_deleted column exists)
    if hasattr(BillingInvoiceLine, "description"):
        try:
            q = q.filter(~BillingInvoiceLine.description.ilike("%(REMOVED)%"))
        except Exception:
            pass

    return q


def _line_is_removed(ln: BillingInvoiceLine) -> bool:
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

        mj = getattr(ln, "meta_json", None)
        if isinstance(mj, dict):
            dv = mj.get("deleted")
            if dv is True or isinstance(dv, dict):
                return True
            if mj.get("deleted_flag") is True:
                return True

        qty = _d(getattr(ln, "qty", 0))
        desc = (getattr(ln, "description", "") or "")
        if qty <= 0 and "(REMOVED)" in desc:
            return True
    except Exception:
        pass
    return False


# ============================================================
# Number generators (compatible with mixed signatures)
# ============================================================
def _safe_call_idgen(fn, db: Session, **kwargs) -> str:
    """
    Calls id_gen functions safely.
    If id_gen requires tenant_id, we pass tenant_id=None fallback.
    """
    try:
        sig = inspect.signature(fn)
        allowed = set(sig.parameters.keys())
    except Exception:
        allowed = set(kwargs.keys())

    def _filtered(d: dict) -> dict:
        return {k: v for k, v in d.items() if k in allowed}

    # try as-is
    try:
        return fn(db, **_filtered(kwargs))
    except TypeError:
        pass

    # fallback tenant_id=None
    kwargs2 = dict(kwargs)
    kwargs2["tenant_id"] = None
    return fn(db, **_filtered(kwargs2))


def _call_next_case_number(
    db: Session,
    *,
    encounter_type: EncounterType,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
    on_date: Optional[datetime] = None,
    padding: Optional[int] = None,
) -> str:
    et = encounter_type.value if hasattr(encounter_type,
                                         "value") else str(encounter_type)

    for args in [
        {
            "encounter_type": et,
            "reset_period": reset_period
        },
        {
            "encounter_type": et,
            "on_date": on_date,
            "padding": padding or 6,
            "reset_period": reset_period
        },
        {
            "encounter_type": et
        },
    ]:
        try:
            return _safe_call_idgen(next_billing_case_number, db, **args)
        except TypeError:
            continue

    return _safe_call_idgen(next_billing_case_number, db, encounter_type=et)


def _call_next_invoice_number(
    db: Session,
    *,
    encounter_type: EncounterType,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
    on_date: Optional[datetime] = None,
    padding: Optional[int] = None,
) -> str:
    et = encounter_type.value if hasattr(encounter_type,
                                         "value") else str(encounter_type)

    for args in [
        {
            "encounter_type": et,
            "reset_period": reset_period
        },
        {
            "encounter_type": et,
            "on_date": on_date,
            "padding": padding or 6,
            "reset_period": reset_period
        },
        {
            "encounter_type": et
        },
    ]:
        try:
            return _safe_call_idgen(next_invoice_number, db, **args)
        except TypeError:
            continue

    return _safe_call_idgen(next_invoice_number, db, encounter_type=et)


def _ensure_case_number(db: Session, case: BillingCase, *,
                        reset_period: NumberResetPeriod) -> None:
    """
    ✅ Fix: case_number stuck as 'TEMP'
    """
    cur = getattr(case, "case_number", None)
    if cur and str(cur).strip() and str(cur).upper() != "TEMP":
        return

    try:
        case.case_number = _call_next_case_number(
            db,
            encounter_type=case.encounter_type,
            reset_period=reset_period,
            on_date=_now_local(),
            padding=6,
        )
    except Exception:
        et = case.encounter_type.value if hasattr(
            case.encounter_type, "value") else str(case.encounter_type)
        case.case_number = f"{et}CASE{_now_local().strftime('%d%m%Y')}{secrets.randbelow(1000000):06d}"

    _set_if_has(case, "updated_at", _utcnow_naive())
    db.flush()


def _ensure_invoice_number(
    db: Session,
    inv: BillingInvoice,
    *,
    encounter_type: EncounterType,
    reset_period: NumberResetPeriod,
) -> None:
    cur = getattr(inv, "invoice_number", None)
    if cur and str(cur).strip() and str(cur).upper() != "TEMP":
        return
    try:
        inv.invoice_number = _call_next_invoice_number(
            db,
            encounter_type=encounter_type,
            reset_period=reset_period,
            on_date=_now_local(),
            padding=6,
        )
    except Exception:
        et = encounter_type.value if hasattr(encounter_type,
                                             "value") else str(encounter_type)
        inv.invoice_number = f"{et}-{_now_local().strftime('%d%m%Y')}-{secrets.randbelow(1000000):06d}"
    _set_if_has(inv, "updated_at", _utcnow_naive())
    db.flush()


# ============================================================
# GST helpers (stored in meta_json)
# ============================================================
def _gst_split(gst_rate: Decimal,
               *,
               intra_state: bool = True) -> Dict[str, Decimal]:
    r = _d(gst_rate)
    if r <= 0:
        return {
            "gst_rate": Decimal("0"),
            "cgst_rate": Decimal("0"),
            "sgst_rate": Decimal("0"),
            "igst_rate": Decimal("0")
        }
    if intra_state:
        half = (r / Decimal("2"))
        return {
            "gst_rate": r,
            "cgst_rate": half,
            "sgst_rate": half,
            "igst_rate": Decimal("0")
        }
    return {
        "gst_rate": r,
        "cgst_rate": Decimal("0"),
        "sgst_rate": Decimal("0"),
        "igst_rate": r
    }


def _gst_amount_split(tax_amount: Decimal,
                      split_rates: Dict[str, Decimal]) -> Dict[str, Decimal]:
    tax = _d(tax_amount)
    if tax <= 0:
        return {
            "cgst": Decimal("0"),
            "sgst": Decimal("0"),
            "igst": Decimal("0")
        }

    cgst_r = _d(split_rates.get("cgst_rate"))
    sgst_r = _d(split_rates.get("sgst_rate"))
    igst_r = _d(split_rates.get("igst_rate"))

    if igst_r > 0 and (cgst_r + sgst_r) <= 0:
        return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": tax}

    half = (tax / Decimal("2"))
    return {"cgst": half, "sgst": half, "igst": Decimal("0")}


# ============================================================
# Tariff
# ============================================================
def get_tariff_rate(
    db: Session,
    *,
    tariff_plan_id: Optional[int],
    item_type: str,
    item_id: int,
) -> Tuple[Decimal, Decimal]:
    """Return (rate, gst_rate). If no plan/rate -> (0, 0)."""
    if not tariff_plan_id:
        return Decimal("0"), Decimal("0")

    row = (db.query(BillingTariffRate).filter(
        BillingTariffRate.tariff_plan_id == int(tariff_plan_id),
        BillingTariffRate.item_type == str(item_type),
        BillingTariffRate.item_id == int(item_id),
        BillingTariffRate.is_active.is_(True),
    ).first())
    if not row:
        return Decimal("0"), Decimal("0")

    return _d(row.rate), _d(row.gst_rate)


def _tariff_lookup_first(
    db: Session,
    *,
    tariff_plan_id: Optional[int],
    item_id: Optional[int],
    item_types: List[str],
) -> Tuple[Decimal, Decimal]:
    if not tariff_plan_id or not item_id:
        return Decimal("0"), Decimal("0")
    for t in item_types:
        r, g = get_tariff_rate(db,
                               tariff_plan_id=tariff_plan_id,
                               item_type=t,
                               item_id=int(item_id))
        if _d(r) > 0:
            return _d(r), _d(g)
    return Decimal("0"), Decimal("0")


# ============================================================
# Case creation (OP/IP)
# ============================================================
def get_or_create_case_for_op_visit(
    db: Session,
    *,
    visit_id: int,
    user: Any,
    appointment_id: Optional[int] = None,
    tariff_plan_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingCase:
    visit_id = int(visit_id)
    op_et = EncounterType.OP if hasattr(EncounterType, "OP") else "OP"

    existing = (db.query(BillingCase).filter(
        BillingCase.encounter_type == op_et,
        BillingCase.encounter_id == visit_id,
    ).first())
    if existing:
        if tariff_plan_id is not None and hasattr(existing, "tariff_plan_id"):
            existing.tariff_plan_id = tariff_plan_id
            existing.updated_by = getattr(user, "id", None)
        _ensure_case_number(db, existing, reset_period=reset_period)
        db.flush()
        return existing

    v = db.get(Visit, visit_id)
    if not v:
        raise ValueError(f"Visit not found: {visit_id}")

    ap_id = appointment_id or getattr(v, "appointment_id", None)
    on_date = None
    if ap_id:
        ap = db.get(Appointment, int(ap_id))
        if ap and getattr(ap, "date", None):
            on_date = ap.date
    if on_date is None:
        vat = getattr(v, "visit_at", None)
        on_date = vat.date() if vat else dt_date.today()

    created_by = int(getattr(user, "id", 0) or 0) or 1

    case = BillingCase(
        patient_id=int(v.patient_id),
        encounter_type=op_et,
        encounter_id=visit_id,
        case_number="TEMP",
        status=BillingCaseStatus.OPEN
        if hasattr(BillingCaseStatus, "OPEN") else "OPEN",
        payer_mode=PayerMode.SELF if hasattr(PayerMode, "SELF") else "SELF",
        tariff_plan_id=tariff_plan_id
        if hasattr(BillingCase, "tariff_plan_id") else None,
        created_by=created_by,
        updated_by=created_by,
    )

    db.add(case)
    db.flush()

    _ensure_case_number(db, case, reset_period=reset_period)
    return case


def get_or_create_case_for_ip_admission(
    db: Session,
    *,
    admission_id: int,
    user,
    tariff_plan_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingCase:
    case = (db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.IP,
        BillingCase.encounter_id == int(admission_id),
    ).first())
    if case:
        if tariff_plan_id is not None:
            case.tariff_plan_id = tariff_plan_id
            case.updated_by = getattr(user, "id", None)
        _ensure_case_number(db, case, reset_period=reset_period)
        db.flush()
        return case

    from app.models.ipd import IpdAdmission  # adjust if needed

    adm = db.get(IpdAdmission, int(admission_id))
    if not adm:
        raise BillingError("IPD Admission not found")

    case = BillingCase(
        patient_id=int(adm.patient_id),
        encounter_type=EncounterType.IP,
        encounter_id=int(admission_id),
        case_number="TEMP",
        status=BillingCaseStatus.OPEN,
        payer_mode=PayerMode.SELF,
        tariff_plan_id=tariff_plan_id if tariff_plan_id is not None else
        getattr(adm, "tariff_plan_id", None),
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(case)
    db.flush()

    _ensure_case_number(db, case, reset_period=reset_period)
    return case


# ============================================================
# Invoice (module-wise)
# ============================================================
def create_invoice(
    db: Session,
    *,
    billing_case_id: int,
    user,
    module: Optional[str] = None,
    invoice_type: InvoiceType = InvoiceType.PATIENT,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingInvoice:
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    inv = BillingInvoice(
        billing_case_id=int(billing_case_id),
        invoice_number="TEMP",
        module=(module or None),
        invoice_type=invoice_type,
        status=DocStatus.DRAFT,
        payer_type=payer_type,
        payer_id=payer_id,
        currency="INR",
        sub_total=Decimal("0"),
        discount_total=Decimal("0"),
        tax_total=Decimal("0"),
        round_off=Decimal("0"),
        grand_total=Decimal("0"),
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(inv)
    db.flush()

    _ensure_invoice_number(db,
                           inv,
                           encounter_type=case.encounter_type,
                           reset_period=reset_period)
    return inv


def get_or_create_active_module_invoice(
    db: Session,
    *,
    billing_case_id: int,
    user,
    module: str,
    invoice_type: InvoiceType = InvoiceType.PATIENT,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingInvoice:
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.module == str(module),
        BillingInvoice.invoice_type == invoice_type,
        BillingInvoice.payer_type == payer_type,
        BillingInvoice.payer_id == payer_id,
        BillingInvoice.status.in_([DocStatus.DRAFT, DocStatus.APPROVED]),
    ).order_by(BillingInvoice.id.desc()).first())
    if inv:
        case = db.get(BillingCase, int(billing_case_id))
        if case:
            _ensure_invoice_number(db,
                                   inv,
                                   encounter_type=case.encounter_type,
                                   reset_period=reset_period)
        return inv

    return create_invoice(
        db,
        billing_case_id=int(billing_case_id),
        user=user,
        module=str(module),
        invoice_type=invoice_type,
        payer_type=payer_type,
        payer_id=payer_id,
        reset_period=reset_period,
    )


def _recalc_invoice_totals(db: Session, invoice_id: int) -> None:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        return

    q = (db.query(
        func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
        func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
    ).filter(BillingInvoiceLine.invoice_id == int(inv.id)))
    q = _apply_active_line_filter(q)
    row = q.first()

    inv.sub_total = _d(row[0] if row else 0)
    inv.discount_total = _d(row[1] if row else 0)
    inv.tax_total = _d(row[2] if row else 0)
    inv.round_off = Decimal("0")
    inv.grand_total = _d(row[3] if row else 0)

    # GST split totals
    ql = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id))
    ql = _apply_active_line_filter(ql)
    lines = ql.all()

    cgst = Decimal("0")
    sgst = Decimal("0")
    igst = Decimal("0")

    for ln in lines:
        tax = _d(getattr(ln, "tax_amount", None))
        if tax <= 0:
            continue

        mj = getattr(ln, "meta_json", None)
        if isinstance(mj, dict) and isinstance(mj.get("gst"), dict):
            g = mj["gst"]
            cgst += _d(g.get("cgst_amount"))
            sgst += _d(g.get("sgst_amount"))
            igst += _d(g.get("igst_amount"))
            continue

        cgst += (tax / Decimal("2"))
        sgst += (tax / Decimal("2"))

    _merge_meta(
        inv,
        {
            "gst": {
                "cgst_total": _dec_s(cgst),
                "sgst_total": _dec_s(sgst),
                "igst_total": _dec_s(igst),
                "tax_total": _dec_s(_d(inv.tax_total)),
            }
        },
    )

    _set_if_has(inv, "updated_at", _utcnow_naive())
    db.flush()


# ============================================================
# Lines (AUTO idempotent + MANUAL)
# ============================================================
def add_auto_line_idempotent(
    db: Session,
    *,
    invoice_id: int,
    billing_case_id: int,
    user,
    service_group: ServiceGroup,
    item_type: Optional[str],
    item_id: Optional[int],
    item_code: Optional[str] = None,
    description: str,
    qty: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal,
    source_module: str,
    source_ref_id: int,
    source_line_key: str,
    doctor_id: Optional[int] = None,
    intra_state_gst: bool = True,
    is_manual: bool = False,
    manual_reason: Optional[str] = None,
    meta_patch: Optional[Dict[str, Any]] = None,
) -> Optional[BillingInvoiceLine]:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError("Cannot add lines to POSTED/VOID invoice")

    # ✅ Idempotency check must match your model's unique key:
    # Usually (source_module, source_ref_id, source_line_key)
    existing_any = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.source_module == str(source_module),
        BillingInvoiceLine.source_ref_id == int(source_ref_id),
        BillingInvoiceLine.source_line_key == str(source_line_key),
    ).order_by(BillingInvoiceLine.id.desc()).first())
    if existing_any:
        # collision protection across cases
        if hasattr(existing_any, "billing_case_id") and int(
                getattr(existing_any, "billing_case_id")
                or 0) != int(billing_case_id):
            raise BillingError(
                f"Idempotency key collision for source ({source_module},{source_ref_id},{source_line_key}). "
                f"Existing line belongs to billing_case_id={getattr(existing_any,'billing_case_id',None)}"
            )
        if _line_is_removed(existing_any):
            return None
        return existing_any

    qty = _d(qty)
    unit_price = _d(unit_price)
    gst_rate = _d(gst_rate)

    line_total = qty * unit_price
    discount_amount = Decimal("0")
    taxable = max(line_total - discount_amount, Decimal("0"))
    tax_amount = (taxable *
                  gst_rate) / Decimal("100") if gst_rate > 0 else Decimal("0")
    net_amount = taxable + tax_amount

    split_rates = _gst_split(gst_rate, intra_state=intra_state_gst)
    split_amt = _gst_amount_split(tax_amount, split_rates)

    ln = BillingInvoiceLine(
        billing_case_id=int(billing_case_id),
        invoice_id=int(invoice_id),
        service_group=service_group,
        item_type=item_type,
        item_id=item_id,
        item_code=item_code,
        description=str(description or "")[:255],
        qty=qty,
        unit_price=unit_price,
        discount_percent=Decimal("0"),
        discount_amount=discount_amount,
        gst_rate=gst_rate,
        tax_amount=tax_amount,
        line_total=line_total,
        net_amount=net_amount,
        revenue_head_id=None,
        cost_center_id=None,
        doctor_id=doctor_id,
        source_module=str(source_module)[:16],
        source_ref_id=int(source_ref_id),
        source_line_key=str(source_line_key)[:64],
        is_covered=CoverageFlag.NO,
        approved_amount=Decimal("0"),
        patient_pay_amount=net_amount,
        requires_preauth=False,
        is_manual=bool(is_manual),
        manual_reason=(str(manual_reason)[:255] if manual_reason else None),
        created_by=getattr(user, "id", None),
    )

    _merge_meta(
        ln,
        {
            "gst": {
                "gst_rate": _dec_s(split_rates["gst_rate"]),
                "cgst_rate": _dec_s(split_rates["cgst_rate"]),
                "sgst_rate": _dec_s(split_rates["sgst_rate"]),
                "igst_rate": _dec_s(split_rates["igst_rate"]),
                "cgst_amount": _dec_s(split_amt["cgst"]),
                "sgst_amount": _dec_s(split_amt["sgst"]),
                "igst_amount": _dec_s(split_amt["igst"]),
                "taxable_amount": _dec_s(taxable),
            },
            "module": str(source_module),
            "source": {
                "ref_id": int(source_ref_id),
                "line_key": str(source_line_key)
            },
        },
    )
    if meta_patch:
        _merge_meta(ln, meta_patch)

    db.add(ln)
    db.flush()
    _recalc_invoice_totals(db, int(invoice_id))
    return ln


def _default_doctor_id_for_case(db: Session,
                                case: BillingCase) -> Optional[int]:
    try:
        from app.models.opd import Visit as OpVisit, Appointment as OpAppt
    except Exception:
        OpVisit = None
        OpAppt = None

    if case.encounter_type == EncounterType.OP and OpVisit:
        v = db.get(OpVisit, int(case.encounter_id))
        if v:
            doc = (getattr(v, "doctor_id", None)
                   or getattr(v, "doctor_user_id", None)
                   or getattr(v, "consulting_doctor_id", None))
            if doc:
                return int(doc)
            appt_id = getattr(v, "appointment_id", None)
            if appt_id and OpAppt:
                a = db.get(OpAppt, int(appt_id))
                if a:
                    doc2 = getattr(a, "doctor_id", None) or getattr(
                        a, "doctor_user_id", None)
                    if doc2:
                        return int(doc2)

    if case.encounter_type == EncounterType.IP:
        try:
            from app.models.ipd import IpdAdmission
            adm = db.get(IpdAdmission, int(case.encounter_id))
            if adm:
                doc = (getattr(adm, "consultant_id", None)
                       or getattr(adm, "consultant_user_id", None)
                       or getattr(adm, "admitting_doctor_id", None))
                if doc:
                    return int(doc)
        except Exception:
            pass

    return None


def add_manual_line(
    db: Session,
    *,
    invoice_id: int,
    user,
    service_group: ServiceGroup,
    description: str,
    qty: Decimal = Decimal("1"),
    unit_price: Decimal = Decimal("0"),
    gst_rate: Decimal = Decimal("0"),
    discount_percent: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    item_type: Optional[str] = None,
    item_id: Optional[int] = None,
    item_code: Optional[str] = None,
    doctor_id: Optional[int] = None,
    manual_reason: Optional[str] = "Manual entry",
    intra_state_gst: bool = True,
) -> BillingInvoiceLine:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")
    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError("Manual lines allowed only in DRAFT/APPROVED")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    if doctor_id is None:
        doctor_id = _default_doctor_id_for_case(db, case)

    qty = _d(qty)
    unit_price = _d(unit_price)
    gst_rate = _d(gst_rate)
    discount_percent = _d(discount_percent)
    discount_amount = _d(discount_amount)

    line_total = qty * unit_price
    if discount_amount <= 0 and discount_percent > 0:
        discount_amount = (line_total * discount_percent) / Decimal("100")
    if discount_amount < 0:
        discount_amount = Decimal("0")
    if discount_amount > line_total:
        discount_amount = line_total

    taxable = max(line_total - discount_amount, Decimal("0"))
    tax_amount = (taxable *
                  gst_rate) / Decimal("100") if gst_rate > 0 else Decimal("0")
    net_amount = taxable + tax_amount

    key = f"MNL:{secrets.token_hex(8)}"

    split_rates = _gst_split(gst_rate, intra_state=intra_state_gst)
    split_amt = _gst_amount_split(tax_amount, split_rates)

    ln = BillingInvoiceLine(
        billing_case_id=int(case.id),
        invoice_id=int(inv.id),
        service_group=service_group,
        item_type=item_type,
        item_id=item_id,
        item_code=item_code,
        description=str(description or "")[:255],
        qty=qty,
        unit_price=unit_price,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        gst_rate=gst_rate,
        tax_amount=tax_amount,
        line_total=line_total,
        net_amount=net_amount,
        revenue_head_id=None,
        cost_center_id=None,
        doctor_id=doctor_id,
        source_module="MANUAL",
        source_ref_id=int(inv.id),
        source_line_key=key,
        is_covered=CoverageFlag.NO,
        approved_amount=Decimal("0"),
        patient_pay_amount=net_amount,
        requires_preauth=False,
        is_manual=True,
        manual_reason=manual_reason,
        created_by=getattr(user, "id", None),
    )

    _merge_meta(
        ln,
        {
            "gst": {
                "gst_rate": _dec_s(split_rates["gst_rate"]),
                "cgst_rate": _dec_s(split_rates["cgst_rate"]),
                "sgst_rate": _dec_s(split_rates["sgst_rate"]),
                "igst_rate": _dec_s(split_rates["igst_rate"]),
                "cgst_amount": _dec_s(split_amt["cgst"]),
                "sgst_amount": _dec_s(split_amt["sgst"]),
                "igst_amount": _dec_s(split_amt["igst"]),
                "taxable_amount": _dec_s(taxable),
            },
            "module": "MANUAL",
            "source": {
                "ref_id": int(inv.id),
                "line_key": key
            },
        },
    )

    db.add(ln)
    db.flush()
    _recalc_invoice_totals(db, int(inv.id))
    return ln


# ============================================================
# ✅ Edit/Delete any line (manual + auto) + FIX totals
# ============================================================


def _q2(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def normalize_gst_rate(gst_rate: Decimal) -> Decimal:
    r = gst_rate or Decimal("0")
    # ✅ common fix: 0.05 stored instead of 5.00
    if r > 0 and r < 1:
        r = r * Decimal("100")
    if r < 0 or r > 100:
        raise BillingError(f"Invalid gst_rate: {r}")
    return r

def update_invoice_line(
    db: Session,
    *,
    line_id: int,
    user,
    description: Optional[str] = None,
    qty: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    gst_rate: Optional[Decimal] = None,
    discount_percent: Optional[Decimal] = None,
    discount_amount: Optional[Decimal] = None,
    doctor_id: Optional[int] = None,
    intra_state_gst: bool = True,
    service_date: Optional[datetime] = None,
    item_code: Optional[str] = None,

    # ✅ accept meta_json patch (can be {} or contain nulls for remove)
    meta_json: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> BillingInvoiceLine:
    ln = db.get(BillingInvoiceLine, int(line_id))
    if not ln:
        raise BillingError("Invoice line not found")
    if _line_is_removed(ln):
        raise BillingStateError("Cannot edit a removed/deleted line")

    inv = db.get(BillingInvoice, int(getattr(ln, "invoice_id")))
    if not inv:
        raise BillingError("Invoice not found for this line")
    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError("Can edit lines only in DRAFT/APPROVED invoice")

    # ✅ apply fields (use is not None, so 0 values work)
    if description is not None:
        ln.description = str(description or "")[:255]
    if qty is not None:
        ln.qty = _d(qty)
    if unit_price is not None:
        ln.unit_price = _d(unit_price)
    if gst_rate is not None:
        ln.gst_rate = _d(gst_rate)

    if item_code is not None and hasattr(ln, "item_code"):
        ln.item_code = (str(item_code)[:64] if item_code is not None else None)

    if doctor_id is not None and hasattr(ln, "doctor_id"):
        ln.doctor_id = doctor_id

    if hasattr(ln, "service_date") and service_date is not None:
        ln.service_date = _to_local_naive(service_date)

    # ✅ meta patch must be checked with "is not None" (so {} doesn't get ignored)
    #    Use {"batch_no": None} / {"expiry_date": ""} to remove keys.
    if meta_json is not None:
        _merge_meta(ln, meta_json)

    # ---- recompute totals ----
    dp = _d(discount_percent) if discount_percent is not None else _d(getattr(ln, "discount_percent", 0))
    da = _d(discount_amount) if discount_amount is not None else _d(getattr(ln, "discount_amount", 0))

    line_total = _q2(_d(getattr(ln, "qty", 0)) * _d(getattr(ln, "unit_price", 0)))

    # compute discount if percent provided and amount not provided
    if (discount_amount is None) and (discount_percent is not None) and da <= 0 and dp > 0:
        da = _q2((line_total * dp) / Decimal("100"))

    if da < 0:
        da = Decimal("0")
    if da > line_total:
        da = line_total

    taxable = _q2(max(line_total - da, Decimal("0")))

    gr_raw = _d(getattr(ln, "gst_rate", 0))
    gr = normalize_gst_rate(gr_raw)

    tax_amount = _q2((taxable * gr) / Decimal("100")) if gr > 0 else Decimal("0.00")
    net_amount = _q2(taxable + tax_amount)

    ln.discount_percent = dp
    ln.discount_amount = _q2(da)
    ln.line_total = line_total
    ln.tax_amount = tax_amount
    ln.net_amount = net_amount
    ln.patient_pay_amount = net_amount

    # split GST (your existing helpers)
    split_rates = _gst_split(gr, intra_state=intra_state_gst)
    split_amt = _gst_amount_split(tax_amount, split_rates)

    _merge_meta(
        ln,
        {
            "gst": {
                "gst_rate": _dec_s(split_rates["gst_rate"]),
                "cgst_rate": _dec_s(split_rates["cgst_rate"]),
                "sgst_rate": _dec_s(split_rates["sgst_rate"]),
                "igst_rate": _dec_s(split_rates["igst_rate"]),
                "cgst_amount": _dec_s(split_amt["cgst"]),
                "sgst_amount": _dec_s(split_amt["sgst"]),
                "igst_amount": _dec_s(split_amt["igst"]),
                "taxable_amount": _dec_s(taxable),
            },
            "edited": {
                "at": _utcnow_naive().isoformat(),
                "by": getattr(user, "id", None),
                "reason": (str(reason)[:255] if reason else None),
            },
        },
    )

    _set_if_has(ln, "updated_at", _utcnow_naive())
    db.flush()

    _recalc_invoice_totals(db, int(inv.id))
    _set_if_has(inv, "updated_by", getattr(user, "id", None))
    db.flush()
    return ln



def delete_invoice_line(
    db: Session,
    *,
    line_id: int,
    user,
    reason: Optional[str] = None,
) -> None:
    ln = db.get(BillingInvoiceLine, int(line_id))
    if not ln:
        raise BillingError("Invoice line not found")

    if _line_is_removed(ln):
        return  # idempotent delete

    inv = db.get(BillingInvoice, int(getattr(ln, "invoice_id")))
    if not inv:
        raise BillingError("Invoice not found for this line")

    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError(
            "Can delete lines only in DRAFT/APPROVED invoices")

    now = _utcnow_naive()

    if hasattr(ln, "is_deleted"):
        ln.is_deleted = True
    if hasattr(ln, "is_active"):
        ln.is_active = False
    if hasattr(ln, "deleted_at"):
        ln.deleted_at = now
    if hasattr(ln, "voided_at"):
        ln.voided_at = now
    if hasattr(ln, "status"):
        try:
            ln.status = "DELETED"
        except Exception:
            pass

    ln.qty = Decimal("0")
    ln.line_total = Decimal("0")
    ln.discount_amount = Decimal("0")
    ln.tax_amount = Decimal("0")
    ln.net_amount = Decimal("0")
    if hasattr(ln, "patient_pay_amount"):
        ln.patient_pay_amount = Decimal("0")
    if hasattr(ln, "insurer_pay_amount"):
        ln.insurer_pay_amount = Decimal("0")

    desc = (getattr(ln, "description", "") or "").strip()
    if "(REMOVED)" not in desc:
        ln.description = (desc + " (REMOVED)").strip()[:255]

    _merge_meta(
        ln,
        {
            "deleted": {
                "at": now.isoformat(),
                "by": getattr(user, "id", None),
                "reason": (str(reason)[:255] if reason else None),
            }
        },
    )

    _set_if_has(ln, "updated_at", now)
    db.add(ln)
    db.flush()

    _recalc_invoice_totals(db, int(inv.id))
    _set_if_has(inv, "updated_by", getattr(user, "id", None))
    _set_if_has(inv, "updated_at", now)
    db.add(inv)
    db.flush()

    try:
        db.add(
            BillingAuditLog(
                billing_case_id=int(inv.billing_case_id),
                invoice_id=int(inv.id),
                action="LINE_DELETE",
                ref_type="BillingInvoiceLine",
                ref_id=int(line_id),
                notes=(str(reason)[:255] if reason else None),
                created_by=getattr(user, "id", None),
                created_at=now,
            ))
        db.flush()
    except Exception:
        pass


def list_invoice_lines(
        db: Session,
        *,
        invoice_id: int,
        include_deleted: bool = False) -> List[BillingInvoiceLine]:
    q = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(invoice_id)).order_by(
            BillingInvoiceLine.id.asc())
    if not include_deleted:
        q = _apply_active_line_filter(q)
    return q.all()


# ============================================================
# Invoice State transitions
# ============================================================
def approve_invoice(db: Session, *, invoice_id: int, user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if str(_enum_value(inv.status)) != "DRAFT":
        raise BillingStateError("Only DRAFT invoice can be approved")

    q = db.query(func.count(BillingInvoiceLine.id)).filter(
        BillingInvoiceLine.invoice_id == int(inv.id))
    q = _apply_active_line_filter(q)
    line_count = q.scalar() or 0
    if int(line_count) <= 0:
        raise BillingError("Cannot approve invoice with no lines")

    _recalc_invoice_totals(db, int(inv.id))

    inv.status = DocStatus.APPROVED
    _set_if_has(inv, "approved_at", _utcnow_naive())
    _set_if_has(inv, "approved_by", getattr(user, "id", None))
    inv.updated_by = getattr(user, "id", None)
    db.flush()
    return inv


def post_invoice(db: Session, *, invoice_id: int,
                 user: User) -> Tuple[BillingInvoice, Optional[BillingClaim]]:
    inv, claim = post_invoice_workflow(db,
                                       invoice_id=int(invoice_id),
                                       user=user)
    db.flush()
    return inv, claim


def request_invoice_edit(db: Session, *, invoice_id: int,
                         reason: Optional[str], user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status == DocStatus.POSTED:
        raise BillingStateError(
            "POSTED invoice cannot be edited (use credit note / reversal flow)"
        )

    if inv.status not in (DocStatus.APPROVED, DocStatus.DRAFT):
        raise BillingStateError("Only DRAFT/APPROVED invoice can request edit")

    _merge_meta(
        inv,
        {
            "edit_request": {
                "at": _utcnow_naive().isoformat(),
                "by": getattr(user, "id", None),
                "reason": (str(reason)[:255] if reason else None),
                "from_status": str(getattr(inv, "status", "")),
            }
        },
    )

    inv.status = DocStatus.DRAFT
    inv.updated_by = getattr(user, "id", None)
    _set_if_has(inv, "updated_at", _utcnow_naive())
    db.flush()
    return inv


def reopen_invoice(db: Session, *, invoice_id: int, reason: Optional[str],
                   user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status == DocStatus.VOID:
        raise BillingStateError(
            "VOID invoice cannot be reopened (create a new invoice)")
    if inv.status == DocStatus.POSTED:
        raise BillingStateError(
            "POSTED invoice cannot be reopened (use credit note / reversal flow)"
        )
    if inv.status not in (DocStatus.APPROVED, DocStatus.DRAFT):
        raise BillingStateError("Only DRAFT/APPROVED invoice can be reopened")

    _merge_meta(
        inv,
        {
            "reopen": {
                "at": _utcnow_naive().isoformat(),
                "by": getattr(user, "id", None),
                "reason": (str(reason)[:255] if reason else None),
                "from_status": str(getattr(inv, "status", "")),
            }
        },
    )
    inv.status = DocStatus.DRAFT
    inv.updated_by = getattr(user, "id", None)
    _set_if_has(inv, "updated_at", _utcnow_naive())
    db.flush()
    return inv


def void_invoice(db: Session, *, invoice_id: int, reason: str,
                 user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status == DocStatus.POSTED:
        raise BillingStateError(
            "POSTED invoice cannot be voided (use credit note / reversal flow)"
        )

    inv.status = DocStatus.VOID
    _set_if_has(inv, "voided_at", _utcnow_naive())
    _set_if_has(inv, "voided_by", getattr(user, "id", None))
    _set_if_has(inv, "void_reason", str(reason or "")[:255])
    inv.updated_by = getattr(user, "id", None)
    db.flush()
    return inv


def _session_get(db: Session, model, pk: Any):
    """
    SQLAlchemy-safe get (works for SA 1.4/2.x style).
    """
    getter = getattr(db, "get", None)
    if callable(getter):
        return getter(model, pk)
    # fallback
    return db.query(model).get(pk)


# ============================================================
# Payments / Advances (wrappers to v2)
# ============================================================
def _unwrap_payment_v2_result(
    db: Session,
    res: Any,
) -> Tuple[BillingPayment, Optional[List[Dict[str, Any]]]]:
    """
    Normalize record_payment_v2() output into:
      (BillingPayment ORM row, allocations|None)

    Supports:
      - BillingPayment
      - {"payment": BillingPayment, "allocations": [...]}
      - {"payment": {"id": ...}, "allocations": [...]}
      - {"id": ...} / {"payment_id": ...}
      - {"payment": <id>, "allocations": [...]}
    """
    # 1) ORM returned directly
    if isinstance(res, BillingPayment):
        return res, None

    # 2) dict shape
    if isinstance(res, dict):
        allocations = res.get("allocations")

        pay_obj = res.get("payment", None)

        # 2a) payment is ORM
        if isinstance(pay_obj, BillingPayment):
            return pay_obj, allocations

        # 2b) payment is an ID
        if isinstance(pay_obj, (int, str)) and str(pay_obj).isdigit():
            pid = int(pay_obj)
            row = _session_get(db, BillingPayment, pid)
            if not row:
                raise HTTPException(
                    status_code=500,
                    detail=f"Payment row not found for id={pid}")
            return row, allocations

        # 2c) payment is dict (serialized)
        if isinstance(pay_obj, dict):
            pid = pay_obj.get("id") or pay_obj.get("payment_id") or res.get(
                "id") or res.get("payment_id")
            if pid:
                row = _session_get(db, BillingPayment, int(pid))
                if not row:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Payment row not found for id={pid}")
                return row, allocations

        # 2d) dict itself contains id/payment_id
        pid = res.get("id") or res.get("payment_id")
        if pid:
            row = _session_get(db, BillingPayment, int(pid))
            if not row:
                raise HTTPException(
                    status_code=500,
                    detail=f"Payment row not found for id={pid}")
            return row, allocations

        # if dict has no usable id -> this is the real bug upstream
        raise HTTPException(
            status_code=500,
            detail=
            f"record_payment_v2() returned dict without payment id keys: keys={list(res.keys())}",
        )

    # 3) unsupported
    raise HTTPException(
        status_code=500,
        detail=
        f"record_payment_v2() returned unsupported type: {type(res).__name__}",
    )


def record_payment_full(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    mode: PayMode = PayMode.CASH,
    invoice_id: Optional[int] = None,
    txn_ref: Optional[str] = None,
    notes: Optional[str] = None,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
) -> Tuple[BillingPayment, Optional[List[Dict[str, Any]]]]:
    """
    Returns (payment ORM row, allocations|None)
    """
    res = record_payment_v2(
        db,
        billing_case_id=billing_case_id,
        user=user,
        amount=amount,
        mode=mode,
        invoice_id=invoice_id,
        txn_ref=txn_ref,
        notes=notes,
        payer_type=payer_type,
        payer_id=payer_id,
        kind=PaymentKind.RECEIPT,
        direction=PaymentDirection.IN,
    )
    return _unwrap_payment_v2_result(db, res)


def record_payment(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    mode: PayMode = PayMode.CASH,
    invoice_id: Optional[int] = None,
    txn_ref: Optional[str] = None,
    notes: Optional[str] = None,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
) -> BillingPayment:
    """
    Backward-compatible: returns ONLY the ORM BillingPayment row.
    """
    payment, _alloc = record_payment_full(
        db,
        billing_case_id=billing_case_id,
        user=user,
        amount=amount,
        mode=mode,
        invoice_id=invoice_id,
        txn_ref=txn_ref,
        notes=notes,
        payer_type=payer_type,
        payer_id=payer_id,
    )
    return payment


def record_advance(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    entry_type: AdvanceType = AdvanceType.ADVANCE,
    mode: PayMode = PayMode.CASH,
    txn_ref: Optional[str] = None,
    remarks: Optional[str] = None,
    advance_type: Optional[AdvanceType] = None,  # legacy alias
    notes: Optional[str] = None,
):
    et = entry_type or advance_type or AdvanceType.ADVANCE
    return record_advance_v2(
        db,
        billing_case_id=billing_case_id,
        user=user,
        amount=amount,
        entry_type=et,
        mode=mode,
        txn_ref=txn_ref,
        remarks=remarks or notes,
    )


def apply_advances_to_case(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    max_apply_amount: Optional[Decimal] = None,
):
    return apply_advances_to_case_v2(
        db,
        billing_case_id=billing_case_id,
        user=user,
        max_apply_amount=max_apply_amount,
    )


def case_financials(db: Session, *, case_id: int) -> Dict[str, Any]:
    return case_financials_v2(db, case_id=case_id)


# ============================================================
# ✅ OP VISIT CONSULTATION FEE AUTO ADD
# ============================================================
def _enum_pick(enum_cls, name: str, fallback):
    try:
        return enum_cls[name]
    except Exception:
        return fallback


def _safe_get(obj, names: list, default=None):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return default


def _load_user_model():
    candidates = [
        ("app.models.user", "User"),
        ("app.models.users", "User"),
        ("app.models.auth", "User"),
        ("app.models.accounts", "User"),
    ]
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            return getattr(m, cls)
        except Exception:
            continue
    return None


def _load_department_model():
    candidates = [
        ("app.models.master", "Department"),
        ("app.models.masters", "Department"),
        ("app.models.department", "Department"),
        ("app.models.opd", "Department"),
    ]
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            return getattr(m, cls)
        except Exception:
            continue
    return None


def ensure_op_visit_consultation_fee(
    db: Session,
    *,
    visit_id: int,
    user,
    tariff_plan_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
    intra_state_gst: bool = True,
) -> Dict[str, Any]:
    """
    Ensures:
      - BillingCase exists and case_number not TEMP
      - DOCTOR_FEE invoice exists
      - Adds ONE idempotent consult fee line
    """
    v = db.get(Visit, int(visit_id))
    if not v:
        raise BillingError("OPD Visit not found")

    case = get_or_create_case_for_op_visit(
        db,
        visit_id=int(visit_id),
        user=user,
        tariff_plan_id=tariff_plan_id,
        reset_period=reset_period,
    )
    _ensure_case_number(db, case, reset_period=reset_period)

    inv = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="DOCTOR_FEE",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
        reset_period=reset_period,
    )

    doctor_id = _safe_get(
        v, ["doctor_id", "doctor_user_id", "consulting_doctor_id"], None)
    dept_id = _safe_get(v, ["department_id", "dept_id"], None)

    appt = None
    appt_id = _safe_get(v, ["appointment_id"], None)
    if appt_id:
        appt = db.get(Appointment, int(appt_id))
        if doctor_id is None and appt is not None:
            doctor_id = _safe_get(appt, ["doctor_id", "doctor_user_id"], None)
        if dept_id is None and appt is not None:
            dept_id = _safe_get(appt, ["department_id", "dept_id"], None)

    doctor_name = None
    dept_name = None

    UserModel = _load_user_model()
    if UserModel and doctor_id:
        try:
            u = db.get(UserModel, int(doctor_id))
            doctor_name = (getattr(u, "full_name", None)
                           or getattr(u, "name", None)
                           or getattr(u, "username", None)
                           or getattr(u, "display_name", None))
        except Exception:
            doctor_name = None

    DeptModel = _load_department_model()
    if DeptModel and dept_id:
        try:
            dpt = db.get(DeptModel, int(dept_id))
            dept_name = getattr(dpt, "name", None) or getattr(
                dpt, "dept_name", None)
        except Exception:
            dept_name = None

    fee = _safe_get(v, [
        "consultation_fee", "consult_fee", "doctor_fee", "fee_amount", "amount"
    ], None)
    gst = _safe_get(v, ["gst_rate", "gst", "tax_rate"], None)

    if appt is not None:
        if fee is None:
            fee = _safe_get(appt, [
                "consultation_fee", "consult_fee", "doctor_fee", "fee_amount",
                "amount"
            ], None)
        if gst is None:
            gst = _safe_get(appt, ["gst_rate", "gst", "tax_rate"], None)

    if _d(fee) <= 0:
        r, g = _tariff_lookup_first(
            db,
            tariff_plan_id=(tariff_plan_id
                            or getattr(case, "tariff_plan_id", None)),
            item_id=int(doctor_id) if str(doctor_id).isdigit() else None,
            item_types=[
                "OP_CONSULT", "OPD_CONSULT", "CONSULTATION", "DOCTOR_CONSULT"
            ],
        )
        if _d(r) > 0:
            fee, gst = r, g

    if _d(fee) <= 0:
        r, g = _tariff_lookup_first(
            db,
            tariff_plan_id=(tariff_plan_id
                            or getattr(case, "tariff_plan_id", None)),
            item_id=int(dept_id) if str(dept_id).isdigit() else None,
            item_types=[
                "OP_CONSULT_DEPT", "OPD_CONSULT_DEPT", "CONSULTATION_DEPT"
            ],
        )
        if _d(r) > 0:
            fee, gst = r, g

    fee = _d(fee)
    gst = _d(gst)

    desc = "Consultation Fees"
    if doctor_name and dept_name:
        desc = f"Consultation Fees - Dr. {doctor_name} ({dept_name})"
    elif doctor_name:
        desc = f"Consultation Fees - Dr. {doctor_name}"
    elif dept_name:
        desc = f"Consultation Fees ({dept_name})"

    sg = _enum_pick(ServiceGroup, "CONSULTATION", fallback=None)
    if sg is None:
        sg = _enum_pick(ServiceGroup, "DOCTOR", fallback=list(ServiceGroup)[0])

    ln = add_auto_line_idempotent(
        db,
        invoice_id=int(inv.id),
        billing_case_id=int(case.id),
        user=user,
        service_group=sg,
        item_type="OP_CONSULTATION",
        item_id=int(doctor_id) if str(doctor_id).isdigit() else None,
        item_code="CONSULT",
        description=desc,
        qty=Decimal("1"),
        unit_price=fee,
        gst_rate=gst,
        source_module="OPD",
        source_ref_id=int(case.id),  # case-scoped to avoid collisions
        source_line_key=
        f"CASE:{int(case.id)}:VISIT:{int(visit_id)}:CONSULT_FEE",
        doctor_id=int(doctor_id) if str(doctor_id).isdigit() else None,
        intra_state_gst=intra_state_gst,
        is_manual=False,
        manual_reason=None,
        meta_patch={
            "opd": {
                "visit_id": int(visit_id),
                "doctor_id":
                int(doctor_id) if str(doctor_id).isdigit() else None,
                "doctor_name": doctor_name,
                "department_id":
                int(dept_id) if str(dept_id).isdigit() else None,
                "department_name": dept_name,
                "fee_source": "visit/appointment/tariff(best-effort)",
            }
        },
    )

    _recalc_invoice_totals(db, int(inv.id))

    return {
        "billing_case_id": int(case.id),
        "case_number": getattr(case, "case_number", None),
        "invoice_id": int(inv.id),
        "invoice_number": getattr(inv, "invoice_number", None),
        "added_line_id": (int(ln.id) if ln else None),
        "skipped": (ln is None),
        "unit_price": float(fee),
        "gst_rate": float(gst),
        "description": desc,
    }


# ============================================================
# PRINT SUMMARY + SPLIT-UP REPORT BUILDERS
# ============================================================
def build_invoice_print_payload(db: Session, *,
                                invoice_id: int) -> Dict[str, Any]:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    ql = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
            BillingInvoiceLine.id.asc())
    ql = _apply_active_line_filter(ql)
    lines = ql.all()

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for ln in lines:
        sg = str(getattr(ln, "service_group", "") or "OTHER")
        grouped.setdefault(sg, []).append({
            "id":
            int(getattr(ln, "id")),
            "description":
            getattr(ln, "description", ""),
            "qty":
            float(_d(getattr(ln, "qty", 0))),
            "unit_price":
            float(_d(getattr(ln, "unit_price", 0))),
            "line_total":
            float(_d(getattr(ln, "line_total", 0))),
            "discount":
            float(_d(getattr(ln, "discount_amount", 0))),
            "tax":
            float(_d(getattr(ln, "tax_amount", 0))),
            "net":
            float(_d(getattr(ln, "net_amount", 0))),
            "gst_rate":
            float(_d(getattr(ln, "gst_rate", 0))),
            "meta":
            getattr(ln, "meta_json", None)
            if hasattr(ln, "meta_json") else None,
            "source_module":
            getattr(ln, "source_module", None),
            "is_manual":
            bool(getattr(ln, "is_manual", False)),
        })

    payments = db.query(BillingPayment).filter(
        BillingPayment.invoice_id == int(inv.id)).order_by(
            BillingPayment.id.asc()).all()
    pay_rows = [{
        "id": int(p.id),
        "mode": str(getattr(p, "mode", "")),
        "amount": float(_d(getattr(p, "amount", 0))),
        "txn_ref": getattr(p, "txn_ref", None),
        "received_by": getattr(p, "received_by", None),
        "notes": getattr(p, "notes", None),
    } for p in payments]

    _recalc_invoice_totals(db, int(inv.id))

    gst_meta = {}
    if hasattr(inv, "meta_json") and isinstance(
            getattr(inv, "meta_json", None), dict):
        gst_meta = inv.meta_json.get("gst", {}) or {}

    return {
        "invoice": {
            "id": int(inv.id),
            "invoice_number": getattr(inv, "invoice_number", ""),
            "module": getattr(inv, "module", None),
            "status": str(getattr(inv, "status", "")),
            "invoice_type": str(getattr(inv, "invoice_type", "")),
            "payer_type": str(getattr(inv, "payer_type", "")),
            "payer_id": getattr(inv, "payer_id", None),
            "currency": getattr(inv, "currency", "INR"),
        },
        "case": {
            "id": int(case.id),
            "case_number": getattr(case, "case_number", ""),
            "encounter_type": str(getattr(case, "encounter_type", "")),
            "encounter_id": int(getattr(case, "encounter_id")),
            "patient_id": int(getattr(case, "patient_id")),
            "status": str(getattr(case, "status", "")),
            "payer_mode": str(getattr(case, "payer_mode", "")),
            "tariff_plan_id": getattr(case, "tariff_plan_id", None),
        },
        "lines_grouped": grouped,
        "totals": {
            "sub_total": float(_d(getattr(inv, "sub_total", 0))),
            "discount_total": float(_d(getattr(inv, "discount_total", 0))),
            "tax_total": float(_d(getattr(inv, "tax_total", 0))),
            "round_off": float(_d(getattr(inv, "round_off", 0))),
            "grand_total": float(_d(getattr(inv, "grand_total", 0))),
            "gst_split": gst_meta,
        },
        "payments": pay_rows,
    }


def build_case_splitup_report(db: Session, *,
                              billing_case_id: int) -> Dict[str, Any]:
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    invoices = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case.id)).order_by(
            BillingInvoice.id.asc()).all()

    inv_payloads = []
    module_totals: Dict[str, Dict[str, Decimal]] = {}

    for inv in invoices:
        _recalc_invoice_totals(db, int(inv.id))
        mod = getattr(inv, "module", None) or "GENERAL"
        module_totals.setdefault(
            mod, {
                "sub_total": Decimal("0"),
                "discount_total": Decimal("0"),
                "tax_total": Decimal("0"),
                "grand_total": Decimal("0")
            })

        module_totals[mod]["sub_total"] += _d(getattr(inv, "sub_total", 0))
        module_totals[mod]["discount_total"] += _d(
            getattr(inv, "discount_total", 0))
        module_totals[mod]["tax_total"] += _d(getattr(inv, "tax_total", 0))
        module_totals[mod]["grand_total"] += _d(getattr(inv, "grand_total", 0))

        inv_payloads.append({
            "id":
            int(inv.id),
            "invoice_number":
            getattr(inv, "invoice_number", ""),
            "module":
            getattr(inv, "module", None),
            "status":
            str(getattr(inv, "status", "")),
            "totals": {
                "sub_total": float(_d(getattr(inv, "sub_total", 0))),
                "discount_total": float(_d(getattr(inv, "discount_total", 0))),
                "tax_total": float(_d(getattr(inv, "tax_total", 0))),
                "grand_total": float(_d(getattr(inv, "grand_total", 0))),
            },
            "meta":
            getattr(inv, "meta_json", None)
            if hasattr(inv, "meta_json") else None,
        })

    payments_sum = (db.query(func.coalesce(
        func.sum(BillingPayment.amount),
        0)).filter(BillingPayment.billing_case_id == int(case.id)).scalar())
    advances_sum = (db.query(func.coalesce(
        func.sum(BillingAdvance.amount),
        0)).filter(BillingAdvance.billing_case_id == int(case.id)).scalar())

    case_grand = sum(v["grand_total"] for v in module_totals.values())
    balance = _d(case_grand) - _d(payments_sum) - _d(advances_sum)

    return {
        "case": {
            "id": int(case.id),
            "case_number": getattr(case, "case_number", ""),
            "encounter_type": str(getattr(case, "encounter_type", "")),
            "encounter_id": int(getattr(case, "encounter_id")),
            "patient_id": int(getattr(case, "patient_id")),
            "status": str(getattr(case, "status", "")),
            "payer_mode": str(getattr(case, "payer_mode", "")),
        },
        "invoices": inv_payloads,
        "module_totals": {
            k: {
                kk: float(_d(vv))
                for kk, vv in v.items()
            }
            for k, v in module_totals.items()
        },
        "case_totals": {
            "grand_total": float(_d(case_grand)),
            "payments_total": float(_d(payments_sum)),
            "advances_total": float(_d(advances_sum)),
            "balance": float(_d(balance)),
        },
    }


# ============================================================
# LIS / RIS -> add lines to invoice (SAFE optional integration)
# ============================================================
def _load_lis_order_and_items(db: Session, lis_order_id: int):
    candidates = [
        ("app.models.lis", "LisOrder"),
        ("app.models.lis", "LabOrder"),
        ("app.models.lis_orders", "LisOrder"),
        ("app.models.lis_order", "LisOrder"),
        ("app.models.lab", "LabOrder"),
    ]

    order = None
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            OrderModel = getattr(m, cls)
            order = db.get(OrderModel, int(lis_order_id))
            if order:
                break
        except Exception:
            continue

    if not order:
        raise BillingError(
            "LIS order not found (LIS module/model not available)")

    item_candidates = [
        ("app.models.lis", "LisOrderItem"),
        ("app.models.lis", "LabOrderItem"),
        ("app.models.lis", "LisOrderTest"),
        ("app.models.lis", "LabOrderTest"),
        ("app.models.lab", "LabOrderItem"),
    ]

    items = []
    for mod, cls in item_candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            ItemModel = getattr(m, cls)
            fk = None
            for fk_name in ["order_id", "lis_order_id", "lab_order_id"]:
                if hasattr(ItemModel, fk_name):
                    fk = getattr(ItemModel, fk_name)
                    break
            if fk is None:
                continue
            items = db.query(ItemModel).filter(fk == int(lis_order_id)).all()
            if items:
                break
        except Exception:
            continue

    return order, items


def _load_ris_order_and_items(db: Session, ris_order_id: int):
    candidates = [
        ("app.models.ris", "RisOrder"),
        ("app.models.ris", "RadiologyOrder"),
        ("app.models.ris_orders", "RisOrder"),
        ("app.models.ris_order", "RisOrder"),
        ("app.models.radiology", "RadiologyOrder"),
    ]

    order = None
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            OrderModel = getattr(m, cls)
            order = db.get(OrderModel, int(ris_order_id))
            if order:
                break
        except Exception:
            continue

    if not order:
        raise BillingError(
            "RIS order not found (RIS module/model not available)")

    item_candidates = [
        ("app.models.ris", "RisOrderItem"),
        ("app.models.ris", "RadiologyOrderItem"),
        ("app.models.radiology", "RadiologyOrderItem"),
        ("app.models.radiology", "RadiologyOrderTest"),
    ]

    items = []
    for mod, cls in item_candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            ItemModel = getattr(m, cls)
            fk = None
            for fk_name in ["order_id", "ris_order_id", "radiology_order_id"]:
                if hasattr(ItemModel, fk_name):
                    fk = getattr(ItemModel, fk_name)
                    break
            if fk is None:
                continue
            items = db.query(ItemModel).filter(fk == int(ris_order_id)).all()
            if items:
                break
        except Exception:
            continue

    return order, items


def add_lines_from_lis_order(db: Session, *, invoice_id: int,
                             lis_order_id: int, user) -> Dict[str, Any]:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")
    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError(
            "Can add LIS lines only to DRAFT/APPROVED invoice")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    order, items = _load_lis_order_and_items(db, int(lis_order_id))

    added = []
    skipped = 0
    sg_lab = _enum_pick(ServiceGroup, "LAB", fallback=list(ServiceGroup)[0])

    if items:
        for it in items:
            item_id = _safe_get(it, ["test_id", "service_id", "item_id", "id"],
                                None)
            code = _safe_get(it, ["test_code", "code", "item_code"], None)
            name = _safe_get(
                it, ["test_name", "name", "service_name", "description"],
                "Lab Test")

            qty = _d(_safe_get(it, ["qty", "quantity"], 1))
            unit_price = _d(
                _safe_get(it, ["rate", "price", "unit_price", "amount"], 0))
            gst_rate = _d(_safe_get(it, ["gst_rate", "gst", "tax_rate"], 0))
            line_key = str(
                _safe_get(it, ["id"], None) or code
                or f"IT:{secrets.token_hex(4)}")

            ln = add_auto_line_idempotent(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=sg_lab,
                item_type="LIS_TEST",
                item_id=int(item_id) if str(item_id).isdigit() else None,
                item_code=str(code) if code else None,
                description=str(name),
                qty=qty,
                unit_price=unit_price,
                gst_rate=gst_rate,
                source_module="LIS",
                source_ref_id=int(lis_order_id),
                source_line_key=line_key,
                doctor_id=None,
                intra_state_gst=True,
                is_manual=False,
                manual_reason=None,
                meta_patch={
                    "lis": {
                        "order_id": int(lis_order_id),
                        "item_id": int(_safe_get(it, ["id"], 0) or 0)
                    }
                },
            )

            if ln is None:
                skipped += 1
            else:
                added.append(int(ln.id))

        _recalc_invoice_totals(db, int(inv.id))
        return {
            "invoice_id": int(inv.id),
            "lis_order_id": int(lis_order_id),
            "added_line_ids": added,
            "skipped": skipped
        }

    total = _d(
        _safe_get(order,
                  ["total_amount", "grand_total", "net_amount", "amount"], 0))
    gst_rate = _d(_safe_get(order, ["gst_rate", "gst", "tax_rate"], 0))
    desc = str(
        _safe_get(order, ["description", "remarks"], "LIS Order Charges"))

    ln = add_auto_line_idempotent(
        db,
        invoice_id=int(inv.id),
        billing_case_id=int(case.id),
        user=user,
        service_group=sg_lab,
        item_type="LIS_ORDER",
        item_id=int(lis_order_id),
        item_code=None,
        description=desc,
        qty=Decimal("1"),
        unit_price=total,
        gst_rate=gst_rate,
        source_module="LIS",
        source_ref_id=int(lis_order_id),
        source_line_key=f"ORDER:{int(lis_order_id)}",
        doctor_id=None,
        intra_state_gst=True,
        is_manual=False,
        manual_reason=None,
        meta_patch={
            "lis": {
                "order_id": int(lis_order_id),
                "mode": "consolidated"
            }
        },
    )

    if ln is None:
        skipped += 1
    else:
        added.append(int(ln.id))

    _recalc_invoice_totals(db, int(inv.id))
    return {
        "invoice_id": int(inv.id),
        "lis_order_id": int(lis_order_id),
        "added_line_ids": added,
        "skipped": skipped
    }


def add_line_from_ris_order(db: Session, *, invoice_id: int, ris_order_id: int,
                            user) -> Dict[str, Any]:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")
    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError(
            "Can add RIS lines only to DRAFT/APPROVED invoice")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    order, items = _load_ris_order_and_items(db, int(ris_order_id))

    sg_scan = _enum_pick(ServiceGroup, "SCAN", fallback=list(ServiceGroup)[0])
    sg_xray = _enum_pick(ServiceGroup, "XRAY", fallback=sg_scan)

    modality = str(
        _safe_get(order, ["modality", "study_type", "service_type"], "")
        or "").upper()
    sg = sg_xray if ("XRAY" in modality or "X-RAY" in modality) else sg_scan

    added = []
    skipped = 0

    if items:
        for it in items:
            item_id = _safe_get(it, ["test_id", "service_id", "item_id", "id"],
                                None)
            code = _safe_get(it, ["test_code", "code", "item_code"], None)
            name = _safe_get(
                it, ["test_name", "name", "service_name", "description"],
                "Radiology")

            qty = _d(_safe_get(it, ["qty", "quantity"], 1))
            unit_price = _d(
                _safe_get(it, ["rate", "price", "unit_price", "amount"], 0))
            gst_rate = _d(_safe_get(it, ["gst_rate", "gst", "tax_rate"], 0))
            line_key = str(
                _safe_get(it, ["id"], None) or code
                or f"IT:{secrets.token_hex(4)}")

            ln = add_auto_line_idempotent(
                db,
                invoice_id=int(inv.id),
                billing_case_id=int(case.id),
                user=user,
                service_group=sg,
                item_type="RIS_TEST",
                item_id=int(item_id) if str(item_id).isdigit() else None,
                item_code=str(code) if code else None,
                description=str(name),
                qty=qty,
                unit_price=unit_price,
                gst_rate=gst_rate,
                source_module="RIS",
                source_ref_id=int(ris_order_id),
                source_line_key=line_key,
                doctor_id=None,
                intra_state_gst=True,
                is_manual=False,
                manual_reason=None,
                meta_patch={
                    "ris": {
                        "order_id": int(ris_order_id),
                        "item_id": int(_safe_get(it, ["id"], 0) or 0)
                    }
                },
            )

            if ln is None:
                skipped += 1
            else:
                added.append(int(ln.id))

        _recalc_invoice_totals(db, int(inv.id))
        return {
            "invoice_id": int(inv.id),
            "ris_order_id": int(ris_order_id),
            "added_line_ids": added,
            "skipped": skipped
        }

    total = _d(
        _safe_get(order,
                  ["total_amount", "grand_total", "net_amount", "amount"], 0))
    gst_rate = _d(_safe_get(order, ["gst_rate", "gst", "tax_rate"], 0))
    desc = str(
        _safe_get(order, ["description", "remarks"], "RIS Order Charges"))

    ln = add_auto_line_idempotent(
        db,
        invoice_id=int(inv.id),
        billing_case_id=int(case.id),
        user=user,
        service_group=sg,
        item_type="RIS_ORDER",
        item_id=int(ris_order_id),
        item_code=None,
        description=desc,
        qty=Decimal("1"),
        unit_price=total,
        gst_rate=gst_rate,
        source_module="RIS",
        source_ref_id=int(ris_order_id),
        source_line_key=f"ORDER:{int(ris_order_id)}",
        doctor_id=None,
        intra_state_gst=True,
        is_manual=False,
        manual_reason=None,
        meta_patch={
            "ris": {
                "order_id": int(ris_order_id),
                "mode": "consolidated"
            }
        },
    )

    added2 = [int(ln.id)] if ln else []
    skipped2 = 0 if ln else 1
    _recalc_invoice_totals(db, int(inv.id))
    return {
        "invoice_id": int(inv.id),
        "ris_order_id": int(ris_order_id),
        "added_line_ids": added2,
        "skipped": skipped2
    }
