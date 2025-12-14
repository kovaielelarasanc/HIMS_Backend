# FILE: app/services/inventory_number_series.py
from __future__ import annotations

from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.pharmacy_inventory import InvNumberSeries


def _date_key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def next_document_number(
    db: Session,
    key: str,          # e.g. "GRN", "PO"
    prefix: str,       # e.g. "GRN", "PO"
    doc_date: date,
    pad: int = 3,      # 001, 002...
) -> str:
    """
    Concurrency-safe number generator using InvNumberSeries with UNIQUE(key, date_key).
    Works correctly in multi-user hospitals.

    Example: GRN20251214001
    """
    dk = _date_key(doc_date)

    # Try to fetch row FOR UPDATE (lock)
    row = (
        db.query(InvNumberSeries)
        .filter(InvNumberSeries.key == key, InvNumberSeries.date_key == dk)
        .with_for_update()
        .first()
    )

    if not row:
        # Create row. If two users create at same time, one will hit IntegrityError.
        row = InvNumberSeries(key=key, date_key=dk, next_seq=1)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # re-fetch with lock
            row = (
                db.query(InvNumberSeries)
                .filter(InvNumberSeries.key == key, InvNumberSeries.date_key == dk)
                .with_for_update()
                .first()
            )
            if not row:
                raise

    seq = int(row.next_seq or 1)
    row.next_seq = seq + 1
    db.flush()

    return f"{prefix}{doc_date.strftime('%Y%m%d')}{seq:0{pad}d}"
