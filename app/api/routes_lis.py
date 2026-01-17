# FILE: app/api/routes_lis.py
from __future__ import annotations

import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.core.config import settings
from app.models.ipd import IpdAdmission
from app.models.lis import LabDepartment, LabService, LisAttachment, LisOrder, LisOrderItem, LisResultLine
from app.models.opd import LabTest, Visit
from app.models.patient import Patient
from app.models.user import User
from app.schemas.lis import (
    LabReportOut,
    LabReportRowOut,
    LabReportSectionOut,
    LisAttachmentIn,
    LisOrderCreate,
    LisOrderItemOut,
    LisOrderOut,
    LisPanelResultSaveIn,
    LisResultLineOut,
)

from app.services.ui_branding import get_ui_branding
# ✅ IMPORTANT: use pdf_lis_report (safe import; weasy is inside try)
from app.services.pdf_lab_report_weasy import build_lab_report_pdf_bytes, _lab_report_pdf_url

from app.services.billing_hooks import autobill_lis_order
from app.services.billing_service import BillingError

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------- Permission helpers ----------------
def _need_any(user: User, codes: List[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) in codes:
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
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws.disconnect(websocket)


async def _notify(kind: str, **data):
    await _ws.broadcast({"type": f"lab.{kind}", **data})


# ---------------- Reference range formatting (DISPLAY ONLY) ----------------
def format_reference_ranges_display(ranges: list[dict] | None) -> str:
    """
    Multi-line display string for UI/PDF.
    IMPORTANT: This is NOT stored in DB (avoid 255-limit issues).
    """
    if not ranges:
        return "-"

    groups: dict[str, list[dict]] = {"F": [], "M": [], "ANY": []}
    for r in ranges:
        sex = (r.get("sex") or "ANY").upper().strip()
        if sex not in groups:
            sex = "ANY"
        groups[sex].append(r)

    out: list[str] = []

    def val_text(r: dict) -> str:
        low = (r.get("low") or "").strip()
        high = (r.get("high") or "").strip()
        textv = (r.get("text") or "").strip()
        if textv:
            return textv
        if low and high:
            return f"{low}-{high}"
        if low:
            return f">= {low}"
        if high:
            return f"<= {high}"
        return "-"

    def age_text(r: dict) -> str:
        age_min = r.get("age_min")
        age_max = r.get("age_max")
        age_unit = (r.get("age_unit") or "Y").strip()
        if age_min is None and age_max is None:
            return ""
        a = "" if age_min is None else str(age_min)
        b = "" if age_max is None else str(age_max)
        return f" ({a}-{b}{age_unit})"

    def add_group(title: str, items: list[dict], add_heading: bool):
        if not items:
            return
        if add_heading:
            out.append(title)
        for r in items:
            label = (r.get("label") or "Range").strip()
            out.append(f"{label}{age_text(r)}: {val_text(r)}")

    has_sex = bool(groups["F"] or groups["M"])
    if groups["F"]:
        add_group("WOMEN", groups["F"], add_heading=True)
    if groups["M"]:
        add_group("MEN", groups["M"], add_heading=True)
    if groups["ANY"]:
        add_group("GENERAL" if has_sex else "", groups["ANY"], add_heading=has_sex)

    out = [x for x in out if x.strip()]
    return "\n".join(out) if out else "-"


def _split_text_to_lines(txt: str, font_name: str, font_size: float, max_w: float) -> list[str]:
    """
    ReportLab wrap with newline support.
    """
    t = (txt or "-").replace("\r\n", "\n").strip()
    if not t:
        return ["-"]

    out: list[str] = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            continue
        words = para.split(" ")
        line = ""
        for w in words:
            trial = (line + " " + w).strip()
            if stringWidth(trial, font_name, font_size) <= max_w:
                line = trial
                continue
            if line:
                out.append(line)
                line = ""
            if stringWidth(w, font_name, font_size) <= max_w:
                line = w
            else:
                chunk = ""
                for ch in w:
                    trial2 = chunk + ch
                    if stringWidth(trial2, font_name, font_size) <= max_w:
                        chunk = trial2
                    else:
                        if chunk:
                            out.append(chunk)
                        chunk = ch
                line = chunk
        if line:
            out.append(line)

    return out if out else ["-"]


# ---------------- Letterhead background (optional) ----------------
def _draw_letterhead_background(c: canvas.Canvas, branding: Any, page_num: int = 1) -> None:
    if not branding or not getattr(branding, "letterhead_path", None):
        return

    position = getattr(branding, "letterhead_position", "background") or "background"
    if position == "none":
        return
    if position == "first_page_only" and page_num != 1:
        return

    if getattr(branding, "letterhead_type", None) not in {"image", None}:
        return

    full_path = Path(settings.STORAGE_DIR).joinpath(branding.letterhead_path)
    if not full_path.exists():
        return

    try:
        img = ImageReader(str(full_path))
        w, h = A4
        c.drawImage(img, 0, 0, width=w, height=h, preserveAspectRatio=True, mask="auto")
    except Exception:
        logger.exception("Failed to draw letterhead background")


# ---------------- Orders ----------------
@router.post("/orders")
def create_order(payload: LisOrderCreate, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["orders.lab.create", "lab.orders.create"])
    if not payload.items:
        raise HTTPException(422, "At least one test is required")

    ctx_type = payload.context_type
    if not ctx_type and payload.context_id:
        ctx_type = _infer_context_type(db, payload.context_id)

    order = LisOrder(
        patient_id=payload.patient_id,
        context_type=ctx_type,
        context_id=payload.context_id,
        ordering_user_id=payload.ordering_user_id or user.id,
        priority=payload.priority or "routine",
        status="ordered",
        created_by=user.id,
        billing_status="not_billed",
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
                test_name=m.name,
                test_code=m.code,
                status="ordered",
                created_by=user.id,
            )
        )

    db.commit()
    return {"id": order.id, "context_type": order.context_type, "message": "LIS order created"}


@router.get("/orders", response_model=list[LisOrderOut])
def list_orders(
    status: str | None = None,
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])
    q = db.query(LisOrder)
    if status:
        q = q.filter(LisOrder.status == status)
    if patient_id:
        q = q.filter(LisOrder.patient_id == patient_id)

    orders = q.order_by(LisOrder.id.desc()).all()
    out: List[LisOrderOut] = []
    for o in orders:
        items = db.query(LisOrderItem).filter(LisOrderItem.order_id == o.id).all()
        out.append(
            LisOrderOut(
                id=o.id,
                patient_id=o.patient_id,
                context_type=o.context_type,
                context_id=o.context_id,
                priority=o.priority,
                status=o.status,
                collected_at=o.collected_at,
                created_at=o.created_at,
                reported_at=o.reported_at,
                items=[
                    LisOrderItemOut(
                        id=i.id,
                        test_id=i.test_id,
                        test_name=i.test_name,
                        test_code=i.test_code,
                        status=i.status,
                        sample_barcode=i.sample_barcode,
                        result_value=i.result_value,
                        unit=i.unit,
                        normal_range=i.normal_range,
                        is_critical=bool(i.is_critical),
                        result_at=i.result_at,
                    )
                    for i in items
                ],
            )
        )
    return out


@router.get("/orders/{order_id}", response_model=LisOrderOut)
def get_order(order_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])
    o = db.query(LisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    items = db.query(LisOrderItem).filter(LisOrderItem.order_id == order_id).all()
    return LisOrderOut(
        id=o.id,
        patient_id=o.patient_id,
        context_type=o.context_type,
        context_id=o.context_id,
        priority=o.priority,
        status=o.status,
        collected_at=o.collected_at,
        created_at=o.created_at,
        reported_at=o.reported_at,
        items=[
            LisOrderItemOut(
                id=i.id,
                test_id=i.test_id,
                test_name=i.test_name,
                test_code=i.test_code,
                status=i.status,
                sample_barcode=i.sample_barcode,
                result_value=i.result_value,
                unit=i.unit,
                normal_range=i.normal_range,
                is_critical=bool(i.is_critical),
                result_at=i.result_at,
            )
            for i in items
        ],
    )


# ---------------- Panel services (Result entry rows) ----------------
@router.get("/orders/{order_id}/panel", response_model=List[LisResultLineOut])
def get_order_panel_services(
    order_id: int,
    department_id: int = Query(...),
    sub_department_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.enter", "lab.orders.view"])

    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    leaf_dept_id = sub_department_id or department_id

    services = (
        db.query(LabService)
        .filter(LabService.department_id == leaf_dept_id)
        .filter(LabService.is_active.is_(True))
        .order_by(LabService.display_order.asc(), LabService.name.asc())
        .all()
    )

    existing = (
        db.query(LisResultLine)
        .filter(LisResultLine.order_id == order_id)
        .filter(LisResultLine.service_id.in_([s.id for s in services] or [0]))
        .all()
    )
    existing_map = {r.service_id: r for r in existing}

    out: List[LisResultLineOut] = []

    for svc in services:
        saved = existing_map.get(svc.id)
        dept = svc.department

        if dept and dept.parent_id:
            main_dept = db.query(LabDepartment).get(dept.parent_id)
            main_dept_id = main_dept.id if main_dept else dept.id
            main_dept_name = main_dept.name if main_dept else dept.name
            sub_dept_id = dept.id
            sub_dept_name = dept.name
        else:
            main_dept_id = dept.id if dept else department_id
            main_dept_name = dept.name if dept else ""
            sub_dept_id = None
            sub_dept_name = None

        rr_display = (
            format_reference_ranges_display(getattr(svc, "reference_ranges", None))
            if getattr(svc, "reference_ranges", None)
            else (svc.normal_range or "-")
        )

        out.append(
            LisResultLineOut(
                id=saved.id if saved else None,
                order_id=order_id,
                service_id=svc.id,
                department_id=main_dept_id,
                department_name=main_dept_name,
                sub_department_id=sub_dept_id,
                sub_department_name=sub_dept_name,
                service_name=(saved.service_name if saved else svc.name),
                unit=(saved.unit if saved else (svc.unit or "-")),
                normal_range=rr_display,
                result_value=(saved.result_value if saved else None),
                flag=(saved.flag if saved else None),
                comments=(saved.comments if saved else None),
            )
        )

    return out


@router.post("/orders/{order_id}/panel/results")
async def save_panel_results(
    order_id: int,
    payload: LisPanelResultSaveIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.enter"])

    try:
        current_db = db.execute(text("SELECT DATABASE()")).scalar()
        logger.info("[LIS] save_panel_results DB=%s order_id=%s", current_db, order_id)
    except Exception:
        logger.exception("[LIS] failed to read current DB")

    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order not found in this DB: {order_id}")

    service_ids = [r.service_id for r in payload.results]
    if not service_ids:
        raise HTTPException(status_code=400, detail="No results provided")

    services = db.query(LabService).filter(LabService.id.in_(service_ids)).all()
    svc_map = {s.id: s for s in services}

    now = datetime.utcnow()
    saved_count = 0

    for row in payload.results:
        svc = svc_map.get(row.service_id)
        if not svc:
            raise HTTPException(status_code=404, detail=f"LabService not found: {row.service_id}")

        dept = svc.department
        if dept and dept.parent_id:
            main_dept = db.query(LabDepartment).get(dept.parent_id)
            main_dept_id = main_dept.id if main_dept else dept.id
            sub_dept_id = dept.id
        else:
            main_dept_id = dept.id if dept else payload.department_id
            sub_dept_id = payload.sub_department_id

        existing: Optional[LisResultLine] = (
            db.query(LisResultLine)
            .filter(LisResultLine.order_id == order_id)
            .filter(LisResultLine.service_id == svc.id)
            .first()
        )

        if not existing:
            existing = LisResultLine(
                order_id=order_id,
                service_id=svc.id,
                department_id=main_dept_id,
                sub_department_id=sub_dept_id,
                service_name=svc.name,
                unit=svc.unit or "-",
                normal_range=(svc.normal_range or "-"),
                entered_by=user.id,
                created_at=now,
            )
            db.add(existing)

        existing.result_value = row.result_value
        existing.flag = row.flag
        existing.comments = row.comments
        existing.updated_at = now
        saved_count += 1

    if order.status in {"ordered", "collected"}:
        order.status = "in_progress"
        order.updated_by = user.id
        order.updated_at = now

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Cannot save results: order not found or data inconsistent.")

    await _notify(
        "panel_results_saved",
        order_id=order_id,
        department_id=payload.department_id,
        sub_department_id=payload.sub_department_id,
    )
    return {"message": "Panel results saved", "count": saved_count}


# ---------------- Structured report data ----------------
@router.get("/orders/{order_id}/report-data", response_model=LabReportOut)
def get_lab_report_data(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.report", "lab.orders.view"])

    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    patient: Optional[Patient] = db.query(Patient).get(order.patient_id)

    lines: List[LisResultLine] = (
        db.query(LisResultLine)
        .filter(LisResultLine.order_id == order_id)
        .order_by(
            LisResultLine.department_id.asc(),
            LisResultLine.sub_department_id.asc(),
            LisResultLine.id.asc(),
        )
        .all()
    )

    sections_map: Dict[Tuple[int, Optional[int]], LabReportSectionOut] = {}

    for line in lines:
        dept_id = line.department_id or 0
        sub_id = line.sub_department_id

        dept_name = ""
        sub_name: Optional[str] = None

        if line.department:
            dept_name = line.department.name
        elif line.service and line.service.department:
            d = line.service.department
            if d.parent_id:
                parent = db.query(LabDepartment).get(d.parent_id)
                dept_name = parent.name if parent else d.name
            else:
                dept_name = d.name

        if line.sub_department:
            sub_name = line.sub_department.name
        else:
            if line.service and line.service.department and line.service.department.parent_id:
                sub_name = line.service.department.name

        key = (dept_id, sub_id)
        if key not in sections_map:
            sections_map[key] = LabReportSectionOut(
                department_id=dept_id,
                department_name=dept_name or "",
                sub_department_id=sub_id,
                sub_department_name=sub_name,
                rows=[],
            )

        svc_obj = getattr(line, "service", None)
        rr_display = None
        if svc_obj and getattr(svc_obj, "reference_ranges", None):
            rr_display = format_reference_ranges_display(svc_obj.reference_ranges)

        sections_map[key].rows.append(
            LabReportRowOut(
                service_name=line.service_name,
                result_value=line.result_value,
                unit=line.unit,
                normal_range=(rr_display or line.normal_range or "-"),
                flag=line.flag,
                comments=line.comments,
            )
        )

    sections = list(sections_map.values())
    sections.sort(key=lambda s: (s.department_id or 0, s.sub_department_id or 0))

    age_text = None
    if patient and getattr(patient, "dob", None):
        try:
            today = datetime.utcnow().date()
            years = today.year - patient.dob.year
            age_text = f"{years} Years"
        except Exception:
            age_text = None

    return LabReportOut(
        order_id=order.id,
        lab_no=str(order.id),
        patient_id=order.patient_id,
        patient_uhid=getattr(patient, "uhid", None),
        patient_name=getattr(patient, "full_name", None) or getattr(patient, "first_name", None),
        patient_gender=getattr(patient, "gender", None),
        patient_dob=patient.dob if patient and getattr(patient, "dob", None) else None,
        patient_age_text=age_text,
        patient_type=(order.context_type.upper() if order.context_type else None),
        bill_no=None,
        received_on=order.collected_at,
        reported_on=order.reported_at,
        referred_by=None,
        sections=sections,
    )


@router.get("/orders/{order_id}/report-pdf")
def get_lab_report_pdf(
    order_id: int,
    request: Request,
    download: int = Query(0),  # ✅ if download=1 -> attachment
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.report", "lab.orders.view"])

    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    patient: Optional[Patient] = db.query(Patient).get(order.patient_id)
    report = get_lab_report_data(order_id=order_id, db=db, user=user)
    branding = get_ui_branding(db)

    collected_by_name = None
    collected_by_id = getattr(order, "collected_by", None)
    if collected_by_id:
        staff = db.query(User).get(collected_by_id)
        if staff:
            collected_by_name = getattr(staff, "full_name", None) or getattr(staff, "first_name", None)

    lab_no = f"LAB-{int(report.order_id):06d}"
    order_date = getattr(order, "created_at", None)

    # ✅ QR must open direct download
    download_url_for_qr = _lab_report_pdf_url(request, order_id, download=True)

    pdf_bytes = build_lab_report_pdf_bytes(
        branding=branding,
        report=report,
        patient=patient,
        lab_no=lab_no,
        order_date=order_date,
        collected_by_name=collected_by_name,
        request=request,  # ✅ key
    )

    buf = BytesIO(pdf_bytes)
    buf.seek(0)

    disp = "attachment" if int(download or 0) == 1 else "inline"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disp}; filename="lab-report-{order_id}.pdf"'},
    )


# ---------------- Finalize ----------------
@router.post("/orders/{order_id}/finalize")
async def finalize(order_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["lab.results.report", "lab.orders.update", "orders.lab.update"])

    o = db.query(LisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    now = datetime.utcnow()
    o.status = "reported"
    o.reported_at = now
    o.updated_by = user.id
    o.updated_at = now

    try:
        if (getattr(o, "billing_status", None) or "not_billed") != "billed":
            res = autobill_lis_order(db, lis_order_id=o.id, user=user)
            o.billing_invoice_id = res.get("invoice_id")
            o.billing_status = "billed"
    except BillingError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Billing failed: {e}")

    db.commit()
    await _notify("finalized", order_id=o.id, billing_invoice_id=o.billing_invoice_id)

    return {"message": "Finalized", "billing_invoice_id": o.billing_invoice_id, "billing_status": o.billing_status}


# ---------------- Attachments ----------------
@router.post("/attachments")
def add_attachment(payload: LisAttachmentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
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
