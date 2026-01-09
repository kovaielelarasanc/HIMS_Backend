# FILE: app/services/pdfs/ot_preop_checklist_pdf.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date, time
from typing import Any, Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics

from app.core.config import settings
from app.models.ui_branding import UiBranding

IST = ZoneInfo("Asia/Kolkata")

# -----------------------------
# Checklist keys MUST match frontend keys
# -----------------------------
CHECKLIST_ITEMS = [
    ("allergy", "ALLERGY"),
    ("consent_form_signed", "CONSENT FORM SIGNED"),
    ("written_high_risk_signed", "WRITTEN HIGH RISK FORM SIGNED"),
    ("identity_bands_checked", "IDENTITY BANDS CHECKED"),
    ("npo", "NILL PER MOUTH (NPO)"),
    ("pre_medication_given", "PRE MEDICATION GIVEN"),
    ("test_dose_given", "TEST DOSE GIVEN"),
    ("bowel_preparation", "BOWEL PREPARATION"),
    ("bladder_empty_time", "BLADDER EMPTY TIME"),
    ("serology_results", "SEROLOGY RESULTS"),
    ("blood_grouping", "BLOOD GROUPING"),
    ("blood_reservation", "BLOOD RESERVATION"),
    ("patient_files_with_records",
     "PATIENT IP / OP FILES ECG / X RAY WITH OLD RECORDS"),
    ("pre_anaesthetic_evaluation", "PRE ANAESTHETIC EVALUATION"),
    ("jewellery_nailpolish_removed",
     "JEWELLERY, NAIL POLISH, MAKE UP REMOVED"),
    ("prosthesis_dentures_wig_contactlens_removed",
     "PROSTHESIS / DENTURES / WIG / CONTACT LENS REMOVED"),
    ("sterile_preparation_done", "STERILE PREPARATION DONE"),
]

# -----------------------------
# Theme
# -----------------------------
BLUE = colors.HexColor("#0b2b55")
TEXT = colors.HexColor("#0b1a33")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#CBD5E1")


# -----------------------------
# Safe getters (object OR dict)
# -----------------------------
def _get(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for n in names:
        if isinstance(obj, dict) and n in obj:
            v = obj.get(n)
            if v not in (None, ""):
                return v
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v not in (None, ""):
                return v
    return default


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("1", "true", "yes", "y", "checked", "on")
    return bool(v)


def _norm_shave(v: Any) -> Optional[str]:
    """
    Accepts: "yes"/"no"/None OR bool OR 1/0
    Returns: "yes" | "no" | None
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return "yes" if v != 0 else "no"
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("yes", "y", "true", "1", "done", "completed"):
            return "yes"
        if s in ("no", "n", "false", "0"):
            return "no"
    return None


# -----------------------------
# Date/Time helpers (FIXED for date + datetime)
# -----------------------------
def _to_ist(dt):
    """
    Accepts datetime | date | str | None
    Returns IST-aware datetime | None
    """
    if not dt:
        return None

    if isinstance(dt, str):
        s = dt.strip()
        try:
            return _to_ist(datetime.fromisoformat(s.replace("Z", "+00:00")))
        except Exception:
            try:
                d = date.fromisoformat(s[:10])
                return datetime(d.year, d.month, d.day, tzinfo=IST)
            except Exception:
                return None

    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime(dt.year, dt.month, dt.day, tzinfo=IST)

    if isinstance(dt, time):
        return None

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))  # assume UTC for naive
        return dt.astimezone(IST)

    return None


def _fmt_date(x) -> str:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x.strftime("%d-%m-%Y")
    d = _to_ist(x)
    return d.strftime("%d-%m-%Y") if d else ""


def _fmt_time(x) -> str:
    if isinstance(x, time):
        return x.strftime("%I:%M %p")
    d = _to_ist(x)
    return d.strftime("%I:%M %p") if d else ""


def _calc_age(dob: Optional[date]) -> str:
    if not dob:
        return ""
    today = date.today()
    years = today.year - dob.year - (
        (today.month, today.day) < (dob.month, dob.day))
    return str(years)


# -----------------------------
# Patient resolver
# -----------------------------
def _resolve_schedule(case: Any) -> Any:
    return (_get(case, "schedule", "ot_schedule", "schedule_obj", default=None)
            or _get(case, "ot_case_schedule", default=None) or None)


def _resolve_patient_any(case: Any, preop_data: Dict[str, Any]) -> Any:
    p = preop_data.get("patient") if isinstance(preop_data, dict) else None
    if p:
        return p

    schedule = _resolve_schedule(case)
    if schedule:
        p = _get(schedule, "patient", "patient_obj", default=None)
        if p:
            return p
        adm = _get(schedule,
                   "admission",
                   "ipd_admission",
                   "admission_obj",
                   default=None)
        p = _get(adm, "patient", "patient_obj", default=None)
        if p:
            return p

    p = _get(case, "patient", "patient_obj", default=None)
    if p:
        return p

    adm = _get(case, "admission", "ipd_admission", "ipd", default=None)
    p = _get(adm, "patient", "patient_obj", default=None)
    if p:
        return p

    visit = _get(case, "visit", "opd_visit", "opd", default=None)
    p = _get(visit, "patient", "patient_obj", default=None)
    if p:
        return p

    return None


def _resolve_patient_fields(
        case: Any, preop_data: Dict[str, Any]) -> Tuple[str, str, str]:
    patient = _resolve_patient_any(case, preop_data)
    schedule = _resolve_schedule(case)

    # NAME
    name = (
        _get(patient,
             "full_name",
             "display_name",
             "name",
             "patient_name",
             default="")
        or _get(preop_data, "patient_name", "name", default="") or
        (f"{_get(patient, 'prefix', 'title', default='')}".strip() + " " +
         f"{_get(patient, 'first_name', 'given_name', default='')}".strip() +
         " " +
         f"{_get(patient, 'last_name', 'family_name', default='')}".strip()
         ).strip() or _get(
             case, "patient_name", "patient_full_name", default="")).strip()

    # SEX
    sex = (str(
        _get(patient, "sex", "gender", "sex_label", default="") or ""
    ) or str(_get(preop_data, "sex", "gender", default="") or "") or str(
        _get(
            case, "sex", "gender", "patient_sex", "patient_gender", default="")
        or "")).strip()

    # AGE
    age = str(
        _get(patient, "age_display", "age", "age_years", default="")
        or "").strip()
    if not age:
        age = str(_get(preop_data, "age", "age_years", default="")
                  or "").strip()
    if not age:
        dob = _get(patient, "dob", "date_of_birth", default=None) or _get(
            preop_data, "dob", default=None)
        if isinstance(dob, date):
            age = _calc_age(dob)

    age_sex = ""
    if age and sex:
        age_sex = f"{age} / {sex}"
    elif age:
        age_sex = age
    elif sex:
        age_sex = sex

    # REG / UHID / MRN
    reg_no = (str(
        _get(patient,
             "uhid",
             "uhid_number",
             "reg_no",
             "mrn",
             "patient_id",
             default="") or "").strip() or str(
                 _get(preop_data, "uhid", "reg_no", "mrn", default="")
                 or "").strip()
              or str(
                  _get(schedule, "patient_uhid", "uhid", "reg_no", default="")
                  or "").strip()
              or str(
                  _get(case, "uhid", "reg_no", "mrn", "patient_id", default="")
                  or "").strip())

    return name or "", age_sex or "", reg_no or ""


# -----------------------------
# Text wrapping + clipping
# -----------------------------
def _clip_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float):
    p = c.beginPath()
    p.rect(x, y, w, h)
    c.clipPath(p, stroke=0, fill=0)


def _wrap_lines(text: str, font_name: str, font_size: float,
                max_width: float) -> List[str]:
    if not text:
        return [""]
    chunks = str(text).splitlines() or [str(text)]
    out: List[str] = []
    for ch in chunks:
        lines = simpleSplit(ch, font_name, font_size, max_width) or [""]
        out.extend(lines)
    return out if out else [""]


def _fit_lines(
    text: str,
    font_name: str,
    max_width: float,
    max_lines: int,
    start: float,
    min_size: float = 6.8,
):
    fs = start
    while fs >= min_size:
        lines = _wrap_lines(text, font_name, fs, max_width)
        if len(lines) <= max_lines:
            return fs, lines
        fs -= 0.3

    fs = min_size
    lines = _wrap_lines(text, font_name, fs, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        ell = "…"
        last = lines[-1]
        while last and pdfmetrics.stringWidth(last + ell, font_name,
                                              fs) > max_width:
            last = last[:-1]
        lines[-1] = (last + ell) if last else ell
    return fs, lines


def _draw_wrapped_cell(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    font_name: str = "Helvetica",
    start_size: float = 9.0,
    max_lines: int = 2,
    align: str = "left",  # left|center|right
    pad_x: float = 2.5 * mm,
):
    c.saveState()
    _clip_rect(c, x, y, w, h)

    max_width = max(10, w - 2 * pad_x)
    fs, lines = _fit_lines(text or "",
                           font_name,
                           max_width,
                           max_lines=max_lines,
                           start=start_size)
    leading = fs * 1.12
    total_h = leading * len(lines)

    baseline_top = y + h - (h - total_h) / 2 - leading * 0.85

    c.setFillColor(TEXT)
    c.setFont(font_name, fs)
    for i, line in enumerate(lines):
        yy = baseline_top - i * leading
        if align == "center":
            c.drawCentredString(x + w / 2, yy, line)
        elif align == "right":
            c.drawRightString(x + w - pad_x, yy, line)
        else:
            c.drawString(x + pad_x, yy, line)

    c.restoreState()


def _draw_checkbox(c: canvas.Canvas, cx: float, cy: float, size: float,
                   checked: bool):
    x = cx - size / 2
    y = cy - size / 2
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1)
    c.rect(x, y, size, size, stroke=1, fill=0)
    if checked:
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", size * 0.9)
        c.drawCentredString(cx, y + size * 0.14, "✓")
    c.restoreState()


# -----------------------------
# Header + Patient strip
# -----------------------------
# ✅ REPLACE ONLY THIS FUNCTION in your ot_preop_checklist_pdf.py


def _draw_brand_header(c: canvas.Canvas, branding: UiBranding, x: float,
                       y_top: float, w: float) -> float:
    """
    Header:
    - Left: Logo
    - Right: Org name + tagline + (Address / Phone / Email / Website / GSTIN) if available
    """
    header_h = 26 * mm

    # bottom divider
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.line(x, y_top - header_h, x + w, y_top - header_h)

    # logo (left)
    logo_w = 58 * mm
    logo_h = 18 * mm
    logo_x = x
    logo_y = y_top - (22 * mm)

    logo_path = (getattr(branding, "logo_path", "") or "").strip()
    logo_drawn = False
    if logo_path:
        abs_path = Path(settings.STORAGE_DIR).joinpath(logo_path)
        if abs_path.exists():
            try:
                img = ImageReader(str(abs_path))
                c.drawImage(
                    img,
                    logo_x,
                    logo_y,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                logo_drawn = True
            except Exception:
                logo_drawn = False

    # org fields
    org = (getattr(branding, "org_name", "") or "").strip()
    tagline = (getattr(branding, "org_tagline", "") or "").strip()

    address = (getattr(branding, "org_address", "") or "").strip()
    phone = (getattr(branding, "org_phone", "") or "").strip()
    email = (getattr(branding, "org_email", "") or "").strip()
    website = (getattr(branding, "org_website", "") or "").strip()
    gstin = (getattr(branding, "org_gstin", "") or "").strip()

    # right block width (avoid colliding with logo)
    right_pad = 0 * mm
    right_x = x + w - right_pad
    gap_after_logo = 4 * mm
    right_block_left = (x + logo_w + gap_after_logo) if logo_drawn else x
    right_block_w = max(40 * mm, (x + w) - right_block_left)

    # title (right)
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(right_x, y_top - 6 * mm, (org or "")[:90])

    # tagline
    y_cursor = y_top - 11 * mm
    if tagline:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawRightString(right_x, y_cursor, tagline[:120])
        y_cursor -= 3.8 * mm
    else:
        y_cursor -= 1.2 * mm

    # ---- details (compact, auto-wrap, max fits within header) ----
    # available vertical space under tagline line inside 26mm header
    # (we keep it safe: max 4 detail lines)
    detail_font = "Helvetica"
    detail_size = 8.4
    line_gap = 3.7 * mm

    def draw_detail_text(text: str, max_lines: int) -> int:
        if not text:
            return 0
        c.setFillColor(MUTED)
        c.setFont(detail_font, detail_size)

        # wrap into the right block width
        lines = _wrap_lines(text, detail_font, detail_size,
                            right_block_w) or []
        if not lines:
            return 0
        lines = lines[:max_lines]

        nonlocal y_cursor
        for ln in lines:
            if y_cursor <= (y_top - header_h + 2.2 * mm):  # stop if too low
                return 0
            c.drawRightString(right_x, y_cursor, ln[:180])
            y_cursor -= line_gap
        return len(lines)

    # Priority: Address (up to 2 lines) → Phone/Email → Website → GSTIN
    used_lines = 0

    if address and used_lines < 4:
        used_lines += draw_detail_text(address,
                                       max_lines=min(2, 4 - used_lines))

    if used_lines < 4:
        parts = []
        if phone:
            parts.append(f"Ph: {phone}")
        if email:
            parts.append(f"Email: {email}")
        if parts:
            used_lines += draw_detail_text("  |  ".join(parts),
                                           max_lines=min(1, 4 - used_lines))

    if used_lines < 4 and website:
        # keep website clean
        web = website.replace("https://", "").replace("http://", "").strip()
        used_lines += draw_detail_text(f"Web: {web}",
                                       max_lines=min(1, 4 - used_lines))

    if used_lines < 4 and gstin:
        used_lines += draw_detail_text(f"GSTIN: {gstin}",
                                       max_lines=min(1, 4 - used_lines))

    # reset
    c.setFillColor(TEXT)
    return header_h


def _draw_patient_strip(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    name: str,
    age_sex: str,
    reg_no: str,
    dt_str: str,
    tm_str: str,
):
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.4)
    y = y_top - h
    c.rect(x, y, w, h, stroke=1, fill=0)

    split = x + w * 0.70
    c.setLineWidth(1.2)
    c.line(split, y, split, y + h)

    pad = 3 * mm

    lx = x + pad
    ly = y_top - 6 * mm

    def row(lbl: str, val: str, yy: float):
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(lx, yy, lbl)
        c.drawString(lx + 24 * mm, yy, ":")

        vx = lx + 28 * mm
        vw = (split - pad) - vx
        _draw_wrapped_cell(
            c,
            vx,
            yy - 3.8,
            vw,
            5.4 * mm,
            val or "",
            font_name="Helvetica",
            start_size=9.5,
            max_lines=1,
            pad_x=0,
        )

    row("Name", name, ly)
    row("Age / Sex", age_sex, ly - 5 * mm)
    row("Reg. No.", reg_no, ly - 10 * mm)

    rx = split + pad
    ry = y_top - 6 * mm

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(rx, ry, "Date")
    c.drawString(rx + 16 * mm, ry, ":")
    c.setFont("Helvetica", 9.5)
    c.drawString(rx + 20 * mm, ry, dt_str or "")

    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(rx, ry - 5 * mm, "Time")
    c.drawString(rx + 16 * mm, ry - 5 * mm, ":")
    c.setFont("Helvetica", 9.5)
    c.drawString(rx + 20 * mm, ry - 5 * mm, tm_str or "")

    c.setFillColor(TEXT)


def _draw_title_bar(c: canvas.Canvas, x: float, y_top: float, w: float,
                    h: float):
    c.setFillColor(BLUE)
    c.setStrokeColor(BLUE)
    c.rect(x, y_top - h, w, h, stroke=1, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(x + w / 2, y_top - h + 2.2 * mm,
                        "PRE - OPERATIVE CHECK LIST")
    c.setFillColor(TEXT)
    c.setStrokeColor(BLUE)


# -----------------------------
# YES/NO tick (NO circle/box)
# -----------------------------
def _draw_yes_no_tick(c: canvas.Canvas, cx: float, y: float, *,
                      yes_selected: bool, no_selected: bool):
    gap = 18 * mm
    left_x = cx - gap
    right_x = cx + 6 * mm

    c.setFillColor(TEXT)

    # YES
    if yes_selected:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(left_x, y, "✓")
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(left_x + 5 * mm, y, "YES")

    # NO
    if no_selected:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(right_x, y, "✓")
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(right_x + 5 * mm, y, "NO")


# -----------------------------
# Human body drawings (vector only, hospital-form style)
# -----------------------------
def _draw_human_front(c: canvas.Canvas, x: float, y: float, w: float,
                      h: float):
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.15)

    cx = x + w * 0.50

    # Head (oval)
    head_w = w * 0.16
    head_h = h * 0.14
    c.ellipse(
        cx - head_w / 2,
        y + h * 0.84,
        cx + head_w / 2,
        y + h * 0.84 + head_h,
        stroke=1,
        fill=0,
    )

    # Neck
    c.line(cx - w * 0.03, y + h * 0.84, cx - w * 0.03, y + h * 0.79)
    c.line(cx + w * 0.03, y + h * 0.84, cx + w * 0.03, y + h * 0.79)

    # Shoulder points
    left_sh = (x + w * 0.34, y + h * 0.78)
    right_sh = (x + w * 0.66, y + h * 0.78)

    # Waist/Hip refs
    left_waist = (x + w * 0.41, y + h * 0.52)
    right_waist = (x + w * 0.59, y + h * 0.52)
    left_hip = (x + w * 0.44, y + h * 0.42)
    right_hip = (x + w * 0.56, y + h * 0.42)

    # shoulders line
    c.line(left_sh[0], left_sh[1], right_sh[0], right_sh[1])

    # left torso curve
    p = c.beginPath()
    p.moveTo(left_sh[0], left_sh[1])
    p.curveTo(x + w * 0.30, y + h * 0.72, x + w * 0.35, y + h * 0.58,
              left_waist[0], left_waist[1])
    p.curveTo(x + w * 0.43, y + h * 0.48, x + w * 0.42, y + h * 0.46,
              left_hip[0], left_hip[1])
    c.drawPath(p, stroke=1, fill=0)

    # right torso curve
    p = c.beginPath()
    p.moveTo(right_sh[0], right_sh[1])
    p.curveTo(x + w * 0.70, y + h * 0.72, x + w * 0.65, y + h * 0.58,
              right_waist[0], right_waist[1])
    p.curveTo(x + w * 0.57, y + h * 0.48, x + w * 0.58, y + h * 0.46,
              right_hip[0], right_hip[1])
    c.drawPath(p, stroke=1, fill=0)

    # Arms (simple hospital outline)
    # Left arm
    c.line(left_sh[0], left_sh[1], x + w * 0.24, y + h * 0.62)
    c.line(x + w * 0.24, y + h * 0.62, x + w * 0.26, y + h * 0.43)
    c.line(x + w * 0.26, y + h * 0.43, x + w * 0.30, y + h * 0.40)  # hand
    c.line(x + w * 0.30, y + h * 0.40, x + w * 0.33, y + h * 0.43)  # hand
    c.line(x + w * 0.33, y + h * 0.43, x + w * 0.30, y + h * 0.62)
    c.line(x + w * 0.30, y + h * 0.62, x + w * 0.38, y + h * 0.72)

    # Right arm
    c.line(right_sh[0], right_sh[1], x + w * 0.76, y + h * 0.62)
    c.line(x + w * 0.76, y + h * 0.62, x + w * 0.74, y + h * 0.43)
    c.line(x + w * 0.74, y + h * 0.43, x + w * 0.70, y + h * 0.40)
    c.line(x + w * 0.70, y + h * 0.40, x + w * 0.67, y + h * 0.43)
    c.line(x + w * 0.67, y + h * 0.43, x + w * 0.70, y + h * 0.62)
    c.line(x + w * 0.70, y + h * 0.62, x + w * 0.62, y + h * 0.72)

    # Pelvis hint (non-graphic)
    c.line(left_hip[0], left_hip[1], cx, y + h * 0.40)
    c.line(cx, y + h * 0.40, right_hip[0], right_hip[1])

    # Legs (two lines each)
    # Left leg
    c.line(left_hip[0], left_hip[1], x + w * 0.46, y + h * 0.24)
    c.line(x + w * 0.46, y + h * 0.24, x + w * 0.45, y + h * 0.06)
    c.line(x + w * 0.45, y + h * 0.06, x + w * 0.49, y + h * 0.06)
    c.line(x + w * 0.49, y + h * 0.06, x + w * 0.49, y + h * 0.24)
    c.line(x + w * 0.49, y + h * 0.24, cx, y + h * 0.40)

    # Right leg
    c.line(right_hip[0], right_hip[1], x + w * 0.54, y + h * 0.24)
    c.line(x + w * 0.54, y + h * 0.24, x + w * 0.55, y + h * 0.06)
    c.line(x + w * 0.55, y + h * 0.06, x + w * 0.51, y + h * 0.06)
    c.line(x + w * 0.51, y + h * 0.06, x + w * 0.51, y + h * 0.24)
    c.line(x + w * 0.51, y + h * 0.24, cx, y + h * 0.40)

    # Dotted shave guide lines (reference-style)
    c.setDash(2, 3)
    c.setLineWidth(0.9)

    # Upper arm bands
    c.arc(x + w * 0.22, y + h * 0.60, x + w * 0.34, y + h * 0.70, 200,
          -20)  # left
    c.arc(x + w * 0.66, y + h * 0.60, x + w * 0.78, y + h * 0.70, 200,
          -20)  # right

    # Waist band
    c.arc(x + w * 0.38, y + h * 0.44, x + w * 0.62, y + h * 0.58, 200, -20)

    # Thigh bands
    c.arc(x + w * 0.41, y + h * 0.20, x + w * 0.50, y + h * 0.32, 200, -20)
    c.arc(x + w * 0.50, y + h * 0.20, x + w * 0.59, y + h * 0.32, 200, -20)

    # Calf bands
    c.arc(x + w * 0.42, y + h * 0.08, x + w * 0.49, y + h * 0.18, 200, -20)
    c.arc(x + w * 0.51, y + h * 0.08, x + w * 0.58, y + h * 0.18, 200, -20)

    c.setDash()
    c.restoreState()


def _draw_human_back(c: canvas.Canvas, x: float, y: float, w: float, h: float):
    _draw_human_front(c, x, y, w, h)
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(0.9)
    c.setDash(2, 3)
    cx = x + w * 0.50
    c.line(cx, y + h * 0.78, cx, y + h * 0.06)  # spine line
    c.setDash()
    c.restoreState()


# -----------------------------
# Main builder
# -----------------------------
def build_ot_preop_checklist_pdf_bytes(
    *,
    branding: UiBranding,
    case: Any,
    preop_data: Dict[str, Any],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    # margins
    m_top = 12 * mm
    m_bot = 12 * mm
    m_lr = 10 * mm

    x0 = m_lr
    w = page_w - (2 * m_lr)
    y = page_h - m_top

    preop_data = preop_data or {}
    name, age_sex, reg_no = _resolve_patient_fields(case, preop_data)

    # ✅ date/time MUST come from schedule date + planned_start_time
    schedule = _resolve_schedule(case)
    date_src = _get(schedule, "date", default=None) or _get(
        case, "scheduled_at", "created_at", default=None)
    time_src = _get(schedule, "planned_start_time", default=None) or _get(
        case, "scheduled_at", "created_at", default=None)
    dt_str = _fmt_date(date_src)
    tm_str = _fmt_time(time_src)

    checklist = preop_data.get("checklist") or {}
    inv = preop_data.get("investigations") or {}
    vit = preop_data.get("vitals") or {}
    shave = _norm_shave(preop_data.get("shave_completed"))
    nurse_signature = (preop_data.get("nurse_signature") or "").strip()

    # 1) header
    used = _draw_brand_header(c, branding, x0, y, w)
    y -= used + 4 * mm

    # 2) patient strip
    patient_h = 18 * mm
    _draw_patient_strip(
        c,
        x0,
        y,
        w,
        patient_h,
        name=name,
        age_sex=age_sex,
        reg_no=reg_no,
        dt_str=dt_str,
        tm_str=tm_str,
    )
    y -= patient_h + 2.5 * mm

    # 3) title
    title_h = 8 * mm
    _draw_title_bar(c, x0, y, w, title_h)
    y -= title_h

    # Bottom box reserved
    bottom_h = 74 * mm
    bottom_y = m_bot
    bottom_top = bottom_y + bottom_h
    gap = 3 * mm

    # Checklist table area
    table_top = y
    table_bottom = bottom_top + gap
    table_h = table_top - table_bottom

    # columns
    col_item = w * 0.50
    col_h = w * 0.14
    col_r = w * 0.14
    col_c = w - (col_item + col_h + col_r)

    x_item = x0
    x_h = x_item + col_item
    x_r = x_h + col_h
    x_c = x_r + col_r

    header_h = 11 * mm
    sig_h = 10 * mm
    n_items = len(CHECKLIST_ITEMS)

    row_h = (table_h - header_h - sig_h) / max(n_items, 1)
    if row_h < 6.2 * mm:
        row_h = 6.2 * mm

    table_h = header_h + (n_items * row_h) + sig_h
    table_bottom = table_top - table_h

    # outer
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.4)
    c.rect(x0, table_bottom, w, table_h, stroke=1, fill=0)

    # vertical lines
    c.setLineWidth(1.2)
    c.line(x_h, table_bottom, x_h, table_bottom + table_h)
    c.line(x_r, table_bottom, x_r, table_bottom + table_h)
    c.line(x_c, table_bottom, x_c, table_bottom + table_h)

    # header line
    y_row_top = table_top
    y_row_bot = y_row_top - header_h
    c.line(x0, y_row_bot, x0 + w, y_row_bot)

    _draw_wrapped_cell(
        c,
        x_h,
        y_row_bot,
        col_h,
        header_h,
        "HANDING\nOVER NURSE",
        font_name="Helvetica-Bold",
        start_size=8.8,
        max_lines=2,
        align="center",
        pad_x=1.0 * mm,
    )
    _draw_wrapped_cell(
        c,
        x_r,
        y_row_bot,
        col_r,
        header_h,
        "RECEIVING\nNURSE",
        font_name="Helvetica-Bold",
        start_size=8.8,
        max_lines=2,
        align="center",
        pad_x=1.0 * mm,
    )
    _draw_wrapped_cell(
        c,
        x_c,
        y_row_bot,
        col_c,
        header_h,
        "COMMENTS",
        font_name="Helvetica-Bold",
        start_size=9.2,
        max_lines=1,
        align="center",
        pad_x=1.0 * mm,
    )

    # items
    cb_size = 4.5 * mm
    y_cursor = y_row_bot

    for key, label in CHECKLIST_ITEMS:
        y_next = y_cursor - row_h
        c.line(x0, y_next, x0 + w, y_next)

        row = checklist.get(key) or {}
        handover = _as_bool(row.get("handover"))
        receiving = _as_bool(row.get("receiving"))
        comments = str(row.get("comments") or "")

        _draw_wrapped_cell(
            c,
            x_item,
            y_next,
            col_item,
            row_h,
            label,
            font_name="Helvetica-Bold",
            start_size=8.6,
            max_lines=2,
            align="left",
            pad_x=2.5 * mm,
        )

        cy = y_next + row_h / 2
        _draw_checkbox(c, x_h + col_h / 2, cy, cb_size, handover)
        _draw_checkbox(c, x_r + col_r / 2, cy, cb_size, receiving)

        _draw_wrapped_cell(
            c,
            x_c,
            y_next,
            col_c,
            row_h,
            comments,
            font_name="Helvetica",
            start_size=8.6,
            max_lines=1,
            align="left",
            pad_x=2.5 * mm,
        )

        y_cursor = y_next

    # signature row
    y_sig_bot = y_cursor - sig_h
    c.line(x0, y_sig_bot, x0 + w, y_sig_bot)

    _draw_wrapped_cell(
        c,
        x_item,
        y_sig_bot,
        col_item,
        sig_h,
        "SIGNATURE WITH NAME",
        font_name="Helvetica-Bold",
        start_size=9.2,
        max_lines=1,
        align="left",
        pad_x=2.5 * mm,
    )

    line_x1 = x_item + 50 * mm
    line_x2 = x0 + w - 6 * mm
    c.setLineWidth(1.0)
    c.line(line_x1, y_sig_bot + 3.2 * mm, line_x2, y_sig_bot + 3.2 * mm)

    c.setFillColor(TEXT)
    c.setFont("Helvetica", 9.2)
    c.drawString(line_x1 + 2 * mm, y_sig_bot + 4.0 * mm, nurse_signature[:70])

    # -----------------------------
    # Bottom box (Investigations / Vitals / Shave)
    # -----------------------------
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.4)
    c.rect(x0, bottom_y, w, bottom_h, stroke=1, fill=0)

    left_w = w * 0.42
    right_w = w - left_w
    split_x = x0 + left_w

    c.setLineWidth(1.2)
    c.line(split_x, bottom_y, split_x, bottom_y + bottom_h)

    mid_x = x0 + left_w / 2
    c.line(mid_x, bottom_y, mid_x, bottom_y + bottom_h)

    def draw_fields(title: str, fields: List[Tuple[str, str]], bx: float,
                    bw: float):
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(bx + 3 * mm, bottom_y + bottom_h - 7 * mm, title)

        yy = bottom_y + bottom_h - 13 * mm
        for k, v in fields:
            c.setFillColor(TEXT)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(bx + 3 * mm, yy, k)
            c.drawString(bx + 28 * mm, yy, ":")

            # ✅ no dotted underline (as requested)
            _draw_wrapped_cell(
                c,
                bx + 32 * mm,
                yy - 3.8,
                bw - 36 * mm,
                5.4 * mm,
                str(v or ""),
                font_name="Helvetica",
                start_size=9.0,
                max_lines=1,
                align="left",
                pad_x=0,
            )
            yy -= 6 * mm

    inv_fields = [
        ("HB", str(inv.get("hb", ""))),
        ("PLATELET", str(inv.get("platelet", ""))),
        ("UREA", str(inv.get("urea", ""))),
        ("CREATININE", str(inv.get("creatinine", ""))),
        ("POTASSIUM", str(inv.get("potassium", ""))),
        ("RBS", str(inv.get("rbs", ""))),
    ]
    vit_fields = [
        ("TEMP", str(vit.get("temp", ""))),
        ("PULSE", str(vit.get("pulse", ""))),
        ("RESP", str(vit.get("resp", ""))),
        ("BP", str(vit.get("bp", ""))),
        ("SPO2", str(vit.get("spo2", ""))),
        ("HEIGHT", str(vit.get("height", ""))),
        ("WEIGHT", str(vit.get("weight", ""))),
    ]

    draw_fields("INVESTIGATION", inv_fields, x0, left_w / 1)
    draw_fields("VITALS", vit_fields, mid_x, left_w / 1)

    # right side: shave + human diagrams
    rx = split_x
    ry = bottom_y

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(rx + right_w / 2, ry + bottom_h - 8 * mm,
                        "SHAVE COMPLETED YES / NO")

    yes_sel = (shave == "yes")
    no_sel = (shave == "no")
    _draw_yes_no_tick(c,
                      rx + right_w / 2,
                      ry + bottom_h - 14 * mm,
                      yes_selected=yes_sel,
                      no_selected=no_sel)

    # side labels (like reference)
    c.setFillColor(BLUE)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(rx + right_w * 0.14, ry + bottom_h - 18 * mm, "RIGHT")
    c.drawRightString(rx + right_w * 0.36, ry + bottom_h - 18 * mm, "LEFT")
    c.drawString(rx + right_w * 0.64, ry + bottom_h - 18 * mm, "LEFT")
    c.drawRightString(rx + right_w * 0.86, ry + bottom_h - 18 * mm, "RIGHT")
    c.setFillColor(TEXT)

    fig_h = bottom_h - 26 * mm
    fig_y = ry + 4 * mm
    fig_w = right_w / 2

    # ✅ final requirement: real human body (vector drawing), no PNG
    _draw_human_front(c, rx + 2 * mm, fig_y, fig_w - 4 * mm, fig_h)
    _draw_human_back(c, rx + fig_w + 2 * mm, fig_y, fig_w - 4 * mm, fig_h)

    c.showPage()
    c.save()
    return buf.getvalue()
