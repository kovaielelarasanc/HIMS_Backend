# app/lab_integration/routes.py
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.schemas.lab_integration import (
    DeviceCreate, DeviceUpdate, DeviceOut,
    MappingCreate, MappingOut,
    IngestPayload, StatsOut
)
from app.lab_integration.engine import stage_pipeline, compute_stats
from app.models.lab_integration import (
    IntegrationDevice, IntegrationMessage,
    LabCodeMapping, LabInboundResult, LabInboundResultItem
)

# ---- Try to use your existing deps; fallback only if needed ----
try:
    from app.api.deps import get_db, current_user  # your project
except Exception:
    from app.db.session import SessionLocal

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def current_user():
        raise HTTPException(status_code=401, detail="Auth not configured (current_user missing)")


router = APIRouter(prefix="/lab/integration", tags=["Lab Integration"])


# ---------------- Permission helper (RBAC) ----------------
def _need_any(user, codes: List[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


def require_view(user=Depends(current_user)):
    _need_any(user, ["lab.integration.view", "lab.integration.manage"])
    return user


def require_manage(user=Depends(current_user)):
    _need_any(user, ["lab.integration.manage"])
    return user


def require_ingest_token(x_integration_token: str | None = Header(default=None, alias="X-Integration-Token")):
    token = os.getenv("LAB_INGEST_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="LAB_INGEST_TOKEN not configured")
    if (x_integration_token or "").strip() != token:
        raise HTTPException(status_code=401, detail="Invalid integration token")


def require_admin_token_or_rbac(
    user=Depends(current_user),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    token = os.getenv("LAB_ADMIN_TOKEN", "").strip()
    if token:
        if (x_admin_token or "").strip() != token:
            raise HTTPException(status_code=401, detail="Admin token required")
        return user
    _need_any(user, ["lab.integration.manage"])
    return user


# ---------------- Devices ----------------
@router.post("/devices", dependencies=[Depends(require_admin_token_or_rbac)], response_model=DeviceOut)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db)):
    d = IntegrationDevice(
        tenant_code=payload.tenant_code,
        name=payload.name,
        protocol=payload.protocol,
        sending_facility_code=payload.sending_facility_code,
        enabled=payload.enabled,
        allowed_remote_ips=payload.allowed_remote_ips,
    )
    db.add(d)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Device already exists for (protocol + sending_facility_code)")
    db.refresh(d)
    return DeviceOut(
        id=d.id,
        tenant_code=d.tenant_code,
        name=d.name,
        protocol=d.protocol,
        sending_facility_code=d.sending_facility_code,
        enabled=d.enabled,
        allowed_remote_ips=d.allowed_remote_ips,
        last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
        last_error_at=d.last_error_at.isoformat() if d.last_error_at else None,
        last_error=d.last_error,
    )


@router.get("/devices", dependencies=[Depends(require_view)], response_model=list[DeviceOut])
def list_devices(tenant_code: str | None = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(IntegrationDevice)
    if tenant_code:
        q = q.filter(IntegrationDevice.tenant_code == tenant_code)
    rows = q.order_by(IntegrationDevice.id.desc()).all()
    return [
        DeviceOut(
            id=d.id,
            tenant_code=d.tenant_code,
            name=d.name,
            protocol=d.protocol,
            sending_facility_code=d.sending_facility_code,
            enabled=d.enabled,
            allowed_remote_ips=d.allowed_remote_ips,
            last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
            last_error_at=d.last_error_at.isoformat() if d.last_error_at else None,
            last_error=d.last_error,
        )
        for d in rows
    ]


@router.patch("/devices/{device_id}", dependencies=[Depends(require_admin_token_or_rbac)], response_model=DeviceOut)
def update_device(device_id: int, payload: DeviceUpdate, db: Session = Depends(get_db)):
    d = db.query(IntegrationDevice).filter(IntegrationDevice.id == device_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Device not found")

    if payload.name is not None:
        d.name = payload.name
    if payload.protocol is not None:
        d.protocol = payload.protocol
    if payload.sending_facility_code is not None:
        d.sending_facility_code = payload.sending_facility_code
    if payload.enabled is not None:
        d.enabled = payload.enabled
    if payload.allowed_remote_ips is not None:
        d.allowed_remote_ips = payload.allowed_remote_ips

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate (protocol + sending_facility_code)")
    db.refresh(d)
    return DeviceOut(
        id=d.id,
        tenant_code=d.tenant_code,
        name=d.name,
        protocol=d.protocol,
        sending_facility_code=d.sending_facility_code,
        enabled=d.enabled,
        allowed_remote_ips=d.allowed_remote_ips,
        last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
        last_error_at=d.last_error_at.isoformat() if d.last_error_at else None,
        last_error=d.last_error,
    )


# ---------------- Mappings ----------------
@router.post("/mappings", dependencies=[Depends(require_manage)], response_model=MappingOut)
def create_mapping(payload: MappingCreate, db: Session = Depends(get_db), user=Depends(current_user)):
    row = LabCodeMapping(
        tenant_code=payload.tenant_code,
        source_device_id=payload.source_device_id,
        external_code=payload.external_code.strip(),
        internal_test_id=payload.internal_test_id,
        active=True,
        updated_by_user_id=getattr(user, "id", None),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mapping already exists (tenant + device + external_code)")
    db.refresh(row)
    return MappingOut(
        id=row.id,
        tenant_code=row.tenant_code,
        source_device_id=row.source_device_id,
        external_code=row.external_code,
        internal_test_id=row.internal_test_id,
        active=row.active,
    )


@router.get("/mappings", dependencies=[Depends(require_view)], response_model=list[MappingOut])
def list_mappings(tenant_code: str = Query(...), source_device_id: int = Query(...), db: Session = Depends(get_db)):
    rows = (
        db.query(LabCodeMapping)
        .filter(
            LabCodeMapping.tenant_code == tenant_code,
            LabCodeMapping.source_device_id == source_device_id,
            LabCodeMapping.active == True,
        )
        .order_by(LabCodeMapping.external_code.asc())
        .all()
    )
    return [
        MappingOut(
            id=r.id,
            tenant_code=r.tenant_code,
            source_device_id=r.source_device_id,
            external_code=r.external_code,
            internal_test_id=r.internal_test_id,
            active=r.active,
        )
        for r in rows
    ]


@router.delete("/mappings/{mapping_id}", dependencies=[Depends(require_manage)])
def deactivate_mapping(mapping_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.query(LabCodeMapping).filter(LabCodeMapping.id == mapping_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Mapping not found")
    r.active = False
    r.updated_by_user_id = getattr(user, "id", None)
    r.updated_at = datetime.utcnow()
    db.add(r)
    db.commit()
    return {"status": True}


# ---------------- Stats ----------------
@router.get("/stats", dependencies=[Depends(require_view)], response_model=StatsOut)
def stats(tenant_code: str | None = Query(default=None), db: Session = Depends(get_db)):
    return compute_stats(db, tenant_code)


# ---------------- Messages ----------------
@router.get("/messages", dependencies=[Depends(require_view)])
def list_messages(
    tenant_code: str | None = Query(default=None),
    device_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(IntegrationMessage)
    if tenant_code:
        q = q.filter(IntegrationMessage.tenant_code == tenant_code)
    if device_id:
        q = q.filter(IntegrationMessage.device_id == device_id)
    if status:
        q = q.filter(IntegrationMessage.parse_status == status)
    rows = q.order_by(IntegrationMessage.id.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "tenant_code": r.tenant_code,
            "device_id": r.device_id,
            "protocol": r.protocol,
            "received_at": r.received_at.isoformat(),
            "parse_status": r.parse_status,
            "message_type": r.message_type,
            "message_control_id": r.message_control_id,
            "error_reason": r.error_reason,
        }
        for r in rows
    ]


@router.get("/messages/{message_id}", dependencies=[Depends(require_view)])
def read_message(message_id: int, db: Session = Depends(get_db)):
    r = db.query(IntegrationMessage).filter(IntegrationMessage.id == message_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Message not found")
    return {
        "id": r.id,
        "tenant_code": r.tenant_code,
        "device_id": r.device_id,
        "protocol": r.protocol,
        "received_at": r.received_at.isoformat(),
        "parse_status": r.parse_status,
        "error_reason": r.error_reason,
        "message_type": r.message_type,
        "message_control_id": r.message_control_id,
        "parsed_json": r.parsed_json,
        "raw_payload": r.raw_payload,
    }


@router.get("/error-queue", dependencies=[Depends(require_view)])
def error_queue(tenant_code: str | None = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(IntegrationMessage).filter(IntegrationMessage.parse_status == "ERROR")
    if tenant_code:
        q = q.filter(IntegrationMessage.tenant_code == tenant_code)
    rows = q.order_by(IntegrationMessage.id.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "tenant_code": r.tenant_code,
            "device_id": r.device_id,
            "received_at": r.received_at.isoformat(),
            "error_reason": r.error_reason,
            "message_control_id": r.message_control_id,
        }
        for r in rows
    ]


@router.post("/messages/{message_id}/reprocess", dependencies=[Depends(require_manage)])
def reprocess_message(message_id: int, db: Session = Depends(get_db)):
    msg = db.query(IntegrationMessage).filter(IntegrationMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    device = None
    if msg.device_id:
        device = db.query(IntegrationDevice).filter(IntegrationDevice.id == msg.device_id).first()

    # delete previous staged results for this message (idempotent reprocess)
    old = db.query(LabInboundResult).filter(LabInboundResult.message_id == msg.id).all()
    for r in old:
        db.delete(r)
    db.commit()

    # reset status
    msg.parse_status = "RECEIVED"
    msg.error_reason = None
    msg.processed_at = None
    db.add(msg)
    db.commit()

    result = stage_pipeline(
        db,
        device=device,
        tenant_code=msg.tenant_code,
        protocol=msg.protocol,
        raw_payload=msg.raw_payload,
        remote_ip=msg.remote_ip,
        kind="AUTO",
        facility_code_override=msg.facility_code,
    )

    return {"status": True, "final_status": result.get("final_status"), "error_reason": result.get("error_reason")}


# ---------------- Universal HTTP Ingest (Agent/Middleware) ----------------
@router.post("/ingest", dependencies=[Depends(require_ingest_token)])
def ingest(payload: IngestPayload, request: Request, db: Session = Depends(get_db)):
    remote_ip = request.client.host if request.client else None

    # Find device by facility_code + protocol family (we accept any HTTP protocol)
    device = (
        db.query(IntegrationDevice)
        .filter(
            IntegrationDevice.enabled == True,
            IntegrationDevice.sending_facility_code == payload.facility_code,
        )
        .order_by(IntegrationDevice.id.desc())
        .first()
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found for facility_code")

    result = stage_pipeline(
        db,
        device=device,
        tenant_code=device.tenant_code,
        protocol=device.protocol,
        raw_payload=payload.payload,
        remote_ip=remote_ip,
        kind=payload.kind,
        facility_code_override=payload.facility_code,
    )

    if result.get("final_status") == "ERROR":
        raise HTTPException(status_code=400, detail=result.get("error_reason") or "Parse failed")
    return result
