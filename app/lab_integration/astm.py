from __future__ import annotations

from typing import Any, Dict, List


def parse_astm_to_result(payload: str) -> Dict[str, Any]:
    """
    Parses basic ASTM records into a normalized dict similar to HL7.
    Works with common record types: H, P, O, R, C, L

    NOTE: real ASTM framing (ENQ/ACK/NAK, checksum) is usually handled by middleware.
    This function assumes you receive clean text records.
    """
    text = payload.replace("\n", "\r")
    lines = [x for x in text.split("\r") if x.strip()]

    specimen_barcode = None
    patient_identifier = None
    items: List[Dict[str, Any]] = []

    for line in lines:
        rec = line.split("|")
        rtype = rec[0].strip() if rec else ""
        if rtype == "P":
            # Patient ID commonly in field 3 or 4 depending on instrument
            # We safely pick first non-empty in P|1|...|patientid...
            for v in rec[2:6]:
                if v and v.strip():
                    patient_identifier = v.strip()
                    break
        elif rtype == "O":
            # specimen/sample id commonly in O-3 (instrument dependent)
            # Example: O|1|SAMPLE123||CBC|R...
            for idx in [2, 3]:
                if len(rec) > idx and rec[idx] and rec[idx].strip():
                    specimen_barcode = rec[idx].strip()
                    break
        elif rtype == "R":
            # R|1|^^^HB|13.2|g/dL|12-16|N|||F
            code_field = rec[2] if len(rec) > 2 else ""
            external_code = code_field.split("^")[-1].strip() if code_field else ""
            value = rec[3].strip() if len(rec) > 3 and rec[3] else ""
            units = rec[4].strip() if len(rec) > 4 and rec[4] else ""
            ref_range = rec[5].strip() if len(rec) > 5 and rec[5] else ""
            abnormal = rec[6].strip() if len(rec) > 6 and rec[6] else ""
            status = rec[9].strip() if len(rec) > 9 and rec[9] else ""

            items.append({
                "external_code": external_code[:80] if external_code else None,
                "value_text": value[:255] if value else None,
                "units": units[:40] if units else None,
                "ref_range": ref_range[:80] if ref_range else None,
                "abnormal_flag": abnormal[:10] if abnormal else None,
                "status": status[:10] if status else None,
            })

    return {
        "patient_identifier": patient_identifier,
        "encounter_identifier": None,
        "specimen_barcode": specimen_barcode,
        "items": items,
    }
