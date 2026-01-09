from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from app.models.billing import BillingNumberSeries, NumberDocType, NumberResetPeriod


def _period_key(dt: datetime, reset: NumberResetPeriod) -> str | None:
    if reset == NumberResetPeriod.NONE:
        return None
    if reset == NumberResetPeriod.YEAR:
        return dt.strftime("%Y")
    return dt.strftime("%Y-%m")  # MONTH


def next_billing_number(
    db: Session,
    *,
    tenant_id: int | None,
    doc_type: NumberDocType,
    reset_period: NumberResetPeriod,
    prefix: str,
    padding: int = 6,
) -> str:
    now = datetime.utcnow()
    pk = _period_key(now, reset_period)

    row = (db.query(BillingNumberSeries).filter(
        BillingNumberSeries.tenant_id == tenant_id,
        BillingNumberSeries.doc_type == doc_type,
        BillingNumberSeries.reset_period == reset_period,
        BillingNumberSeries.prefix == prefix,
        BillingNumberSeries.is_active.is_(True),
    ).with_for_update().first())

    if not row:
        row = BillingNumberSeries(
            tenant_id=tenant_id,
            doc_type=doc_type,
            prefix=prefix,
            reset_period=reset_period,
            padding=padding,
            next_number=1,
            last_period_key=pk,
            is_active=True,
        )
        db.add(row)
        db.flush()

    # reset logic
    if reset_period != NumberResetPeriod.NONE and row.last_period_key != pk:
        row.last_period_key = pk
        row.next_number = 1

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    return f"{prefix}{str(n).zfill(int(row.padding or padding))}"
