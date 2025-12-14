from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict, Any
# add near imports
from fastapi import Request
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.opd import RadiologyTest  # master table
from app.models.ris import RisOrder, RisAttachment
from app.schemas.ris import (RisOrderCreate, RisScheduleIn, RisReportIn,
                             RisAttachmentIn, RisOrderOut, RadiologyTestIn,
                             RadiologyTestOut)
from app.models.opd import Visit
from app.models.ipd import IpdAdmission
from app.services.ris_billing import bill_ris_order

from app.utils.files import save_upload

router = APIRouter(prefix="/ris", tags=["RIS"])


# ---------- permission helpers ----------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code == code:
                return True
    return False


def require_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


def _infer_context_type(db: Session, context_id: int | None) -> str | None:
    if not context_id:
        return None
    if db.query(Visit.id).filter(Visit.id == context_id).first():
        return "opd"
    if db.query(IpdAdmission.id).filter(IpdAdmission.id == context_id).first():
        return "ipd"
    return None


# ---------- realtime (WebSocket) ----------
class _WSManager:

    def __init__(self):
        self.connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.discard(ws)

    async def broadcast(self, payload: Dict[str, Any]):
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_ws = _WSManager()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await _ws.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws.disconnect(websocket)


async def _notify(kind: str, **data):
    await _ws.broadcast({"type": f"ris.{kind}", **data})


# =====================================================================
# Masters (RadiologyTest) — thin CRUD so frontend never 404s
# =====================================================================


@router.get("/masters/tests", response_model=List[RadiologyTestOut])
def list_tests(
        q: Optional[str] = None,
        active: Optional[bool] = None,
        page: int = 1,
        page_size: int = 50,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(
        user,
        ["masters.ris.manage", "radiology.masters.manage", "masters.view"])
    qry = db.query(RadiologyTest)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(RadiologyTest.code.ilike(like),
                RadiologyTest.name.ilike(like)))
    if active is not None and hasattr(RadiologyTest, "is_active"):
        qry = qry.filter(RadiologyTest.is_active.is_(active))
    rows = (qry.order_by(RadiologyTest.code.asc()).offset(
        (page - 1) * page_size).limit(page_size).all())
    out = []
    for r in rows:
        out.append(
            RadiologyTestOut(
                id=r.id,
                code=r.code,
                name=r.name,
                price=float(getattr(r, "price", 0) or 0),
                modality=getattr(r, "modality", None) if hasattr(
                    r, "modality") else None,
                body_part=getattr(r, "body_part", None) if hasattr(
                    r, "body_part") else None,
                is_active=getattr(r, "is_active", True) if hasattr(
                    r, "is_active") else True,
            ))
    return out


@router.post("/masters/tests", response_model=RadiologyTestOut)
def create_test(
        payload: RadiologyTestIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["masters.ris.manage", "radiology.masters.manage"])
    # Guard attributes that may not exist in your RadiologyTest model (fixes "modality invalid keyword" error)
    m = RadiologyTest(code=payload.code.strip(), name=payload.name.strip())
    if hasattr(m, "price"): m.price = payload.price or 0
    if hasattr(m, "modality"): m.modality = payload.modality
    if hasattr(m, "body_part"): m.body_part = payload.body_part
    if hasattr(m, "is_active"):
        m.is_active = True if payload.is_active is None else payload.is_active
    db.add(m)
    db.commit()
    return RadiologyTestOut(
        id=m.id,
        code=m.code,
        name=m.name,
        price=float(getattr(m, "price", 0) or 0),
        modality=getattr(m, "modality", None)
        if hasattr(m, "modality") else None,
        body_part=getattr(m, "body_part", None)
        if hasattr(m, "body_part") else None,
        is_active=getattr(m, "is_active", True)
        if hasattr(m, "is_active") else True,
    )


@router.put("/masters/tests/{test_id}", response_model=RadiologyTestOut)
def update_test(
        test_id: int,
        payload: RadiologyTestIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["masters.ris.manage", "radiology.masters.manage"])
    m = db.query(RadiologyTest).get(test_id)
    if not m:
        raise HTTPException(404, "Radiology test not found")
    m.code = payload.code.strip()
    m.name = payload.name.strip()
    if hasattr(m, "price"): m.price = payload.price or 0
    if hasattr(m, "modality"): m.modality = payload.modality
    if hasattr(m, "body_part"): m.body_part = payload.body_part
    if hasattr(m, "is_active"):
        m.is_active = True if payload.is_active is None else payload.is_active
    db.commit()
    return RadiologyTestOut(
        id=m.id,
        code=m.code,
        name=m.name,
        price=float(getattr(m, "price", 0) or 0),
        modality=getattr(m, "modality", None)
        if hasattr(m, "modality") else None,
        body_part=getattr(m, "body_part", None)
        if hasattr(m, "body_part") else None,
        is_active=getattr(m, "is_active", True)
        if hasattr(m, "is_active") else True,
    )


@router.delete("/masters/tests/{test_id}")
def delete_test(
        test_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["masters.ris.manage", "radiology.masters.manage"])
    m = db.query(RadiologyTest).get(test_id)
    if not m:
        raise HTTPException(404, "Radiology test not found")
    db.delete(m)
    db.commit()
    return {"message": "Deleted"}


# =====================================================================
# Orders & Reporting
# =====================================================================


@router.post("/orders")
async def create_order(
        payload: RisOrderCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(
        user,
        ["orders.ris.create", "radiology.orders.create", "visits.update"])
    m = db.query(RadiologyTest).get(payload.test_id)
    if not m:
        raise HTTPException(404, "Radiology test not found")
    ctx_type = payload.context_type
    if not ctx_type and payload.context_id:
        ctx_type = _infer_context_type(db, payload.context_id)

    o = RisOrder(
        patient_id=payload.patient_id,
        context_type=ctx_type,
        context_id=payload.context_id,
        ordering_user_id=payload.ordering_user_id or user.id,
        test_id=m.id,
        test_name=m.name,
        test_code=m.code,
        modality=getattr(m, "modality", None)
        if hasattr(m, "modality") else None,
        status="ordered",
        created_by=user.id,
        billing_status="not_billed",
    )

    db.add(o)
    db.commit()
    await _notify("ordered", order_id=o.id)
    return {"id": o.id, "message": "RIS order created"}


@router.get("/orders", response_model=List[RisOrderOut])
def list_orders(
        patient_id: Optional[int] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.orders.view", "orders.ris.view"])
    query = db.query(RisOrder)
    if patient_id:
        query = query.filter(RisOrder.patient_id == patient_id)
    if status:
        query = query.filter(RisOrder.status == status)
    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            query = query.filter(RisOrder.created_at >= df)
        except Exception:
            raise HTTPException(400, "Invalid date_from")
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            query = query.filter(RisOrder.created_at <= dt)
        except Exception:
            raise HTTPException(400, "Invalid date_to")
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(RisOrder.test_name.ilike(like),
                RisOrder.test_code.ilike(like)))
    rows = query.order_by(RisOrder.id.desc()).limit(limit).all()
    out: List[RisOrderOut] = []
    for o in rows:
        out.append(
            RisOrderOut(
                id=o.id,
                patient_id=o.patient_id,
                context_type=o.context_type,
                context_id=o.context_id,
                test_id=o.test_id,
                test_name=o.test_name,
                test_code=o.test_code,
                modality=o.modality,
                status=o.status,
                scheduled_at=o.scheduled_at.isoformat()
                if o.scheduled_at else None,
                scanned_at=o.scanned_at.isoformat() if o.scanned_at else None,
                reported_at=o.reported_at.isoformat()
                if o.reported_at else None,
                report_text=o.report_text,
                approved_at=o.approved_at.isoformat()
                if o.approved_at else None,
                created_at=o.created_at.isoformat() if o.created_at else None,
            ))
    return out


@router.get("/orders/{order_id}", response_model=RisOrderOut)
def get_order(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.orders.view", "orders.ris.view"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    return RisOrderOut(
        id=o.id,
        patient_id=o.patient_id,
        context_type=o.context_type,
        context_id=o.context_id,
        test_id=o.test_id,
        test_name=o.test_name,
        test_code=o.test_code,
        modality=o.modality,
        status=o.status,
        scheduled_at=o.scheduled_at.isoformat() if o.scheduled_at else None,
        scanned_at=o.scanned_at.isoformat() if o.scanned_at else None,
        reported_at=o.reported_at.isoformat() if o.reported_at else None,
        report_text=o.report_text,
        approved_at=o.approved_at.isoformat() if o.approved_at else None,
        created_at=o.created_at.isoformat() if o.created_at else None,
    )


@router.post("/orders/{order_id}/schedule")
async def schedule(
        order_id: int,
        payload: RisScheduleIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.schedule.manage"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    try:
        dt = datetime.fromisoformat(payload.scheduled_at)
    except Exception:
        raise HTTPException(400, "Invalid ISO datetime")
    o.scheduled_at = dt
    o.status = "scheduled"
    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()
    await _notify("scheduled", order_id=o.id, at=o.scheduled_at)
    return {"message": "Scheduled", "id": o.id}


@router.post("/orders/{order_id}/scan")
async def mark_scanned(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.scan.update"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    o.status = "scanned"
    o.scanned_at = datetime.utcnow()
    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()
    await _notify("scanned", order_id=o.id, at=o.scanned_at)
    return {"message": "Scan marked", "id": o.id}


@router.post("/orders/{order_id}/report")
async def add_report(
        order_id: int,
        payload: RisReportIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.report.create"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    if (getattr(o, "billing_status", None) or "not_billed") != "billed":
        inv = bill_ris_order(db, order=o, created_by=user.id)
        o.billing_invoice_id = inv.id
        o.billing_status = "billed"
    o.report_text = payload.report_text
    o.status = "reported"
    o.reported_at = datetime.utcnow()
    o.primary_signoff_by = user.id
    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()

    # Optional billing hook — never break API if billing service missing
    try:
        from app.services.billing_auto import auto_add_item_for_event
        auto_add_item_for_event(db,
                                service_type="radiology",
                                ref_id=o.id,
                                patient_id=o.patient_id,
                                context_type=o.context_type,
                                context_id=o.context_id,
                                user_id=user.id)
        db.commit()
    except Exception:
        pass

    await _notify("reported", order_id=o.id)
    return {"message": "Report saved", "id": o.id}


# For your frontend `updateRadiologyReport` call
@router.put("/orders/{order_id}/report")
async def update_report(
        order_id: int,
        payload: RisReportIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.report.create"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    o.report_text = payload.report_text
    if o.status not in {"reported", "approved"}:
        o.status = "reported"
        o.reported_at = datetime.utcnow()
    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()
    await _notify("report_updated", order_id=o.id)
    return {"message": "Report updated", "id": o.id}


@router.post("/orders/{order_id}/approve")
async def approve_report(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.report.approve"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    if o.status != "reported":
        raise HTTPException(400, "Report not ready for approval")
        # ✅ Billing: on approve (idempotent)
    if (getattr(o, "billing_status", None) or "not_billed") != "billed":
        inv = bill_ris_order(db, order=o, created_by=user.id)
        o.billing_invoice_id = inv.id
        o.billing_status = "billed"
    o.status = "approved"
    o.secondary_signoff_by = user.id
    o.approved_at = datetime.utcnow()
    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()

    # Optional billing hook — safe
    try:
        from app.services.billing_auto import auto_add_item_for_event
        auto_add_item_for_event(db,
                                service_type="radiology",
                                ref_id=o.id,
                                patient_id=o.patient_id,
                                context_type=o.context_type,
                                context_id=o.context_id,
                                user_id=user.id)
        db.commit()
    except Exception:
        pass

    await _notify("approved", order_id=o.id)
    return {"message": "Approved", "id": o.id}


# ---------- Attachments (link + upload) ----------
@router.post("/attachments/{order_id}")
def add_attachment(
        order_id: int,
        payload: RisAttachmentIn,
        request: Request,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.attachments.add"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    att = RisAttachment(
        order_id=o.id,
        file_url=payload.file_url,
        note=payload.note or "",
        created_by=user.id,
    )
    db.add(att)
    db.commit()

    return {
        "id": att.id,
        "message": "Attachment added",
        "file_url": _abs_url(request, att.file_url),  # ✅ absolute
    }


@router.post("/orders/{order_id}/upload")
def upload_attachment(
        order_id: int,
        request: Request,
        file: UploadFile = File(...),
        note: Optional[str] = Form(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.attachments.add"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    meta = save_upload(file, "ris")  # must return "/files/ris/...."
    att = RisAttachment(
        order_id=o.id,
        file_url=meta["public_url"],
        note=note or "",
        created_by=user.id,
    )
    db.add(att)
    db.commit()

    return {
        "id": att.id,
        "file_url": _abs_url(request, att.file_url),  # ✅ absolute
        "message": "Uploaded",
    }


def _abs_url(request: Request, u: str) -> str:
    if not u:
        return u
    if u.startswith("http://") or u.startswith("https://"):
        return u

    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get(
        "host") or request.url.netloc
    base = f"{proto}://{host}"

    if u.startswith("/"):
        return base + u
    return base + "/" + u


# ---------- Queue (dashboard) ----------
@router.get("/queue")
def ris_queue(
        status: str = Query(
            "scheduled",
            pattern="^(ordered|scheduled|scanned|reported|approved)$"),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.orders.view", "orders.ris.view"])
    q = db.query(RisOrder).filter(RisOrder.status == status).order_by(
        RisOrder.updated_at.desc())
    rows = q.limit(limit).all()
    return [{
        "order_id": r.id,
        "patient_id": r.patient_id,
        "test_name": r.test_name,
        "modality": r.modality,
        "status": r.status,
        "scheduled_at": r.scheduled_at,
        "scanned_at": r.scanned_at,
        "reported_at": r.reported_at,
        "approved_at": r.approved_at,
        "updated_at": r.updated_at,
    } for r in rows]


# ADD in your routes file (same file you shared)
from pydantic import BaseModel


class RisAttachmentOut(BaseModel):
    id: int
    order_id: int
    file_url: str
    note: str | None = None
    created_at: str | None = None
    created_by: int | None = None


class RisNotesIn(BaseModel):
    notes: str | None = None


@router.get("/orders/{order_id}/attachments",
            response_model=List[RisAttachmentOut])
def list_attachments(
        order_id: int,
        request: Request,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.orders.view", "orders.ris.view"])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    rows = (db.query(RisAttachment).filter(
        RisAttachment.order_id == order_id).order_by(
            RisAttachment.id.desc()).all())

    return [
        RisAttachmentOut(
            id=r.id,
            order_id=r.order_id,
            file_url=_abs_url(request, r.file_url),  # ✅ absolute
            note=getattr(r, "note", None),
            created_by=getattr(r, "created_by", None),
            created_at=r.created_at.isoformat() if getattr(
                r, "created_at", None) else None,
        ) for r in rows
    ]


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
        attachment_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, ["radiology.attachments.add"])
    att = db.query(RisAttachment).get(attachment_id)
    if not att:
        raise HTTPException(404, "Attachment not found")

    # allow admin OR creator
    if not getattr(user, "is_admin", False):
        if getattr(att, "created_by", None) != user.id:
            raise HTTPException(403, "Not permitted")

    db.delete(att)
    db.commit()
    return {"message": "Deleted"}


@router.put("/orders/{order_id}/notes")
def update_order_notes(
        order_id: int,
        payload: RisNotesIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    require_any(user, [
        "radiology.report.create", "radiology.orders.update",
        "orders.ris.update"
    ])
    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    # ✅ IMPORTANT: do NOT change status/workflow
    txt = (payload.notes or "").strip()

    # prefer a dedicated "notes" column if you add later; else fallback safely
    if hasattr(o, "notes"):
        o.notes = txt
    else:
        o.report_text = txt  # fallback storage

    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Saved", "notes": txt}


@router.post("/orders/{order_id}/finalize")
def finalize_order(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ permission
    require_any(user, [
        "orders.ris.finalize", "radiology.orders.finalize",
        "billing.invoices.create"
    ])

    o = db.query(RisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    # ✅ idempotent
    if (o.status or "").lower() == "finalized":
        return {
            "message": "Already finalized",
            "status": o.status,
            "billing_status": getattr(o, "billing_status", None),
            "invoice_id": getattr(o, "billing_invoice_id", None),
        }

    # ✅ auto billing if not billed
    if (getattr(o, "billing_status", None) or "not_billed") != "billed":
        inv = bill_ris_order(db, order=o, created_by=user.id)
        o.billing_invoice_id = inv.id
        o.billing_status = "billed"

    # ✅ manual finalize status
    o.status = "finalized"
    if hasattr(o, "finalized_at"):
        o.finalized_at = datetime.utcnow()

    o.updated_by = user.id
    o.updated_at = datetime.utcnow()
    db.commit()

    return {
        "message": "Finalized",
        "status": o.status,
        "billing_status": getattr(o, "billing_status", None),
        "invoice_id": getattr(o, "billing_invoice_id", None),
    }
