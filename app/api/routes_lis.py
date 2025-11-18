# app/api/routes_lis.py
from __future__ import annotations
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.opd import LabTest  # master (code = NABL code)
from app.models.lis import LisOrder, LisOrderItem, LisAttachment
from app.schemas.lis import (LisOrderCreate, LisCollectIn, LisResultIn,
                             LisAttachmentIn, LisOrderOut, LisOrderItemOut)

router = APIRouter(prefix="/lab", tags=["LIS"])


# ---------------- Permission helpers ----------------
def _has(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in user.roles:
        for p in r.permissions:
            if p.code in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


# ---------------- Real-time (WebSocket) ----------------
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
async def lab_ws(websocket: WebSocket):
    await _ws.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # optional ping/pong
    except WebSocketDisconnect:
        _ws.disconnect(websocket)


async def _notify(kind: str, **data):
    await _ws.broadcast({"type": f"lab.{kind}", **data})


# ------------------- Masters passthrough (prevents 404) -------------------
@router.get("/masters/tests", response_model=dict)
def list_lab_masters(
        q: str = "",
        active: Optional[bool] = Query(None),
        page: int = 1,
        page_size: int = 50,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    base = db.query(LabTest)
    if q:
        like = f"%{q.strip()}%"
        base = base.filter((LabTest.code.ilike(like))
                           | (LabTest.name.ilike(like)))
    if active is not None and hasattr(LabTest, "is_active"):
        base = base.filter(LabTest.is_active.is_(bool(active)))
    total = base.count()
    rows = (base.order_by(LabTest.name.asc()).offset(
        (page - 1) * page_size).limit(page_size).all())
    # keep a simple envelope your frontend can consume
    items = [{
        "id": m.id,
        "code": getattr(m, "code", None),
        "name": getattr(m, "name", None),
        "price": float(getattr(m, "price", 0) or 0),
        "is_active": bool(getattr(m, "is_active", True)),
    } for m in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# ------------------- Create Order -------------------
@router.post("/orders")
def create_order(
        payload: LisOrderCreate,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["orders.lab.create", "lab.orders.create"])
    if not payload.items:
        raise HTTPException(422, "At least one test is required")

    order = LisOrder(
        patient_id=payload.patient_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        ordering_user_id=payload.ordering_user_id or user.id,
        priority=payload.priority or "routine",
        status="ordered",
        created_by=user.id,
    )
    db.add(order)
    db.flush()

    for it in payload.items:
        m = db.query(LabTest).get(it.test_id)
        if not m:
            raise HTTPException(404, f"LabTest not found: {it.test_id}")
        db.add(
            LisOrderItem(
                order_id=order.id,
                test_id=m.id,
                test_name=getattr(m, "name", f"Test {m.id}"),
                test_code=getattr(m, "code", ""),
                status="ordered",
                created_by=user.id,
            ))

    db.commit()
    _ = order.id
    return {"id": order.id, "message": "LIS order created"}


# ------------------- List Orders (with pagination) -------------------
@router.get("/orders", response_model=List[LisOrderOut])
def list_orders(
        patient_id: Optional[int] = Query(None),
        status: Optional[str] = Query(
            None,
            description=
            "ordered/collected/in_progress/validated/reported/cancelled"),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])

    q = db.query(LisOrder)
    if patient_id: q = q.filter(LisOrder.patient_id == patient_id)
    if status: q = q.filter(LisOrder.status == status)
    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            q = q.filter(LisOrder.created_at >= df)
        except Exception:
            raise HTTPException(400, "Invalid date_from")
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            q = q.filter(LisOrder.created_at <= dt)
        except Exception:
            raise HTTPException(400, "Invalid date_to")

    rows = (q.order_by(LisOrder.id.desc()).offset(
        (page - 1) * page_size).limit(page_size).all())

    out: list[LisOrderOut] = []
    for o in rows:
        items = db.query(LisOrderItem).filter(
            LisOrderItem.order_id == o.id).all()
        out.append(
            LisOrderOut(id=o.id,
                        patient_id=o.patient_id,
                        context_type=o.context_type,
                        context_id=o.context_id,
                        priority=o.priority,
                        status=o.status,
                        collected_at=o.collected_at.isoformat()
                        if o.collected_at else None,
                        reported_at=o.reported_at.isoformat()
                        if o.reported_at else None,
                        items=[
                            LisOrderItemOut(
                                id=i.id,
                                test_id=i.test_id,
                                test_name=i.test_name,
                                test_code=i.test_code,
                                status=i.status,
                                sample_barcode=i.sample_barcode,
                                result_value=i.result_value,
                                is_critical=bool(i.is_critical),
                                result_at=i.result_at.isoformat()
                                if i.result_at else None,
                            ) for i in items
                        ]))
    return out


# ------------------- Get Order -------------------
@router.get("/orders/{order_id}", response_model=LisOrderOut)
def get_order(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])
    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order.id).all()
    return LisOrderOut(
        id=order.id,
        patient_id=order.patient_id,
        context_type=order.context_type,
        context_id=order.context_id,
        priority=order.priority,
        status=order.status,
        collected_at=order.collected_at.isoformat()
        if order.collected_at else None,
        reported_at=order.reported_at.isoformat()
        if order.reported_at else None,
        items=[
            LisOrderItemOut(
                id=i.id,
                test_id=i.test_id,
                test_name=i.test_name,
                test_code=i.test_code,
                status=i.status,
                sample_barcode=i.sample_barcode,
                result_value=i.result_value,
                is_critical=bool(i.is_critical),
                result_at=i.result_at.isoformat() if i.result_at else None,
            ) for i in items
        ])


# ------------------- Queue (dashboard) -------------------
@router.get("/queue")
def queue(
        status: str = Query(
            "in_progress",
            pattern="^(ordered|collected|in_progress|validated)$"),
        patient_id: Optional[int] = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])
    q = db.query(LisOrderItem)
    if patient_id:
        q = q.join(LisOrder, LisOrderItem.order_id == LisOrder.id).filter(
            LisOrder.patient_id == patient_id)
    q = q.filter(LisOrderItem.status == status).order_by(
        LisOrderItem.updated_at.desc())
    rows = q.limit(200).all()
    return [{
        "item_id": i.id,
        "order_id": i.order_id,
        "test_name": i.test_name,
        "status": i.status,
        "barcode": i.sample_barcode,
        "result_value": i.result_value,
        "updated_at": i.updated_at,
    } for i in rows]


# ------------------- Sample Collection -------------------
@router.post("/orders/{order_id}/collect")
async def collect_samples(
        order_id: int,
        payload: LisCollectIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.samples.collect"])
    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    items: List[LisOrderItem] = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order.id).all()
    if not items:
        raise HTTPException(400, "No items to collect")

    any_change = False
    now = datetime.utcnow()
    for it in items:
        if it.status in {"validated", "reported", "cancelled"}:
            continue
        it.sample_barcode = payload.barcode
        it.status = "collected"
        it.updated_by = user.id
        it.updated_at = now
        any_change = True

    if any_change:
        order.status = "collected"
        order.collected_at = now
        order.updated_by = user.id
        order.updated_at = now

    db.commit()
    await _notify("collected", order_id=order.id, barcode=payload.barcode)
    return {"message": "Samples collected", "order_id": order.id}


# ------------------- Result Entry -------------------
@router.post("/orders/{order_id}/results")
async def enter_results(
        order_id: int,
        results: List[LisResultIn],
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.enter"])
    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    now = datetime.utcnow()
    for r in results:
        it = db.query(LisOrderItem).get(r.item_id)
        if not it or it.order_id != order.id:
            raise HTTPException(404, f"Item not found: {r.item_id}")
        if it.status in {"validated", "reported", "cancelled"}:
            continue
        it.result_value = r.result_value
        it.is_critical = r.is_critical
        it.result_at = now
        it.status = "in_progress"
        it.updated_by = user.id
        it.updated_at = now

    order.status = "in_progress"
    order.updated_by = user.id
    order.updated_at = now

    db.commit()
    await _notify("results_saved", order_id=order.id)
    return {"message": "Results saved"}


# ------------------- Validation -------------------
@router.post("/items/{item_id}/validate")
async def validate_item(
        item_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.validate"])
    it = db.query(LisOrderItem).get(item_id)
    if not it:
        raise HTTPException(404, "Item not found")
    if not it.result_value:
        raise HTTPException(400, "Result missing")

    it.status = "validated"
    it.validated_by = user.id
    it.updated_by = user.id
    it.updated_at = datetime.utcnow()

    order = db.query(LisOrder).get(it.order_id)
    all_items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == it.order_id).all()
    if all(i.status in {"validated", "reported"} for i in all_items):
        order.status = "validated"
        order.updated_by = user.id
        order.updated_at = datetime.utcnow()

    db.commit()

    # Optional billing hook — never crash API if service missing
    try:
        from app.services.billing_auto import auto_add_item_for_event
        auto_add_item_for_event(db,
                                service_type="lab",
                                ref_id=it.id,
                                patient_id=order.patient_id,
                                context_type=order.context_type,
                                context_id=order.context_id,
                                user_id=user.id)
        db.commit()
    except Exception:
        pass

    await _notify("validated", order_id=order.id, item_id=it.id)
    return {"message": "Item validated", "order_id": order.id}


# ------------------- Final Report -------------------
@router.post("/orders/{order_id}/finalize")
async def finalize_report(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.report"])
    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order.id).all()
    if not items or not all(i.status in {"validated", "reported"}
                            for i in items):
        raise HTTPException(400,
                            "All items must be validated before final report")

    now = datetime.utcnow()
    for i in items:
        i.status = "reported"
        i.reported_by = user.id
        i.updated_by = user.id
        i.updated_at = now

    order.status = "reported"
    order.reported_at = now
    order.updated_by = user.id
    order.updated_at = now

    db.commit()
    await _notify("reported", order_id=order.id)
    return {"message": "Final report ready", "order_id": order.id}


# ------------------- Attachments (link-style) -------------------
@router.post("/attachments")
def add_attachment(
        payload: LisAttachmentIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.attachments.add"])
    it = db.query(LisOrderItem).get(payload.item_id)
    if not it:
        raise HTTPException(404, "Item not found")
    att = LisAttachment(
        order_item_id=it.id,
        file_url=payload.file_url,
        note=payload.note or "",
        created_by=user.id,
    )
    db.add(att)
    db.commit()
    return {"id": att.id, "message": "Attachment added"}
