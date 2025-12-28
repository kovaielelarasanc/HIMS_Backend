from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.ot_master import OtSurgeryMaster
from app.models.ot import OtOrder, OtAttachment
from app.schemas.ot_master import OtSurgeryMasterIn, OtSurgeryMasterOut
from app.schemas.ot import (OtOrderCreate, OtOrderScheduleIn, OtOrderStatusIn,
                            OtAttachmentIn, OtOrderOut)
from app.utils.files import save_upload
from datetime import datetime, date, timedelta, time, timezone
from zoneinfo import ZoneInfo

router = APIRouter(prefix="/ot", tags=["OT"])


IST = ZoneInfo("Asia/Kolkata")

def as_aware(dt: datetime | None, assume_tz=IST) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=assume_tz)  
    return dt

def to_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return as_aware(dt).astimezone(timezone.utc)

def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    # if DB returns naive, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def _to_out(o: OtOrder) -> dict:
    iso = lambda dt: dt.isoformat() if dt else None
    return {
        "id":
        o.id,
        "patient_id":
        o.patient_id,
        "context_type":
        o.context_type,
        "context_id":
        o.context_id,
        "surgery_master_id":
        o.surgery_master_id,
        "surgery_code":
        o.surgery_code,
        "surgery_name":
        o.surgery_name,
        "estimated_cost":
        float(o.estimated_cost or 0),
        "surgeon_id":
        o.surgeon_id,
        "anaesthetist_id":
        o.anaesthetist_id,
        "preop_notes":
        o.preop_notes,
        "status":
        o.status,
        "scheduled_start":
        iso(o.scheduled_start),
        "scheduled_end":
        iso(o.scheduled_end),
        "actual_start":
        iso(o.actual_start),
        "actual_end":
        iso(o.actual_end),
        "created_at":
        iso(o.created_at),
        "updated_at":
        iso(o.updated_at),
        "approved_at":
        iso(getattr(o, "approved_at", None)),
        "reported_at":
        iso(getattr(o, "reported_at", None)),
        "scanned_at":
        iso(getattr(o, "scanned_at", None)),

        # NEW: attachments (for anaesthesia records / reports)
        "attachments": [
            {
                "id": a.id,
                "url": a.file_url,  # keep 'url' for history compatibility
                "file_url": a.file_url,  # explicit
                "note": a.note,
                "created_at": iso(a.created_at),
            } for a in (o.attachments or [])
        ],
    }


# ---------- permissions ----------
def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False): return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes: return
    raise HTTPException(403, "Not permitted")


# ---------- realtime ----------
class _WS:

    def __init__(self):
        self.conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.conns.add(ws)

    def disconnect(self, ws: WebSocket):
        self.conns.discard(ws)

    async def broadcast(self, data: Dict[str, Any]):
        gone = []
        for ws in list(self.conns):
            try:
                await ws.send_json(data)
            except Exception:
                gone.append(ws)
        for ws in gone:
            self.disconnect(ws)


_ws = _WS()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _ws.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive/no-op
    except WebSocketDisconnect:
        _ws.disconnect(ws)


async def _notify(kind: str, **data):
    await _ws.broadcast({"type": f"ot.{kind}", **data})


# ===================== ORDERS =====================
@router.post("/orders")
async def create_ot_order(
        payload: OtOrderCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.create", "ipd.manage", "visits.update"])

    surgery_name = payload.surgery_name
    estimated_cost = float(payload.estimated_cost or 0.0)
    surgery_code = None
    master_id = None

    if payload.surgery_master_id:
        master = db.query(OtSurgeryMaster).get(payload.surgery_master_id)
        if not master or not master.active:
            raise HTTPException(404, "Surgery master not found/active")
        master_id = master.id
        surgery_code = master.code
        surgery_name = master.name
        if (payload.estimated_cost
                in (None, 0, 0.0)) and master.default_cost is not None:
            estimated_cost = float(master.default_cost or 0)

    if not surgery_name:
        raise HTTPException(
            422, "Either surgery_master_id or surgery_name is required")

    ot = OtOrder(
        patient_id=payload.patient_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        surgery_master_id=master_id,
        surgery_code=surgery_code,
        surgery_name=surgery_name.strip(),
        estimated_cost=estimated_cost,
        surgeon_id=payload.surgeon_id,
        anaesthetist_id=payload.anaesthetist_id,
        preop_notes=(payload.preop_notes or ""),
        status="planned",
        created_by=user.id,
    )

    if payload.scheduled_start:
        try:
            ot.scheduled_start = datetime.fromisoformat(
                payload.scheduled_start)
        except Exception:
            raise HTTPException(400, "Invalid scheduled_start")
        if payload.scheduled_end:
            try:
                ot.scheduled_end = datetime.fromisoformat(
                    payload.scheduled_end)
            except Exception:
                raise HTTPException(400, "Invalid scheduled_end")
        ot.status = "scheduled"

    db.add(ot)
    db.commit()
    await _notify("created", order_id=ot.id, status=ot.status)
    return {"id": ot.id, "message": "OT order created"}


@router.get("/orders", response_model=List[OtOrderOut])
def list_ot_orders(
        patient_id: Optional[int] = Query(None),
        context_type: Optional[str] = Query(None, description="opd|ipd"),
        status: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.view", "ipd.view", "visits.update"])
    q = db.query(OtOrder)
    if patient_id: q = q.filter(OtOrder.patient_id == patient_id)
    if context_type: q = q.filter(OtOrder.context_type == context_type)
    if status: q = q.filter(OtOrder.status == status)
    if date_from:
        try:
            q = q.filter(
                OtOrder.created_at >= datetime.fromisoformat(date_from))
        except Exception:
            raise HTTPException(400, "Invalid date_from")
    if date_to:
        try:
            q = q.filter(OtOrder.created_at <= datetime.fromisoformat(date_to))
        except Exception:
            raise HTTPException(400, "Invalid date_to")
    rows = q.order_by(OtOrder.id.desc()).limit(limit).all()
    return [_to_out(r) for r in rows]


@router.get("/orders/{order_id}", response_model=OtOrderOut)
def get_ot_order(order_id: int,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.view", "ipd.view"])
    o = db.query(OtOrder).get(order_id)
    if not o:
        raise HTTPException(404, "OT order not found")
    return _to_out(o)


@router.post("/orders/{order_id}/schedule")
async def schedule_ot(order_id: int,
                      payload: OtOrderScheduleIn,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.update"])
    ot = db.query(OtOrder).get(order_id)
    if not ot: raise HTTPException(404, "OT order not found")
    try:
        ot.scheduled_start = datetime.fromisoformat(payload.scheduled_start)
    except Exception:
        raise HTTPException(400, "Invalid scheduled_start")
    if payload.scheduled_end:
        try:
            ot.scheduled_end = datetime.fromisoformat(payload.scheduled_end)
        except Exception:
            raise HTTPException(400, "Invalid scheduled_end")
    ot.status = "scheduled"
    ot.updated_by = user.id
    ot.updated_at = datetime.utcnow()
    db.commit()
    await _notify("scheduled", order_id=ot.id, at=ot.scheduled_start)
    return {"message": "OT scheduled", "id": ot.id}


@router.post("/orders/{order_id}/status")
async def update_ot_status(order_id: int,
                           payload: OtOrderStatusIn,
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.update"])
    ot = db.query(OtOrder).get(order_id)
    if not ot: raise HTTPException(404, "OT order not found")
    new_status = (payload.status or "").lower()
    if new_status not in {
            "planned", "scheduled", "in_progress", "completed", "cancelled"
    }:
        raise HTTPException(400, "Invalid status")
    now = datetime.utcnow()
    if new_status == "in_progress" and not ot.actual_start:
        ot.actual_start = now
    if new_status == "completed" and not ot.actual_end: ot.actual_end = now
    ot.status = new_status
    ot.updated_by = user.id
    ot.updated_at = now
    db.commit()

    if new_status == "completed":
        try:
            from app.services.billing_auto import auto_add_item_for_event
            auto_add_item_for_event(db,
                                    service_type="ot",
                                    ref_id=ot.id,
                                    patient_id=ot.patient_id,
                                    context_type=ot.context_type,
                                    context_id=ot.context_id,
                                    user_id=user.id)
            db.commit()
        except Exception:
            pass

    await _notify("status", order_id=ot.id, status=new_status)
    return {"message": "Status updated", "id": ot.id, "status": ot.status}


# Attachments: link-style
@router.post("/orders/{order_id}/attachments")
def add_ot_attachment(order_id: int,
                      payload: OtAttachmentIn,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.update", "ot.masters.manage"])
    ot = db.query(OtOrder).get(order_id)
    if not ot: raise HTTPException(404, "OT order not found")
    att = OtAttachment(order_id=ot.id,
                       file_url=payload.file_url,
                       note=payload.note or "",
                       created_by=user.id)
    db.add(att)
    db.commit()
    return {"id": att.id, "message": "Attachment added"}


# Attachments: upload => /files/ot/YYYY/MM/DD/<uuid>  (served by app.mount("/files", ...))
@router.post("/orders/{order_id}/upload")
def upload_ot_attachment(order_id: int,
                         file: UploadFile = File(...),
                         note: Optional[str] = Form(None),
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.update"])
    ot = db.query(OtOrder).get(order_id)
    if not ot: raise HTTPException(404, "OT order not found")
    meta = save_upload(file, "ot")
    att = OtAttachment(order_id=ot.id,
                       file_url=meta["public_url"],
                       note=note or "",
                       created_by=user.id)
    db.add(att)
    db.commit()
    return {"id": att.id, "url": meta["public_url"], "message": "Uploaded"}


# Queue for dashboards
@router.get("/queue")
def ot_queue(status: str = Query(
    "scheduled",
    pattern="^(planned|scheduled|in_progress|completed|cancelled)$"),
             limit: int = Query(200, ge=1, le=500),
             db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    _need_any(user, ["ot.cases.view"])
    q = db.query(OtOrder).filter(OtOrder.status == status).order_by(
        OtOrder.updated_at.desc())
    rows = q.limit(limit).all()
    return [{
        "order_id": r.id,
        "patient_id": r.patient_id,
        "surgery_name": r.surgery_name,
        "status": r.status,
        "scheduled_start": r.scheduled_start,
        "scheduled_end": r.scheduled_end,
        "actual_start": r.actual_start,
        "actual_end": r.actual_end,
        "updated_at": r.updated_at,
    } for r in rows]
