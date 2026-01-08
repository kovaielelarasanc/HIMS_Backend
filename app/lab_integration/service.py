from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal  # ✅ adjust if your SessionLocal path differs
from app.lab_integration.hl7 import parse_msh, build_ack, parse_hl7_oru_to_result
from app.models.lab_integration import (
    IntegrationDevice, IntegrationMessage,
    LabInboundResult, LabInboundResultItem, LabCodeMapping
)

MLLP_START = b"\x0b"
MLLP_END = b"\x1c\x0d"


def _decode_bytes(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("latin-1", errors="ignore")


def _ip_allowed(device: IntegrationDevice, remote_ip: str) -> bool:
    ips = device.allowed_remote_ips or []
    if not ips:
        return True
    return remote_ip in set(map(str, ips))


def _map_internal_test_id(db, tenant_code: str, device_id: int, external_code: str) -> Optional[int]:
    if not external_code:
        return None
    row = (
        db.query(LabCodeMapping)
        .filter(
            LabCodeMapping.tenant_code == tenant_code,
            LabCodeMapping.source_device_id == device_id,
            LabCodeMapping.external_code == external_code,
            LabCodeMapping.active == True,
        )
        .first()
    )
    return int(row.internal_test_id) if row else None


def process_hl7_payload_sync(payload_text: str, remote_ip: str) -> str:
    """
    Sync function called from thread: stores message, parses and stages results, returns ACK string.
    """
    db = SessionLocal()
    try:
        msh = parse_msh(payload_text)

        # Find device by sending facility code
        device = (
            db.query(IntegrationDevice)
            .filter(
                IntegrationDevice.protocol == "HL7_MLLP",
                IntegrationDevice.enabled == True,
                IntegrationDevice.sending_facility_code == (msh.sending_facility or ""),
            )
            .first()
        )

        if not device:
            # still ACK with AE (unknown facility) to stop endless retries
            return build_ack(msh, "AE", f"Unknown facility: {msh.sending_facility}")

        if not _ip_allowed(device, remote_ip):
            device.last_error_at = datetime.utcnow()
            device.last_error = f"Rejected remote IP: {remote_ip}"
            db.add(device)
            db.commit()
            return build_ack(msh, "AE", "Remote IP not allowed")

        # Save message (idempotent by device_id + message_control_id)
        msg = IntegrationMessage(
            tenant_code=device.tenant_code,
            device_id=device.id,
            protocol="HL7_MLLP",
            direction="IN",
            received_at=datetime.utcnow(),
            remote_ip=remote_ip,
            message_type=msh.message_type,
            message_control_id=msh.message_control_id or None,
            facility_code=msh.sending_facility or device.sending_facility_code,
            parse_status="RECEIVED",
            raw_payload=payload_text,
        )

        db.add(msg)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Duplicate message control id -> mark as duplicate (safe) and return AA
            # (We still accept; do not create duplicate results)
            return build_ack(msh, "AA", "Duplicate ignored")

        # Update device heartbeat
        device.last_seen_at = datetime.utcnow()
        db.add(device)
        db.commit()

        # Parse ORU result content to normalized dict
        normalized = parse_hl7_oru_to_result(payload_text)
        msg.parsed_json = {
            "patient_identifier": normalized.get("patient_identifier"),
            "encounter_identifier": normalized.get("encounter_identifier"),
            "specimen_barcode": normalized.get("specimen_barcode"),
            "item_count": len(normalized.get("items") or []),
        }
        msg.parse_status = "PARSED"
        db.add(msg)
        db.commit()

        # Stage inbound result
        res = LabInboundResult(
            tenant_code=device.tenant_code,
            message_id=msg.id,
            patient_identifier=normalized.get("patient_identifier"),
            encounter_identifier=normalized.get("encounter_identifier"),
            specimen_barcode=normalized.get("specimen_barcode"),
            report_status="RECEIVED",
            observed_at=None,
        )
        db.add(res)
        db.commit()

        unmapped = []
        for it in normalized.get("items") or []:
            ext_code = (it.get("external_code") or "").strip()
            internal_id = _map_internal_test_id(db, device.tenant_code, device.id, ext_code)
            if ext_code and not internal_id:
                unmapped.append(ext_code)

            item = LabInboundResultItem(
                result_id=res.id,
                external_code=ext_code or None,
                internal_test_id=internal_id,
                value_text=it.get("value_text"),
                units=it.get("units"),
                ref_range=it.get("ref_range"),
                abnormal_flag=it.get("abnormal_flag"),
                status=it.get("status"),
                observed_at=None,
            )
            db.add(item)

        db.commit()

        # If unmapped codes exist: mark message ERROR (application-level), but ACK AA
        if unmapped:
            msg.parse_status = "ERROR"
            msg.error_reason = "Unmapped test codes: " + ", ".join(sorted(set(unmapped))[:50])
        else:
            msg.parse_status = "PROCESSED"
            msg.processed_at = datetime.utcnow()

        db.add(msg)
        db.commit()

        return build_ack(msh, "AA", "OK")

    except Exception as e:
        # If we can parse MSH we already created ack. If parsing failed, return generic AE ACK.
        try:
            msh = parse_msh(payload_text)
            return build_ack(msh, "AE", f"Parse error: {str(e)[:120]}")
        except Exception:
            # Generic ACK without referencing control id
            now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            return (
                f"MSH|^~\\&|HMIS|HOSP|LIS|LAB|{now}||ACK|{now}|P|2.3\r"
                f"MSA|AE||Bad HL7\r"
            )
    finally:
        db.close()


class HL7MLLPServer:
    def __init__(self):
        self.enabled = os.getenv("LAB_MLLP_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        self.host = os.getenv("LAB_MLLP_HOST", "0.0.0.0")
        self.port = int(os.getenv("LAB_MLLP_PORT", "2575"))
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self):
        if not self.enabled:
            return
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        remote_ip = peer[0] if peer else "unknown"

        buf = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk

                # handle possibly multiple messages in buffer
                while True:
                    start = buf.find(MLLP_START)
                    if start == -1:
                        break
                    end = buf.find(MLLP_END, start + 1)
                    if end == -1:
                        break

                    frame = buf[start + 1 : end]  # HL7 bytes
                    buf = buf[end + len(MLLP_END) :]

                    payload_text = _decode_bytes(frame).strip()

                    # process in thread (DB is sync)
                    ack = await asyncio.to_thread(process_hl7_payload_sync, payload_text, remote_ip)

                    # send ACK with MLLP framing
                    writer.write(MLLP_START + ack.encode("utf-8") + MLLP_END)
                    await writer.drain()

        except Exception:
            # do not crash server
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
