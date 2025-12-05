# FILE: app/api/routes_lis.py
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Dict, Any
from reportlab.lib import colors  # already imported above

from sqlalchemy.exc import IntegrityError
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.opd import LabTest
from app.models.lis import (
    LisOrder,
    LisOrderItem,
    LisAttachment,
    LabDepartment,
    LabService,
    LisResultLine,
)
from app.models.patient import Patient
from app.schemas.lis import (
    LisOrderCreate,
    LisCollectIn,
    LisResultIn,
    LisAttachmentIn,
    LisOrderOut,
    LisOrderItemOut,
    LisPanelResultSaveIn,
    LisResultLineOut,
    LabReportOut,
    LabReportSectionOut,
    LabReportRowOut,
)

from io import BytesIO
from fastapi.responses import StreamingResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from pathlib import Path
import logging

from app.core.config import settings
from app.services.ui_branding import get_ui_branding

router = APIRouter()
logger = logging.getLogger(__name__)


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
    await _WSManager().connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws.disconnect(websocket)


async def _notify(kind: str, **data):
    await _ws.broadcast({"type": f"lab.{kind}", **data})


def _draw_letterhead_background(
    c: canvas.Canvas,
    branding,
    page_num: int = 1,
) -> None:
    """
    Draw full-page letterhead background if an image letterhead is configured
    and position is appropriate.

    Uses branding.letterhead_position:
    - "background": all pages
    - "first_page_only": only first page
    - "none" or empty: do not draw background
    """
    if not branding or not getattr(branding, "letterhead_path", None):
        return

    position = getattr(branding, "letterhead_position",
                       "background") or "background"

    # Respect position flag
    if position == "none":
        return
    if position == "first_page_only" and page_num != 1:
        return

    # Only support image type for full-page background
    if getattr(branding, "letterhead_type", None) not in {"image", None}:
        return

    full_path = Path(settings.STORAGE_DIR).joinpath(branding.letterhead_path)
    if not full_path.exists():
        logger.warning("Letterhead file not found: %s", full_path)
        return

    try:
        img = ImageReader(str(full_path))
        width, height = A4
        c.drawImage(
            img,
            0,
            0,
            width=width,
            height=height,
            preserveAspectRatio=True,
            mask="auto",
        )
    except Exception:
        logger.exception("Failed to draw letterhead background")
        return


# ------------------- Lab Masters (tests) -------------------
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
        "page_size": page_size,
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
    return {"id": order.id, "message": "LIS order created"}


# ---------------- LIST ORDERS ----------------
@router.get("/orders", response_model=list[LisOrderOut])
def list_orders(
        status: str | None = None,
        patient_id: int | None = None,
        db: Session = Depends(get_db),
        user=Depends(current_user),
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
        items = db.query(LisOrderItem).filter(
            LisOrderItem.order_id == o.id).all()
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
                    ) for i in items
                ],
            ))
    return out


# ------------- GET SINGLE ORDER ---------------
@router.get("/orders/{order_id}", response_model=LisOrderOut)
def get_order(
        order_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["lab.orders.view", "orders.lab.view"])

    o = db.query(LisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    items = db.query(LisOrderItem).filter(
        LisOrderItem.order_id == order_id).all()

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
            ) for i in items
        ],
    )


# ------------------- Panel Services -------------------
@router.get("/orders/{order_id}/panel", response_model=List[LisResultLineOut])
def get_order_panel_services(
        order_id: int,
        department_id: int = Query(..., description="Top-level department ID"),
        sub_department_id: Optional[int] = Query(
            None, description="Sub-department ID (panel)"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.enter", "lab.orders.view"])

    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # which department owns services
    leaf_dept_id = sub_department_id or department_id

    svc_q = (db.query(LabService).filter(
        LabService.department_id == leaf_dept_id).filter(
            LabService.is_active.is_(True)).order_by(
                LabService.display_order.asc(), LabService.name.asc()))
    services = svc_q.all()

    existing = (db.query(LisResultLine).filter(
        LisResultLine.order_id == order_id).filter(
            LisResultLine.service_id.in_([s.id for s in services]
                                         or [0])).all())
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

        out.append(
            LisResultLineOut(
                id=saved.id if saved else None,
                order_id=order_id,
                service_id=svc.id,
                department_id=main_dept_id,
                department_name=main_dept_name,
                sub_department_id=sub_dept_id,
                sub_department_name=sub_dept_name,
                service_name=saved.service_name if saved else svc.name,
                unit=saved.unit if saved else (svc.unit or "-"),
                normal_range=saved.normal_range if saved else
                (svc.normal_range or "-"),
                result_value=saved.result_value if saved else None,
                flag=saved.flag if saved else None,
                comments=saved.comments if saved else None,
            ))

    return out


@router.post("/orders/{order_id}/panel/results")
async def save_panel_results(
        order_id: int,
        payload: LisPanelResultSaveIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.enter"])

    # --- DEBUG 1: which DB am I connected to? ---
    try:
        current_db = db.execute(text("SELECT DATABASE()")).scalar()
        logger.info(
            "[LIS] save_panel_results using DB=%s order_id=%s",
            current_db,
            order_id,
        )
    except Exception:
        logger.exception("[LIS] failed to read current DB")

    order = db.query(LisOrder).get(order_id)
    if not order:
        logger.error("[LIS] order not found in ORM for id=%s", order_id)
        raise HTTPException(
            status_code=404,
            detail=f"Order not found in this DB: {order_id}",
        )

    logger.info(
        "[LIS] ORM found order row: id=%s patient_id=%s status=%s",
        order.id,
        order.patient_id,
        order.status,
    )

    service_ids = [r.service_id for r in payload.results]
    if not service_ids:
        raise HTTPException(status_code=400, detail="No results provided")

    services = db.query(LabService).filter(
        LabService.id.in_(service_ids)).all()
    svc_map = {s.id: s for s in services}

    now = datetime.utcnow()
    saved_count = 0

    for row in payload.results:
        svc = svc_map.get(row.service_id)
        if not svc:
            raise HTTPException(
                status_code=404,
                detail=f"LabService not found: {row.service_id}",
            )

        dept = svc.department
        if dept and dept.parent_id:
            main_dept = db.query(LabDepartment).get(dept.parent_id)
            main_dept_id = main_dept.id if main_dept else dept.id
            sub_dept_id = dept.id
        else:
            main_dept_id = dept.id if dept else payload.department_id
            sub_dept_id = payload.sub_department_id

        existing: Optional[LisResultLine] = (db.query(LisResultLine).filter(
            LisResultLine.order_id == order_id).filter(
                LisResultLine.service_id == svc.id).first())

        if not existing:
            existing = LisResultLine(
                order_id=order_id,
                service_id=svc.id,
                department_id=main_dept_id,
                sub_department_id=sub_dept_id,
                service_name=svc.name,
                unit=svc.unit or "-",
                normal_range=svc.normal_range or "-",
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

    # --- DEBUG 2: ensure parent truly exists in SAME DB ---
    try:
        cnt = db.execute(
            text("SELECT COUNT(*) FROM lis_orders WHERE id = :oid"),
            {
                "oid": order_id
            },
        ).scalar()
        logger.info(
            "[LIS] lis_orders count with id=%s in this DB = %s",
            order_id,
            cnt,
        )
    except Exception:
        logger.exception("[LIS] failed to check lis_orders row before commit")

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.exception("[LIS] FK error while saving LIS panel results")
        raise HTTPException(
            status_code=400,
            detail=("Cannot save results: LIS order not found "
                    "or data is inconsistent."),
        )

    await _notify(
        "panel_results_saved",
        order_id=order_id,
        department_id=payload.department_id,
        sub_department_id=payload.sub_department_id,
    )
    return {"message": "Panel results saved", "count": saved_count}


# ------------------- Structured report data -------------------
# ------------------- Structured report data -------------------
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

    lines: List[LisResultLine] = (db.query(LisResultLine).filter(
        LisResultLine.order_id == order_id).order_by(
            LisResultLine.department_id.asc(),
            LisResultLine.sub_department_id.asc(),
            LisResultLine.id.asc(),
        ).all())

    sections_map: Dict[tuple[int, Optional[int]], LabReportSectionOut] = {}

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
            if (line.service and line.service.department
                    and line.service.department.parent_id):
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

        sections_map[key].rows.append(
            LabReportRowOut(
                service_name=line.service_name,
                result_value=line.result_value,
                unit=line.unit,
                normal_range=line.normal_range,
                flag=line.flag,
                comments=line.comments,
            ))

    sections = list(sections_map.values())
    sections.sort(
        key=lambda s: (s.department_id or 0, s.sub_department_id or 0))

    # Age text
    age_text = None
    if patient and patient.dob:
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
        patient_name=getattr(patient, "full_name", None)
        or getattr(patient, "first_name", None),
        patient_gender=getattr(patient, "gender", None),
        patient_dob=patient.dob if patient and patient.dob else None,
        patient_age_text=age_text,
        patient_type=order.context_type.upper()
        if order.context_type else None,
        bill_no=None,
        received_on=order.collected_at,
        reported_on=order.reported_at,
        referred_by=None,
        sections=sections,
    )


def _format_lab_no(raw: Any) -> str:
    """
    Convert internal order/lab id into a friendly LAB-000123 style code.
    No raw DB ids in the visible PDF.
    """
    if raw is None:
        return "-"
    try:
        num = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    return f"LAB-{num:06d}"


def _format_dt(value: Any) -> str:
    """
    Consistent human-readable datetime for PDF (dd-MM-YYYY HH:MM).
    """
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return str(value)


# ------------------- Letterhead PDF -------------------
# ------------------- Letterhead PDF -------------------
@router.get("/orders/{order_id}/report-pdf")
def get_lab_report_pdf(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["lab.results.report", "lab.orders.view"])

    # Fresh order + patient (for collected_at / collected_by / prefix)
    order = db.query(LisOrder).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    patient: Optional[Patient] = db.query(Patient).get(order.patient_id)

    # Structured data (sections, age text etc.)
    report = get_lab_report_data(order_id=order_id, db=db, user=user)
    branding = get_ui_branding(db)

    # Optional: sample collected_by name (if you add LisOrder.collected_by FK)
    collected_by_name = None
    collected_by_id = getattr(order, "collected_by", None)
    if collected_by_id:
        staff = db.query(User).get(collected_by_id)
        if staff:
            collected_by_name = getattr(staff, "full_name", None) or getattr(
                staff, "first_name", None)

    # Helper: nice datetime formatting
    def fmt_datetime(v) -> str:
        if not v:
            return "-"
        try:
            if isinstance(v, datetime):
                dt = v
            else:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt.strftime("%d-%b-%Y %I:%M %p")
        except Exception:
            return str(v)

    # Build display patient name: "Prefix. First Last (Age / Sex)"
    prefix = getattr(patient, "title", None) or getattr(
        patient, "prefix", None)
    first = getattr(patient, "first_name", None)
    last = getattr(patient, "last_name", None)

    base_name = report.patient_name or " ".join(
        [p for p in [first, last] if p]).strip()
    if prefix:
        prefix_clean = prefix.strip().rstrip(".")
        display_name = f"{prefix_clean}. {base_name}" if base_name else f"{prefix_clean}."
    else:
        display_name = base_name or "-"

    age_gender_parts = []
    if report.patient_age_text:
        age_gender_parts.append(report.patient_age_text)
    if report.patient_gender:
        age_gender_parts.append(report.patient_gender)
    age_gender_text = " / ".join(
        age_gender_parts) if age_gender_parts else None

    # Lab Order Number (no raw id text)
    lab_order_no = f"LAB-{report.order_id:06d}"

    # Sample collected
    sample_collected_at = getattr(order, "collected_at",
                                  None) or report.received_on

    # PDF SETUP
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # Margins – extra room for footer so nothing overlaps
    left_margin = 18 * mm
    right_margin = 18 * mm
    bottom_margin = 30 * mm  # <- bigger footer space

    # Column positions
    col_investigation = left_margin + 2 * mm
    col_result = left_margin + 70 * mm
    col_ref = left_margin + 120 * mm
    col_unit = left_margin + 165 * mm

    # Colors
    PRIMARY = colors.HexColor("#0060B6")
    TABLE_HEADER_BG = colors.HexColor("#F3F4F6")
    ROW_ALT_BG = colors.HexColor("#FAFAFA")
    TEXT_MUTED = colors.HexColor("#4B5563")
    HIGH_COLOR = colors.HexColor("#DC2626")
    LOW_COLOR = colors.HexColor("#2563EB")
    NORMAL_COLOR = colors.HexColor("#16A34A")

    # Letterhead height reservation
    header_height_mm = getattr(branding, "pdf_header_height_mm", None) or 35
    header_h = header_height_mm * mm

    def has_letterhead_image() -> bool:
        return bool(branding
                    and getattr(branding, "letterhead_type", None) == "image"
                    and getattr(branding, "letterhead_path", None)
                    and getattr(branding, "letterhead_position",
                                "background") != "none")

    # Footer ("Generated on" + page X)
    generated_on = fmt_datetime(report.reported_on or datetime.utcnow())

    def draw_footer(page_no: int):
        c.setFont("Helvetica", 7)
        c.setFillColor(TEXT_MUTED)
        footer_y = bottom_margin - 8 * mm
        c.drawString(left_margin, footer_y, f"Generated on: {generated_on}")
        if getattr(branding, "pdf_show_page_number", False):
            c.drawRightString(
                width - right_margin,
                footer_y,
                f"Page {page_no}",
            )

    page_num = 1

    def start_page(page_no: int) -> float:
        """
        Draw letterhead + patient card (only on first page).
        Returns starting y for content.
        """
        # Letterhead background (image)
        _draw_letterhead_background(c, branding, page_no)

        y = height

        # Reserve letterhead area
        y -= header_h

        # PATIENT CARD – only on first page
        if page_no == 1:
            y -= 6 * mm
            box_top = y
            box_height = 28 * mm
            box_bottom = box_top - box_height

            # Card
            c.setFillColor(colors.white)
            c.setStrokeColor(colors.HexColor("#E5E7EB"))
            c.setLineWidth(0.7)
            c.roundRect(
                left_margin,
                box_bottom,
                width - left_margin - right_margin,
                box_height,
                4 * mm,
                stroke=1,
                fill=1,
            )

            # Left block – patient details
            px = left_margin + 5 * mm
            py = box_top - 5 * mm

            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(px, py, display_name)
            py -= 4 * mm

            c.setFont("Helvetica", 8)
            c.setFillColor(TEXT_MUTED)
            if age_gender_text:
                c.drawString(px, py, f"({age_gender_text})")
                py -= 4 * mm

            if report.patient_uhid:
                c.drawString(px, py, f"UHID: {report.patient_uhid}")
                py -= 4 * mm

            # Right block – order/sample details
            rx = width / 2 + 5 * mm
            ry = box_top - 5 * mm

            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(colors.black)
            c.drawString(rx, ry, "Sample / Order Details")
            ry -= 4 * mm

            c.setFont("Helvetica", 8)
            c.setFillColor(TEXT_MUTED)
            if report.patient_type:
                c.drawString(rx, ry, f"Type: {report.patient_type}")
                ry -= 4 * mm

            c.drawString(rx, ry, f"Lab Order No: {lab_order_no}")
            ry -= 4 * mm

            c.drawString(
                rx,
                ry,
                f"Sample Collected: {fmt_datetime(sample_collected_at)}",
            )
            ry -= 4 * mm

            c.drawString(
                rx,
                ry,
                f"Sample Collected By: {collected_by_name or '-'}",
            )
            ry -= 4 * mm

            c.drawString(
                rx,
                ry,
                f"Reported On: {fmt_datetime(report.reported_on)}",
            )

            y = box_bottom - 10 * mm
        else:
            # Small gap below header image for inner pages
            y -= 8 * mm

        # Report title
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(PRIMARY)
        c.drawCentredString(width / 2, y, "LABORATORY REPORT")
        y -= 10 * mm

        return y

    # Start first page
    current_y = start_page(page_num)

    def ensure_space(min_space_mm: float):
        """
        If not enough space for the next block, draw footer and start a new page.
        """
        nonlocal current_y, page_num
        if current_y < bottom_margin + (min_space_mm * mm):
            draw_footer(page_num)
            c.showPage()
            page_num += 1
            current_y = start_page(page_num)

    # ---------- TABLE SECTIONS ----------
    for section in report.sections:
        ensure_space(35.0)

        dept_label = section.department_name or "Department"
        sub_label = (f" / {section.sub_department_name}"
                     if section.sub_department_name else "")

        # Section title bar
        c.setFillColor(colors.HexColor("#E5F2FF"))
        c.rect(
            left_margin,
            current_y - 7 * mm,
            width - left_margin - right_margin,
            7 * mm,
            stroke=0,
            fill=1,
        )
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(PRIMARY)
        c.drawString(
            left_margin + 2 * mm,
            current_y - 5 * mm,
            f"{dept_label}{sub_label}",
        )
        current_y -= 9 * mm

        # Table header
        c.setFillColor(TABLE_HEADER_BG)
        c.rect(
            left_margin,
            current_y - 6 * mm,
            width - left_margin - right_margin,
            6 * mm,
            stroke=0,
            fill=1,
        )
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(colors.black)
        c.drawString(col_investigation, current_y - 4 * mm, "Investigation")
        c.drawString(col_result, current_y - 4 * mm, "Result")
        c.drawString(col_ref, current_y - 4 * mm, "Reference Value")
        c.drawString(col_unit, current_y - 4 * mm, "Unit")
        current_y -= 8 * mm

        # Rows
        row_idx = 0
        c.setFont("Helvetica", 8)
        for row in section.rows:
            ensure_space(20.0)
            row_height = 6 * mm
            row_bottom = current_y - row_height

            # Alternate row background
            if row_idx % 2 == 1:
                c.setFillColor(ROW_ALT_BG)
                c.rect(
                    left_margin,
                    row_bottom,
                    width - left_margin - right_margin,
                    row_height,
                    stroke=0,
                    fill=1,
                )

            # Investigation
            c.setFillColor(colors.black)
            c.drawString(col_investigation, current_y - 4 * mm,
                         row.service_name or "-")

            # Result + colored flag
            result_val = row.result_value if row.result_value not in (
                None, "") else "-"
            flag_text = ""
            flag_color = None
            if row.flag:
                f = str(row.flag).upper()
                if f.startswith("H"):
                    flag_text = "High"
                    flag_color = HIGH_COLOR
                elif f.startswith("L"):
                    flag_text = "Low"
                    flag_color = LOW_COLOR
                elif f.startswith("N"):
                    flag_text = "Normal"
                    flag_color = NORMAL_COLOR

            if flag_color:
                c.setFillColor(flag_color)
            else:
                c.setFillColor(colors.black)
            c.drawString(col_result, current_y - 4 * mm, str(result_val))

            if flag_text:
                c.setFont("Helvetica-Bold", 8)
                c.drawString(col_result + 28 * mm, current_y - 4 * mm,
                             flag_text)
                c.setFont("Helvetica", 8)

            # Ref range + unit
            c.setFillColor(TEXT_MUTED)
            c.drawString(col_ref, current_y - 4 * mm, row.normal_range or "-")
            c.drawString(col_unit, current_y - 4 * mm, row.unit or "-")

            current_y -= row_height
            row_idx += 1

        current_y -= 5 * mm  # gap after section

    # ---------- NOTES + SIGNATURES ON LAST PAGE ----------
    ensure_space(45.0)

    # Notes
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.black)
    c.drawString(left_margin, current_y, "Note:")
    current_y -= 4 * mm

    c.setFont("Helvetica", 7)
    c.setFillColor(TEXT_MUTED)
    notes = [
        "1. Laboratory values should be correlated with clinical findings.",
        "2. Marginally abnormal values may not be clinically significant in all patients.",
    ]
    for line in notes:
        c.drawString(left_margin + 4 * mm, current_y, line)
        current_y -= 4 * mm

    current_y -= 10 * mm

    # Signatures above footer area
    sig_y = bottom_margin + 22 * mm

    c.setFont("Helvetica", 8)
    c.setFillColor(TEXT_MUTED)

    # Technician
    c.line(left_margin, sig_y, left_margin + 50 * mm, sig_y)
    c.drawCentredString(
        left_margin + 25 * mm,
        sig_y - 4 * mm,
        "Lab Technician",
    )

    # Pathologist / Authorized Signatory
    rx = width - right_margin - 50 * mm
    c.line(rx, sig_y, rx + 50 * mm, sig_y)
    c.drawCentredString(
        rx + 25 * mm,
        sig_y - 4 * mm,
        "Pathologist / Authorized Signatory",
    )

    # Final footer for last page
    draw_footer(page_num)

    c.save()
    buf.seek(0)

    headers = {
        "Content-Disposition":
        f'inline; filename=\"lab-report-{order_id}.pdf\"',
    }
    return StreamingResponse(buf,
                             media_type="application/pdf",
                             headers=headers)


# ------------------- Queue -------------------
@router.get("/queue")
def queue(
        status: str = Query(
            "in_progress",
            pattern="^(ordered|collected|in_progress|validated)$",
        ),
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


# ---------------- SAMPLE COLLECTION ----------------
@router.post("/orders/{order_id}/collect")
def collect_sample(
        order_id: int,
        payload: dict,
        db: Session = Depends(get_db),
):
    barcode = payload.get("barcode")
    if not barcode:
        raise HTTPException(400, "Barcode required")

    o = db.query(LisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    o.collected_at = datetime.now()
    o.status = "collected"
    db.commit()
    return {"message": "Sample collected"}


# ---------------- ENTER RESULTS ----------------
@router.post("/orders/{order_id}/results")
def save_results(
        order_id: int,
        results: list[dict],
        db: Session = Depends(get_db),
):
    for r in results:
        it = db.query(LisOrderItem).get(r["item_id"])
        if not it:
            continue
        it.result_value = r["result_value"]
        it.is_critical = r.get("is_critical", False)
        it.status = "in_progress"

    db.commit()
    return {"message": "Saved"}


# ---------------- VALIDATE ITEM ----------------
@router.post("/items/{item_id}/validate")
def validate_item(
        item_id: int,
        db: Session = Depends(get_db),
):
    item = db.query(LisOrderItem).get(item_id)
    if not item:
        raise HTTPException(404, "Item not found")

    item.status = "validated"
    db.commit()
    return {"message": "Validated"}


# ---------------- FINALIZE ORDER ----------------
@router.post("/orders/{order_id}/finalize")
def finalize(order_id: int, db: Session = Depends(get_db)):
    o = db.query(LisOrder).get(order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    o.status = "reported"
    o.reported_at = datetime.now()
    db.commit()
    return {"message": "Finalized"}


# ------------------- Attachments -------------------
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
