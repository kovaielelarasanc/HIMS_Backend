# app/lab_integration/parsers/hl7_v2.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


def _ts_to_dt(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    ts = ts.strip()
    # YYYYMMDDHHMMSS, YYYYMMDDHHMM, YYYYMMDD
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(ts[: len(datetime.utcnow().strftime(fmt))], fmt)
        except Exception:
            continue
    return None


def parse_msh(msg: str) -> Dict[str, Any]:
    if not msg:
        raise ValueError("Empty HL7")
    segs = [s for s in msg.replace("\n", "\r").split("\r") if s.strip()]
    msh = next((s for s in segs if s.startswith("MSH")), None)
    if not msh:
        raise ValueError("Missing MSH segment")

    field_sep = msh[3:4]
    parts = msh.split(field_sep)
    enc = parts[1] if len(parts) > 1 else "^~\\&"

    return {
        "field_sep": field_sep,
        "encoding_chars": enc,
        "sending_app": parts[2] if len(parts) > 2 else "",
        "sending_facility": parts[3] if len(parts) > 3 else "",
        "message_datetime": _ts_to_dt(parts[6] if len(parts) > 6 else None),
        "message_type": parts[8] if len(parts) > 8 else "",
        "message_control_id": parts[9] if len(parts) > 9 else "",
        "processing_id": parts[10] if len(parts) > 10 else "",
        "version_id": parts[11] if len(parts) > 11 else "",
        "charset": parts[17] if len(parts) > 17 else "",
        "segments": segs,
    }


def _first_comp(v: str | None, sep: str = "^") -> Optional[str]:
    if not v:
        return None
    x = v.split(sep)[0].strip()
    return x or None


def parse_oru_r01(msg: str) -> Dict[str, Any]:
    msh = parse_msh(msg)
    fs = msh["field_sep"]
    comp = (msh["encoding_chars"] or "^~\\&")[0:1] or "^"
    segs = msh["segments"]

    pid = next((s for s in segs if s.startswith("PID" + fs)), None)
    obr = next((s for s in segs if s.startswith("OBR" + fs)), None)
    obx_list = [s for s in segs if s.startswith("OBX" + fs)]

    patient_identifier = None
    if pid:
        p = pid.split(fs)
        patient_identifier = _first_comp(p[3] if len(p) > 3 else None, comp)

    specimen_barcode = None
    observed_at = None
    if obr:
        o = obr.split(fs)
        specimen_barcode = (o[3] if len(o) > 3 else "").strip() or None
        observed_at = _ts_to_dt(o[7] if len(o) > 7 else None) or _ts_to_dt(o[6] if len(o) > 6 else None)

    items: List[Dict[str, Any]] = []
    for obx in obx_list:
        x = obx.split(fs)
        obx3 = (x[3] if len(x) > 3 else "").strip()
        code = _first_comp(obx3, comp) or (obx3 or None)

        value_type = (x[2] if len(x) > 2 else "").strip()
        value = (x[5] if len(x) > 5 else "").strip()
        units = (x[6] if len(x) > 6 else "").strip()
        ref_range = (x[7] if len(x) > 7 else "").strip()
        abnormal = (x[8] if len(x) > 8 else "").strip()
        status = (x[11] if len(x) > 11 else "").strip()

        if value_type == "ED" and value:
            value = "[BINARY_ED_PAYLOAD]"

        if not code and not value:
            continue

        items.append(
            {
                "external_code": code,
                "value_text": value or None,
                "units": units or None,
                "ref_range": ref_range or None,
                "abnormal_flag": abnormal or None,
                "status": status or "F",
                "observed_at": observed_at,
            }
        )

    return {
        "msh": msh,
        "patient_identifier": patient_identifier,
        "encounter_identifier": None,
        "specimen_barcode": specimen_barcode,
        "observed_at": observed_at,
        "items": items,
    }


def build_ack(incoming_msh: Dict[str, Any], ack_code: str = "AA") -> str:
    field_sep = "|"
    enc = "^~\\&"
    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    incoming_ctrl = (incoming_msh.get("message_control_id") or "").strip()
    version = (incoming_msh.get("version_id") or "2.3.1").strip() or "2.3.1"
    charset = (incoming_msh.get("charset") or "UNICODE").strip() or "UNICODE"

    msh = (
        f"MSH{field_sep}{enc}{field_sep}NUTRYAH_HMIS{field_sep}{field_sep}"
        f"{field_sep}{field_sep}{now}{field_sep}{field_sep}ACK^R01{field_sep}1{field_sep}P{field_sep}{version}"
        f"{field_sep}{field_sep}{field_sep}{field_sep}{field_sep}{field_sep}{charset}"
    )
    msa = f"MSA{field_sep}{ack_code}{field_sep}{incoming_ctrl}"
    return msh + "\r" + msa + "\r"
