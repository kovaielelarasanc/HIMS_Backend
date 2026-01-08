# app/lab_integration/engine.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from app.models.lab_integration import (
    IntegrationDevice,
    IntegrationMessage,
    LabCodeMapping,
    LabInboundResult,
    LabInboundResultItem,
)

# Keep your existing parsers (already in your project)
from app.lab_integration.astm import parse_astm_to_result
from app.lab_integration.hl7 import parse_hl7_oru_to_result  # your legacy HL7 parser

# New parsers from this module
from app.lab_integration.parsers.hl7_v2 import parse_msh, parse_oru_r01
from app.lab_integration.parsers.mispa_viva import parse_mispa_viva_packet

Normalized = Dict[str, Any]
ParserFn = Callable[[str], Normalized]

PARSERS: Dict[str, ParserFn] = {
    # HL7
    "HL7_ORU": lambda raw: parse_oru_r01(raw),
    "HL7_ORU_LEGACY": lambda raw: parse_hl7_oru_to_result(raw),

    # ASTM
    "ASTM": lambda raw: parse_astm_to_result(raw),

    # Vendor packets
    "MISPA_VIVA": lambda raw: parse_mispa_viva_packet(raw),
}


def detect_kind(raw: str) -> str:
    s = (raw or "").lstrip()
    if s.startswith("MSH|"):
        return "HL7"
    if s.startswith("H|") or "H|" in s[:200]:
        return "ASTM"
    if "PTID:" in s or "TestName:" in s:
        return "MISPA_VIVA"
    return "RAW"


def choose_parser(device_protocol: str, kind: str, raw: str) -> str:
    p = (device_protocol or "").strip().upper()
    k = (kind or "AUTO").strip().upper()
    if k == "AUTO":
        k = detect_kind(raw)

    # protocol based
    if p == "HL7_MLLP" or p == "HL7_HTTP":
        return "HL7_ORU"
    if p == "ASTM_HTTP":
        return "ASTM"
    if p == "MISPA_VIVA_HTTP":
        return "MISPA_VIVA"

    # kind based fallback
    if k == "HL7":
        return "HL7_ORU"
    if k == "ASTM":
        return "ASTM"
    if k == "MISPA_VIVA":
        return "MISPA_VIVA"

    # safest fallback
    return "HL7_ORU_LEGACY" if raw.lstrip().startswith("MSH|") else "ASTM"


def extract_hl7_meta(raw: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    s = (raw or "").lstrip()
    if not s.startswith("MSH|"):
        return None, None, None
    msh = parse_msh(raw)
    return (
        (msh.get("message_type") or None),
        (msh.get("message_control_id") or None),
        (msh.get("sending_facility") or None),
    )


def stage_pipeline(
    db: Session,
    device: Optional[IntegrationDevice],
    *,
    tenant_code: str,
    protocol: str,
    raw_payload: str,
    remote_ip: Optional[str],
    kind: str = "AUTO",
    facility_code_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Universal pipeline for HL7/ASTM/vendor formats.
    - safe dedupe
    - safe staging
    - unmapped -> ERROR queue
    """

    msg_type, msg_ctl, hl7_fac = extract_hl7_meta(raw_payload)
    facility_code = (facility_code_override or hl7_fac or (device.sending_facility_code if device else None))

    # allowlist check if device exists
    if device and device.allowed_remote_ips and remote_ip and remote_ip not in (device.allowed_remote_ips or []):
        raise ValueError("Remote IP not allowed for this device")

    msg = IntegrationMessage(
        tenant_code=tenant_code,
        device_id=device.id if device else None,
        protocol=protocol,
        direction="IN",
        received_at=datetime.utcnow(),
        processed_at=None,
        remote_ip=remote_ip,
        message_type=msg_type or (kind if kind != "AUTO" else None),
        message_control_id=msg_ctl or None,
        facility_code=facility_code,
        parse_status="RECEIVED",
        error_reason=None,
        raw_payload=raw_payload,
        parsed_json=None,
    )
    db.add(msg)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Duplicate only happens when device_id+message_control_id matches
        return {"status": True, "duplicate": True, "final_status": "DUPLICATE", "message_id": None}

    db.refresh(msg)

    parser_key = choose_parser(protocol, kind, raw_payload)
    parser = PARSERS.get(parser_key)
    if not parser:
        msg.parse_status = "ERROR"
        msg.error_reason = f"No parser registered: {parser_key}"
        db.add(msg)
        db.commit()
        return {"status": False, "message_id": msg.id, "final_status": "ERROR", "error_reason": msg.error_reason}

    try:
        normalized = parser(raw_payload)
    except Exception as e:
        msg.parse_status = "ERROR"
        msg.error_reason = f"Parse failed ({parser_key}): {str(e)[:200]}"
        db.add(msg)
        if device:
            device.last_error_at = datetime.utcnow()
            device.last_error = msg.error_reason
            db.add(device)
        db.commit()
        return {"status": False, "message_id": msg.id, "final_status": "ERROR", "error_reason": msg.error_reason}

    items = normalized.get("items") or []
    msg.parsed_json = {
        "parser": parser_key,
        "patient_identifier": normalized.get("patient_identifier"),
        "encounter_identifier": normalized.get("encounter_identifier"),
        "specimen_barcode": normalized.get("specimen_barcode"),
        "item_count": len(items),
    }
    msg.parse_status = "PARSED"
    msg.error_reason = None
    db.add(msg)
    db.commit()

    # stage result header
    res = LabInboundResult(
        tenant_code=tenant_code,
        message_id=msg.id,
        patient_identifier=normalized.get("patient_identifier"),
        encounter_identifier=normalized.get("encounter_identifier"),
        specimen_barcode=normalized.get("specimen_barcode"),
        report_status="RECEIVED",
        observed_at=normalized.get("observed_at"),
        created_at=datetime.utcnow(),
    )
    db.add(res)
    db.commit()
    db.refresh(res)

    # stage items + mapping
    unmapped: List[str] = []
    for it in items:
        ext_code = (it.get("external_code") or "").strip()
        internal_id = None

        if device and ext_code:
            m = (
                db.query(LabCodeMapping)
                .filter(
                    LabCodeMapping.tenant_code == tenant_code,
                    LabCodeMapping.source_device_id == device.id,
                    LabCodeMapping.external_code == ext_code,
                    LabCodeMapping.active == True,
                )
                .first()
            )
            internal_id = int(m.internal_test_id) if m else None
            if not internal_id:
                unmapped.append(ext_code)

        db.add(
            LabInboundResultItem(
                result_id=res.id,
                external_code=ext_code or None,
                internal_test_id=internal_id,
                value_text=it.get("value_text"),
                units=it.get("units"),
                ref_range=it.get("ref_range"),
                abnormal_flag=it.get("abnormal_flag"),
                status=it.get("status"),
                observed_at=it.get("observed_at") or normalized.get("observed_at"),
            )
        )

    db.commit()

    if unmapped:
        msg.parse_status = "ERROR"
        msg.error_reason = "Unmapped test codes: " + ", ".join(sorted(set(unmapped))[:50])
        if device:
            device.last_error_at = datetime.utcnow()
            device.last_error = msg.error_reason
            db.add(device)
    else:
        msg.parse_status = "PROCESSED"
        msg.processed_at = datetime.utcnow()

    db.add(msg)
    db.commit()

    if device:
        device.last_seen_at = datetime.utcnow()
        db.add(device)
        db.commit()

    return {"status": True, "message_id": msg.id, "final_status": msg.parse_status, "error_reason": msg.error_reason}


def compute_stats(db: Session, tenant_code: Optional[str] = None) -> Dict[str, int]:
    q = db.query(IntegrationMessage)
    if tenant_code:
        q = q.filter(IntegrationMessage.tenant_code == tenant_code)

    def _count(status: str) -> int:
        return int(q.filter(IntegrationMessage.parse_status == status).count())

    since = datetime.utcnow() - timedelta(hours=24)
    last24 = q.filter(IntegrationMessage.received_at >= since).count()

    return {
        "received": _count("RECEIVED"),
        "parsed": _count("PARSED"),
        "processed": _count("PROCESSED"),
        "error": _count("ERROR"),
        "duplicate": _count("DUPLICATE"),
        "last_24h": int(last24),
    }
