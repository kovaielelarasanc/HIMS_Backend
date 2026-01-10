# FILE: app/utils/timezone.py
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """
    Returns a *naive* datetime representing IST time.
    This avoids SQLAlchemy/MySQL issues when your DateTime columns are naive.
    """
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()
