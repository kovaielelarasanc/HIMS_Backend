from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class HL7MSH:
    sending_app: str = ""
    sending_facility: str = ""
    receiving_app: str = ""
    receiving_facility: str = ""
    timestamp: str = ""
    message_type: str = ""
    message_control_id: str = ""
    version: str = "2.3"


def _safe_split(line: str, sep: str = "|") -> List[str]:
    return line.split(sep)


def parse_msh(hl7_text: str) -> HL7MSH:
    """
    Minimal MSH parse for ACK + routing.
    """
    lines = [x for x in hl7_text.replace("\n", "\r").split("\r") if x.strip()]
    msh_line = next((l for l in lines if l.startswith("MSH")), None)
    if not msh_line:
        raise ValueError("Missing MSH segment")

    f = _safe_split(msh_line, "|")
    # MSH has special field indexing: f[0]="MSH", f[1]=encoding chars
    # Standard positions:
    # 2 Sending App, 3 Sending Facility, 4 Receiving App, 5 Receiving Facility
    # 6 Date/Time, 8 Message Type, 9 Control ID, 11 Version (depending on exact HL7)
    msh = HL7MSH()
    msh.sending_app = f[2] if len(f) > 2 else ""
    msh.sending_facility = f[3] if len(f) > 3 else ""
    msh.receiving_app = f[4] if len(f) > 4 else ""
    msh.receiving_facility = f[5] if len(f) > 5 else ""
    msh.timestamp = f[6] if len(f) > 6 else ""
    msh.message_type = f[8] if len(f) > 8 else ""         # ex: ORU^R01
    msh.message_control_id = f[9] if len(f) > 9 else ""   # unique id
    msh.version = f[11] if len(f) > 11 else "2.3"
    return msh


def build_ack(original: HL7MSH, ack_code: str = "AA", text: str = "OK") -> str:
    """
    Returns an HL7 ACK message (string) for MLLP transport.
    """
    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    # Swap sender/receiver for ACK
    msh = (
        "MSH|^~\\&|"
        f"{original.receiving_app}|{original.receiving_facility}|"
        f"{original.sending_app}|{original.sending_facility}|"
        f"{now}||ACK|{original.message_control_id or now}|P|{original.version or '2.3'}"
    )
    msa = f"MSA|{ack_code}|{original.message_control_id or ''}|{text}"
    return msh + "\r" + msa + "\r"


def parse_hl7_oru_to_result(hl7_text: str) -> Dict[str, Any]:
    """
    Parses ORU/ORM minimally into a normalized dict:
    - patient_identifier (PID-3)
    - encounter_identifier (PV1-19 preferred; else PV1-50 etc if present)
    - specimen_barcode (SPM-2 if exists; else OBR-3)
    - items from OBX segments
    """
    lines = [x for x in hl7_text.replace("\n", "\r").split("\r") if x.strip()]
    segs: List[Tuple[str, List[str]]] = []
    for l in lines:
        parts = _safe_split(l, "|")
        segs.append((parts[0], parts))

    def first(seg: str) -> Optional[List[str]]:
        for s, p in segs:
            if s == seg:
                return p
        return None

    pid = first("PID")
    pv1 = first("PV1")

    patient_identifier = ""
    if pid and len(pid) > 3:
        # PID-3 may contain "UHID^^^HOSP^MR"
        patient_identifier = (pid[3] or "").split("^")[0].strip()

    encounter_identifier = ""
    if pv1:
        # PV1-19 is common "Visit Number"
        if len(pv1) > 19 and pv1[19]:
            encounter_identifier = pv1[19].split("^")[0].strip()
        elif len(pv1) > 50 and pv1[50]:
            encounter_identifier = pv1[50].split("^")[0].strip()

    specimen_barcode = ""
    spm = first("SPM")
    if spm and len(spm) > 2 and spm[2]:
        specimen_barcode = spm[2].split("^")[0].strip()

    # fallback: OBR-3
    if not specimen_barcode:
        obr = first("OBR")
        if obr and len(obr) > 3 and obr[3]:
            specimen_barcode = obr[3].split("^")[0].strip()

    items: List[Dict[str, Any]] = []
    current_obr = None
    for seg, p in segs:
        if seg == "OBR":
            current_obr = p
        elif seg == "OBX":
            # OBX-3 code, OBX-5 value, OBX-6 units, OBX-7 ref, OBX-8 abnormal, OBX-11 status
            code = p[3] if len(p) > 3 else ""
            # code can be "HB^Hemoglobin^L"
            external_code = (code or "").split("^")[0].strip()
            value = p[5] if len(p) > 5 else ""
            units = p[6] if len(p) > 6 else ""
            ref_range = p[7] if len(p) > 7 else ""
            abnormal = p[8] if len(p) > 8 else ""
            status = p[11] if len(p) > 11 else ""

            items.append({
                "external_code": external_code,
                "value_text": (value or "")[:255],
                "units": (units or "")[:40],
                "ref_range": (ref_range or "")[:80],
                "abnormal_flag": (abnormal or "")[:10],
                "status": (status or "")[:10],
            })

    return {
        "patient_identifier": patient_identifier or None,
        "encounter_identifier": encounter_identifier or None,
        "specimen_barcode": specimen_barcode or None,
        "items": items,
    }
