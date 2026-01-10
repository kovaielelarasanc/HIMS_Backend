# FILE: app/services/billing_invoice_create.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.billing import (
    BillingInvoice,
    BillingCase,
    DocStatus,
    InvoiceType,
    PayerType,
    NumberResetPeriod,
    BillingNumberSeries,
    NumberResetPeriod,
)
from app.services.id_gen import next_invoice_number

class BillingError(Exception):

    def __init__(self,
                 msg: str,
                 status_code: int = 400,
                 extra: Optional[dict] = None):
        super().__init__(msg)
        self.status_code = status_code
        self.extra = extra


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def _normalize_module(module: Optional[str]) -> str:
    m = (module or "").strip().upper()
    return m or "MISC"


def _period_key(reset_period: NumberResetPeriod, now: datetime) -> str:
    rp = _enum_value(reset_period)
    if rp in ("NONE", None, ""):
        return ""
    if rp == "YEAR":
        return now.strftime("%Y")
    if rp == "MONTH":
        return now.strftime("%Y%m")
    if rp == "DAY":
        return now.strftime("%Y%m%d")
    return ""


def _series_doc_type(base: str, reset_period: NumberResetPeriod,
                     now: datetime) -> str:
    """
    Example:
      INVOICE_MISC_2026
      INVOICE_LAB_202601
    """
    p = _period_key(reset_period, now)
    return f"{base}_{p}" if p else base


def _default_prefix(doc_type: str) -> str:
    # INVOICE_MISC_202601 -> INV/MISC/202601/
    parts = doc_type.split("_")
    if len(parts) >= 3 and parts[0] == "INVOICE":
        mod = parts[1]
        per = parts[2]
        return f"INV/{mod}/{per}/"
    if len(parts) >= 2 and parts[0] == "INVOICE":
        mod = parts[1]
        return f"INV/{mod}/"
    return "INV/"


def _tenant_id_from_case_or_db(db: Session,
                               case: BillingCase) -> Optional[int]:
    # Prefer BillingCase.tenant_id if present (multi-tenant)
    tid = getattr(case, "tenant_id", None)
    if tid is not None:
        try:
            return int(tid)
        except Exception:
            pass

    # fallback: db.info["tenant_id"] (if you store it there)
    try:
        t2 = db.info.get("tenant_id", None)  # type: ignore[attr-defined]
        return int(t2) if t2 is not None else None
    except Exception:
        return None


def _next_number(
    db: Session,
    *,
    tenant_id: Optional[int],
    doc_type: str,
    reset_period: NumberResetPeriod,
    padding: int = 5,
) -> str:
    """
    ✅ Locks the series row for concurrency safety.
    ✅ Supports tenant_id if the column exists.
    ✅ Filters by doc_type + prefix + reset_period (safe even if multiple rows exist).
    """
    now = datetime.now()
    prefix = _default_prefix(doc_type)
    pad = int(padding or 5)

    q = db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == doc_type)

    # If your BillingNumberSeries has these columns, include them for correctness.
    if hasattr(BillingNumberSeries, "prefix"):
        q = q.filter(BillingNumberSeries.prefix == prefix)
    if hasattr(BillingNumberSeries, "reset_period"):
        q = q.filter(BillingNumberSeries.reset_period == reset_period)
    if tenant_id is not None and hasattr(BillingNumberSeries, "tenant_id"):
        q = q.filter(BillingNumberSeries.tenant_id == int(tenant_id))

    series = q.with_for_update().first()

    if not series:
        series = BillingNumberSeries(
            doc_type=doc_type,
            prefix=prefix,
            reset_period=reset_period,
            padding=pad,
            next_number=1,
            is_active=True
            if hasattr(BillingNumberSeries, "is_active") else None,
            last_period_key=_period_key(reset_period, now) if hasattr(
                BillingNumberSeries, "last_period_key") else None,
        )
        if tenant_id is not None and hasattr(series, "tenant_id"):
            setattr(series, "tenant_id", int(tenant_id))

        db.add(series)
        db.flush()

    # Optional: if you store last_period_key and reset_period changes,
    # keep it consistent (not required because doc_type already includes period in your design).
    if hasattr(series, "padding") and (getattr(series, "padding", None)
                                       or 0) != pad:
        setattr(series, "padding", pad)

    n = int(getattr(series, "next_number", 1) or 1)
    setattr(series, "next_number", n + 1)

    num = str(n).zfill(int(getattr(series, "padding", pad) or pad))
    return f"{(getattr(series, 'prefix', prefix) or prefix)}{num}"


def _find_existing_draft_invoice(
    db: Session,
    *,
    case_id: int,
    module: str,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: Optional[int],
) -> Optional[BillingInvoice]:
    return (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case_id)).filter(
            BillingInvoice.status == DocStatus.DRAFT).filter(
                (BillingInvoice.module == module)
                | (BillingInvoice.module == None)).filter(
                    BillingInvoice.invoice_type == invoice_type).filter(
                        BillingInvoice.payer_type == payer_type).filter(
                            BillingInvoice.payer_id == payer_id).order_by(
                                BillingInvoice.id.desc()).first())


def create_new_invoice_for_case(
    db: Session,
    *,
    case: BillingCase,
    user,
    module: str,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: Optional[int],
    reset_period: NumberResetPeriod = NumberResetPeriod.
    NONE,  # kept for signature compat (not used in numbering)
    allow_duplicate_draft: bool = False,
) -> BillingInvoice:
    """
    ✅ FINAL:
      - invoice_number generated BEFORE insert (never NULL)
      - uses BillingNumberSeries correctly (NO tenant_id, doc_type Enum)
      - number format: ORG3(from UiBranding.org_name) + DDMMYYYY + ######
      - safe concurrent generation (FOR UPDATE in id_gen)
    """
    mod = _normalize_module(module)

    if not allow_duplicate_draft:
        ex = _find_existing_draft_invoice(
            db,
            case_id=int(case.id),
            module=mod,
            invoice_type=invoice_type,
            payer_type=payer_type,
            payer_id=payer_id,
        )
        if ex:
            if not (getattr(ex, "module", None) or "").strip():
                ex.module = mod
                db.add(ex)
            return ex

    # ✅ Generate invoice_number FIRST
    now = datetime.now()
    invoice_number = next_invoice_number(db, on_date=now, padding=6)

    inv = BillingInvoice(
        billing_case_id=int(case.id),
        invoice_number=invoice_number,
        module=mod,
        invoice_type=invoice_type,
        payer_type=payer_type,
        payer_id=payer_id,
        status=DocStatus.DRAFT,
        sub_total=0,
        discount_total=0,
        tax_total=0,
        round_off=0,
        grand_total=0,
        service_date=now,  # optional; remove if you don’t want auto set
    )

    # audit fields if present
    if hasattr(inv, "created_by"):
        setattr(inv, "created_by", getattr(user, "id", None))

    db.add(inv)

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise BillingError("Invoice create failed (constraint error)",
                           status_code=409)

    return inv
