# FILE: analyzer_parsers.py
"""
Generic analyzers parser utilities for Nutryah HIMS connector.

Supported formats:
- CSV (simple line-based)
- ASTM (H/P/O/R/L records)
- HL7 v2 (OBX segments)

Each parser returns a list[dict] shaped like DeviceResultItemIn:

{
  "sample_id": "...",
  "external_test_code": "...",
  "external_test_name": "... or None",
  "result_value": "...",
  "unit": "... or None",
  "flag": "... or None",
  "reference_range": "... or None",
  "measured_at": ISO-8601 string or None,
}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Dict, Any


# -------------------------------------------------------------------
#  Helper – safe timestamp
# -------------------------------------------------------------------

def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# -------------------------------------------------------------------
#  CSV parser (your existing simple format)
# -------------------------------------------------------------------

def parse_csv_message(raw: str) -> List[Dict[str, Any]]:
    """
    Simple CSV:

    sample_id,external_test_code,result_value,unit,flag,reference_range

    Example:
        SMP-00123,WBC,5.6,10^3/uL,,4.0-11.0
        SMP-00123,RBC,4.5,10^6/uL,,4.0-5.5
    """
    results: list[dict[str, Any]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Skip header / comments
        if line.lower().startswith("sample_id") or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            # malformed
            continue

        sample_id = parts[0]
        ext_code = parts[1]
        value = parts[2]
        unit = parts[3] if len(parts) > 3 else ""
        flag = parts[4] if len(parts) > 4 else ""
        ref_range = parts[5] if len(parts) > 5 else ""

        results.append(
            {
                "sample_id": sample_id,
                "external_test_code": ext_code,
                "external_test_name": None,
                "result_value": value,
                "unit": unit or None,
                "flag": flag or None,
                "reference_range": ref_range or None,
                "measured_at": now_iso_utc(),
            }
        )

    return results


# -------------------------------------------------------------------
#  ASTM parser (simplified generic)
# -------------------------------------------------------------------

def parse_astm_message(raw: str) -> List[Dict[str, Any]]:
    """
    Very generic ASTM parser.

    ASTM structure (simplified):
      H|...         -> Header
      P|1|...       -> Patient
      O|1|SampleID^... -> Order, sample ID usually in field 3
      R|1|^^^TEST^1|VALUE|UNIT|FLAG|REF RANGE|...
      L|1|F        -> Terminator

    We will:
      - Track current sample_id from O record
      - Read R records for that sample
      - Map them to DeviceResultItemIn style dicts
    """
    results: list[dict[str, Any]] = []
    current_sample_id: str | None = None

    # ASTM frames might be split, here we just split by lines.
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Remove STX/ETX control chars if present
        line = line.replace("\x02", "").replace("\x03", "")

        parts = line.split("|")
        rec_type = parts[0] if parts else ""

        if rec_type.startswith("H"):  # header – ignore
            continue

        elif rec_type.startswith("P"):  # patient – ignore for now
            continue

        elif rec_type.startswith("O"):  # order
            # Sample ID often in field 3: e.g. O|1|SampleID^1^...
            if len(parts) > 2:
                sample_field = parts[2]
                # Sometimes SampleID^.., sometimes just SampleID
                current_sample_id = sample_field.split("^")[0].strip()
            else:
                current_sample_id = None

        elif rec_type.startswith("R"):  # result
            # Minimal safety
            if current_sample_id is None:
                # Cannot map this result without sample_id
                continue

            # ASTM typical result R record:
            # R|1|^^^GLU^1|5.6|mmol/L|N|...|REF RANGE|...
            # index: 0 1   2        3   4       5 6      7 ...
            # parts[2] = ^^^GLU^1  -> test code in 4th ^
            # parts[3] = value
            # parts[4] = unit
            # parts[5] = flag (e.g., N, H, L)
            # parts[7] = reference range (varies by vendor)

            test_code = ""
            if len(parts) > 2:
                comp = parts[2].split("^")
                if len(comp) >= 4:
                    test_code = comp[3]
                elif len(comp) >= 2:
                    test_code = comp[1]
                else:
                    test_code = parts[2]
            value = parts[3].strip() if len(parts) > 3 else ""
            unit = parts[4].strip() if len(parts) > 4 else ""
            flag = parts[5].strip() if len(parts) > 5 else ""
            ref_range = parts[7].strip() if len(parts) > 7 else ""

            if not test_code or not value:
                continue

            results.append(
                {
                    "sample_id": current_sample_id,
                    "external_test_code": test_code,
                    "external_test_name": None,  # you can map code->name in backend
                    "result_value": value,
                    "unit": unit or None,
                    "flag": flag or None,
                    "reference_range": ref_range or None,
                    "measured_at": now_iso_utc(),
                }
            )

        elif rec_type.startswith("L"):  # terminator
            # End of transmission – nothing to do
            continue

        else:
            # Unhandled line type – safe to ignore for now
            continue

    return results


# -------------------------------------------------------------------
#  HL7 v2 parser (simplified generic)
# -------------------------------------------------------------------

def parse_hl7_message(raw: str) -> List[Dict[str, Any]]:
    """
    Very generic HL7 v2 parser (ORU^R01 style).

    Typical structure:
      MSH|^~\&|...
      PID|...
      OBR|1||SampleID^...|...
      OBX|1|NM|TEST^CODE^...|...|VALUE|UNIT|...
    We'll:
      - Track current sample_id from OBR segment
      - Parse OBX segments for numeric results
    """
    results: list[dict[str, Any]] = []
    current_sample_id: str | None = None

    # HL7 segments separated by \r or \n
    segments = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        seg = line.strip()
        if seg:
            segments.append(seg)

    for seg in segments:
        fields = seg.split("|")
        if not fields:
            continue
        seg_type = fields[0]

        if seg_type == "MSH":
            # Header – ignore for now
            continue

        elif seg_type == "PID":
            # Patient – ignore in this connector
            continue

        elif seg_type == "OBR":
            # OBR-3 usually contains Sample ID: <placer order #> or <filler order #>
            # Example: OBR|1||SMP-00123^LAB|...
            if len(fields) > 3:
                sample_field = fields[3]
                current_sample_id = sample_field.split("^")[0].strip()
            else:
                current_sample_id = None

        elif seg_type == "OBX":
            # OBX|1|NM|GLU^Glucose^LN|...|5.6|mmol/L|4.0-7.0|...
            # fields[2] = value type (NM numeric)
            # fields[3] = identifier: code^text^system
            # fields[5] = result value
            # fields[6] = unit
            # fields[7] = reference range
            if current_sample_id is None:
                continue

            if len(fields) < 6:
                continue

            id_field = fields[3]
            components = id_field.split("^")
            code = components[0].strip() if components else ""
            text = components[1].strip() if len(components) > 1 else None

            value = fields[5].strip()
            unit = fields[6].strip() if len(fields) > 6 else ""
            ref_range = fields[7].strip() if len(fields) > 7 else ""
            # ABNORMAL flags often in field 8 (e.g., H, L)
            flag = fields[8].strip() if len(fields) > 8 else ""

            if not code or not value:
                continue

            results.append(
                {
                    "sample_id": current_sample_id,
                    "external_test_code": code,
                    "external_test_name": text or None,
                    "result_value": value,
                    "unit": unit or None,
                    "flag": flag or None,
                    "reference_range": ref_range or None,
                    "measured_at": now_iso_utc(),
                }
            )

        else:
            # Other segments – ignore
            continue

    return results


# -------------------------------------------------------------------
#  Main router – choose parser by protocol
# -------------------------------------------------------------------

def parse_message_by_protocol(
    raw: str,
    protocol: str,
) -> List[Dict[str, Any]]:
    """
    protocol: 'csv' | 'astm' | 'hl7'
    """
    proto = protocol.lower()
    if proto == "csv":
        return parse_csv_message(raw)
    if proto == "astm":
        return parse_astm_message(raw)
    if proto == "hl7":
        return parse_hl7_message(raw)

    # Fallback: treat as CSV
    return parse_csv_message(raw)
