# app/lab_integration/mllp_server.py
from __future__ import annotations

import asyncio
import os
from typing import Optional
from datetime import datetime

from app.db.session import SessionLocal
from app.models.lab_integration import IntegrationDevice
from app.lab_integration.engine import stage_pipeline
from app.lab_integration.parsers.hl7_v2 import parse_msh, build_ack

SB = b"\x0b"        # VT
EB_CR = b"\x1c\x0d" # FS + CR


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "")
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class MLLPServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        await self._server.start_serving()

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        remote_ip = peer[0] if peer else None
        buf = b""

        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk

                while True:
                    start = buf.find(SB)
                    if start < 0:
                        if len(buf) > 8192:
                            buf = buf[-2048:]
                        break

                    end = buf.find(EB_CR, start)
                    if end < 0:
                        break

                    payload = buf[start + 1 : end]
                    buf = buf[end + len(EB_CR) :]

                    hl7_text = payload.decode("utf-8", errors="replace")
                    await self._process_one(hl7_text, remote_ip, writer)

        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_one(self, hl7_text: str, remote_ip: Optional[str], writer: asyncio.StreamWriter):
        db = SessionLocal()
        try:
            msh = parse_msh(hl7_text)
            sending_facility = (msh.get("sending_facility") or "").strip().upper()

            device = (
                db.query(IntegrationDevice)
                .filter(
                    IntegrationDevice.protocol == "HL7_MLLP",
                    IntegrationDevice.enabled == True,
                    IntegrationDevice.sending_facility_code == sending_facility,
                )
                .first()
            )

            if not device:
                # store under UNKNOWN so you can see it in Messages UI
                stage_pipeline(
                    db,
                    device=None,
                    tenant_code="UNKNOWN",
                    protocol="HL7_MLLP",
                    raw_payload=hl7_text,
                    remote_ip=remote_ip,
                    kind="HL7",
                    facility_code_override=sending_facility or "UNKNOWN",
                )
                ack = build_ack(msh, "AE")
                writer.write(SB + ack.encode("utf-8") + EB_CR)
                await writer.drain()
                return

            result = stage_pipeline(
                db,
                device=device,
                tenant_code=device.tenant_code,
                protocol="HL7_MLLP",
                raw_payload=hl7_text,
                remote_ip=remote_ip,
                kind="HL7",
            )

            # Always ACK AA for success & duplicates; AE for hard errors
            ack_code = "AA" if result.get("final_status") in ("PROCESSED", "PARSED", "DUPLICATE") else "AE"
            ack = build_ack(msh, ack_code)
            writer.write(SB + ack.encode("utf-8") + EB_CR)
            await writer.drain()

        finally:
            db.close()


def should_start_mllp() -> bool:
    return env_bool("LAB_MLLP_ENABLED", False)
