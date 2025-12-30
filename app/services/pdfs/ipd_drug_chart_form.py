from __future__ import annotations

import io
import os
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sqlalchemy.orm import Session, joinedload

from app.models.ui_branding import UiBranding
from app.models.ipd import (
    IpdAdmission,
    IpdBed,
    IpdRoom,
    IpdIvFluidOrder,
    IpdMedicationOrder,
    IpdMedicationAdministration,
    IpdDrugChartNurseRow,
    IpdDrugChartDoctorAuth,
)

# Optional: your Patient model may be in different module
PatientModel = None
for mod in ("app.models.patients", "app.models.patient", "app.models.emr"):
    try:
        PatientModel = __import__(mod, fromlist=["Patient"]).Patient
        break
    except Exception:
        pass

INK = colors.HexColor("#1F2A44")
GRID = colors.HexColor("#2F3B5A")
MUTED = colors.HexColor("#6B7280")


# -------------------------
# Helpers
# -------------------------
def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()

def _fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M")
    return _s(v)

def _fmt_date(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d-%m-%Y")
    return _s(v)

def _fmt_time(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    return _s(v)

def _initials(name: str) -> str:
    parts = [p for p in _s(name).replace(".", " ").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

def _set_font(c: canvas.Canvas, name="Helvetica", size=9):
    c.setFont(name, size)

def _draw_box(c: canvas.Canvas, x, y, w, h, lw=0.8, stroke=GRID):
    c.setLineWidth(lw)
    c.setStrokeColor(stroke)
    c.rect(x, y, w, h, stroke=1, fill=0)

def _draw_hline(c: canvas.Canvas, x1, x2, y, lw=0.6, stroke=GRID):
    c.setLineWidth(lw)
    c.setStrokeColor(stroke)
    c.line(x1, y, x2, y)

def _draw_vline(c: canvas.Canvas, x, y1, y2, lw=0.6, stroke=GRID):
    c.setLineWidth(lw)
    c.setStrokeColor(stroke)
    c.line(x, y1, x, y2)

def _draw_label(c: canvas.Canvas, x, y, text, size=8, bold=False, color=INK):
    c.setFillColor(color)
    _set_font(c, "Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, text)

def _draw_center(c: canvas.Canvas, x, y, w, text, size=9, bold=True, color=INK):
    c.setFillColor(color)
    _set_font(c, "Helvetica-Bold" if bold else "Helvetica", size)
    c.drawCentredString(x + w / 2, y, text)

def _grid_cols(c: canvas.Canvas, x, y, col_ws, rows, h, lw=0.5):
    w = sum(col_ws)
    _draw_box(c, x, y, w, h, lw=0.8)
    row_h = h / rows
    cx = x
    for w_i in col_ws[:-1]:
        cx += w_i
        _draw_vline(c, cx, y, y + h, lw=lw)
    for j in range(1, rows):
        _draw_hline(c, x, x + w, y + j * row_h, lw=lw)

def _cell_xy_cols(x: float, y: float, col_ws: List[float], rows: int, h: float, row_top_idx: int, col_idx: int):
    row_h = h / rows
    x0 = x + sum(col_ws[:col_idx])
    y0 = y + h - (row_top_idx + 1) * row_h
    return x0, y0, col_ws[col_idx], row_h

def _write_cell(c: canvas.Canvas, x0, y0, w, h, text: str, size=7.2, bold=False, color=INK, pad=1.5*mm):
    t = _s(text)
    if not t:
        return
    c.setFillColor(color)
    _set_font(c, "Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x0 + pad, y0 + (h/2 - size*0.30), t[:80])

def _draw_logo(c: canvas.Canvas, x, y, w, h, branding: Optional[UiBranding], ctx: Dict[str, Any]):
    """
    Supports local path stored in branding (common in HIS setups).
    If you store URL, download it separately and store path OR extend this function.
    """
    logo_path = _s(getattr(branding, "org_logo_path", "")) or _s(ctx.get("org_logo_path"))
    if not logo_path:
        return
    if not os.path.exists(logo_path):
        return
    try:
        img = ImageReader(logo_path)
        c.drawImage(img, x, y, w, h, preserveAspectRatio=True, mask="auto", anchor="c")
    except Exception:
        pass


# -------------------------
# Fetch data from DB
# -------------------------
def _get_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).order_by(UiBranding.id.asc()).first()

def _get_patient(db: Session, adm: IpdAdmission):
    if hasattr(adm, "patient") and adm.patient is not None:
        return adm.patient
    if PatientModel is None:
        return None
    return db.query(PatientModel).filter(PatientModel.id == adm.patient_id).first()

def _load_admission(db: Session, admission_id: int) -> IpdAdmission:
    q = (
        db.query(IpdAdmission)
        .options(
            joinedload(IpdAdmission.current_bed)
            .joinedload(IpdBed.room)
            .joinedload(IpdRoom.ward),
            joinedload(IpdAdmission.drug_chart_meta),
            joinedload(IpdAdmission.iv_fluid_orders),
            joinedload(IpdAdmission.medication_orders),
            joinedload(IpdAdmission.medication_administrations),
            joinedload(IpdAdmission.drug_chart_nurse_rows),
            joinedload(IpdAdmission.doctor_auth_rows),
        )
        .filter(IpdAdmission.id == admission_id)
    )
    adm = q.first()
    if not adm:
        raise ValueError("Admission not found")
    return adm


# -------------------------
# Page-1 (PORTRAIT A4)
# -------------------------
def _page1(c: canvas.Canvas, branding: Optional[UiBranding], ctx: Dict[str, Any]):
    W, H = A4
    m = 10 * mm
    gap = 6 * mm
    x = m
    y = H - m
    full_w = W - 2 * m

    # ===== Header (Full width) =====
    header_h = 26 * mm
    y -= header_h
    _draw_box(c, x, y, full_w, header_h, lw=0.9)

    # logo box (left)
    logo_box = 18 * mm
    _draw_box(c, x + 2*mm, y + (header_h - logo_box)/2, logo_box, logo_box, lw=0.6)
    _draw_logo(c, x + 2*mm, y + (header_h - logo_box)/2, logo_box, logo_box, branding, ctx)

    org = _s(getattr(branding, "org_name", "")) or _s(ctx.get("org_name")) or "HOSPITAL / CLINIC NAME"
    tag = _s(getattr(branding, "org_tagline", "")) or _s(ctx.get("org_tagline"))
    addr = _s(getattr(branding, "org_address", "")) or _s(ctx.get("org_address"))
    phone = _s(getattr(branding, "org_phone", "")) or _s(ctx.get("org_phone"))
    website = _s(getattr(branding, "org_website", "")) or _s(ctx.get("org_website"))

    # Center text
    _draw_center(c, x, y + header_h - 8*mm, full_w, org, 12, True, INK)
    if tag:
        _draw_center(c, x, y + header_h - 13*mm, full_w, tag, 8, True, MUTED)

    line = "  |  ".join([t for t in [addr, phone, website] if t])
    if line:
        _draw_center(c, x, y + 4.5*mm, full_w, line[:160], 7.2, False, MUTED)

    # Right title
    _draw_label(c, x + full_w - 45*mm, y + header_h - 12*mm, "DRUG CHART", 12, True, INK)

    y -= 5 * mm

    # ===== Patient Identity (Full width, 2 columns) =====
    pid_h = 28 * mm
    y -= pid_h
    _draw_box(c, x, y, full_w, pid_h, lw=0.9)

    mid = x + full_w / 2
    _draw_vline(c, mid, y, y + pid_h, lw=0.6)

    _draw_label(c, x + 2*mm, y + pid_h - 7*mm, "PATIENT IDENTITY", 8, True, INK)
    _set_font(c, "Helvetica", 7.4)
    c.setFillColor(MUTED)
    c.drawString(x + 2*mm, y + pid_h - 14*mm, f"Name : {_s(ctx['patient_name'])[:36]}")
    c.drawString(x + 2*mm, y + pid_h - 19*mm, f"UHID : {_s(ctx['uhid'])}   IP No : {_s(ctx['ip_no'])}")
    c.drawString(x + 2*mm, y + pid_h - 24*mm, f"Ward/Bed : {_s(ctx['ward_bed'])}")

    _draw_label(c, mid + 2*mm, y + pid_h - 10*mm, "ALLERGIC TO :", 8, True, MUTED)
    _draw_hline(c, mid + 26*mm, x + full_w - 4*mm, y + pid_h - 10.5*mm, lw=0.5)
    _draw_label(c, mid + 2*mm, y + pid_h - 18*mm, "DIAGNOSIS :", 8, True, MUTED)
    _draw_hline(c, mid + 22*mm, x + full_w - 4*mm, y + pid_h - 18.5*mm, lw=0.5)

    _set_font(c, "Helvetica", 8.0)
    c.setFillColor(INK)
    if ctx["allergic_to"]:
        c.drawString(mid + 28*mm, y + pid_h - 10.2*mm, _s(ctx["allergic_to"])[:70])
    if ctx["diagnosis"]:
        c.drawString(mid + 24*mm, y + pid_h - 18.2*mm, _s(ctx["diagnosis"])[:70])

    y -= 5 * mm

    # ===== Weight/Height/BG/BSA/BMI (Full width) =====
    wh_h = 8 * mm
    y -= wh_h
    _draw_box(c, x, y, full_w, wh_h, lw=0.9)
    _set_font(c, "Helvetica", 7.4)
    c.setFillColor(MUTED)
    c.drawString(
        x + 2*mm, y + 2.4*mm,
        f"Weight : {_s(ctx['weight'])} kg   Height : {_s(ctx['height'])} cm   Blood Group : {_s(ctx['blood_group'])}   BSA : {_s(ctx['bsa'])}   BMI : {_s(ctx['bmi'])}"
    )

    y -= 5 * mm

    # ===== Dietary Advice (Full width) =====
    diet_h = 16 * mm
    y -= diet_h
    _draw_box(c, x, y, full_w, diet_h, lw=0.9)
    _draw_label(c, x + 2*mm, y + diet_h - 6*mm, "DIETARY ADVICE :", 8.5, True, INK)
    _set_font(c, "Helvetica", 7.2)
    c.setFillColor(MUTED)
    c.drawString(x + 2*mm, y + diet_h - 11.5*mm, f"Oral Fluid : {_s(ctx['oral_fluid'])} ml/day     Salt : {_s(ctx['salt'])} gm/day")
    c.drawString(x + 2*mm, y + diet_h - 15.2*mm, f"Calorie : {_s(ctx['calorie'])} kcal/day     Protein : {_s(ctx['protein'])} gm/day")

    y -= 6 * mm

    # ===== Bottom Nurse Specimen Table RESERVED (always visible) =====
    nurse_h = 40 * mm
    nurse_y = m
    _draw_label(c, x, nurse_y + nurse_h + 2*mm, "Name of the Nurse, Specimen Sign and Emp. no.", 8.5, True, INK)
    _draw_box(c, x, nurse_y, full_w, nurse_h, lw=0.9)

    # split into 2 blocks (5 rows each) like your paper
    mid2 = x + full_w/2
    _draw_vline(c, mid2, nurse_y, nurse_y + nurse_h, lw=0.6)

    def _nurse_block(x0, block_w, rows_data: List[Dict[str, Any]]):
        cols = [10*mm, 46*mm, 22*mm, block_w - (10+46+22)*mm]
        _grid_cols(c, x0, nurse_y, cols, rows=6, h=nurse_h, lw=0.45)
        hy = nurse_y + nurse_h - 6*mm
        _draw_label(c, x0 + 2*mm, hy, "S.No.", 7, False, MUTED)
        _draw_label(c, x0 + 12*mm, hy, "Name", 7, False, MUTED)
        _draw_label(c, x0 + 58*mm, hy, "Sign", 7, False, MUTED)
        _draw_label(c, x0 + 80*mm, hy, "Emp. No.", 7, False, MUTED)
        for i, r in enumerate(rows_data[:5], start=1):
            _write_cell(c, *_cell_xy_cols(x0, nurse_y, cols, 6, nurse_h, i, 0), str(r.get("sno", "")), 7)
            _write_cell(c, *_cell_xy_cols(x0, nurse_y, cols, 6, nurse_h, i, 1), _s(r.get("name")), 7)
            _write_cell(c, *_cell_xy_cols(x0, nurse_y, cols, 6, nurse_h, i, 2), _s(r.get("sign")), 7)
            _write_cell(c, *_cell_xy_cols(x0, nurse_y, cols, 6, nurse_h, i, 3), _s(r.get("emp")), 7)

    _nurse_block(x, full_w/2, ctx["nurse_rows"][:5])
    _nurse_block(mid2, full_w/2, ctx["nurse_rows"][5:10])

    # ===== Middle area (between diet and nurse table): Left SOS/STAT/NOTE, Right IV =====
    mid_top = y
    mid_bottom = nurse_y + nurse_h + 6*mm
    mid_h = max(10*mm, mid_top - mid_bottom)

    left_w = 80 * mm
    gap_col = 6 * mm
    right_w = full_w - left_w - gap_col
    xL = x
    xR = x + left_w + gap_col

    # Heights
    sos_h = 48 * mm
    stat_h = 60 * mm
    note_h = max(18*mm, mid_h - sos_h - stat_h - 6*mm)

    # --- SOS table (left) ---
    sos_y = mid_top - sos_h
    _draw_label(c, xL, sos_y + sos_h + 2*mm, "SOS MEDICATIONS", 9, True)

    sos_cols = [
        12*mm,  # Date
        30*mm,  # Drug
        12*mm,  # Dose
        14*mm,  # Route/Freq
        left_w - (12+30+12+14)*mm,  # Doctor/Sign
    ]
    _grid_cols(c, xL, sos_y, sos_cols, rows=9, h=sos_h, lw=0.45)
    hy = sos_y + sos_h - 6*mm
    _draw_label(c, xL + 2*mm, hy, "Date", 7, False, MUTED)
    _draw_label(c, xL + 14*mm, hy, "Drug (CAPITALS)", 7, False, MUTED)
    _draw_label(c, xL + 44*mm, hy, "Dose", 7, False, MUTED)
    _draw_label(c, xL + 56*mm, hy, "Route/Freq", 7, False, MUTED)
    _draw_label(c, xL + 70*mm, hy, "Doctor/Sign", 7, False, MUTED)

    for i, r in enumerate(ctx["sos_orders"][:8], start=1):
        _write_cell(c, *_cell_xy_cols(xL, sos_y, sos_cols, 9, sos_h, i, 0), _fmt_date(r.get("date")), 6.8)
        _write_cell(c, *_cell_xy_cols(xL, sos_y, sos_cols, 9, sos_h, i, 1), _s(r.get("drug")).upper(), 6.8)
        _write_cell(c, *_cell_xy_cols(xL, sos_y, sos_cols, 9, sos_h, i, 2), _s(r.get("dose")), 6.8)
        rf = f"{_s(r.get('route'))}/{_s(r.get('freq'))}".strip("/")
        _write_cell(c, *_cell_xy_cols(xL, sos_y, sos_cols, 9, sos_h, i, 3), rf, 6.6)
        _write_cell(c, *_cell_xy_cols(xL, sos_y, sos_cols, 9, sos_h, i, 4), _s(r.get("doctor")), 6.6)

    # --- STAT table (left) ---
    stat_y = sos_y - 4*mm - stat_h
    _draw_label(c, xL, stat_y + stat_h + 2*mm, "STAT MEDICATIONS / PREMEDICATION", 9, True)

    stat_cols = [
        18*mm,  # DateTime
        28*mm,  # Drug
        12*mm,  # Dose
        10*mm,  # Route
        left_w - (18+28+12+10)*mm,  # Doctor/Sign
    ]
    _grid_cols(c, xL, stat_y, stat_cols, rows=9, h=stat_h, lw=0.45)
    hy = stat_y + stat_h - 6*mm
    _draw_label(c, xL + 2*mm, hy, "Date/Time", 7, False, MUTED)
    _draw_label(c, xL + 20*mm, hy, "Drug", 7, False, MUTED)
    _draw_label(c, xL + 48*mm, hy, "Dose", 7, False, MUTED)
    _draw_label(c, xL + 60*mm, hy, "Rt", 7, False, MUTED)
    _draw_label(c, xL + 70*mm, hy, "Doctor/Sign", 7, False, MUTED)

    for i, r in enumerate(ctx["stat_orders"][:8], start=1):
        _write_cell(c, *_cell_xy_cols(xL, stat_y, stat_cols, 9, stat_h, i, 0), _fmt_dt(r.get("datetime")), 6.2)
        _write_cell(c, *_cell_xy_cols(xL, stat_y, stat_cols, 9, stat_h, i, 1), _s(r.get("drug")).upper(), 6.6)
        _write_cell(c, *_cell_xy_cols(xL, stat_y, stat_cols, 9, stat_h, i, 2), _s(r.get("dose")), 6.6)
        _write_cell(c, *_cell_xy_cols(xL, stat_y, stat_cols, 9, stat_h, i, 3), _s(r.get("route")), 6.6)
        _write_cell(c, *_cell_xy_cols(xL, stat_y, stat_cols, 9, stat_h, i, 4), _s(r.get("doctor")), 6.2)

    # --- NOTE box (left) ---
    note_y = mid_bottom
    _draw_box(c, xL, note_y, left_w, note_h, lw=0.9)
    _draw_label(c, xL + 2*mm, note_y + note_h - 6*mm, "NOTE", 8, True, INK)
    _set_font(c, "Helvetica", 6.6)
    c.setFillColor(MUTED)
    c.drawString(xL + 2*mm, note_y + note_h - 11*mm,
                 "• If medicines are not administered, mark NOT GIVEN and document reason in nurses notes.")
    c.drawString(xL + 2*mm, note_y + note_h - 15*mm,
                 "• Verbal orders: note as 'V'. Practice READBACK. High-risk verbal orders not accepted.")

    # --- IV Fluids (right) ---
    iv_h = mid_h
    iv_y = mid_bottom
    _draw_label(c, xR, iv_y + iv_h + 2*mm, "INTRAVENOUS FLUIDS", 9, True)

    iv_cols = [
        20*mm,  # Date/Time
        24*mm,  # Fluid
        16*mm,  # Additive
        12*mm,  # Dose
        10*mm,  # Rate
        right_w - (20+24+16+12+10)*mm,  # Doctor/Nurse
    ]
    _grid_cols(c, xR, iv_y, iv_cols, rows=11, h=iv_h, lw=0.45)
    hy = iv_y + iv_h - 6*mm
    _draw_label(c, xR + 2*mm, hy, "Date/Time", 7, False, MUTED)
    _draw_label(c, xR + 22*mm, hy, "Fluid", 7, False, MUTED)
    _draw_label(c, xR + 46*mm, hy, "Additive", 7, False, MUTED)
    _draw_label(c, xR + 62*mm, hy, "Dose", 7, False, MUTED)
    _draw_label(c, xR + 74*mm, hy, "Rate", 7, False, MUTED)
    _draw_label(c, xR + 84*mm, hy, "Doctor/Nurse", 7, False, MUTED)

    for i, r in enumerate(ctx["iv_fluids"][:10], start=1):
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 0), _fmt_dt(r.get("ordered")), 6.2)
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 1), _s(r.get("fluid")).upper(), 6.6)
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 2), _s(r.get("additive")), 6.4)
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 3), _s(r.get("dose")), 6.4)
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 4), _s(r.get("rate")), 6.4)
        dn = _s(r.get("doctor"))
        sn = _s(r.get("start_nurse"))
        combined = (dn + (" / " + sn if sn else "")).strip()
        _write_cell(c, *_cell_xy_cols(xR, iv_y, iv_cols, 11, iv_h, i, 5), combined, 6.2)


# -------------------------
# Page-2 (Portrait A4) – 8 drug blocks
# -------------------------
def _drug_block(c: canvas.Canvas, x, y, w, h, idx: int, info: Optional[Dict[str, Any]]):
    _draw_box(c, x, y, w, h, lw=0.9)

    pad = 2 * mm
    left_w = min(36 * mm, w * 0.45)
    grid_x = x + left_w
    grid_w = w - left_w

    date_h = 6 * mm
    name_h = 12 * mm
    route_h = 7 * mm
    time_h = 7 * mm
    inst_h = 10 * mm

    y_top = y + h
    y1 = y_top - date_h
    y2 = y1 - name_h
    y3 = y2 - route_h
    y4 = y3 - time_h
    y5 = y4 - inst_h

    for yy in [y1, y2, y3, y4, y5]:
        _draw_hline(c, x, x + left_w, yy, lw=0.5)

    _draw_hline(c, grid_x, x + w, y2, lw=0.5)

    _draw_label(c, x + pad, y_top - 4.8*mm, "DATE", 7.2, True, MUTED)
    _draw_label(c, x + pad, y1 - 5*mm, f"{idx}. Drug Name", 7.6, True, INK)
    _draw_label(c, x + pad, y1 - 10.5*mm, "CAPITAL LETTER", 6.8, False, MUTED)

    _draw_label(c, x + left_w - 20*mm, y1 - 5*mm, "Dose", 7.0, True, INK)
    _draw_label(c, x + left_w - 20*mm, y1 - 10.5*mm, "& Freq", 6.8, False, MUTED)

    # route check boxes
    box_y = y2 - 6.2*mm
    bw = 8 * mm
    labels = ["PO", "IM", "SC", "IV"]
    for i, lab in enumerate(labels):
        bx = x + pad + i * (bw + 2*mm)
        _draw_box(c, bx, box_y, bw, 5.5*mm, lw=0.5)
        _draw_label(c, bx + 2.2*mm, box_y + 1.6*mm, lab, 6.8, True, INK)

    _draw_label(c, x + pad, y3 - 4.8*mm, "Doctor Name & Sign", 6.8, False, MUTED)
    _draw_label(c, x + pad, y4 - 4.8*mm, "Time :", 6.8, False, MUTED)
    _draw_label(c, x + pad, y5 + 3.5*mm, "Special Instructions", 6.8, False, MUTED)

    # Right grid
    pairs = 8
    cols = pairs * 2
    rows = 5
    grid_h = h - (date_h + name_h)

    # grid
    _draw_box(c, grid_x, y, grid_w, grid_h, lw=0.9)
    colw = grid_w / cols
    rowh = grid_h / rows
    for i in range(1, cols):
        _draw_vline(c, grid_x + i*colw, y, y + grid_h, lw=0.45)
    for j in range(1, rows):
        _draw_hline(c, grid_x, grid_x + grid_w, y + j*rowh, lw=0.45)

    # header labels
    top_y = y + grid_h - rowh + 2*mm
    for p in range(pairs):
        hx = grid_x + (p*2) * colw
        sx = grid_x + (p*2 + 1) * colw
        _draw_center(c, hx, top_y, colw, "Hrs", 6.2, True, MUTED)
        _draw_center(c, sx, top_y, colw, "Sign", 6.2, True, MUTED)

    if not info:
        return

    # fill left content
    _set_font(c, "Helvetica", 7.2)
    c.setFillColor(INK)
    c.drawString(x + 14*mm, y_top - 4.8*mm, _s(info.get("date"))[:18])

    _set_font(c, "Helvetica-Bold", 8.0)
    c.drawString(x + pad, y1 - 8.2*mm, _s(info.get("drug")).upper()[:24])
    _set_font(c, "Helvetica", 7.2)
    c.drawString(x + left_w - 20*mm, y1 - 8.2*mm, _s(info.get("dose_freq"))[:18])

    _set_font(c, "Helvetica", 6.9)
    c.drawString(x + pad, y3 - 4.9*mm, _s(info.get("doctor"))[:26])

    c.setFillColor(MUTED)
    c.drawString(x + 14*mm, y4 - 4.9*mm, _s(info.get("first_time"))[:10])
    c.drawString(x + pad, y5 + 1.8*mm, _s(info.get("instructions"))[:42])

    route = _s(info.get("route")).lower()
    route_map = {"po": "PO", "oral": "PO", "im": "IM", "sc": "SC", "subcutaneous": "SC", "iv": "IV", "intravenous": "IV"}
    tick = route_map.get(route, "")
    if tick and tick in labels:
        i = labels.index(tick)
        bx = x + pad + i * (bw + 2*mm)
        c.setFillColor(INK)
        _set_font(c, "Helvetica-Bold", 10)
        c.drawString(bx + 2.2*mm, box_y + 0.6*mm, "✓")

    # fill grid administrations
    admin: List[Dict[str, Any]] = info.get("admin", [])
    for k, a in enumerate(admin[:32]):
        row = 1 + (k // 8)
        if row > 4:
            break
        pair = k % 8
        hrs_col = pair * 2
        sign_col = hrs_col + 1

        cell_y = y + grid_h - (row + 1) * rowh
        x_hrs = grid_x + hrs_col * colw
        x_sign = grid_x + sign_col * colw

        c.setFillColor(INK)
        _set_font(c, "Helvetica", 6.4)
        c.drawString(x_hrs + 0.8*mm, cell_y + (rowh/2 - 2), _s(a.get("time"))[:5])

        _set_font(c, "Helvetica-Bold", 6.4)
        c.drawString(x_sign + 0.6*mm, cell_y + (rowh/2 - 2), _s(a.get("sign"))[:4])


def _page2(c: canvas.Canvas, ctx: Dict[str, Any]):
    W, H = A4
    m = 10 * mm
    gap = 6 * mm
    usable_w = W - 2*m
    usable_h = H - 2*m

    col_w = (usable_w - gap) / 2
    xL = m
    xR = m + col_w + gap
    y0 = m

    auth_h = 12 * mm
    block_gap = 4 * mm
    block_h = (usable_h - auth_h - 3*block_gap) / 4

    meds: List[Dict[str, Any]] = ctx["med_blocks"]

    for i in range(4):
        y = y0 + auth_h + (3 - i) * (block_h + block_gap)
        _drug_block(c, xL, y, col_w, block_h, idx=i+1, info=meds[i] if i < len(meds) else None)

    for i in range(4):
        y = y0 + auth_h + (3 - i) * (block_h + block_gap)
        idx = i + 5
        _drug_block(c, xR, y, col_w, block_h, idx=idx, info=meds[idx-1] if (idx-1) < len(meds) else None)

    def _auth_box(x, items: List[str]):
        _draw_box(c, x, y0, col_w, auth_h, lw=0.9)
        _draw_label(c, x + 2*mm, y0 + 4*mm, "Doctor's Daily Authorization", 8.3, True, INK)
        _set_font(c, "Helvetica", 7)
        c.setFillColor(INK)
        for i, line in enumerate(items[:2]):
            c.drawString(x + 62*mm, y0 + (auth_h - (4 + i*3.5)*mm), line[:55])

    _auth_box(xL, ctx["doctor_auth_lines"][:3])
    _auth_box(xR, ctx["doctor_auth_lines"][3:6])


# -------------------------
# Build context from DB
# -------------------------
def _build_ctx(db: Session, adm: IpdAdmission, branding: Optional[UiBranding]) -> Dict[str, Any]:
    patient = _get_patient(db, adm)
    meta = getattr(adm, "drug_chart_meta", None)

    patient_name = _s(getattr(patient, "name", "")) or _s(getattr(patient, "full_name", "")) or ""
    uhid = _s(getattr(patient, "uhid", "")) or _s(getattr(patient, "patient_code", "")) or str(adm.patient_id)
    ip_no = _s(getattr(adm, "display_code", "")) or _s(getattr(adm, "admission_code", "")) or f"IP-{adm.id:06d}"

    bed = adm.current_bed
    ward_bed = ""
    if bed:
        # be defensive (different schemas)
        ward = _s(getattr(getattr(getattr(bed, "room", None), "ward", None), "name", "")) or _s(getattr(bed, "ward_name", ""))
        room = _s(getattr(getattr(bed, "room", None), "name", "")) or _s(getattr(bed, "room_name", ""))
        bcode = _s(getattr(bed, "code", "")) or _s(getattr(bed, "bed_code", ""))
        ward_bed = " / ".join([x for x in [ward, room, bcode] if x])

    allergic_to = _s(getattr(meta, "allergic_to", "")) if meta else ""
    diagnosis = _s(getattr(meta, "diagnosis", "")) if meta else ""

    weight = _s(getattr(meta, "weight_kg", "")) if meta else ""
    height = _s(getattr(meta, "height_cm", "")) if meta else ""
    blood_group = _s(getattr(meta, "blood_group", "")) if meta else ""
    bsa = _s(getattr(meta, "bsa", "")) if meta else ""
    bmi = _s(getattr(meta, "bmi", "")) if meta else ""

    oral_fluid = _s(getattr(meta, "oral_fluid_per_day_ml", "")) if meta else ""
    salt = _s(getattr(meta, "salt_gm_per_day", "")) if meta else ""
    calorie = _s(getattr(meta, "calorie_per_day_kcal", "")) if meta else ""
    protein = _s(getattr(meta, "protein_gm_per_day", "")) if meta else ""

    # IV fluids
    iv_fluids = []
    for o in sorted(adm.iv_fluid_orders or [], key=lambda x: x.ordered_datetime or datetime.min):
        iv_fluids.append({
            "ordered": o.ordered_datetime,
            "fluid": getattr(o, "fluid", None),
            "additive": getattr(o, "additive", None),
            "dose": f"{_s(getattr(o, 'dose_ml', ''))} ml" if getattr(o, "dose_ml", None) is not None else "",
            "rate": f"{_s(getattr(o, 'rate_ml_per_hr', ''))}" if getattr(o, "rate_ml_per_hr", None) is not None else "",
            "doctor": _s(getattr(o, "doctor_name", "")) or _s(getattr(getattr(o, "doctor", None), "name", "")),
            "start_nurse": _s(getattr(o, "start_nurse_name", "")) or _s(getattr(getattr(o, "start_nurse", None), "name", "")),
        })

    # Medication orders split
    sos_orders = []
    stat_orders = []
    regular_orders: List[IpdMedicationOrder] = []

    for o in adm.medication_orders or []:
        ot = _s(getattr(o, "order_type", "")).lower()
        if ot == "sos":
            sos_orders.append({
                "date": getattr(o, "start_datetime", None).date() if getattr(o, "start_datetime", None) else None,
                "drug": getattr(o, "drug_name", None),
                "dose": f"{_s(getattr(o, 'dose', ''))} {_s(getattr(o, 'dose_unit', ''))}".strip(),
                "route": getattr(o, "route", None),
                "freq": getattr(o, "frequency", None),
                "doctor": _s(getattr(getattr(o, "ordered_by_user", None), "name", "")),
            })
        elif ot in ("stat", "premed"):
            stat_orders.append({
                "datetime": getattr(o, "start_datetime", None),
                "drug": getattr(o, "drug_name", None),
                "dose": f"{_s(getattr(o, 'dose', ''))} {_s(getattr(o, 'dose_unit', ''))}".strip(),
                "route": getattr(o, "route", None),
                "doctor": _s(getattr(getattr(o, "ordered_by_user", None), "name", "")),
            })
        else:
            if _s(getattr(o, "order_status", "")).lower() in ("active", "ongoing", ""):
                regular_orders.append(o)

    # MAR map
    admin_by_order: Dict[int, List[IpdMedicationAdministration]] = {}
    for a in adm.medication_administrations or []:
        oid = getattr(a, "med_order_id", None)
        if oid is None:
            continue
        admin_by_order.setdefault(oid, []).append(a)
    for k in admin_by_order:
        admin_by_order[k].sort(key=lambda x: getattr(x, "scheduled_datetime", None) or datetime.min)

    # Page-2 blocks
    blocks = []
    for o in sorted(regular_orders, key=lambda x: getattr(x, "start_datetime", None) or datetime.min)[:8]:
        admins = admin_by_order.get(getattr(o, "id", 0), [])
        admin_cells = []
        for a in admins[:32]:
            nurse_name = _s(getattr(getattr(a, "given_by_user", None), "name", ""))
            admin_cells.append({
                "time": _fmt_time(getattr(a, "scheduled_datetime", None)),
                "sign": _initials(nurse_name) if nurse_name else "",
            })

        blocks.append({
            "date": _fmt_date(getattr(o, "start_datetime", None).date() if getattr(o, "start_datetime", None) else None),
            "drug": getattr(o, "drug_name", None),
            "dose_freq": f"{_s(getattr(o, 'dose', ''))}{(' '+_s(getattr(o,'dose_unit',''))) if getattr(o,'dose_unit',None) else ''} / {_s(getattr(o,'frequency',''))}".strip(),
            "route": getattr(o, "route", None),
            "doctor": _s(getattr(getattr(o, "ordered_by_user", None), "name", "")),
            "first_time": _fmt_time(getattr(admins[0], "scheduled_datetime", None)) if admins else "",
            "instructions": _s(getattr(o, "special_instructions", "")),
            "admin": admin_cells,
        })

    nurse_rows = []
    for r in sorted(adm.drug_chart_nurse_rows or [], key=lambda x: getattr(x, "serial_no", 9999) or 9999):
        nurse_rows.append({
            "sno": getattr(r, "serial_no", "") or "",
            "name": getattr(r, "nurse_name", "") or "",
            "sign": getattr(r, "specimen_sign", "") or "",
            "emp": getattr(r, "emp_no", "") or "",
        })

    auth_lines = []
    for r in sorted(adm.doctor_auth_rows or [], key=lambda x: getattr(x, "auth_date", date.min) or date.min, reverse=True):
        dn = _s(getattr(r, "doctor_name", "")) or _s(getattr(getattr(r, "doctor", None), "name", ""))
        auth_lines.append(f"{_fmt_date(getattr(r, 'auth_date', None))} - {dn}")

    return {
        "org_name": _s(getattr(branding, "org_name", "")),
        "org_tagline": _s(getattr(branding, "org_tagline", "")),
        "org_address": _s(getattr(branding, "org_address", "")),
        "org_phone": _s(getattr(branding, "org_phone", "")),
        "org_website": _s(getattr(branding, "org_website", "")),
        "org_logo_path": _s(getattr(branding, "org_logo_path", "")),

        "patient_name": patient_name,
        "uhid": uhid,
        "ip_no": ip_no,
        "ward_bed": ward_bed,

        "allergic_to": allergic_to,
        "diagnosis": diagnosis,

        "weight": weight,
        "height": height,
        "blood_group": blood_group,
        "bsa": bsa,
        "bmi": bmi,

        "oral_fluid": oral_fluid,
        "salt": salt,
        "calorie": calorie,
        "protein": protein,

        "iv_fluids": iv_fluids,
        "sos_orders": sos_orders,
        "stat_orders": stat_orders,
        "med_blocks": blocks,

        "nurse_rows": nurse_rows,
        "doctor_auth_lines": auth_lines,
    }


# -------------------------
# Public function
# -------------------------
def build_ipd_drug_chart_pdf_bytes(db: Session, admission_id: int) -> bytes:
    branding = _get_branding(db)
    adm = _load_admission(db, admission_id)
    ctx = _build_ctx(db, adm, branding)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)  # ✅ PORTRAIT
    c.setAuthor("NABH HIMS")
    c.setTitle("Drug Chart")

    _page1(c, branding, ctx)
    c.showPage()
    _page2(c, ctx)
    c.save()
    return buf.getvalue()
