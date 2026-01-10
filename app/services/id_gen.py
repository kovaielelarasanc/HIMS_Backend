# FILE: app/services/id_gen.py
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ui_branding import UiBranding
from app.models.billing import BillingNumberSeries, NumberDocType, NumberResetPeriod

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# Time helpers (Hospital TZ)
# ============================================================
def _hospital_tz() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))


def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone(_hospital_tz()).date()


def _normalize_to_date(d: Optional[Union[date, datetime]]) -> date:
    if d is None:
        return _today_local()

    if isinstance(d, datetime):
        tz = _hospital_tz()
        # if naive, treat as UTC (keeps old behavior)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(tz).date()

    return d


def _dt_ddmmyyyy(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%d%m%Y")


def _dt_yyyy(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%Y")


def _dt_yyyy_mm(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%Y-%m")


# ============================================================
# Org code = UiBranding.org_name first 3 letters (as requested)
# ============================================================
def _clean_org3_from_name(org_name: str) -> str:
    """
    Requirement: UI_Branding ORG_NAME first three letters.
    We take first 3 alphanumeric characters (ignoring spaces/symbols), uppercase.
    Example:
      "Sushrutha Medical Centre" -> "SUS"
      "NUTRYAH Digital Health"   -> "NUT"
    """
    s = re.sub(r"[^A-Za-z0-9]", "", (org_name or "").strip().upper())
    return (s[:3] or "").strip()


def _org3_from_settings() -> str:
    # fallback only if org_name not available
    candidates = [
        getattr(settings, "PROVIDER_TENANT_CODE", None),
        getattr(settings, "VITE_PROVIDER_TENANT_CODE", None),
        getattr(settings, "TENANT_CODE", None),
        getattr(settings, "APP_CODE", None),
        getattr(settings, "APP_NAME", None),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            v = re.sub(r"[^A-Za-z0-9]", "", c.strip().upper())
            if v:
                return (v[:3] or "NH").strip()
    return "NH"


def _org3_from_branding(db: Session) -> str:
    """
    Uses latest UiBranding row (tenant DB usually has one branding row).
    If UiBranding has tenant_id and db.info["tenant_id"] exists, filters.
    """
    q = db.query(UiBranding)

    try:
        tid = db.info.get("tenant_id", None)  # type: ignore[attr-defined]
        if tid is not None and hasattr(UiBranding, "tenant_id"):
            q = q.filter(UiBranding.tenant_id == int(tid))
    except Exception:
        pass

    b = q.order_by(UiBranding.id.desc()).first()
    if not b:
        return _org3_from_settings()

    org_name = getattr(b, "org_name", None) or ""
    code = _clean_org3_from_name(org_name)
    return code or _org3_from_settings()


# ============================================================
# Series core (matches your BillingNumberSeries model)
# UNIQUE(doc_type, reset_period, prefix)
# ============================================================
def _period_key(reset_period: NumberResetPeriod,
                on_date: Optional[Union[date, datetime]]) -> Optional[str]:
    if reset_period == NumberResetPeriod.NONE:
        return None
    if reset_period == NumberResetPeriod.YEAR:
        return _dt_yyyy(on_date)
    if reset_period == NumberResetPeriod.MONTH:
        return _dt_yyyy_mm(on_date)
    return None


def _get_or_create_series_row(
    db: Session,
    *,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod,
    padding: int,
    period_key: Optional[str],
) -> BillingNumberSeries:
    """
    Concurrency-safe:
      - try SELECT ... FOR UPDATE
      - if missing, INSERT in nested transaction (safe)
      - if IntegrityError due to race, re-fetch FOR UPDATE
    """
    prefix = (prefix or "").strip().upper()

    row = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == doc_type,
        BillingNumberSeries.reset_period == reset_period,
        BillingNumberSeries.prefix == prefix,
    ).with_for_update().first())
    if row:
        return row

    try:
        with db.begin_nested():
            new_row = BillingNumberSeries(
                doc_type=doc_type,
                prefix=prefix,
                reset_period=reset_period,
                padding=int(padding or 6),
                next_number=1,
                last_period_key=period_key,
                is_active=True,
            )
            db.add(new_row)
            db.flush()

        row2 = (db.query(BillingNumberSeries).filter(
            BillingNumberSeries.doc_type == doc_type,
            BillingNumberSeries.reset_period == reset_period,
            BillingNumberSeries.prefix == prefix,
        ).with_for_update().first())
        if not row2:
            raise RuntimeError("BillingNumberSeries row create failed")
        return row2

    except IntegrityError:
        row3 = (db.query(BillingNumberSeries).filter(
            BillingNumberSeries.doc_type == doc_type,
            BillingNumberSeries.reset_period == reset_period,
            BillingNumberSeries.prefix == prefix,
        ).with_for_update().first())
        if not row3:
            raise RuntimeError(
                "BillingNumberSeries row not found after collision")
        return row3


def _next_series_number(
    db: Session,
    *,
    doc_type: NumberDocType,
    series_prefix: str,
    output_prefix: str,
    reset_period: NumberResetPeriod,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Returns: output_prefix + zero_padded(next_number)
    Applies YEAR/MONTH reset using last_period_key.
    """
    series_prefix = (series_prefix or "").strip().upper()
    output_prefix = (output_prefix or "").strip().upper()

    pkey = _period_key(reset_period, on_date)

    row = _get_or_create_series_row(
        db,
        doc_type=doc_type,
        prefix=series_prefix,
        reset_period=reset_period,
        padding=int(padding or 6),
        period_key=pkey,
    )

    if not row.is_active:
        raise RuntimeError("Number series is inactive")

    # period reset only when using YEAR/MONTH
    if reset_period != NumberResetPeriod.NONE:
        if (row.last_period_key or "") != (pkey or ""):
            row.next_number = 1
            row.last_period_key = pkey

    n = int(row.next_number or 1)
    row.next_number = n + 1
    row.padding = int(padding or row.padding or 6)
    db.flush()

    return f"{output_prefix}{n:0{row.padding}d}"


# ============================================================
# OP / IP / Rx IDs (kept same)
# ============================================================
def make_op_episode_id(
    db: Session,
    visit_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 4,
) -> str:
    org = _org3_from_branding(db)
    return f"{org}OP{_dt_ddmmyyyy(on_date)}{visit_id:0{id_width}d}"


def make_ip_admission_code(
    db: Session,
    admission_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 6,
) -> str:
    org = _org3_from_branding(db)
    return f"{org}IP{_dt_ddmmyyyy(on_date)}{admission_id:0{id_width}d}"


def make_rx_number(
    db: Session,
    rx_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 5,
) -> str:
    org = _org3_from_branding(db)
    return f"{org}RX{_dt_ddmmyyyy(on_date)}{rx_id:0{id_width}d}"


# ============================================================
# Generic series number (PUBLIC API)
# ============================================================
def next_number_from_series(
    db: Session,
    *,
    doc_type: NumberDocType,
    prefix: str,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
    on_date: Optional[Union[date, datetime]] = None,
) -> str:
    """
    Safe sequential generator using BillingNumberSeries (tenant DB).
    """
    series_prefix = (prefix or "").strip().upper()
    output_prefix = series_prefix
    return _next_series_number(
        db,
        doc_type=doc_type,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )


# ============================================================
# Billing identifiers (YOUR REQUIRED FORMAT)
# Format: ORG3 + DDMMYYYY + ######
# Example: SUS10012026000001
# ============================================================
def _org_date_prefix(db: Session, on_date: Optional[Union[date,
                                                          datetime]]) -> str:
    org = _org3_from_branding(db)
    dstr = _dt_ddmmyyyy(on_date)
    return f"{org}{dstr}"


def next_billing_case_number(
    db: Session,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Output: ORG3 + DDMMYYYY + ######
    Uses doc_type=CASE (separate sequence from invoices/receipts).
    Default: DAILY sequence (series_prefix includes date).
    """
    base = _org_date_prefix(db, on_date)
    return _next_series_number(
        db,
        doc_type=NumberDocType.CASE,
        series_prefix=base,  # daily series
        output_prefix=base,  # printed format same
        reset_period=NumberResetPeriod.NONE,
        on_date=on_date,
        padding=padding,
    )


def next_invoice_number(
    db: Session,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Output: ORG3 + DDMMYYYY + ######
    Example: SUS10012026000001
    Default: DAILY sequence (series_prefix includes date).
    """
    base = _org_date_prefix(db, on_date)
    return _next_series_number(
        db,
        doc_type=NumberDocType.INVOICE,
        series_prefix=base,  # daily series
        output_prefix=base,  # printed format same
        reset_period=NumberResetPeriod.NONE,
        on_date=on_date,
        padding=padding,
    )


def next_note_number(
    db: Session,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Credit/Debit notes share NOTE doc_type => one shared running sequence
    (because output format does not include CN/DN tag).
    """
    base = _org_date_prefix(db, on_date)
    return _next_series_number(
        db,
        doc_type=NumberDocType.NOTE,
        series_prefix=base,
        output_prefix=base,
        reset_period=NumberResetPeriod.NONE,
        on_date=on_date,
        padding=padding,
    )


def next_receipt_number(
    db: Session,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Output: ORG3 + DDMMYYYY + ######
    """
    base = _org_date_prefix(db, on_date)
    return _next_series_number(
        db,
        doc_type=NumberDocType.RECEIPT,
        series_prefix=base,
        output_prefix=base,
        reset_period=NumberResetPeriod.NONE,
        on_date=on_date,
        padding=padding,
    )


# Backward-compatible aliases (if your code calls these)
def next_credit_note_number(db: Session,
                            *,
                            on_date: Optional[Union[date, datetime]] = None,
                            padding: int = 6) -> str:
    return next_note_number(db, on_date=on_date, padding=padding)


def next_debit_note_number(db: Session,
                           *,
                           on_date: Optional[Union[date, datetime]] = None,
                           padding: int = 6) -> str:
    return next_note_number(db, on_date=on_date, padding=padding)
