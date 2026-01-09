# FILE: app/services/pdfs/ot_anaesthesia_record_pdf.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Sequence, Tuple, Set
from zoneinfo import ZoneInfo
from pathlib import Path
import re

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.pdfbase import pdfmetrics

IST = ZoneInfo("Asia/Kolkata")

# -------------------------
# GOVERNMENT FORM STYLE (BLACK ONLY)
# -------------------------
BLACK = colors.black
WHITE = colors.white

TEXT = BLACK
FORM_LINE = BLACK

GRID_MINOR = colors.Color(0.0, 0.0, 0.0, alpha=0.06)  # chart-only (optional)
GRID_MAJOR = colors.Color(0.0, 0.0, 0.0, alpha=0.10)  # chart-only (optional)


# -------------------------
# Safe getters / formatters
# -------------------------
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


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("none", "null") else s


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


def _to_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST)


def _fmt_dt(v: Any) -> str:
    dt = _to_ist(_to_dt(v))
    return dt.strftime("%d-%b-%Y %I:%M %p") if dt else ""


# ✅ robust dd-mm-YYYY / dd/mm/YYYY / dd-Mon-YYYY / ISO → outputs dd-Mon-YYYY
_DDMMYYYY_RE = re.compile(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$")
_DD_MON_YYYY_RE = re.compile(
    r"^\s*(\d{1,2})[-\s]([A-Za-z]{3,})[-\s](\d{4})\s*$")


def _fmt_date(v: Any) -> str:
    dt = _to_ist(_to_dt(v))
    if dt:
        return dt.strftime("%d-%b-%Y")

    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return ""
        m = _DDMMYYYY_RE.match(s)
        if m:
            dd, mm_, yy = m.groups()
            try:
                d2 = date(int(yy), int(mm_), int(dd))
                return d2.strftime("%d-%b-%Y")
            except Exception:
                return s
        m2 = _DD_MON_YYYY_RE.match(s)
        if m2:
            return s
        dt2 = _to_ist(_to_dt(s))
        if dt2:
            return dt2.strftime("%d-%b-%Y")
        return s

    return ""


def _fmt_hhmm_from_dt(v: Any) -> str:
    dt = _to_ist(v if isinstance(v, datetime) else _to_dt(v))
    return dt.strftime("%H:%M") if dt else ""


def _sw(txt: str, font: str, fs: float) -> float:
    return pdfmetrics.stringWidth(txt or "", font, fs)


def _wrap_lines(txt: str, width: float, font: str, fs: float) -> List[str]:
    t = (txt or "").replace("\n", " ").strip()
    return simpleSplit(t, font, fs, width) or [""]


def _safe_ellipsis() -> str:
    # ASCII-only, avoids server font issues
    return "..."


def _ellipsize(txt: str, width: float, font: str, fs: float) -> str:
    t = (txt or "").strip()
    if not t:
        return ""
    if _sw(t, font, fs) <= width:
        return t

    ell = _safe_ellipsis()
    max_w = max(0.0, width - _sw(ell, font, fs))
    lo, hi = 0, len(t)
    while lo < hi:
        mid = (lo + hi) // 2
        if _sw(t[:mid], font, fs) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    cut = max(0, lo - 1)
    return (t[:cut].rstrip() + ell) if cut > 0 else ell


# -------------------------
# Branding helpers (UiBranding fields)
# -------------------------
def _read_brand_logo_bytes(branding: Any) -> Optional[bytes]:
    if branding is None:
        return None

    b = _get(branding, "logo_bytes", "logo_blob", "logo_data", default=None)
    if isinstance(b, (bytes, bytearray)) and len(b) > 0:
        return bytes(b)

    rel = _as_str(_get(branding, "logo_path", "logo_file", "logo", default=""))
    if not rel:
        return None

    try:
        from app.core.config import settings  # type: ignore

        p = Path(rel)
        if not p.is_absolute():
            p = Path(settings.STORAGE_DIR).joinpath(rel)
        if p.exists() and p.is_file():
            return p.read_bytes()
    except Exception:
        return None

    return None


def _branding_text_fields(branding: Any) -> Dict[str, str]:
    return {
        "org_name":
        _as_str(
            _get(branding,
                 "org_name",
                 "hospital_name",
                 "name",
                 default="HOSPITAL")),
        "org_tagline":
        _as_str(
            _get(branding,
                 "org_tagline",
                 "tagline",
                 "sub_title",
                 "subtitle",
                 default="")),
        "org_address":
        _as_str(_get(branding, "org_address", "address", "addr", default="")),
        "org_phone":
        _as_str(
            _get(branding,
                 "org_phone",
                 "phone",
                 "mobile",
                 "contact_no",
                 default="")),
        "org_email":
        _as_str(_get(branding, "org_email", "email", default="")),
        "org_website":
        _as_str(_get(branding, "org_website", "website", "url", default="")),
        "org_gstin":
        _as_str(_get(branding, "org_gstin", "gstin", default="")),
    }


# -------------------------
# Form drawing primitives (BLACK)
# -------------------------
def _ink(c: canvas.Canvas, lw: float = 1.0):
    c.setStrokeColor(FORM_LINE)
    c.setLineWidth(lw)


def _rect_form(c: canvas.Canvas,
               x: float,
               y_top: float,
               w: float,
               h: float,
               lw: float = 1.0):
    c.saveState()
    _ink(c, lw)
    c.rect(x, y_top - h, w, h, stroke=1, fill=0)
    c.restoreState()


def _hline_form(c: canvas.Canvas,
                x0: float,
                x1: float,
                y: float,
                lw: float = 1.0):
    c.saveState()
    _ink(c, lw)
    c.line(x0, y, x1, y)
    c.restoreState()


def _vline_form(c: canvas.Canvas,
                x: float,
                y0: float,
                y1: float,
                lw: float = 1.0):
    c.saveState()
    _ink(c, lw)
    c.line(x, y0, x, y1)
    c.restoreState()


def _wrap_block(txt: str, width: float, font: str, fs: float) -> List[str]:
    t = (txt or "").replace("\r", "").strip()
    if not t:
        return [""]
    out: List[str] = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue
        out.extend(simpleSplit(para, font, fs, width) or [""])
    return out or [""]


def _block_text_form(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    text: str,
    *,
    fs: float = 8.4,
    font: str = "Helvetica",
    pad_x: float = 2.6 * mm,
    pad_top: float = 5.5 * mm,
    pad_bottom: float = 2.6 * mm,
):
    max_w = max(0.0, w - 2 * pad_x)
    lines = _wrap_block(text or "", max_w, font, fs)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont(font, fs)

    yy = y_top - pad_top
    line_h = fs * 1.25
    bottom_limit = (y_top - h + pad_bottom)
    for ln in lines:
        if yy < bottom_limit:
            break
        c.drawString(x + pad_x, yy, ln)
        yy -= line_h

    c.restoreState()


def _field_line(
    c: canvas.Canvas,
    x: float,
    y_baseline: float,
    w: float,
    label: str,
    value: str,
    *,
    fs: float = 8.4,
    label_fs: float = 8.4,
    font_label: str = "Helvetica",
    font_val: str = "Helvetica",
):
    """
    ✅ Form-style label + value (NO underline)
    """
    pad = 1.6 * mm
    gap = 1.2 * mm
    label = (label or "").strip()
    value = (value or "").strip()

    # keep a guaranteed area for value
    min_value_w = 16.0 * mm
    max_label_w = max(0.0, w - (2 * pad + gap + min_value_w))

    lfs = float(label_fs)
    while lfs > 6.6 and _sw(label, font_label, lfs) > max_label_w:
        lfs -= 0.2

    # if still too long, ellipsize label itself
    if _sw(label, font_label, lfs) > max_label_w and max_label_w > 0:
        label = _ellipsize(label, max_label_w, font_label, lfs)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont(font_label, lfs)
    c.drawString(x + pad, y_baseline, label)

    vx = x + pad + _sw(label, font_label, lfs) + gap
    avail = max(0.0, (x + w - pad) - vx)

    if value and avail > 1:
        c.setFont(font_val, fs)
        c.drawString(vx, y_baseline, _ellipsize(value, avail, font_val, fs))
    c.restoreState()


def _parse_asa_grade(v: Any) -> Optional[int]:
    s = _as_str(v).upper()
    for d in ("1", "2", "3", "4", "5"):
        if d in s:
            return int(d)
    return None


def _draw_tick(c: canvas.Canvas,
               x: float,
               y: float,
               *,
               size: float = 3.0 * mm,
               lw: float = 1.2):
    """
    Draw a small ✓ tick inside a box. (x,y) is bottom-left of the box area.
    """
    c.saveState()
    _ink(c, lw)
    c.line(x + 0.65 * mm, y + 1.55 * mm, x + 1.35 * mm, y + 0.85 * mm)
    c.line(x + 1.35 * mm, y + 0.85 * mm, x + (size - 0.55 * mm),
           y + (size - 0.70 * mm))
    c.restoreState()


# -------------------------
# Checklists helpers (kept for future; page-1 checklist removed as per requirement)
# -------------------------
def _norm_key(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 +/()-]", "", s)
    return s


def _parse_str_list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, dict):
        out = []
        for k, vv in v.items():
            if _as_bool(vv):
                ks = str(k).strip()
                if ks:
                    out.append(ks)
        return out
    if isinstance(v, str):
        s = v.replace("\r", "\n")
        parts = re.split(r"[,;\n]+", s)
        return [p.strip() for p in parts if p.strip()]
    return [str(v).strip()] if str(v).strip() else []


def _selected_set_from_any(v: Any) -> Set[str]:
    return {_norm_key(x) for x in _parse_str_list(v) if _norm_key(x)}


# -------------------------
# Letterhead + Titles + Footer (BLACK)
# -------------------------
def _draw_brand_letterhead_template(
    c: canvas.Canvas,
    page_w: float,
    page_h: float,
    mx: float,
    my: float,
    branding: Any,
) -> float:
    x = mx
    y_top = page_h - my
    w = page_w - 2 * mx

    fields = _branding_text_fields(branding)
    org = (fields["org_name"] or "HOSPITAL").strip()
    tagline = (fields["org_tagline"] or "").strip()
    address = (fields["org_address"] or "").strip()
    phone = (fields["org_phone"] or "").strip()
    email = (fields["org_email"] or "").strip()
    website = (fields["org_website"] or "").strip()
    gstin = (fields["org_gstin"] or "").strip()

    header_h = 29.0 * mm
    logo_w = 72.0 * mm
    gap = 4.0 * mm

    y0 = y_top - header_h

    # LEFT: logo (starts from left edge)
    logo_bytes = _read_brand_logo_bytes(branding)
    if logo_bytes:
        try:
            c.drawImage(
                ImageReader(BytesIO(logo_bytes)),
                x,
                y0,
                logo_w,
                header_h,
                preserveAspectRatio=True,
                anchor="sw",  # ✅ left/bottom anchored inside the box area
                mask="auto",
            )
        except Exception:
            pass
    else:
        _rect_form(c, x, y_top, logo_w, header_h, lw=1.0)
        c.saveState()
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(x + logo_w / 2.0, y0 + header_h / 2.0 - 5, "")
        c.restoreState()

    # RIGHT: org details aligned to END (right edge)
    rx0 = x + logo_w + gap
    rx1 = x + w  # ✅ right edge for "end alignment"
    rw = max(1.0, rx1 - rx0)  # width available

    yy = y_top - 5.0 * mm
    c.saveState()
    c.setFillColor(TEXT)

    # Org name (right aligned)
    c.setFont("Helvetica-Bold", 14.0)
    c.drawRightString(rx1, yy,
                      _ellipsize(org.upper(), rw, "Helvetica-Bold", 14.0))
    yy -= 6.0 * mm

    # Tagline (right aligned)
    if tagline:
        c.setFont("Helvetica-Bold", 9.2)
        c.drawRightString(rx1, yy,
                          _ellipsize(tagline, rw, "Helvetica-Bold", 9.2))
        yy -= 4.6 * mm

    # Address (max 2 lines, right aligned)
    if address:
        c.setFont("Helvetica", 8.4)
        addr_lines = simpleSplit(address, "Helvetica", 8.4, rw)[:2]
        for ln in addr_lines:
            c.drawRightString(rx1, yy, _ellipsize(ln, rw, "Helvetica", 8.4))
            yy -= 3.9 * mm

    # Phone (right aligned)
    if phone:
        c.setFont("Helvetica-Bold", 8.6)
        c.drawRightString(rx1, yy, _ellipsize(phone, rw, "Helvetica-Bold",
                                              8.6))
        yy -= 4.0 * mm

    # Email | Website (single line, right aligned)
    info_bits = []
    if email:
        info_bits.append(email)
    if website:
        info_bits.append(website)
    if info_bits:
        info = "  |  ".join(info_bits)
        fs = 8.4
        while fs > 7.0 and _sw(info, "Helvetica", fs) > rw:
            fs -= 0.2
        c.setFont("Helvetica", fs)
        c.drawRightString(rx1, yy, _ellipsize(info, rw, "Helvetica", fs))
        yy -= 3.9 * mm

    # GSTIN (right aligned)
    if gstin:
        c.setFont("Helvetica", 8.2)
        c.drawRightString(rx1, yy,
                          _ellipsize(f"GSTIN: {gstin}", rw, "Helvetica", 8.2))
        yy -= 3.6 * mm

    c.restoreState()

    # Bottom rule
    y_line = y0 - 2.0 * mm
    _hline_form(c, x, x + w, y_line, lw=1.2)
    return y_line


def _draw_page_title(
    c: canvas.Canvas,
    *,
    x: float,
    w: float,
    y_top: float,
    title: str,
    subtitle: str = "",
) -> float:
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 14.5)
    c.drawCentredString(x + w / 2.0, y_top - 6.0 * mm, title)
    y = y_top - 10.0 * mm
    if subtitle:
        c.setFont("Helvetica", 9.2)
        c.drawCentredString(x + w / 2.0, y, subtitle)
        y -= 4.0 * mm
    c.restoreState()

    y_line = y - 2.0 * mm
    _hline_form(c, x, x + w, y_line, lw=1.0)
    return y_line


def _draw_footer(
    c: canvas.Canvas,
    page_w: float,
    my: float,
    *,
    page_no: int,
    total_pages: Optional[int] = None,
):
    y = my - 3.0 * mm
    if y < 4 * mm:
        y = 4 * mm
    stamp = datetime.now(IST).strftime("%d-%b-%Y %I:%M %p")
    right = f"Page {page_no}" + (f" / {total_pages}" if total_pages else "")
    c.saveState()
    c.setFont("Helvetica", 7.6)
    c.setFillColor(TEXT)
    c.drawString(10 * mm, y, f"Generated on {stamp}")
    c.drawRightString(page_w - 10 * mm, y, right)
    c.restoreState()


# -------------------------
# Patient strip (BLACK)
# -------------------------
def _draw_patient_strip(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    case: Any,
) -> float:
    h = 10.0 * mm
    _rect_form(c, x, y_top, w, h, lw=1.0)

    prefix = _as_str(
        record.get("patient_prefix") or patient_fields.get("patient_prefix")
        or patient_fields.get("prefix") or patient_fields.get("title")
        or patient_fields.get("salutation"))
    p_name = _as_str(
        patient_fields.get("name") or patient_fields.get("patient_name"))
    patient_name = (f"{prefix} {p_name}").strip(
    ) if prefix and not p_name.lower().startswith(prefix.lower()) else p_name

    uhid = _as_str(
        patient_fields.get("uhid") or patient_fields.get("patient_id")
        or patient_fields.get("uid"))
    age_sex = _as_str(patient_fields.get("age_sex") or "")

    dt_val = _pick_anaesthetic_created_date(record, case)

    case_no = _as_str(
        record.get("case_no") or patient_fields.get("case_no")
        or _get(case, "case_no", "case_number", "number", default=""))
    or_no = _as_str(
        record.get("or_no") or patient_fields.get("or_no")
        or _get(case, "or_no", "ot_room", default=""))

    cols = [
        ("Patient", patient_name),
        ("UHID", uhid),
        ("Age/Sex", age_sex),
        ("Created Date", dt_val),
        ("Case", case_no),
        ("OR", or_no),
    ]
    col_w = [w * 0.26, w * 0.14, w * 0.12, w * 0.16, w * 0.18, w * 0.14]

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 8.0)
    y_lab = y_top - 3.8 * mm
    y_val = y_top - 8.0 * mm
    xx = x
    for (lab, val), cw in zip(cols, col_w):
        c.drawString(xx + 2 * mm, y_lab, lab)
        c.setFont("Helvetica", 8.4)
        c.drawString(xx + 2 * mm, y_val,
                     _ellipsize(_as_str(val), cw - 4 * mm, "Helvetica", 8.4))
        c.setFont("Helvetica-Bold", 8.0)
        xx += cw
        if xx < x + w - 0.1:
            _vline_form(c, xx, y_top, y_top - h, lw=0.9)
    c.restoreState()

    return y_top - h


# -------------------------
# Date pickers (FIXED)
# -------------------------
def _pick_anaesthetic_created_date(record: Dict[str, Any], case: Any) -> str:
    """
    ✅ "Anaesthetic Created Date" (tries many keys)
    """
    cand = (record.get("anaesthetic_created_at")
            or record.get("anaesthetic_created_on") or record.get("created_at")
            or record.get("createdAt") or record.get("created_on")
            or record.get("createdOn") or record.get("created_date")
            or record.get("createdDate") or record.get("created_datetime")
            or record.get("createdDatetime") or _get(case,
                                                     "created_at",
                                                     "created_on",
                                                     "created_date",
                                                     "createdAt",
                                                     default=None)
            or record.get("ot_date") or record.get("date") or _get(
                case,
                "ot_date",
                "scheduled_date",
                "scheduled_at",
                "start_time",
                "start_at",
                "date",
                default=None,
            ))
    return _fmt_date(cand)


# -------------------------
# Yes/No choice helper (BLACK)
# -------------------------
def _yes_no_choice(
    c: canvas.Canvas,
    x: float,
    y: float,
    *,
    selected: Optional[bool],
    label: str = "Anticipated :",
):
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 8.6)
    c.drawString(x, y, label)

    base_x = x + 28.0 * mm
    r = 2.6 * mm

    c.drawString(base_x + 6.0 * mm, y, "Yes")
    _ink(c, 1.0)
    c.circle(base_x, y + 2.1, r, stroke=1, fill=0)
    if selected is True:
        c.saveState()
        _ink(c, 1.2)
        c.circle(base_x, y + 2.1, r - 1.0, stroke=1, fill=1)
        c.restoreState()

    nx = base_x + 20.0 * mm
    c.drawString(nx + 6.0 * mm, y, "No")
    _ink(c, 1.0)
    c.circle(nx, y + 2.1, r, stroke=1, fill=0)
    if selected is False:
        c.saveState()
        _ink(c, 1.2)
        c.circle(nx, y + 2.1, r - 1.0, stroke=1, fill=1)
        c.restoreState()

    c.restoreState()


# -------------------------
# Tables / Boxes (LEGACY - used by old intra-op; kept)
# -------------------------
def _box(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    title: str,
    *,
    title_h: float = 8.0 * mm,
):
    _rect_form(c, x, y_top, w, h, lw=1.0)
    _hline_form(c, x, x + w, y_top - title_h, lw=1.0)
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.4)
    c.drawString(x + 3.0 * mm, y_top - 5.6 * mm, title)
    c.restoreState()


def _table(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    col_w: Sequence[float],
    *,
    fs: float = 7.8,
) -> float:
    h_h = 7.5 * mm

    _rect_form(c, x, y_top, w, h_h, lw=1.0)
    xx = x
    for i in range(len(headers) - 1):
        xx += col_w[i]
        _vline_form(c, xx, y_top, y_top - h_h, lw=0.9)

    c.saveState()
    c.setFont("Helvetica-Bold", fs)
    c.setFillColor(TEXT)
    xx = x
    for i, htxt in enumerate(headers):
        c.drawString(xx + 2 * mm, y_top - 5.0 * mm, _as_str(htxt))
        xx += col_w[i]
    c.restoreState()

    y = y_top - h_h

    for r in rows:
        max_lines = 1
        cells: List[List[str]] = []
        for i, cell in enumerate(r):
            lines = _wrap_lines(_as_str(cell), col_w[i] - 4 * mm, "Helvetica",
                                fs)
            max_lines = max(max_lines, len(lines))
            cells.append(lines)

        row_h = max(6.5 * mm, (max_lines * fs * 1.25) + 2.4 * mm)
        _rect_form(c, x, y, w, row_h, lw=0.9)

        xx = x
        for i in range(len(headers) - 1):
            xx += col_w[i]
            _vline_form(c, xx, y, y - row_h, lw=0.8)

        c.saveState()
        c.setFont("Helvetica", fs)
        c.setFillColor(TEXT)
        xx = x
        for i, lines in enumerate(cells):
            yy = y - 3.0 * mm - fs
            for ln in lines[:10]:
                c.drawString(xx + 2 * mm, yy, ln)
                yy -= fs * 1.25
            xx += col_w[i]
        c.restoreState()

        y -= row_h

    return y


def _table_paginated(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    bottom_y: float,
    headers: Sequence[str],
    rows: List[List[str]],
    col_w: Sequence[float],
    fs: float,
) -> float:
    h_h = 7.5 * mm
    avail = y_top - bottom_y
    if avail <= h_h + 8 * mm:
        _table(c, x, y_top, w, headers, rows[:1], col_w, fs=fs)
        del rows[:1]
        return bottom_y

    chunk: List[List[str]] = []
    y_cursor = y_top - h_h

    def est_row_h(r: List[str]) -> float:
        max_lines = 1
        for i, cell in enumerate(r):
            lines = _wrap_lines(_as_str(cell), col_w[i] - 4 * mm, "Helvetica",
                                fs)
            max_lines = max(max_lines, len(lines))
        return max(6.5 * mm, (max_lines * fs * 1.25) + 2.4 * mm)

    while rows:
        rh = est_row_h(rows[0])
        if (y_cursor - rh) < bottom_y and chunk:
            break
        if (y_cursor - rh) < bottom_y and not chunk:
            chunk.append(rows[0])
            del rows[0]
            break
        chunk.append(rows[0])
        del rows[0]
        y_cursor -= rh

    y_after = _table(c, x, y_top, w, headers, chunk, col_w, fs=fs)
    return y_after


# -------------------------
# Chart grid (USED ONLY IN CHART)
# -------------------------
def _paper_grid(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    *,
    minor: float = 4 * mm,
    major: float = 12 * mm,
    minor_alpha: float = 0.06,
    major_alpha: float = 0.10,
):
    y0 = y_top - h
    x0 = x
    x1 = x + w
    y1 = y_top

    c.saveState()
    c.setStrokeColor(BLACK)

    c.setLineWidth(0.18)
    if hasattr(c, "setStrokeAlpha"):
        c.setStrokeAlpha(minor_alpha)
    xx = x0
    while xx <= x1 + 0.1:
        c.line(xx, y0, xx, y1)
        xx += minor
    yy = y0
    while yy <= y1 + 0.1:
        c.line(x0, yy, x1, yy)
        yy += minor

    c.setLineWidth(0.28)
    if hasattr(c, "setStrokeAlpha"):
        c.setStrokeAlpha(major_alpha)
    xx = x0
    while xx <= x1 + 0.1:
        c.line(xx, y0, xx, y1)
        xx += major
    yy = y0
    while yy <= y1 + 0.1:
        c.line(x0, yy, x1, yy)
        yy += major

    c.restoreState()


# -------------------------
# PRE-ANAESTHETIC SHEET (Page-1) - DO NOT CHANGE
# -------------------------
def _draw_preanaesthetic_record_sheet_page(
    c: canvas.Canvas,
    *,
    page_w: float,
    page_h: float,
    mx: float,
    my: float,
    branding: Any,
    case: Any,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    airway_names: Optional[List[str]] = None,
    monitor_names: Optional[List[str]] = None,
):
    # ✅ PRE-OP UI MUST REMAIN UNCHANGED (your existing perfect page)
    # (kept exactly as you pasted)
    x = mx
    y_top = page_h - my
    w = page_w - 2 * mx

    # Letterhead
    y_line = _draw_brand_letterhead_template(c, page_w, page_h, mx, my,
                                             branding)

    # Main form box
    form_top = y_line - 4.0 * mm
    form_bottom = my + 5.0 * mm
    form_h = form_top - form_bottom
    _rect_form(c, x, form_top, w, form_h, lw=1.2)

    # Row 1: title
    title_h = 10.0 * mm
    y_title_bottom = form_top - title_h
    _hline_form(c, x, x + w, y_title_bottom, lw=1.1)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 15.0)
    c.drawCentredString(x + w / 2.0, form_top - 6.8 * mm, "ANAESTHETIC RECORD")
    c.restoreState()

    # Row 2: Pre Anaesthetic + ASA PS block
    hdr_h = 10.0 * mm
    y_hdr_bottom = y_title_bottom - hdr_h
    _hline_form(c, x, x + w, y_hdr_bottom, lw=1.1)

    right_col_w = 80.0 * mm
    x_split = x + w - right_col_w

    _vline_form(c, x_split, y_hdr_bottom, form_bottom, lw=1.2)

    x_asa = x_split
    asa_label_w = 16.0 * mm
    _vline_form(c, x_asa + asa_label_w, y_title_bottom, y_hdr_bottom, lw=1.0)

    y_asa_mid = y_hdr_bottom + hdr_h * 0.48
    _hline_form(c, x_asa + asa_label_w, x + w, y_asa_mid, lw=1.0)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 10.0)
    c.drawCentredString(x + (x_split - x) / 2.0, y_title_bottom - 6.7 * mm,
                        "Pre Anaesthetic Record")
    c.setFont("Helvetica-Bold", 9.2)
    c.drawCentredString(x_asa + asa_label_w / 2.0, y_title_bottom - 6.7 * mm,
                        "ASA PS")
    c.restoreState()

    asa_grade = _parse_asa_grade(record.get("asa_grade"))
    raw_emg = record.get(
        "asa_emergency") if "asa_emergency" in record else record.get(
            "is_emergency")
    asa_emg: Optional[bool] = None if raw_emg in (None,
                                                  "") else _as_bool(raw_emg)

    gx0 = x_asa + asa_label_w
    gx1 = x + w
    span = max(1.0, gx1 - gx0)
    step = span / 5.0

    num_y = y_title_bottom - 4.2 * mm
    grade_r = 2.4 * mm

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.4)
    for i in range(5):
        n = i + 1
        cx = gx0 + step * (i + 0.5)
        c.drawCentredString(cx, num_y, str(n))
        if asa_grade == n:
            _ink(c, 1.0)
            c.circle(cx, num_y + 0.9 * mm, grade_r, stroke=1, fill=0)
    c.restoreState()

    e_box = 3.0 * mm
    e_center_y = (y_hdr_bottom + y_asa_mid) / 2.0
    e_box_x = gx0 + 10.0 * mm
    e_box_y = e_center_y - e_box / 2.0

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(gx0 + 2.5 * mm, e_center_y - 2.0, "E")
    _ink(c, 1.0)
    c.rect(e_box_x, e_box_y, e_box, e_box, stroke=1, fill=0)
    if asa_emg is True:
        _draw_tick(c, e_box_x, e_box_y, size=e_box, lw=1.2)
    c.restoreState()

    # Patient + Date/OR/Case area
    info_h = 36.0 * mm
    y_info_bottom = y_hdr_bottom - info_h
    _hline_form(c, x, x + w, y_info_bottom, lw=1.1)

    y_info_mid = y_hdr_bottom - (info_h / 2.0)
    _hline_form(c, x, x_split, y_info_mid, lw=1.0)

    x_age = x + (x_split - x) * 0.62
    _vline_form(c, x_age, y_hdr_bottom, y_info_mid, lw=1.0)

    x_opip = x + (x_split - x) * 0.55
    _vline_form(c, x_opip, y_info_mid, y_info_bottom, lw=1.0)

    list_h = 20.0 * mm
    y_list_bottom = y_hdr_bottom - list_h
    _hline_form(c, x_split, x + w, y_list_bottom, lw=1.0)

    grid_h = info_h - list_h
    grid_row_h = grid_h / 2.0
    y_grid_mid = y_list_bottom - grid_row_h
    _hline_form(c, x_split, x + w, y_grid_mid, lw=1.0)

    x_rmid = x_split + (right_col_w / 2.0)
    _vline_form(c, x_rmid, y_list_bottom, y_info_bottom, lw=1.0)

    prefix = _as_str(
        record.get("patient_prefix") or patient_fields.get("patient_prefix")
        or patient_fields.get("prefix") or patient_fields.get("title")
        or patient_fields.get("salutation"))
    p_name = _as_str(
        patient_fields.get("name") or patient_fields.get("patient_name"))
    patient_name = (f"{prefix} {p_name}").strip(
    ) if prefix and not p_name.lower().startswith(prefix.lower()) else p_name

    age_sex = _as_str(patient_fields.get("age_sex") or "")
    op_no = _as_str(patient_fields.get("op_no"))
    ip_no = _as_str(patient_fields.get("ip_no"))

    dt_val = _pick_anaesthetic_created_date(record, case)
    or_no = _as_str(
        record.get("or_no") or patient_fields.get("or_no")
        or _get(case, "or_no", "ot_room", default=""))
    case_no = _as_str(
        record.get("case_no") or patient_fields.get("case_no")
        or _get(case, "case_no", "case_number", "number", default=""))

    weight = _as_str(
        record.get("weight") or record.get("weight_kg")
        or patient_fields.get("weight"))
    height = _as_str(
        record.get("height") or record.get("height_cm")
        or patient_fields.get("height"))
    hb = _as_str(record.get("hb") or record.get("haemoglobin"))
    bgrp = _as_str(
        record.get("blood_group") or patient_fields.get("blood_group"))

    _field_line(c,
                x,
                y_hdr_bottom - 7.0 * mm,
                x_age - x,
                "Patient name :",
                patient_name,
                fs=8.6,
                label_fs=8.6)
    _field_line(c,
                x_age,
                y_hdr_bottom - 7.0 * mm,
                x_split - x_age,
                "Age / Sex :",
                age_sex,
                fs=8.6,
                label_fs=8.6)

    _field_line(c,
                x,
                y_info_mid - 7.0 * mm,
                x_opip - x,
                "OP No :",
                op_no,
                fs=8.6,
                label_fs=8.6)
    _field_line(c,
                x_opip,
                y_info_mid - 7.0 * mm,
                x_split - x_opip,
                "IP No :",
                ip_no,
                fs=8.6,
                label_fs=8.6)

    y_date = y_hdr_bottom - 6.8 * mm
    y_or = y_date - 6.2 * mm
    y_case = y_or - 6.2 * mm

    _field_line(c,
                x_split,
                y_date,
                right_col_w,
                "Date :",
                dt_val,
                fs=8.6,
                label_fs=8.6)
    _field_line(c,
                x_split,
                y_or,
                right_col_w,
                "OR No :",
                or_no,
                fs=8.6,
                label_fs=8.6)
    _field_line(c,
                x_split,
                y_case,
                right_col_w,
                "Case No :",
                case_no,
                fs=8.6,
                label_fs=8.6)

    r1_top = y_list_bottom
    r2_top = y_grid_mid
    _field_line(c,
                x_split,
                r1_top - 6.4 * mm,
                right_col_w / 2.0,
                "Ht :",
                height,
                fs=8.4,
                label_fs=8.4)
    _field_line(c,
                x_rmid,
                r1_top - 6.4 * mm,
                right_col_w / 2.0,
                "Wt :",
                weight,
                fs=8.4,
                label_fs=8.4)
    _field_line(c,
                x_split,
                r2_top - 6.4 * mm,
                right_col_w / 2.0,
                "Hb :",
                hb,
                fs=8.4,
                label_fs=8.4)
    _field_line(c,
                x_rmid,
                r2_top - 6.4 * mm,
                right_col_w / 2.0,
                "Blood :",
                bgrp,
                fs=8.4,
                label_fs=8.4)

    # Diagnosis + Proposed operation row
    diag_h = 12.0 * mm
    y_diag_bottom = y_info_bottom - diag_h
    _hline_form(c, x, x + w, y_diag_bottom, lw=1.1)

    diag = _as_str(record.get("diagnosis") or patient_fields.get("diagnosis"))
    proposed = _as_str(
        record.get("proposed_operation")
        or patient_fields.get("proposed_operation")
        or _get(case, "procedure_name", "operation", "procedure", default=""))

    _field_line(c,
                x,
                y_info_bottom - 7.2 * mm,
                x_split - x,
                "Diagnosis :",
                diag,
                fs=8.6,
                label_fs=8.6)
    _field_line(c,
                x_split,
                y_info_bottom - 7.2 * mm,
                right_col_w,
                "Proposed operation :",
                proposed,
                fs=8.6,
                label_fs=8.6)

    # Right column: Investigation reports
    body_top = y_diag_bottom
    body_h = body_top - form_bottom

    inv_txt = _as_str(
        record.get("investigation_reports") or record.get("investigations"))
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.4)
    c.drawString(x_split + 2.5 * mm, body_top - 6.0 * mm,
                 "Investigation Reports")
    c.restoreState()

    _block_text_form(
        c,
        x_split,
        body_top,
        right_col_w,
        body_h,
        inv_txt,
        fs=8.4,
        pad_top=10.0 * mm,
        pad_bottom=2.6 * mm,
    )

    # Left column: Physical Exam + Airway Exam + Bottom 3 rows
    left_w = x_split - x

    phys_h = 56.0 * mm
    airway_h = 42.0 * mm  # ✅ slightly more room to avoid any squeeze
    min_bottom_h = 60.0 * mm

    total_left_h = body_h
    fixed = phys_h + airway_h
    if total_left_h - fixed < min_bottom_h:
        usable = max(40.0 * mm, total_left_h - min_bottom_h)
        phys_h = usable * 0.58
        airway_h = usable * 0.42

    y_phys_bottom = body_top - phys_h
    y_airway_bottom = y_phys_bottom - airway_h

    _hline_form(c, x, x_split, y_phys_bottom, lw=1.1)
    _hline_form(c, x, x_split, y_airway_bottom, lw=1.1)

    # Physical Exam
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.6)
    c.drawString(x + 2.5 * mm, body_top - 6.0 * mm, "Physical Exam")
    c.restoreState()

    pulse = _as_str(record.get("preop_pulse"))
    bp = _as_str(record.get("preop_bp"))
    rr = _as_str(record.get("preop_rr"))
    temp = _as_str(record.get("preop_temp_c"))
    veins = _as_str(record.get("preop_veins"))
    spine = _as_str(record.get("preop_spine"))
    cvs = _as_str(record.get("preop_cvs"))
    rs = _as_str(record.get("preop_rs"))
    cns = _as_str(record.get("preop_cns"))
    pa = _as_str(record.get("preop_pa"))

    pad_x = 2.5 * mm
    col_w = (left_w - 2 * pad_x) / 3.0
    row_gap = 10.0 * mm
    yy = body_top - 16.0 * mm

    _field_line(c, x + pad_x + col_w * 0, yy, col_w, "Pulse :", pulse)
    _field_line(c, x + pad_x + col_w * 1, yy, col_w, "BP :", bp)
    _field_line(c, x + pad_x + col_w * 2, yy, col_w, "RR :", rr)

    yy -= row_gap
    _field_line(c, x + pad_x + col_w * 0, yy, col_w, "Temp :", temp)
    _field_line(c, x + pad_x + col_w * 1, yy, col_w, "Veins :", veins)
    _field_line(c, x + pad_x + col_w * 2, yy, col_w, "Spine :", spine)

    yy -= row_gap
    _field_line(c, x + pad_x + col_w * 0, yy, col_w, "CVS :", cvs)
    _field_line(c, x + pad_x + col_w * 1, yy, col_w, "RS :", rs)
    _field_line(c, x + pad_x + col_w * 2, yy, col_w, "CNS :", cns)

    yy -= row_gap
    _field_line(c, x + pad_x + col_w * 0, yy, col_w, "PA :", pa)

    # Airway Examination
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.6)
    c.drawString(x + 2.5 * mm, y_phys_bottom - 6.0 * mm, "Airway Examination")
    c.restoreState()

    teeth = _as_str(record.get("airway_teeth_status")) or "intact / Loose"
    denture = _as_str(record.get("airway_denture"))
    neck = _as_str(record.get("airway_neck_movements"))
    mall = _as_str(record.get("airway_mallampati_class"))
    raw_diff = record.get("difficult_airway_anticipated")
    diff_sel: Optional[bool] = None if raw_diff in (None,
                                                    "") else _as_bool(raw_diff)

    half = (left_w - 2 * pad_x) / 2.0
    ax1 = x + pad_x
    ax2 = x + pad_x + half + pad_x

    yy2 = y_phys_bottom - 16.0 * mm
    gap2 = 10.0 * mm

    _field_line(c, ax1, yy2, half, "Teeth :", teeth)
    _field_line(c, ax2, yy2, half, "Denture :", denture)

    yy2 -= gap2
    _field_line(c, ax1, yy2, half, "Neck movements :", neck)
    _field_line(c, ax2, yy2, half, "Mallampati :", mall)

    # Mallampati classes line under right
    class_line_y = yy2 - 4.8 * mm
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 8.2)
    c.drawString(ax2, class_line_y, "Class 1 / Class 2 / Class 3 / Class 4")
    c.restoreState()

    # ✅ Difficult airway block (SAFE placement)
    airway_top = y_phys_bottom
    airway_bottom = y_airway_bottom

    y_floor = airway_bottom + 2.0 * mm
    y_ceiling = class_line_y - 2.0 * mm

    gap_da = 6.0 * mm
    y_heading = y_ceiling
    y_yes = y_heading - gap_da

    if y_yes < y_floor:
        y_yes = y_floor
        y_heading = y_yes + gap_da

    if y_heading > y_ceiling:
        gap_da2 = 5.0 * mm
        y_heading = y_ceiling
        y_yes = max(y_floor, y_heading - gap_da2)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(ax1, y_heading, "Difficult airway")
    c.restoreState()

    _yes_no_choice(c, ax1, y_yes, selected=diff_sel, label="Anticipated :")

    # Bottom 3 rows: Risk Factor / Anaesthetic Plan / Pre OP instruction
    bottom_top = y_airway_bottom
    bottom_h = bottom_top - form_bottom
    row_h = bottom_h / 3.0

    _hline_form(c, x, x_split, bottom_top - row_h, lw=1.0)
    _hline_form(c, x, x_split, bottom_top - 2 * row_h, lw=1.0)

    rf = _as_str(record.get("risk_factors"))
    plan = _as_str(
        record.get("anaesthetic_plan_detail")
        or record.get("anaesthetic_plan"))
    inst = _as_str(record.get("preop_instructions"))

    labels = ["Risk Factor", "Anaesthetic Plan", "Pre OP Instruction"]
    values = [rf, plan, inst]

    for i, (lab, val) in enumerate(zip(labels, values)):
        row_top = bottom_top - (i * row_h)

        c.saveState()
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 10.0)
        c.drawCentredString(x + left_w / 2.0, row_top - 6.0 * mm, lab)
        c.restoreState()

        _block_text_form(
            c,
            x,
            row_top,
            left_w,
            row_h,
            val,
            fs=8.2,
            pad_top=11.0 * mm,
            pad_bottom=2.6 * mm,
            pad_x=3.0 * mm,
        )


# -------------------------
# Vitals chart helpers
# -------------------------
def _try_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _parse_bp(bp: Any) -> Tuple[Optional[float], Optional[float]]:
    if bp is None or bp == "":
        return None, None
    if isinstance(bp, (int, float)):
        return float(bp), None
    s = str(bp).strip()
    if "/" in s:
        a, b = s.split("/", 1)
        return _try_float(a), _try_float(b)
    return _try_float(s), None


def _vital_time_minutes(v: Dict[str, Any]) -> Optional[int]:
    t = v.get("time")
    if isinstance(t, str) and ":" in t:
        try:
            hh, mm_ = t.strip().split(":", 1)
            return int(hh) * 60 + int(mm_)
        except Exception:
            pass

    dt = v.get("time_dt") or v.get("time")
    dt2 = _to_ist(dt if isinstance(dt, datetime) else _to_dt(dt))
    if dt2:
        return dt2.hour * 60 + dt2.minute
    return None


def _nice_step_minutes(span_min: int) -> int:
    if span_min <= 20:
        return 2
    if span_min <= 45:
        return 5
    if span_min <= 90:
        return 10
    if span_min <= 180:
        return 15
    if span_min <= 360:
        return 30
    return 60


def _hhmm_from_minutes(m: int) -> str:
    hh = (m // 60) % 24
    mm_ = m % 60
    return f"{hh:02d}:{mm_:02d}"


def _vitals_time_range(vitals: List[Dict[str, Any]]) -> str:
    mins: List[int] = []
    for v in vitals or []:
        tm = _vital_time_minutes(v)
        if tm is not None:
            mins.append(tm)
    if not mins:
        return ""
    mins.sort()
    return f"{_hhmm_from_minutes(mins[0])} - {_hhmm_from_minutes(mins[-1])}"


# -------------------------
# ✅ NEW INTRA-OP UI PRIMITIVES (Premium Government Form, Black Only)
# -------------------------
def _box_intraop(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    title: str,
    *,
    right_note: str = "",
    title_h: float = 8.6 * mm,
):
    # outer frame
    _rect_form(c, x, y_top, w, h, lw=1.15)
    # title rule
    _hline_form(c, x, x + w, y_top - title_h, lw=1.0)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 9.8)
    c.drawString(x + 3.0 * mm, y_top - 5.8 * mm, title)

    if right_note:
        c.setFont("Helvetica", 8.2)
        c.drawRightString(x + w - 3.0 * mm, y_top - 5.8 * mm,
                          _ellipsize(right_note, w * 0.48, "Helvetica", 8.2))
    c.restoreState()


def _trim_cell_lines(lines: List[str], max_lines: int, width: float, font: str,
                     fs: float) -> List[str]:
    if max_lines <= 0:
        return [""]
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    # ellipsize last visible line
    kept[-1] = _ellipsize(kept[-1], width, font, fs)
    return kept


def _table_intraop(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    col_w: Sequence[float],
    *,
    fs: float = 8.0,
    max_lines: int = 2,
) -> float:
    header_h = 8.2 * mm

    # header frame
    _rect_form(c, x, y_top, w, header_h, lw=1.0)
    xx = x
    for i in range(len(headers) - 1):
        xx += col_w[i]
        _vline_form(c, xx, y_top, y_top - header_h, lw=0.9)

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", fs)
    xx = x
    for i, htxt in enumerate(headers):
        c.drawString(xx + 2.0 * mm, y_top - 5.3 * mm, _as_str(htxt))
        xx += col_w[i]
    c.restoreState()

    y = y_top - header_h

    # body rows
    for r in rows:
        # compute max lines (capped) per row
        row_max = 1
        cells: List[List[str]] = []
        for i, cell in enumerate(r):
            cw = max(1.0, col_w[i] - 4 * mm)
            lines = _wrap_lines(_as_str(cell), cw, "Helvetica", fs)
            lines = _trim_cell_lines(lines, max_lines, cw, "Helvetica", fs)
            row_max = max(row_max, len(lines))
            cells.append(lines)

        row_h = max(7.0 * mm, (row_max * fs * 1.20) + 3.0 * mm)

        _rect_form(c, x, y, w, row_h, lw=0.9)
        xx = x
        for i in range(len(headers) - 1):
            xx += col_w[i]
            _vline_form(c, xx, y, y - row_h, lw=0.75)

        c.saveState()
        c.setFillColor(TEXT)
        c.setFont("Helvetica", fs)
        xx = x
        for i, lines in enumerate(cells):
            yy = y - 3.0 * mm - (fs * 0.2)
            for ln in lines:
                c.drawString(xx + 2.0 * mm, yy, ln)
                yy -= fs * 1.20
            xx += col_w[i]
        c.restoreState()

        y -= row_h

    return y


def _table_paginated_intraop(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    bottom_y: float,
    headers: Sequence[str],
    rows: List[List[str]],
    col_w: Sequence[float],
    fs: float,
    max_lines: int = 2,
) -> float:
    header_h = 8.2 * mm
    avail = y_top - bottom_y
    if avail <= header_h + 10 * mm:
        _table_intraop(c,
                       x,
                       y_top,
                       w,
                       headers,
                       rows[:1],
                       col_w,
                       fs=fs,
                       max_lines=max_lines)
        del rows[:1]
        return bottom_y

    chunk: List[List[str]] = []
    y_cursor = y_top - header_h

    def est_row_h(r: List[str]) -> float:
        row_max = 1
        for i, cell in enumerate(r):
            cw = max(1.0, col_w[i] - 4 * mm)
            lines = _wrap_lines(_as_str(cell), cw, "Helvetica", fs)
            lines = _trim_cell_lines(lines, max_lines, cw, "Helvetica", fs)
            row_max = max(row_max, len(lines))
        return max(7.0 * mm, (row_max * fs * 1.20) + 3.0 * mm)

    while rows:
        rh = est_row_h(rows[0])
        if (y_cursor - rh) < bottom_y and chunk:
            break
        if (y_cursor - rh) < bottom_y and not chunk:
            chunk.append(rows[0])
            del rows[0]
            break
        chunk.append(rows[0])
        del rows[0]
        y_cursor -= rh

    return _table_intraop(c,
                          x,
                          y_top,
                          w,
                          headers,
                          chunk,
                          col_w,
                          fs=fs,
                          max_lines=max_lines)


def _draw_intraop_header(
    c: canvas.Canvas,
    *,
    page_w: float,
    page_h: float,
    mx: float,
    my: float,
    branding: Any,
    title: str,
    subtitle: str,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    case: Any,
) -> float:
    y_line = _draw_brand_letterhead_template(c, page_w, page_h, mx, my,
                                             branding)

    x = mx
    w = page_w - 2 * mx
    y_top = y_line - 2.0 * mm

    # Title block (clean, form-style)
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 13.6)
    c.drawCentredString(x + w / 2.0, y_top - 6.0 * mm, title.upper())
    if subtitle:
        c.setFont("Helvetica", 9.0)
        c.drawCentredString(x + w / 2.0, y_top - 10.8 * mm, subtitle)
    c.restoreState()

    # double rule for "premium form" feel
    y_rule = y_top - (14.0 * mm if subtitle else 12.0 * mm)
    _hline_form(c, x, x + w, y_rule, lw=1.05)
    _hline_form(c, x, x + w, y_rule - 1.6 * mm, lw=0.55)

    # patient strip
    y_after = _draw_patient_strip(
        c,
        x=x,
        y_top=y_rule - 3.0 * mm,
        w=w,
        patient_fields=patient_fields,
        record=record,
        case=case,
    )
    return y_after - 6.0 * mm


# -------------------------
# ✅ NEW INTRA-OP CHART (Dual scale, clearer legend)
# -------------------------
def _draw_vitals_chart_intraop(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    w: float,
    h: float,
    vitals: List[Dict[str, Any]],
):
    # padding (extra right for HR axis)
    pad_l = 18 * mm
    pad_r = 18 * mm
    pad_t = 8 * mm
    pad_b = 12 * mm

    ix = x + pad_l
    iy_top = y_top - pad_t
    iw = w - pad_l - pad_r
    ih = h - pad_t - pad_b

    # chart frame + grid
    _paper_grid(c,
                ix,
                iy_top,
                iw,
                ih,
                minor=3 * mm,
                major=12 * mm,
                minor_alpha=0.06,
                major_alpha=0.10)
    _rect_form(c, ix, iy_top, iw, ih, lw=1.0)

    # collect points
    pts: List[Tuple[int, Optional[float], Optional[float],
                    Optional[float]]] = []
    for v in vitals or []:
        tmin = _vital_time_minutes(v)
        if tmin is None:
            continue
        hr = _try_float(v.get("hr") or v.get("pulse"))
        sbp = _try_float(v.get("bp_systolic"))
        dbp = _try_float(v.get("bp_diastolic"))

        if sbp is None and dbp is None:
            s_sys, s_dia = _parse_bp(v.get("bp"))
            sbp = sbp if sbp is not None else s_sys
            dbp = dbp if dbp is not None else s_dia

        pts.append((tmin, hr, sbp, dbp))

    if not pts:
        c.saveState()
        c.setFillColor(TEXT)
        c.setFont("Helvetica", 9)
        c.drawString(ix + 4 * mm, (iy_top - ih) + ih / 2, "No vitals to plot")
        c.restoreState()
        return

    pts.sort(key=lambda x: x[0])
    t0 = pts[0][0]
    t1 = pts[-1][0]
    span = max(10, t1 - t0)

    # scales
    y_bp_min, y_bp_max = 40.0, 240.0
    y_hr_min, y_hr_max = 40.0, 200.0

    def x_map(tmin: int) -> float:
        return ix + ((tmin - t0) / span) * iw

    def y_map_bp(vv: float) -> float:
        vv = max(y_bp_min, min(y_bp_max, vv))
        k = (vv - y_bp_min) / (y_bp_max - y_bp_min)
        return (iy_top - ih) + (1 - k) * ih

    def y_map_hr(vv: float) -> float:
        vv = max(y_hr_min, min(y_hr_max, vv))
        k = (vv - y_hr_min) / (y_hr_max - y_hr_min)
        return (iy_top - ih) + (1 - k) * ih

    # axis labels (left BP, right HR)
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 7.4)
    for tick in [40, 80, 120, 160, 200, 240]:
        yy = y_map_bp(float(tick))
        c.drawRightString(ix - 2.0 * mm, yy - 2, str(tick))
    c.setFont("Helvetica", 7.2)
    for tick in [40, 80, 120, 160, 200]:
        yy = y_map_hr(float(tick))
        c.drawString(ix + iw + 2.0 * mm, yy - 2, str(tick))
    c.restoreState()

    # axis captions + legend
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 8.2)
    c.drawString(ix, iy_top + 2.0 * mm, "BP (mmHg)")
    c.drawRightString(ix + iw, iy_top + 2.0 * mm, "HR (bpm)")
    c.restoreState()

    # time ticks
    step = _nice_step_minutes(span)
    t_start = (t0 // step) * step
    t_end = ((t1 + step - 1) // step) * step
    ticks = list(range(t_start, t_end + 1, step))
    if len(ticks) > 10:
        # downsample labels if too many
        stride = max(1, len(ticks) // 8)
        ticks = ticks[::stride]

    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica", 7.2)
    for tm in ticks:
        xx = x_map(tm)
        # small tick mark
        _ink(c, 0.8)
        c.line(xx, (iy_top - ih), xx, (iy_top - ih) - 1.4 * mm)
        c.drawCentredString(xx, (iy_top - ih) - 6.0 * mm,
                            _hhmm_from_minutes(tm))
    c.restoreState()

    # legend (inside top-right)
    leg_y = iy_top - 4.0 * mm
    leg_x = ix + iw - 2.0 * mm

    def _legend_item(dx: float, label: str, style: str):
        # style: "sbp", "dbp", "hr"
        x0 = leg_x - dx
        y0 = leg_y
        c.saveState()
        _ink(c, 1.2)
        if style == "dbp":
            c.setDash(4, 2)
        elif style == "hr":
            c.setDash(1, 2)
        else:
            c.setDash()
        c.line(x0 - 10 * mm, y0, x0 - 3 * mm, y0)
        c.setDash()
        c.setFillColor(BLACK)
        if style == "sbp":
            c.circle(x0 - 6.5 * mm, y0, 1.0 * mm, stroke=0, fill=1)
        elif style == "dbp":
            s = 2.0 * mm
            c.rect(x0 - 6.5 * mm - s / 2, y0 - s / 2, s, s, stroke=0, fill=1)
        else:
            p = c.beginPath()
            p.moveTo(x0 - 6.5 * mm, y0 + 1.2 * mm)
            p.lineTo(x0 - 7.7 * mm, y0 - 0.8 * mm)
            p.lineTo(x0 - 5.3 * mm, y0 - 0.8 * mm)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
        c.restoreState()

        c.saveState()
        c.setFillColor(TEXT)
        c.setFont("Helvetica", 7.8)
        c.drawRightString(x0 - 12.0 * mm, y0 - 2.8 * mm, label)
        c.restoreState()

    _legend_item(0 * mm, "SBP", "sbp")
    _legend_item(18 * mm, "DBP", "dbp")
    _legend_item(36 * mm, "Pulse", "hr")

    # series builder
    sbp_xy: List[Tuple[float, float]] = []
    dbp_xy: List[Tuple[float, float]] = []
    hr_xy: List[Tuple[float, float]] = []

    for tm, hr, sbp, dbp in pts:
        xx = x_map(tm)
        if sbp is not None:
            sbp_xy.append((xx, y_map_bp(sbp)))
        if dbp is not None:
            dbp_xy.append((xx, y_map_bp(dbp)))
        if hr is not None:
            hr_xy.append((xx, y_map_hr(hr)))

    def draw_line(series_xy: List[Tuple[float, float]],
                  dash: Optional[Tuple[int, int]] = None,
                  lw: float = 1.25):
        if len(series_xy) < 2:
            return
        c.saveState()
        c.setStrokeColor(BLACK)
        c.setLineWidth(lw)
        if dash:
            c.setDash(dash[0], dash[1])
        p = c.beginPath()
        p.moveTo(series_xy[0][0], series_xy[0][1])
        for xx, yy in series_xy[1:]:
            p.lineTo(xx, yy)
        c.drawPath(p, stroke=1, fill=0)
        c.restoreState()

    def draw_circle(points: List[Tuple[float, float]]):
        c.saveState()
        c.setFillColor(BLACK)
        for xx, yy in points:
            c.circle(xx, yy, 1.0 * mm, stroke=0, fill=1)
        c.restoreState()

    def draw_square(points: List[Tuple[float, float]]):
        c.saveState()
        c.setFillColor(BLACK)
        s = 2.0 * mm
        for xx, yy in points:
            c.rect(xx - s / 2, yy - s / 2, s, s, stroke=0, fill=1)
        c.restoreState()

    def draw_triangle(points: List[Tuple[float, float]]):
        c.saveState()
        c.setFillColor(BLACK)
        for xx, yy in points:
            p = c.beginPath()
            p.moveTo(xx, yy + 1.2 * mm)
            p.lineTo(xx - 1.1 * mm, yy - 0.8 * mm)
            p.lineTo(xx + 1.1 * mm, yy - 0.8 * mm)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
        c.restoreState()

    draw_line(sbp_xy, dash=None)
    draw_circle(sbp_xy)

    draw_line(dbp_xy, dash=(4, 2))
    draw_square(dbp_xy)

    draw_line(hr_xy, dash=(1, 2), lw=1.05)
    draw_triangle(hr_xy)


# -------------------------
# ✅ INTRA-OP HISTORY + NOTES PAGES (NEW)
# -------------------------
def _yn_text(v: Any) -> str:
    if v is None or v == "":
        return ""
    return "Yes" if _as_bool(v) else "No"


def _sum_numeric_from_vitals(vitals: List[Dict[str, Any]], key: str) -> str:
    total = 0.0
    found = False
    for v in vitals or []:
        val = v.get(key)
        if val is None or val == "":
            continue
        try:
            total += float(val)
            found = True
        except Exception:
            continue
    if not found:
        return ""
    if abs(total - int(total)) < 1e-9:
        return str(int(total))
    return str(round(total, 2))


def _kv_grid_in_box(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    h: float,
    pairs: List[Tuple[str, str]],
    cols: int = 2,
    fs: float = 8.2,
    row_h: float = 7.2 * mm,
):
    """
    Simple 2-col key/value grid (for short values). Uses _field_line (no underlines).
    """
    if not pairs:
        return
    cols = max(1, int(cols))
    cell_w = w / cols
    rows = (len(pairs) + cols - 1) // cols

    # baseline for first row
    y0 = y_top - 5.2 * mm
    for r in range(rows):
        yb = y0 - (r * row_h)
        if (y_top - yb) > h:
            break
        for col in range(cols):
            idx = r * cols + col
            if idx >= len(pairs):
                continue
            lab, val = pairs[idx]
            _field_line(
                c,
                x + col * cell_w,
                yb,
                cell_w,
                lab,
                _as_str(val),
                fs=fs,
                label_fs=fs,
            )


# -------------------------
# ✅ Devices & Monitors helpers (NEW)
# -------------------------
def _dedupe_str_list(items: Sequence[Any]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for it in items or []:
        s = _as_str(it).strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _device_items(primary: Optional[List[str]], legacy: Any) -> List[str]:
    prim = _dedupe_str_list(primary or [])
    if prim:
        return prim
    return _dedupe_str_list(_parse_str_list(legacy))


def _wrap_bullets(items: List[str], width: float, font: str,
                  fs: float) -> List[str]:
    """
    Turn ["A", "B long ..."] into wrapped bullet lines.
    Uses ASCII '-' to avoid font issues.
    """
    if not items:
        return ["—"]
    out: List[str] = []
    for it in items:
        base = f"- {it}"
        lines = simpleSplit(base, font, fs, width) or [base]
        if len(lines) > 1:
            # indent continuation lines
            lines = [lines[0]] + [("  " + ln) for ln in lines[1:]]
        out.extend(lines)
    return out


def _draw_two_col_lists_in_box(
    c: canvas.Canvas,
    *,
    x: float,
    y_top: float,
    w: float,
    h: float,
    left_title: str,
    left_items: List[str],
    right_title: str,
    right_items: List[str],
    title_h: float = 8.6 * mm,
):
    """
    Draws 2-column lists INSIDE an existing _box_intraop() frame.
    (So call _box_intraop() first, then call this.)
    """
    pad_x = 3.0 * mm
    pad_t = 3.0 * mm
    pad_b = 2.6 * mm
    gap = 4.0 * mm

    head_fs = 8.8
    fs = 8.2
    head_font = "Helvetica-Bold"
    font = "Helvetica"

    content_top = y_top - title_h - pad_t
    content_bottom = (y_top - h) + pad_b

    inner_x = x + pad_x
    inner_w = w - 2 * pad_x
    col_w = (inner_w - gap) / 2.0
    lx = inner_x
    rx = inner_x + col_w + gap

    # divider between columns (only content area)
    divider_x = inner_x + col_w + (gap / 2.0)
    _vline_form(c, divider_x, y_top - title_h, y_top - h, lw=0.8)

    # headings
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont(head_font, head_fs)
    c.drawString(lx, content_top,
                 _ellipsize(left_title, col_w, head_font, head_fs))
    c.drawString(rx, content_top,
                 _ellipsize(right_title, col_w, head_font, head_fs))
    c.restoreState()

    # list area
    list_top = content_top - 4.2 * mm
    line_h = fs * 1.25

    def _draw_lines(xx: float, lines: List[str]):
        avail_h = max(0.0, list_top - content_bottom)
        max_lines = int(avail_h // line_h) if line_h > 0 else 0
        max_lines = max(1, max_lines)

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            # mark truncation in last line
            lines[-1] = _ellipsize(lines[-1] + " ...", col_w, font, fs)

        c.saveState()
        c.setFillColor(TEXT)
        c.setFont(font, fs)
        yy = list_top
        for ln in lines:
            if yy < content_bottom:
                break
            c.drawString(xx, yy, _ellipsize(ln, col_w, font, fs))
            yy -= line_h
        c.restoreState()

    l_lines = _wrap_bullets(left_items, col_w, font, fs)
    r_lines = _wrap_bullets(right_items, col_w, font, fs)

    _draw_lines(lx, l_lines)
    _draw_lines(rx, r_lines)


def _draw_intraop_history_and_notes_pages(
    c: canvas.Canvas,
    *,
    page_w: float,
    page_h: float,
    mx: float,
    my: float,
    branding: Any,
    case: Any,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    airway_names: List[str],
    monitor_names: List[str],
    vitals: List[Dict[str, Any]],
    page_no: int,
) -> int:
    """
    Draws:
      - Intra-Operative History page (may spill to more pages safely)
      - Notes continuation pages if notes is long
    Returns final page_no.
    """

    def _history_header(sub: str = "") -> float:
        return _draw_intraop_header(
            c,
            page_w=page_w,
            page_h=page_h,
            mx=mx,
            my=my,
            branding=branding,
            title="Intra-Operative History",
            subtitle=sub
            or "Induction, airway, ventilation, fluids, blocks and notes",
            patient_fields=patient_fields,
            record=record,
            case=case,
        )

    y = _history_header(
        "Induction, airway, ventilation, fluids, blocks and notes")

    x0 = mx
    w = page_w - 2 * mx
    bottom_y = my + 14 * mm

    def _ensure_space(need_h: float, *, subtitle: str = "(continued)"):
        nonlocal y, page_no
        if (y - need_h) < bottom_y:
            _draw_footer(c, page_w, my, page_no=page_no)
            c.showPage()
            page_no += 1
            y = _history_header(subtitle)

    # ---------- Box 1: Header ----------
    box_h = 28 * mm
    _ensure_space(box_h + 6 * mm)
    _box_intraop(c, x0, y, w, box_h, "Intra-Op Header")

    intra_date = _as_str(record.get("intra_date") or record.get("date"))
    intra_or = _as_str(record.get("intra_or_no") or record.get("or_no"))
    intra_anaes = _as_str(record.get("intra_anaesthesiologist"))
    intra_surgeon = _as_str(record.get("intra_surgeon"))
    intra_case_type = _as_str(record.get("intra_case_type"))
    intra_proc = _as_str(
        record.get("intra_surgical_procedure")
        or record.get("proposed_operation"))
    intra_anaes_type = _as_str(
        record.get("intra_anaesthesia_type") or record.get("anaesthesia_type"))

    _kv_grid_in_box(
        c,
        x=x0 + 3 * mm,
        y_top=y - 8.6 * mm,
        w=w - 6 * mm,
        h=box_h - 10.0 * mm,
        pairs=[
            ("Date :", intra_date),
            ("OR No :", intra_or),
            ("Anaesthetist :", intra_anaes),
            ("Surgeon :", intra_surgeon),
            ("Case Type :", intra_case_type),
            ("Procedure :", intra_proc),
            ("Anaesthesia Type :", intra_anaes_type),
            ("", ""),
        ],
        cols=2,
        fs=8.2,
        row_h=7.2 * mm,
    )
    y = y - box_h - 6 * mm

    # ---------- Box 2: Induction & Intubation ----------
    box_h = 64 * mm
    _ensure_space(box_h + 6 * mm)
    _box_intraop(c, x0, y, w, box_h, "Induction & Intubation")

    _kv_grid_in_box(
        c,
        x=x0 + 3 * mm,
        y_top=y - 8.6 * mm,
        w=w - 6 * mm,
        h=box_h - 10.0 * mm,
        pairs=[
            ("Preoxygenation :", _as_str(record.get("preoxygenation"))),
            ("Cricoid Pressure :", _as_str(record.get("cricoid_pressure"))),
            ("Induction Route :", _as_str(record.get("induction_route"))),
            ("Intubation Done :", _yn_text(record.get("intubation_done"))),
            ("Intubation Route :", _as_str(record.get("intubation_route"))),
            ("Intubation State :", _as_str(record.get("intubation_state"))),
            ("Technique :", _as_str(record.get("intubation_technique"))),
            ("Laryngoscopy Grade :",
             _as_str(record.get("laryngoscopy_grade"))),
            ("Tube Type :", _as_str(record.get("tube_type"))),
            ("Tube Size :", _as_str(record.get("tube_size"))),
            ("Fixed At :", _as_str(record.get("tube_fixed_at"))),
            ("Cuff Used :", _yn_text(record.get("cuff_used"))),
            ("Cuff Medium :", _as_str(record.get("cuff_medium"))),
            ("Bilateral Breath Sounds :",
             _yn_text(record.get("bilateral_breath_sounds"))),
            ("Added Sounds :", _as_str(record.get("added_sounds"))),
            ("", ""),
        ],
        cols=2,
        fs=8.0,
        row_h=6.9 * mm,
    )
    y = y - box_h - 6 * mm

    # ---------- ✅ Box 3: Devices & Monitors (FIXED UI + VISIBLE LISTS) ----------
    box_h = 42 * mm  # ✅ increased height so lists are readable
    _ensure_space(box_h + 6 * mm)
    _box_intraop(c,
                 x0,
                 y,
                 w,
                 box_h,
                 "Devices & Monitors",
                 right_note="(auto from master lists)")

    airway_items = _device_items(
        airway_names,
        record.get("airway_devices") or record.get("airway_device")
        or record.get("airway_device_names"),
    )
    monitor_items = _device_items(
        monitor_names,
        record.get("monitors") or record.get("monitor_devices")
        or record.get("monitor_names"),
    )

    _draw_two_col_lists_in_box(
        c,
        x=x0,
        y_top=y,
        w=w,
        h=box_h,
        left_title=f"Airway Devices ({len(airway_items)})",
        left_items=airway_items,
        right_title=f"Monitors ({len(monitor_items)})",
        right_items=monitor_items,
        title_h=8.6 * mm,
    )
    y = y - box_h - 6 * mm

    # ---------- Box 4: Ventilation • Position • Lines ----------
    box_h = 48 * mm
    _ensure_space(box_h + 6 * mm)
    _box_intraop(c, x0, y, w, box_h, "Ventilation • Position • Lines")

    _kv_grid_in_box(
        c,
        x=x0 + 3 * mm,
        y_top=y - 8.6 * mm,
        w=w - 6 * mm,
        h=box_h - 10.0 * mm,
        pairs=[
            ("Vent Mode :", _as_str(record.get("ventilation_mode_baseline"))),
            ("Breathing System :", _as_str(record.get("breathing_system"))),
            ("VT :", _as_str(record.get("ventilator_vt"))),
            ("Rate :", _as_str(record.get("ventilator_rate"))),
            ("PEEP :", _as_str(record.get("ventilator_peep"))),
            ("Position :", _as_str(record.get("patient_position"))),
            ("Eyes Taped :", _yn_text(record.get("eyes_taped"))),
            ("Foil Cover :", _yn_text(record.get("eyes_covered_with_foil"))),
            ("Pressure Points :",
             _yn_text(record.get("pressure_points_padded"))),
            ("Tourniquet :", _yn_text(record.get("tourniquet_used"))),
            ("Lines :", _as_str(record.get("lines"))),
            ("", ""),
        ],
        cols=2,
        fs=8.0,
        row_h=6.9 * mm,
    )
    y = y - box_h - 6 * mm

    # ---------- Box 5: Fluids • Blood • Antibiotics • Totals ----------
    box_h = 44 * mm
    _ensure_space(box_h + 6 * mm)
    _box_intraop(c, x0, y, w, box_h, "Fluids • Blood • Antibiotics • Totals")

    abx = (record.get("antibiotics") or record.get("antibiotic")
           or record.get("antibiotics_given")
           or record.get("prophylactic_antibiotics") or "")
    blood_loss_total = _sum_numeric_from_vitals(vitals, "blood_loss_ml")
    urine_total = _sum_numeric_from_vitals(vitals, "urine_output_ml")

    fluids_block = (
        f"IV Fluids Plan: {_as_str(record.get('iv_fluids_plan'))}\n"
        f"Blood Components Plan: {_as_str(record.get('blood_components_plan'))}\n"
        f"Antibiotics: {_as_str(abx)}\n"
        f"Totals (from logs): Blood loss = {_as_str(blood_loss_total)} ml, "
        f"Urine output = {_as_str(urine_total)} ml")

    _block_text_form(
        c,
        x0,
        y,
        w,
        box_h,
        fluids_block,
        fs=8.2,
        pad_top=10.2 * mm,
        pad_bottom=2.6 * mm,
        pad_x=3.0 * mm,
    )
    y = y - box_h - 6 * mm

    # ---------- Box 6: Regional Block ----------
    # ✅ ensure at least 42mm space, else move to next page
    min_needed = 42 * mm
    _ensure_space(min_needed)

    avail = y - bottom_y
    if avail < min_needed:
        _ensure_space(min_needed)

    avail = y - bottom_y
    box_h = max(40 * mm, avail)

    _box_intraop(c, x0, y, w, box_h, "Regional Block")
    _kv_grid_in_box(
        c,
        x=x0 + 3 * mm,
        y_top=y - 8.6 * mm,
        w=w - 6 * mm,
        h=box_h - 10.0 * mm,
        pairs=[
            ("Block Type :", _as_str(record.get("regional_block_type"))),
            ("Position :", _as_str(record.get("regional_position"))),
            ("Approach :", _as_str(record.get("regional_approach"))),
            ("Space Depth :", _as_str(record.get("regional_space_depth"))),
            ("Needle Type :", _as_str(record.get("regional_needle_type"))),
            ("Drug / Dose :", _as_str(record.get("regional_drug_dose"))),
            ("Level :", _as_str(record.get("regional_level"))),
            ("Complications :", _as_str(record.get("regional_complications"))),
            ("Adequacy :", _as_str(record.get("block_adequacy"))),
            ("Sedation Needed :", _yn_text(record.get("sedation_needed"))),
            ("Conversion to GA :", _yn_text(record.get("conversion_to_ga"))),
            ("", ""),
        ],
        cols=2,
        fs=8.0,
        row_h=6.9 * mm,
    )

    _draw_footer(c, page_w, my, page_no=page_no)

    # --------- NOTES PAGES (auto-continue) ----------
    notes = _as_str(
        record.get("notes") or record.get("intraop_summary")
        or record.get("summary")).strip()
    if not notes:
        return page_no

    font = "Helvetica"
    fs = 8.4
    pad_x = 3.0 * mm
    pad_top = 11.0 * mm
    pad_bottom = 2.8 * mm

    lines = _wrap_block(notes, (w - 2 * pad_x), font, fs)
    line_h = fs * 1.25

    while lines:
        c.showPage()
        page_no += 1

        y2 = _draw_intraop_header(
            c,
            page_w=page_w,
            page_h=page_h,
            mx=mx,
            my=my,
            branding=branding,
            title="Intra-Operative Summary / Notes",
            subtitle="(continued)",
            patient_fields=patient_fields,
            record=record,
            case=case,
        )

        bottom_y2 = my + 14 * mm
        box_h2 = max(60 * mm, y2 - bottom_y2)
        _box_intraop(c, x0, y2, w, box_h2, "Intra-Op Summary / Notes")

        usable_h = max(0.0, box_h2 - (pad_top + pad_bottom))
        max_lines = int(usable_h // line_h) if line_h > 0 else 0
        max_lines = max(1, max_lines)

        chunk = lines[:max_lines]
        lines = lines[max_lines:]

        _block_text_form(
            c,
            x0,
            y2,
            w,
            box_h2,
            "\n".join(chunk),
            fs=fs,
            pad_top=pad_top,
            pad_bottom=pad_bottom,
            pad_x=pad_x,
        )
        _draw_footer(c, page_w, my, page_no=page_no)

    return page_no


# -------------------------
# Single page builder (Pre-op only)
# -------------------------
def build_ot_preanaesthetic_record_pdf_bytes(
    *,
    branding: Any,
    case: Any,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    airway_names: Optional[List[str]] = None,
    monitor_names: Optional[List[str]] = None,
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    mx, my = 10 * mm, 10 * mm

    _draw_preanaesthetic_record_sheet_page(
        c,
        page_w=page_w,
        page_h=page_h,
        mx=mx,
        my=my,
        branding=branding,
        case=case,
        patient_fields=patient_fields,
        record=record,
        airway_names=airway_names,
        monitor_names=monitor_names,
    )

    _draw_footer(c, page_w, my, page_no=1)
    c.save()
    return buf.getvalue()


# -------------------------
# Main PDF builder
# (Preop + Vitals + Advanced Monitoring + Drug log)
# -------------------------
def build_ot_anaesthesia_record_pdf_bytes(
    *,
    branding: Any,
    case: Any,
    patient_fields: Dict[str, str],
    record: Dict[str, Any],
    airway_names: List[str],
    monitor_names: List[str],
    vitals: List[Dict[str, Any]],
    drugs: List[Dict[str, Any]],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    mx, my = 10 * mm, 10 * mm

    page_no = 1

    # -------------------------
    # PAGE 1 (PRE-OP) - DO NOT CHANGE
    # -------------------------
    _draw_preanaesthetic_record_sheet_page(
        c,
        page_w=page_w,
        page_h=page_h,
        mx=mx,
        my=my,
        branding=branding,
        case=case,
        patient_fields=patient_fields,
        record=record,
        airway_names=airway_names,
        monitor_names=monitor_names,
    )
    _draw_footer(c, page_w, my, page_no=page_no)
    # -------------------------
    # PAGE 2+: INTRA-OP HISTORY + NOTES (NEW)
    # -------------------------
    c.showPage()
    page_no += 1

    page_no = _draw_intraop_history_and_notes_pages(
        c,
        page_w=page_w,
        page_h=page_h,
        mx=mx,
        my=my,
        branding=branding,
        case=case,
        patient_fields=patient_fields,
        record=record,
        airway_names=airway_names,
        monitor_names=monitor_names,
        vitals=vitals,
        page_no=page_no,
    )

    # -------------------------
    # NEXT PAGE: VITALS CHART + LOG (existing)
    # -------------------------

    c.showPage()
    page_no += 1

    y = _draw_intraop_header(
        c,
        page_w=page_w,
        page_h=page_h,
        mx=mx,
        my=my,
        branding=branding,
        title="Intra-Operative Vitals",
        subtitle="Vitals trend and intra-op observations",
        patient_fields=patient_fields,
        record=record,
        case=case,
    )

    x0 = mx
    w = page_w - 2 * mx
    bottom_y = my + 14 * mm

    # Chart box
    chart_h = 82 * mm
    t_range = _vitals_time_range(vitals)
    _box_intraop(c,
                 x0,
                 y,
                 w,
                 chart_h,
                 "Vitals Trend Chart",
                 right_note=(f"Time: {t_range}" if t_range else ""))
    _draw_vitals_chart_intraop(c, x0 + 3 * mm, y - 10.5 * mm, w - 6 * mm,
                               chart_h - 15.0 * mm, vitals)

    y = y - chart_h - 6 * mm

    # Vitals table box (core)
    core_headers = ["Time", "HR", "BP", "SpO2", "RR", "Temp", "Comments"]
    core_col_w = [
        16 * mm,
        10 * mm,
        18 * mm,
        12 * mm,
        10 * mm,
        12 * mm,
        (w - 6 * mm) - (16 + 10 + 18 + 12 + 10 + 12) * mm,
    ]

    core_rows: List[List[str]] = []
    for v in vitals or []:
        t = _as_str(v.get("time")) or _fmt_hhmm_from_dt(v.get("time_dt"))
        hr = _as_str(v.get("hr") or v.get("pulse"))
        bp = _as_str(v.get("bp"))
        if not bp:
            sbp = _as_str(v.get("bp_systolic"))
            dbp = _as_str(v.get("bp_diastolic"))
            if sbp or dbp:
                bp = f"{sbp}/{dbp}" if dbp else sbp

        core_rows.append([
            t,
            hr,
            bp,
            _as_str(v.get("spo2")),
            _as_str(v.get("rr")),
            _as_str(v.get("temp_c")),
            _as_str(v.get("comments")),
        ])

    if not core_rows:
        core_rows = [["—"] * len(core_headers)]

    # If not enough height to render a clean box, continue log on next page
    min_box_h = 40 * mm
    avail_h = y - bottom_y
    if avail_h < min_box_h:
        _draw_footer(c, page_w, my, page_no=page_no)
        c.showPage()
        page_no += 1
        y = _draw_intraop_header(
            c,
            page_w=page_w,
            page_h=page_h,
            mx=mx,
            my=my,
            branding=branding,
            title="Intra-Operative Vitals Log",
            subtitle="(continued)",
            patient_fields=patient_fields,
            record=record,
            case=case,
        )

    box_h = max(min_box_h, y - bottom_y)
    _box_intraop(c,
                 x0,
                 y,
                 w,
                 box_h,
                 "Core Vitals Log",
                 right_note="(auto from vitals entries)")
    y_table_top = y - 10.0 * mm

    rows_mut = [r[:] for r in core_rows]
    while rows_mut:
        _table_paginated_intraop(
            c,
            x=x0 + 3 * mm,
            y_top=y_table_top,
            w=w - 6 * mm,
            bottom_y=bottom_y,
            headers=core_headers,
            rows=rows_mut,
            col_w=core_col_w,
            fs=7.9,
            max_lines=2,
        )
        _draw_footer(c, page_w, my, page_no=page_no)

        if rows_mut:
            c.showPage()
            page_no += 1
            y_table_top = _draw_intraop_header(
                c,
                page_w=page_w,
                page_h=page_h,
                mx=mx,
                my=my,
                branding=branding,
                title="Intra-Operative Vitals Log",
                subtitle="(continued)",
                patient_fields=patient_fields,
                record=record,
                case=case,
            )
            _box_intraop(
                c, x0, y_table_top + 6 * mm, w,
                page_h - (my + 14 * mm) - (y_table_top + 6 * mm) + 2 * mm,
                "Core Vitals Log (Cont.)")
            y_table_top = (y_table_top + 6 * mm) - 10.0 * mm

    # -------------------------
    # Advanced monitoring page (only if any advanced values exist) - REDESIGNED
    # -------------------------
    has_advanced = any(
        any(
            _as_str(v.get(k)) for k in (
                "etco2",
                "ventilation_mode",
                "peak_airway_pressure",
                "cvp_pcwp",
                "urine_output_ml",
                "blood_loss_ml",
            )) for v in (vitals or []))

    if has_advanced:
        c.showPage()
        page_no += 1

        y = _draw_intraop_header(
            c,
            page_w=page_w,
            page_h=page_h,
            mx=mx,
            my=my,
            branding=branding,
            title="Intra-Operative Monitoring",
            subtitle="Advanced monitoring parameters",
            patient_fields=patient_fields,
            record=record,
            case=case,
        )

        _box_intraop(c, x0, y, w, page_h - my - y + 2 * mm,
                     "Advanced Monitoring Log")
        y -= 10.0 * mm

        adv_headers = [
            "Time", "EtCO2", "Vent", "Peak P", "CVP/PCWP", "Urine (ml)",
            "Blood loss (ml)", "Remarks"
        ]
        adv_col_w = [
            16 * mm,
            14 * mm,
            18 * mm,
            14 * mm,
            18 * mm,
            18 * mm,
            22 * mm,
            (w - 6 * mm) - (16 + 14 + 18 + 14 + 18 + 18 + 22) * mm,
        ]

        adv_rows: List[List[str]] = []
        for v in vitals or []:
            t = _as_str(v.get("time")) or _fmt_hhmm_from_dt(v.get("time_dt"))
            adv_rows.append([
                t,
                _as_str(v.get("etco2")),
                _as_str(v.get("ventilation_mode")),
                _as_str(v.get("peak_airway_pressure")),
                _as_str(v.get("cvp_pcwp")),
                _as_str(v.get("urine_output_ml")),
                _as_str(v.get("blood_loss_ml")),
                _as_str(v.get("comments")),
            ])

        if not adv_rows:
            adv_rows = [["—"] * len(adv_headers)]

        rows_mut2 = [r[:] for r in adv_rows]
        y_table_top = y

        while rows_mut2:
            _table_paginated_intraop(
                c,
                x=x0 + 3 * mm,
                y_top=y_table_top,
                w=w - 6 * mm,
                bottom_y=bottom_y,
                headers=adv_headers,
                rows=rows_mut2,
                col_w=adv_col_w,
                fs=7.8,
                max_lines=2,
            )
            _draw_footer(c, page_w, my, page_no=page_no)

            if rows_mut2:
                c.showPage()
                page_no += 1
                y_table_top = _draw_intraop_header(
                    c,
                    page_w=page_w,
                    page_h=page_h,
                    mx=mx,
                    my=my,
                    branding=branding,
                    title="Intra-Operative Monitoring",
                    subtitle="(continued)",
                    patient_fields=patient_fields,
                    record=record,
                    case=case,
                )
                _box_intraop(
                    c, x0, y_table_top + 6 * mm, w,
                    page_h - (my + 14 * mm) - (y_table_top + 6 * mm) + 2 * mm,
                    "Advanced Monitoring Log (Cont.)")
                y_table_top = (y_table_top + 6 * mm) - 10.0 * mm

    # -------------------------
    # Drug log page(s) - REDESIGNED
    # -------------------------
    c.showPage()
    page_no += 1

    y = _draw_intraop_header(
        c,
        page_w=page_w,
        page_h=page_h,
        mx=mx,
        my=my,
        branding=branding,
        title="Intra-Operative Drug Log",
        subtitle="Drug administration record",
        patient_fields=patient_fields,
        record=record,
        case=case,
    )

    _box_intraop(c, x0, y, w, page_h - my - y + 2 * mm,
                 "Drug Administration Log")
    y -= 10.0 * mm

    d_headers = ["Time", "Drug", "Dose", "Route", "Remarks"]
    d_col_w = [
        16 * mm,
        78 * mm,
        24 * mm,
        22 * mm,
        (w - 6 * mm) - (16 + 78 + 24 + 22) * mm,
    ]

    d_rows: List[List[str]] = []
    for d in drugs or []:
        d_rows.append([
            _as_str(d.get("time")) or _fmt_hhmm_from_dt(d.get("time_dt")),
            _as_str(d.get("drug_name")),
            _as_str(d.get("dose")),
            _as_str(d.get("route")),
            _as_str(d.get("remarks")),
        ])

    if not d_rows:
        d_rows = [["—"] * len(d_headers)]

    rows_mut3 = [r[:] for r in d_rows]
    y_table_top = y

    while rows_mut3:
        _table_paginated_intraop(
            c,
            x=x0 + 3 * mm,
            y_top=y_table_top,
            w=w - 6 * mm,
            bottom_y=bottom_y,
            headers=d_headers,
            rows=rows_mut3,
            col_w=d_col_w,
            fs=8.2,
            max_lines=2,
        )
        _draw_footer(c, page_w, my, page_no=page_no)

        if rows_mut3:
            c.showPage()
            page_no += 1
            y_table_top = _draw_intraop_header(
                c,
                page_w=page_w,
                page_h=page_h,
                mx=mx,
                my=my,
                branding=branding,
                title="Intra-Operative Drug Log",
                subtitle="(continued)",
                patient_fields=patient_fields,
                record=record,
                case=case,
            )
            _box_intraop(
                c, x0, y_table_top + 6 * mm, w,
                page_h - (my + 14 * mm) - (y_table_top + 6 * mm) + 2 * mm,
                "Drug Administration Log (Cont.)")
            y_table_top = (y_table_top + 6 * mm) - 10.0 * mm

    c.save()
    return buf.getvalue()
