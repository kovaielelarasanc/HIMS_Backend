from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from typing import Any, Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo
from sqlalchemy import func
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
)

from app.services.id_gen import next_billing_case_number, next_invoice_number


# ============================================================
# Errors
# ============================================================
class BillingError(RuntimeError):
    pass


class BillingStateError(BillingError):
    pass


# ============================================================
# Small helpers
# ============================================================
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


def _merge_meta(obj: Any, patch: Dict[str, Any]) -> None:
    """
    Safe meta_json merge:
    - If your models don't have meta_json, this becomes no-op.
    - If meta_json exists but is NULL, initializes dict.
    """
    if not hasattr(obj, "meta_json"):
        return
    current = getattr(obj, "meta_json", None)
    if not isinstance(current, dict):
        current = {}
    # shallow merge
    current.update(patch or {})
    setattr(obj, "meta_json", current)


# ============================================================
# Number generators (compatible with your mixed signatures)
# ============================================================
import inspect


def _safe_call_idgen(fn, db: Session, **kwargs) -> str:
    """
    Calls your id_gen functions without tenant logic.
    If id_gen still requires tenant_id, we pass tenant_id=None as a compatibility fallback.
    """
    sig = None
    try:
        sig = inspect.signature(fn)
        allowed = set(sig.parameters.keys())
    except Exception:
        allowed = set(kwargs.keys())

    def _filtered(d: dict) -> dict:
        return {k: v for k, v in d.items() if k in allowed}

    # 1) try without tenant_id
    try:
        return fn(db, **_filtered(kwargs))
    except TypeError:
        pass

    # 2) compatibility fallback ONLY if id_gen requires tenant_id
    #    (still not using tenant logic; value is None)
    try:
        kwargs2 = dict(kwargs)
        kwargs2["tenant_id"] = None
        return fn(db, **_filtered(kwargs2))
    except TypeError as e:
        raise e


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

    # Try your common signatures (no tenant_id)
    for args in [
        {
            "encounter_type": et,
            "reset_period": reset_period
        },
        {
            "encounter_type": et,
            "on_date": on_date,
            "padding": padding or 6
        },
        {
            "encounter_type": et
        },
    ]:
        try:
            return _safe_call_idgen(next_billing_case_number, db, **args)
        except TypeError:
            continue

    # if all fail, raise the last signature error
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
            "padding": padding or 6
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


# ============================================================
# GST split helpers (stored into meta_json)
# ============================================================
def _gst_split(
    gst_rate: Decimal,
    *,
    intra_state: bool = True,
) -> Dict[str, Decimal]:
    """
    If intra_state: GST splits into CGST+SGST (half+half)
    Else: IGST only (full)
    """
    r = _d(gst_rate)
    if r <= 0:
        return {
            "gst_rate": Decimal("0"),
            "cgst_rate": Decimal("0"),
            "sgst_rate": Decimal("0"),
            "igst_rate": Decimal("0"),
        }
    if intra_state:
        half = (r / Decimal("2"))
        return {
            "gst_rate": r,
            "cgst_rate": half,
            "sgst_rate": half,
            "igst_rate": Decimal("0"),
        }
    return {
        "gst_rate": r,
        "cgst_rate": Decimal("0"),
        "sgst_rate": Decimal("0"),
        "igst_rate": r,
    }


def _gst_amount_split(
    tax_amount: Decimal,
    split_rates: Dict[str, Decimal],
) -> Dict[str, Decimal]:
    """
    Splits the computed tax_amount into CGST/SGST/IGST amounts.
    """
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

    # If IGST used, all tax is IGST
    if igst_r > 0 and (cgst_r + sgst_r) <= 0:
        return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": tax}

    # Otherwise split equally into cgst/sgst (safe even if rate odd)
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


# ============================================================
# Encounter -> patient_id resolvers
# ============================================================
def _patient_id_from_visit(db: Session, visit_id: int) -> int:
    # local imports to avoid circular deps
    from app.models.opd import Visit, Appointment  # adjust if your path differs

    v = db.get(Visit, int(visit_id))
    if not v:
        raise BillingError("OPD Visit not found")

    pid = getattr(v, "patient_id", None)
    if pid:
        return int(pid)

    appt_id = getattr(v, "appointment_id", None)
    if appt_id:
        a = db.get(Appointment, int(appt_id))
        pid2 = getattr(a, "patient_id", None) if a else None
        if pid2:
            return int(pid2)

    raise BillingError(
        "Visit has no patient_id and appointment link has no patient_id")


# ============================================================
# Case creation
# ============================================================
def get_or_create_case_for_op_visit(
    db: Session,
    *,
    visit_id: int,
    user,
    tariff_plan_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingCase:
    # NO tenant filter
    case = (db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.OP,
        BillingCase.encounter_id == int(visit_id),
    ).first())
    if case:
        if tariff_plan_id is not None:
            case.tariff_plan_id = tariff_plan_id
            case.updated_by = getattr(user, "id", None)
            db.flush()
        return case

    pid = _patient_id_from_visit(db, int(visit_id))

    case = BillingCase(
        patient_id=pid,
        encounter_type=EncounterType.OP,
        encounter_id=int(visit_id),
        case_number="TEMP",
        status=BillingCaseStatus.OPEN,
        payer_mode=PayerMode.SELF,
        tariff_plan_id=tariff_plan_id,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(case)
    db.flush()

    case.case_number = _call_next_case_number(
        db,
        encounter_type=EncounterType.OP,
        reset_period=reset_period,
    )
    db.flush()
    return case


def get_or_create_case_for_ip_admission(
    db: Session,
    *,
    admission_id: int,
    user,
    tariff_plan_id: Optional[int] = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> BillingCase:
    # NO tenant filter
    case = (db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.IP,
        BillingCase.encounter_id == int(admission_id),
    ).first())
    if case:
        if tariff_plan_id is not None:
            case.tariff_plan_id = tariff_plan_id
            case.updated_by = getattr(user, "id", None)
            db.flush()
        return case

    from app.models.ipd import IpdAdmission  # adjust if your path differs

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

    case.case_number = _call_next_case_number(
        db,
        encounter_type=EncounterType.IP,
        reset_period=reset_period,
    )
    db.flush()
    return case


# ============================================================
# Invoice (module-wise)
# ============================================================
def _now_local():
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return datetime.now(timezone.utc).astimezone(tz)


def create_invoice(
        db: Session,
        *,
        billing_case_id: int,
        user,
        module: Optional[str] = None,
        invoice_type: InvoiceType = InvoiceType.PATIENT,
        payer_type: PayerType = PayerType.PATIENT,
        payer_id: Optional[int] = None,
        reset_period: NumberResetPeriod = NumberResetPeriod.
    NONE,  # daily series (date included)
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

    # ✅ THIS is your good format generator:
    inv.invoice_number = next_invoice_number(
        db,
        tenant_id=None,  # no tenant use; id_gen maps this to 0 internally
        encounter_type=(case.encounter_type.value if hasattr(
            case.encounter_type, "value") else str(case.encounter_type)),
        on_date=_now_local(),  # ensures DDMMYYYY is correct for IST
        padding=6,
        reset_period=reset_period,
    )
    db.flush()
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
    """
    Module-wise invoice rule:
    - Return latest invoice for (case, module, invoice_type, payer_type, payer_id)
      that is in DRAFT/APPROVED (safe for adding lines)
    - If only POSTED exists -> create new DRAFT invoice (same module/payer)
    """
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.module == str(module),
        BillingInvoice.invoice_type == invoice_type,
        BillingInvoice.payer_type == payer_type,
        BillingInvoice.payer_id == payer_id,
        BillingInvoice.status.in_([DocStatus.DRAFT, DocStatus.APPROVED]),
    ).order_by(BillingInvoice.id.desc()).first())
    if inv:
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

    # Use DB truth from lines
    row = (db.query(
        func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
        func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
    ).filter(BillingInvoiceLine.invoice_id == int(inv.id)).first())

    inv.sub_total = _d(row[0] if row else 0)
    inv.discount_total = _d(row[1] if row else 0)
    inv.tax_total = _d(row[2] if row else 0)
    inv.round_off = Decimal("0")
    inv.grand_total = _d(row[3] if row else 0)

    # GST split totals (meta_json)
    lines = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).all())
    cgst = Decimal("0")
    sgst = Decimal("0")
    igst = Decimal("0")

    for ln in lines:
        tax = _d(getattr(ln, "tax_amount", None))
        if tax <= 0:
            continue

        # Prefer explicit meta_json split if present
        mj = getattr(ln, "meta_json", None)
        if isinstance(mj, dict) and isinstance(mj.get("gst"), dict):
            g = mj["gst"]
            cgst += _d(g.get("cgst_amount"))
            sgst += _d(g.get("sgst_amount"))
            igst += _d(g.get("igst_amount"))
            continue

        # Fallback split: intra-state assumed (half/half)
        cgst += (tax / Decimal("2"))
        sgst += (tax / Decimal("2"))

    _merge_meta(
        inv, {
            "gst": {
                "cgst_total": _dec_s(cgst),
                "sgst_total": _dec_s(sgst),
                "igst_total": _dec_s(igst),
                "tax_total": _dec_s(_d(inv.tax_total)),
            }
        })

    inv.updated_at = datetime.utcnow()
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
    is_manual: bool = False,  # ✅ to stop your current TypeError from hooks
    manual_reason: Optional[str] = None,
    meta_patch: Optional[Dict[str, Any]] = None,
) -> Optional[BillingInvoiceLine]:
    """
    Uses unique key: (billing_case_id, source_module, source_ref_id, source_line_key)
    If already exists -> returns None
    """
    exists = (db.query(BillingInvoiceLine.id).filter(
        BillingInvoiceLine.billing_case_id == int(billing_case_id),
        BillingInvoiceLine.source_module == str(source_module),
        BillingInvoiceLine.source_ref_id == int(source_ref_id),
        BillingInvoiceLine.source_line_key == str(source_line_key),
    ).first())
    if exists:
        return None

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

    # ✅ GST split stored in meta_json (if exists)
    _merge_meta(
        ln, {
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
                "line_key": str(source_line_key),
            },
        })
    if meta_patch:
        _merge_meta(ln, meta_patch)

    db.add(ln)
    db.flush()
    _recalc_invoice_totals(db, int(invoice_id))
    return ln


def upsert_auto_line(
    db: Session,
    *,
    invoice_id: int,
    billing_case_id: int,
    user,
    service_group: ServiceGroup,
    item_type: Optional[str],
    item_id: Optional[int],
    item_code: Optional[str],
    description: str,
    qty: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal,
    discount_percent: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    source_module: str,
    source_ref_id: int,
    source_line_key: str,
    doctor_id: Optional[int] = None,
    intra_state_gst: bool = True,
    service_date: Optional[datetime] = None,
    meta_patch: Optional[Dict[str, Any]] = None,
) -> BillingInvoiceLine:
    """
    ✅ Upsert behavior for the wizard:
    - If the unique key already exists → UPDATE qty (add) and recalc amounts
    - Else → create new auto line (idempotent)
    """

    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")
    if inv.status not in (DocStatus.DRAFT, DocStatus.APPROVED):
        raise BillingStateError("Cannot add lines to POSTED/VOID invoice")

    # find existing unique key
    existing = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.billing_case_id == int(billing_case_id),
        BillingInvoiceLine.source_module == str(source_module),
        BillingInvoiceLine.source_ref_id == int(source_ref_id),
        BillingInvoiceLine.source_line_key == str(source_line_key),
    ).first())

    if existing:
        # ✅ increase qty (user selected again)
        existing.qty = _d(existing.qty) + _d(qty)

        # keep latest price & gst if user overrides
        existing.unit_price = _d(unit_price)
        existing.gst_rate = _d(gst_rate)

        # discounts
        dp = _d(discount_percent)
        da = _d(discount_amount)

        line_total = _d(existing.qty) * _d(existing.unit_price)
        if da <= 0 and dp > 0:
            da = (line_total * dp) / Decimal("100")
        if da < 0:
            da = Decimal("0")
        if da > line_total:
            da = line_total

        taxable = max(line_total - da, Decimal("0"))
        tax_amount = (taxable * _d(existing.gst_rate)) / Decimal("100") if _d(
            existing.gst_rate) > 0 else Decimal("0")
        net_amount = taxable + tax_amount

        existing.discount_percent = dp
        existing.discount_amount = da
        existing.line_total = line_total
        existing.tax_amount = tax_amount
        existing.net_amount = net_amount
        existing.patient_pay_amount = net_amount
        existing.updated_at = datetime.utcnow()

        if hasattr(existing, "service_date"):
            existing.service_date = service_date

        # GST split meta (if meta_json exists)
        split_rates = _gst_split(_d(existing.gst_rate),
                                 intra_state=intra_state_gst)
        split_amt = _gst_amount_split(tax_amount, split_rates)
        _merge_meta(
            existing, {
                "gst": {
                    "gst_rate": _dec_s(split_rates["gst_rate"]),
                    "cgst_rate": _dec_s(split_rates["cgst_rate"]),
                    "sgst_rate": _dec_s(split_rates["sgst_rate"]),
                    "igst_rate": _dec_s(split_rates["igst_rate"]),
                    "cgst_amount": _dec_s(split_amt["cgst"]),
                    "sgst_amount": _dec_s(split_amt["sgst"]),
                    "igst_amount": _dec_s(split_amt["igst"]),
                    "taxable_amount": _dec_s(taxable),
                }
            })
        if meta_patch:
            _merge_meta(existing, meta_patch)

        db.flush()
        _recalc_invoice_totals(db, int(invoice_id))
        return existing

    # create new line if not exists
    ln = add_auto_line_idempotent(
        db,
        invoice_id=int(invoice_id),
        billing_case_id=int(billing_case_id),
        user=user,
        service_group=service_group,
        item_type=item_type,
        item_id=item_id,
        item_code=item_code,
        description=description,
        qty=qty,
        unit_price=unit_price,
        gst_rate=gst_rate,
        source_module=source_module,
        source_ref_id=source_ref_id,
        source_line_key=source_line_key,
        doctor_id=doctor_id,
        intra_state_gst=intra_state_gst,
        is_manual=False,
        manual_reason=None,
        meta_patch=meta_patch,
    )

    # add service_date if column exists
    if ln and hasattr(ln, "service_date"):
        ln.service_date = service_date

    db.flush()
    _recalc_invoice_totals(db, int(invoice_id))
    return ln


def _default_doctor_id_for_case(db: Session,
                                case: BillingCase) -> Optional[int]:
    try:
        from app.models.opd import Visit, Appointment  # adjust if your path differs
    except Exception:
        Visit = None
        Appointment = None

    if case.encounter_type == EncounterType.OP and Visit:
        v = db.get(Visit, int(case.encounter_id))
        if v:
            doc = (getattr(v, "doctor_id", None)
                   or getattr(v, "doctor_user_id", None)
                   or getattr(v, "consulting_doctor_id", None))
            if doc:
                return int(doc)
            appt_id = getattr(v, "appointment_id", None)
            if appt_id and Appointment:
                a = db.get(Appointment, int(appt_id))
                if a:
                    doc2 = getattr(a, "doctor_id", None) or getattr(
                        a, "doctor_user_id", None)
                    if doc2:
                        return int(doc2)

    if case.encounter_type == EncounterType.IP:
        try:
            from app.models.ipd import IpdAdmission  # adjust if your path differs
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

    key = f"MNL:{secrets.token_hex(8)}"  # <= 64 chars

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
        ln, {
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
        })

    db.add(ln)
    db.flush()
    _recalc_invoice_totals(db, int(inv.id))
    return ln


# ============================================================
# Invoice State transitions
# ============================================================
def approve_invoice(db: Session, *, invoice_id: int, user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status != DocStatus.DRAFT:
        raise BillingStateError("Only DRAFT invoice can be approved")

    line_count = (db.query(func.count(BillingInvoiceLine.id)).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).scalar() or 0)
    if int(line_count) <= 0:
        raise BillingError("Cannot approve invoice with no lines")

    _recalc_invoice_totals(db, int(inv.id))

    inv.status = DocStatus.APPROVED
    inv.approved_at = datetime.utcnow()
    inv.approved_by = getattr(user, "id", None)
    inv.updated_by = getattr(user, "id", None)
    db.flush()
    return inv


def post_invoice(db: Session, *, invoice_id: int, user) -> BillingInvoice:
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    if inv.status != DocStatus.APPROVED:
        raise BillingStateError("Only APPROVED invoice can be posted")

    inv.status = DocStatus.POSTED
    inv.posted_at = datetime.utcnow()
    inv.posted_by = getattr(user, "id", None)
    inv.updated_by = getattr(user, "id", None)
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
    inv.voided_at = datetime.utcnow()
    inv.voided_by = getattr(user, "id", None)
    inv.void_reason = str(reason or "")[:255]
    inv.updated_by = getattr(user, "id", None)
    db.flush()
    return inv


# ============================================================
# Payments / Advances
# ============================================================
def _pick_invoice_for_payment(db: Session,
                              billing_case_id: int) -> BillingInvoice:
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.status.in_([DocStatus.APPROVED, DocStatus.POSTED]),
    ).order_by(BillingInvoice.id.desc()).first())
    if not inv:
        raise BillingError("No APPROVED/POSTED invoice found for this case")
    return inv


def record_payment(
    db: Session,
    *,
    billing_case_id: int,
    amount: Decimal,
    user,
    mode: PayMode = PayMode.CASH,
    invoice_id: Optional[int] = None,
    txn_ref: Optional[str] = None,
    notes: Optional[str] = None,
    payer_type: PayerType = PayerType.PATIENT,
    payer_id: Optional[int] = None,
) -> BillingPayment:
    if invoice_id is None:
        inv = _pick_invoice_for_payment(db, int(billing_case_id))
        invoice_id = int(inv.id)
    else:
        inv = db.get(BillingInvoice, int(invoice_id))
        if not inv:
            raise BillingError("Invoice not found")

    if int(inv.billing_case_id) != int(billing_case_id):
        raise BillingError("Invoice does not belong to billing case")

    if inv.status not in (DocStatus.APPROVED, DocStatus.POSTED):
        raise BillingStateError(
            "Payments allowed only for APPROVED/POSTED invoices")

    p = BillingPayment(
        billing_case_id=int(billing_case_id),
        invoice_id=int(invoice_id),
        payer_type=payer_type,
        payer_id=payer_id,
        mode=mode,
        amount=_d(amount),
        txn_ref=txn_ref,
        received_by=getattr(user, "id", None),
        notes=notes,
    )
    db.add(p)
    db.flush()
    return p


def record_advance(
    db: Session,
    *,
    billing_case_id: int,
    amount: Decimal,
    user,
    entry_type: AdvanceType = AdvanceType.ADVANCE,
    mode: PayMode = PayMode.CASH,
    txn_ref: Optional[str] = None,
    remarks: Optional[str] = None,
) -> BillingAdvance:
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    adv = BillingAdvance(
        billing_case_id=int(billing_case_id),
        entry_type=entry_type,
        mode=mode,
        amount=_d(amount),
        txn_ref=txn_ref,
        entry_by=getattr(user, "id", None),
        remarks=remarks,
    )
    db.add(adv)
    db.flush()
    return adv


def add_payment_for_invoice(
    db: Session,
    *,
    billing_case_id: int,
    invoice_id: int,
    amount: Decimal,
    user,
    txn_ref: Optional[str] = None,
    mode: PayMode = PayMode.CASH,
    notes: Optional[str] = None,
) -> BillingPayment:
    p = BillingPayment(
        billing_case_id=int(billing_case_id),
        invoice_id=int(invoice_id),
        payer_type=PayerType.PATIENT,
        payer_id=None,
        mode=mode,
        amount=_d(amount),
        txn_ref=txn_ref,
        received_by=getattr(user, "id", None),
        notes=notes,
    )
    _set_if_has(p, "received_at", datetime.utcnow())
    _set_if_has(p, "paid_at", datetime.utcnow())
    db.add(p)
    db.flush()
    return p


# ============================================================
# PRINT SUMMARY + SPLIT-UP REPORT BUILDERS (for your exact bill formats)
# ============================================================
def build_invoice_print_payload(db: Session, *,
                                invoice_id: int) -> Dict[str, Any]:
    """
    Returns a print-ready structure:
      - invoice header
      - case + encounter
      - line items (grouped)
      - totals + GST split
      - payments (optional)
    """
    inv = db.get(BillingInvoice, int(invoice_id))
    if not inv:
        raise BillingError("Invoice not found")

    case = db.get(BillingCase, int(inv.billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    # Pull lines
    lines = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
            BillingInvoiceLine.id.asc()).all())

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
        })

    # Payments for this invoice
    payments = (db.query(BillingPayment).filter(
        BillingPayment.invoice_id == int(inv.id)).order_by(
            BillingPayment.id.asc()).all())
    pay_rows = [{
        "id": int(p.id),
        "mode": str(getattr(p, "mode", "")),
        "amount": float(_d(getattr(p, "amount", 0))),
        "txn_ref": getattr(p, "txn_ref", None),
        "received_by": getattr(p, "received_by", None),
        "notes": getattr(p, "notes", None),
    } for p in payments]

    # Ensure totals up to date
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
    """
    Your “single dashboard” split-up report:
      - All invoices under a case (module-wise)
      - Module totals
      - Case totals + payments + advances
    """
    case = db.get(BillingCase, int(billing_case_id))
    if not case:
        raise BillingError("Billing case not found")

    invoices = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case.id)).order_by(
            BillingInvoice.id.asc()).all())

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
                "grand_total": Decimal("0"),
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


def _load_lis_order_and_items(db: Session, lis_order_id: int):
    """
    Best-effort loader (works even if your LIS model names differ).
    Returns: (order_row, item_rows[list])
    """
    candidates = [
        ("app.models.lis", "LisOrder"),
        ("app.models.lis", "LabOrder"),
        ("app.models.lis_orders", "LisOrder"),
        ("app.models.lis_order", "LisOrder"),
        ("app.models.lab", "LabOrder"),
    ]

    order = None
    OrderModel = None
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

    # Try item models
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
            # common FK names
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


def add_lines_from_lis_order(
    db: Session,
    *,
    invoice_id: int,
    lis_order_id: int,
    user,
) -> Dict[str, Any]:
    """
    Adds LIS lines into invoice using idempotent source keys.
    SAFE even if LIS module not present (returns BillingError with readable msg).
    """
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

    # If items exist -> add item-wise
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
                        "item_id": int(_safe_get(it, ["id"], 0) or 0),
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

    # Fallback: single consolidated line
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


def add_line_from_ris_order(
    db: Session,
    *,
    invoice_id: int,
    ris_order_id: int,
    user,
) -> Dict[str, Any]:
    """
    Adds RIS lines into invoice (idempotent).
    """
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

    # pick service group
    sg_scan = _enum_pick(ServiceGroup, "SCAN", fallback=list(ServiceGroup)[0])
    sg_xray = _enum_pick(ServiceGroup, "XRAY", fallback=sg_scan)

    modality = str(
        _safe_get(order, ["modality", "study_type", "service_type"], "")
        or "").upper()
    sg = sg_xray if "XRAY" in modality or "X-RAY" in modality else sg_scan

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

    # fallback consolidated
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

    added = [int(ln.id)] if ln else []
    skipped = 0 if ln else 1
    _recalc_invoice_totals(db, int(inv.id))
    return {
        "invoice_id": int(inv.id),
        "ris_order_id": int(ris_order_id),
        "added_line_ids": added,
        "skipped": skipped
    }
