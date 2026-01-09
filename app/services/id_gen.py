# FILE: app/services/id_gen.py
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ui_branding import UiBranding

# Billing number series models
from app.models.billing import BillingNumberSeries, NumberDocType, NumberResetPeriod


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
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(tz).date()

    return d


def _dt_ddmmyyyy(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%d%m%Y")


def _dt_yyyymm(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%Y%m")


def _dt_yyyy(d: Optional[Union[date, datetime]]) -> str:
    return _normalize_to_date(d).strftime("%Y")


# ============================================================
# Org code helpers (UiBranding)
# ============================================================
def _clean_code(s: str, max_len: int = 3) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", (s or "").strip().upper())
    return (cleaned[:max_len] or "").strip()


def _acronym_from_org_name(org_name: str, max_len: int = 3) -> str:
    name = (org_name or "").strip()
    if not name:
        return "NH"

    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    if not words:
        return "NH"

    if len(words) >= 2:
        code = "".join(w[0] for w in words[:max_len])
    else:
        code = words[0][:max_len]

    return (code or "NH").strip()


def _org_code_from_settings(max_len: int = 3) -> str:
    candidates = [
        getattr(settings, "PROVIDER_TENANT_CODE", None),
        getattr(settings, "VITE_PROVIDER_TENANT_CODE", None),
        getattr(settings, "TENANT_CODE", None),
        getattr(settings, "APP_CODE", None),
        getattr(settings, "APP_NAME", None),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            code = _clean_code(c, max_len=max_len)
            if code:
                return code
    return "NH"


def _org_code_from_branding(db: Session, max_len: int = 3) -> str:
    b = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not b:
        return _org_code_from_settings(max_len=max_len)

    org_name = getattr(b, "org_name", None) or ""
    code = _acronym_from_org_name(org_name, max_len=max_len)
    return _clean_code(
        code, max_len=max_len) or _org_code_from_settings(max_len=max_len)


# ============================================================
# Common helpers
# ============================================================
def _enc_code(encounter_type: Optional[str]) -> str:
    if not encounter_type:
        return ""
    s = str(encounter_type).strip().upper()
    if s in ("OP", "IP", "OT", "ER"):
        return s
    if s.startswith("OP"):
        return "OP"
    if s.startswith("IP"):
        return "IP"
    if s.startswith("OT"):
        return "OT"
    if s.startswith("ER"):
        return "ER"
    return ""


def _period_key(
    on_date: Optional[Union[date, datetime]],
    reset_period: NumberResetPeriod,
) -> Optional[str]:
    if reset_period == NumberResetPeriod.NONE:
        return None
    if reset_period == NumberResetPeriod.YEAR:
        return _dt_yyyy(on_date)
    if reset_period == NumberResetPeriod.MONTH:
        return _dt_yyyymm(on_date)
    return None


def _series_prefix_and_output_prefix(
    db: Session,
    *,
    tag: str,  # "BC" / "INV" / "CN" / "DN" / "RC"
    encounter_type: Optional[str],
    on_date: Optional[Union[date, datetime]],
    reset_period: NumberResetPeriod,
) -> tuple[str, str]:
    """
    We want output numbers always like:
      ORG + ENC + TAG + DDMMYYYY + 000001

    But we DON'T want new series rows every day when reset_period is YEAR/MONTH.
    So:
      - If reset_period == NONE -> series_prefix includes date (daily unique series)
      - Else -> series_prefix excludes date (one series row per month/year), but output still includes date
    """
    code = _org_code_from_branding(db, max_len=3)
    enc = _enc_code(encounter_type)
    dd = _dt_ddmmyyyy(on_date)

    base = f"{code}{enc}{tag}"
    if reset_period == NumberResetPeriod.NONE:
        series_prefix = f"{base}{dd}"
    else:
        series_prefix = base

    output_prefix = f"{base}{dd}"
    return series_prefix, output_prefix


def _ensure_series_row(
    db: Session,
    *,
    tenant_id: int,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod,
    padding: int,
    last_period_key: Optional[str],
) -> None:
    """
    Concurrency-safe upsert so we can reliably lock with SELECT ... FOR UPDATE next.
    Requires UNIQUE KEY on (tenant_id, doc_type, reset_period, prefix).
    """
    db.execute(
        text("""
            INSERT INTO billing_number_series
              (tenant_id, doc_type, prefix, reset_period, padding, next_number, last_period_key, is_active, created_at, updated_at)
            VALUES
              (:tenant_id, :doc_type, :prefix, :reset_period, :padding, 1, :last_period_key, 1, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
              id = id
            """),
        {
            "tenant_id": int(tenant_id),
            "doc_type": str(doc_type.value),
            "prefix": str(prefix),
            "reset_period": str(reset_period.value),
            "padding": int(padding),
            "last_period_key": last_period_key,
        },
    )


def _next_series_number(
    db: Session,
    *,
    tenant_id: Optional[int],
    doc_type: NumberDocType,
    series_prefix: str,
    output_prefix: str,
    reset_period: NumberResetPeriod,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
) -> str:
    """
    Atomic sequential number generator using BillingNumberSeries.

    IMPORTANT:
      ✅ Must be called inside a DB transaction.
      Example:
        with db.begin():
            inv_no = next_invoice_number(...)
            ...
    """
    # IMPORTANT FIX FOR MYSQL UNIQUE + NULL:
    # MySQL UNIQUE allows multiple NULLs, so we NEVER store NULL tenant_id in number series.
    tenant_key = int(tenant_id or 0)

    series_prefix = (series_prefix or "").strip().upper() or "NH"
    output_prefix = (output_prefix or "").strip().upper() or "NH"

    pkey = _period_key(on_date, reset_period)

    # Ensure the row exists (safe under concurrency)
    _ensure_series_row(
        db,
        tenant_id=tenant_key,
        doc_type=doc_type,
        prefix=series_prefix,
        reset_period=reset_period,
        padding=int(padding or 6),
        last_period_key=pkey,
    )

    row = (
        db.query(BillingNumberSeries).filter(
            BillingNumberSeries.tenant_id == tenant_key,
            BillingNumberSeries.doc_type == doc_type,
            BillingNumberSeries.prefix == series_prefix,
            BillingNumberSeries.reset_period == reset_period,
            BillingNumberSeries.is_active == True,  # noqa: E712
        ).with_for_update().first())

    if not row:
        # Should never happen due to _ensure_series_row, but keep safe fallback
        raise RuntimeError("BillingNumberSeries row not found after upsert")

    # Reset if period changed (MONTH/YEAR)
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
# OP / IP / Rx IDs (your existing style)
# ============================================================
def make_op_episode_id(
    db: Session,
    visit_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 4,
) -> str:
    code = _org_code_from_branding(db, max_len=3)
    return f"{code}OP{_dt_ddmmyyyy(on_date)}{visit_id:0{id_width}d}"


def make_ip_admission_code(
    db: Session,
    admission_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 6,
) -> str:
    code = _org_code_from_branding(db, max_len=3)
    return f"{code}IP{_dt_ddmmyyyy(on_date)}{admission_id:0{id_width}d}"


def make_rx_number(
    db: Session,
    rx_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 5,
) -> str:
    code = _org_code_from_branding(db, max_len=3)
    return f"{code}RX{_dt_ddmmyyyy(on_date)}{rx_id:0{id_width}d}"


# ============================================================
# BILLING IDs (Series-based audit-safe sequential)
# ============================================================
def next_billing_case_number(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> str:
    series_prefix, output_prefix = _series_prefix_and_output_prefix(
        db,
        tag="BC",
        encounter_type=encounter_type,
        on_date=on_date,
        reset_period=reset_period,
    )
    return _next_series_number(
        db,
        tenant_id=tenant_id,
        doc_type=NumberDocType.CASE,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )


def next_invoice_number(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> str:
    series_prefix, output_prefix = _series_prefix_and_output_prefix(
        db,
        tag="INV",
        encounter_type=encounter_type,
        on_date=on_date,
        reset_period=reset_period,
    )
    return _next_series_number(
        db,
        tenant_id=tenant_id,
        doc_type=NumberDocType.INVOICE,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )


def next_credit_note_number(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> str:
    series_prefix, output_prefix = _series_prefix_and_output_prefix(
        db,
        tag="CN",
        encounter_type=encounter_type,
        on_date=on_date,
        reset_period=reset_period,
    )
    return _next_series_number(
        db,
        tenant_id=tenant_id,
        doc_type=NumberDocType.NOTE,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )


def next_debit_note_number(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> str:
    series_prefix, output_prefix = _series_prefix_and_output_prefix(
        db,
        tag="DN",
        encounter_type=encounter_type,
        on_date=on_date,
        reset_period=reset_period,
    )
    return _next_series_number(
        db,
        tenant_id=tenant_id,
        doc_type=NumberDocType.NOTE,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )


def next_receipt_number(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    encounter_type: Optional[str] = None,
    on_date: Optional[Union[date, datetime]] = None,
    padding: int = 6,
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE,
) -> str:
    series_prefix, output_prefix = _series_prefix_and_output_prefix(
        db,
        tag="RC",
        encounter_type=encounter_type,
        on_date=on_date,
        reset_period=reset_period,
    )
    return _next_series_number(
        db,
        tenant_id=tenant_id,
        doc_type=NumberDocType.RECEIPT,
        series_prefix=series_prefix,
        output_prefix=output_prefix,
        reset_period=reset_period,
        on_date=on_date,
        padding=padding,
    )
