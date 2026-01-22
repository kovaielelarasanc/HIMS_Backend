# FILE: app/services/billing_numbers.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from app.models.billing import BillingNumberSeries, NumberDocType, NumberResetPeriod


def _period_key(now: datetime, reset: NumberResetPeriod) -> str | None:
    if reset == NumberResetPeriod.NONE:
        return None
    if reset == NumberResetPeriod.YEAR:
        return now.strftime("%Y")
    if reset == NumberResetPeriod.MONTH:
        return now.strftime("%Y-%m")
    return None


def next_number(
    db: Session,
    *,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    key = _period_key(now, reset_period)

    row = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.doc_type == doc_type).filter(
            BillingNumberSeries.prefix == (prefix or "")).filter(
                BillingNumberSeries.reset_period ==
                reset_period).with_for_update().first())

    if not row:
        row = BillingNumberSeries(
            doc_type=doc_type,
            prefix=prefix or "",
            reset_period=reset_period,
            padding=padding,
            next_number=1,
            last_period_key=key,
            is_active=True,
        )
        db.add(row)
        db.flush()

    # reset if period changed
    if reset_period != NumberResetPeriod.NONE and row.last_period_key != key:
        row.last_period_key = key
        row.next_number = 1

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    return f"{row.prefix}{str(n).zfill(int(row.padding or padding))}"
