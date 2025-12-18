#app/service/id_gen.py
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ui_branding import UiBranding


# ----------------------------
# Time helpers (Hospital TZ)
# ----------------------------
def _hospital_tz() -> ZoneInfo:
    # put TIMEZONE="Asia/Kolkata" in your settings/env
    return ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))


def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone(_hospital_tz()).date()


def _normalize_to_date(d: Optional[Union[date, datetime]]) -> date:
    """
    - If None -> hospital local date (Asia/Kolkata)
    - If datetime:
        * naive treated as UTC (because you use utcnow() in most places)
        * then converted to hospital local date
    - If date -> returned as-is
    """
    if d is None:
        return _today_local()

    if isinstance(d, datetime):
        tz = _hospital_tz()
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(tz).date()

    # plain date
    return d


def _dt_ddmmyyyy(d: Optional[Union[date, datetime]]) -> str:
    dd = _normalize_to_date(d)
    return dd.strftime("%d%m%Y")


# ----------------------------
# Org code helpers
# ----------------------------
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
    return code or "NH"


def _org_code_from_branding(db: Session, max_len: int = 3) -> str:
    b = db.query(UiBranding).order_by(UiBranding.id.desc()).first()
    if not b:
        return "NH"

    direct = (getattr(b, "org_code", None)
              or getattr(b, "org_short_code", None)
              or getattr(b, "short_code", None))
    if isinstance(direct, str) and direct.strip():
        cleaned = re.sub(r"[^A-Za-z0-9]", "", direct.strip().upper())
        return cleaned[:max_len] or "NH"

    return _acronym_from_org_name(getattr(b, "org_name", "") or "",
                                  max_len=max_len)


# ----------------------------
# ID generators
# ----------------------------
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



# ----------------------------
# Pharmacy / Rx ID generator
# ----------------------------
# FILE: app/services/id_gen.py
def make_rx_number(
    db: Session,
    rx_id: int,
    *,
    on_date: Optional[Union[date, datetime]] = None,
    id_width: int = 5,
) -> str:
    code = _org_code_from_branding(db, max_len=3)
    return f"{code}RX{_dt_ddmmyyyy(on_date)}{rx_id:0{id_width}d}"



