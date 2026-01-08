# app/lab_integration/parsers/mispa_viva.py
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

_SPLIT_RE = re.compile(r"[\r\n\x12\x15]+")  # supports device CR/LF variants


def _parse_date_time(date_s: str | None, time_s: str | None) -> Optional[datetime]:
    if not date_s:
        return None
    date_s = date_s.strip()
    time_s = (time_s or "").strip()

    for dfmt in ("%d:%m:%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(date_s, dfmt)
            break
        except ValueError:
            d = None
    if not d:
        return None

    if time_s:
        for tfmt in ("%H:%M", "%H:%M:%S"):
            try:
                t = datetime.strptime(time_s, tfmt).time()
                return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second)
            except ValueError:
                pass

    return datetime(d.year, d.month, d.day)


def parse_mispa_viva_packet(raw: str) -> Dict[str, Any]:
    if raw is None:
        raw = ""
    s = raw.replace("\x00", "").strip()
    lines = [ln.strip() for ln in _SPLIT_RE.split(s) if ln.strip()]
    data: Dict[str, str] = {}

    for ln in lines:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k = (k or "").strip().lower()
        v = (v or "").strip()
        if k:
            data[k] = v

    time_s = data.get("time")
    date_s = data.get("date")
    testname = data.get("testname") or data.get("test name")
    ptid = data.get("ptid") or data.get("patientid") or data.get("patient id")
    result = data.get("result")
    flag = data.get("flag") or data.get("flags")

    observed_at = _parse_date_time(date_s, time_s)

    item = {
        "external_code": (testname or "").strip() or None,
        "value_text": (result or "").strip() or None,
        "units": None,
        "ref_range": None,
        "abnormal_flag": (flag or "").strip() or None,
        "status": "F",
        "observed_at": observed_at,
    }

    return {
        "patient_identifier": (ptid or "").strip() or None,
        "encounter_identifier": None,
        "specimen_barcode": None,
        "observed_at": observed_at,
        "items": [item] if (item["external_code"] or item["value_text"]) else [],
    }
