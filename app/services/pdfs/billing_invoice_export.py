# FILE: app/services/pdfs/billing_invoice_export.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, A5, landscape, portrait
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.pdfgen import canvas

from app.core.config import settings


# ----------------------------
# helpers (safe formatting)
# ----------------------------
def _get(obj: Any, key: str, default=None):
    try:
        return getattr(obj, key, default)
    except Exception:
        return default


def _s(v: Any, dash: str = "—") -> str:
    if v is None:
        return dash
    s = str(v).strip()
    return s if s else dash


def _safe(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).strip()
    return s if s else "—"


def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")


def _money(x: Any) -> str:
    v = _d(x)
    return f"{v:.2f}"


def _upper(v: Any) -> str:
    return str(v or "").strip().upper()


def _local_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt
    tz = ZoneInfo(getattr(settings, "TIMEZONE", "Asia/Kolkata"))
    return dt.astimezone(tz).replace(tzinfo=None)


def _fmt_date_dt(dt: Optional[datetime]) -> str:
    d = _local_dt(dt)
    if not d:
        return "—"
    return d.strftime("%d-%m-%Y")

def _meta(v: Any) -> Dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}

def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v

    # ✅ IMPORTANT: meta flags may be dict like {"deleted": {"at": "...", "by": 1}}
    if isinstance(v, (dict, list, tuple, set)):
        return len(v) > 0

    if isinstance(v, (int, float, Decimal)):
        return v != 0

    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "t", "on")

def _line_meta_any(ln: Any) -> Dict[str, Any]:
    return _meta(
        getattr(ln, "meta_json", None)
        or getattr(ln, "meta", None)
        or getattr(ln, "extra_json", None)
        or getattr(ln, "payload_json", None)
    )
def _status_norm(st: Any) -> str:
    if st is None:
        return ""
    v = getattr(st, "value", st)
    return str(v or "").strip().upper()

def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "t", "on")

def _line_meta_any(ln: Any) -> Dict[str, Any]:
    # support different field names across installs
    return _meta(
        getattr(ln, "meta_json", None)
        or getattr(ln, "meta", None)
        or getattr(ln, "extra_json", None)
        or getattr(ln, "payload_json", None)
    )
def _line_is_removed(ln: Any) -> bool:
    # 1) status-based remove (strict + contains)
    st = _status_norm(getattr(ln, "status", None) or getattr(ln, "line_status", None))
    if st in ("VOID", "DELETED", "CANCELLED", "CANCELED", "REMOVED", "INACTIVE"):
        return True
    if "REMOV" in st or "VOID" in st or "CANCEL" in st or "DELET" in st:
        return True

    # 2) common boolean columns (if exist)
    for attr in ("is_deleted", "is_void", "is_cancelled", "is_canceled", "is_removed"):
        try:
            if _truthy(getattr(ln, attr, None)):
                return True
        except Exception:
            pass

    # 3) soft-delete timestamps (if exist)
    for attr in ("deleted_at", "voided_at", "cancelled_at", "canceled_at", "removed_at"):
        try:
            if getattr(ln, attr, None):
                return True
        except Exception:
            pass

    # 4) active flag (if exist)
    try:
        if getattr(ln, "is_active", None) is False:
            return True
    except Exception:
        pass

    # 5) meta flags (handles JSON-string meta)
    meta = _line_meta_any(ln)
    for k in (
        "is_deleted", "deleted", "deleted_flag", "deletedFlag",
        "is_void", "void", "voided",
        "is_removed", "removed", "removed_flag", "removedFlag",
        "is_cancelled", "cancelled", "is_canceled", "canceled",
        "is_inactive", "inactive",
        "isRemoved", "removedAt", "removedOn",
    ):
        if _truthy(meta.get(k)) or (k.endswith("At") or k.endswith("On")) and meta.get(k):
            return True

    # 6) ✅ UI marker in description: "(REMOVED)" / "REMOVED"
    desc = str(getattr(ln, "description", "") or "").strip()
    udesc = desc.upper()

    if "REMOVED" in udesc:
        # many systems mark removed rows only by text + set qty/amount to 0
        qty = _d(getattr(ln, "qty", 0))
        amt = _d(getattr(ln, "net_amount", 0))
        if "(REMOVED)" in udesc:
            return True
        if qty == 0 or amt == 0:
            return True

    return False


def _meta_pick(m: Dict[str, Any], keys: List[str], default: str = "—") -> str:
    for k in keys:
        v = m.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


# ----------------------------
# Paper size resolver
# ----------------------------
_PAPER_MAP: Dict[str, Tuple[float, float]] = {"A3": A3, "A4": A4, "A5": A5}


def _resolve_pagesize(paper: str, orientation_: str) -> Tuple[float, float]:
    p = (paper or "A4").strip().upper()
    base = _PAPER_MAP.get(p, A4)
    o = (orientation_ or "portrait").strip().lower()
    if o.startswith("land"):
        return landscape(base)
    return portrait(base)


def _page_scale(pagesize: Tuple[float, float]) -> float:
    # scale relative to A4
    bw, bh = A4
    w, h = pagesize
    s = min(w / bw, h / bh)
    if s < 0.62:
        s = 0.62
    if s > 1.70:
        s = 1.70
    return s


# ----------------------------
# Branding header drawer (govt-form: clean, black rules)
# ----------------------------
def _bget(b: Any, *names: str) -> str:
    for n in names:
        try:
            v = getattr(b, n, None)
            if v not in (None, "", []):
                return str(v).strip()
        except Exception:
            pass
    return ""


def _cap_lines(lines: list[str], max_lines: int) -> list[str]:
    if not lines:
        return []
    if len(lines) <= max_lines:
        return lines
    trimmed = lines[:max_lines]
    last = trimmed[-1].rstrip()
    if not last.endswith("..."):
        trimmed[-1] = (last[:max(0,
                                 len(last) - 3)] +
                       "...") if len(last) > 6 else (last + "...")
    return trimmed


def _read_logo_reader(branding: Any) -> Optional[ImageReader]:
    rel = (_bget(branding, "logo_path", "logo_file", "logo", "logo_rel_path")
           or "").strip()
    if not rel:
        return None
    base = Path(getattr(settings, "STORAGE_DIR", "."))
    abs_path = base.joinpath(rel)
    if not abs_path.exists() or not abs_path.is_file():
        return None
    try:
        return ImageReader(str(abs_path))
    except Exception:
        return None


def _estimate_branding_drop(branding: Optional[Any], w: float,
                            scale: float) -> float:
    # Must match _draw_branding_header drop: header_h + S(2)
    b = branding or type("B", (), {})()

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    def F(pt: float) -> float:
        return max(7.2, pt * scale)

    logo_h = S(18)
    gutter = S(5)

    logo_col = min(max(S(62), w * 0.36), S(78))
    right_w = max(S(58), w - logo_col - gutter)

    org = _safe(_bget(b, "org_name", "name", "hospital_name"))
    tag = _safe(_bget(b, "org_tagline", "tagline"))
    addr = _safe(_bget(b, "org_address", "address"))
    phone = _safe(_bget(b, "org_phone", "phone", "mobile"))
    email = _safe(_bget(b, "org_email", "email"))
    website = _safe(_bget(b, "org_website", "website"))
    gstin = _safe(_bget(b, "org_gstin", "gstin"))

    contact_bits = []
    if phone != "—":
        contact_bits.append(f"Ph: {phone}")
    if email != "—":
        contact_bits.append(f"Email: {email}")
    contact_line = " | ".join(contact_bits)

    meta_lines: list[str] = []
    if addr != "—":
        meta_lines.extend(
            _cap_lines(simpleSplit(addr, "Helvetica", F(8.4), right_w), 2))
    if contact_line:
        meta_lines.extend(
            _cap_lines(simpleSplit(contact_line, "Helvetica", F(8.4), right_w),
                       1))

    extra_bits = []
    if website != "—":
        extra_bits.append(f"{website}")
    if gstin != "—":
        extra_bits.append(f"GSTIN: {gstin}")
    if extra_bits and len(meta_lines) < 3:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(" | ".join(extra_bits), "Helvetica", F(8.4),
                            right_w), 1))

    meta_lines = _cap_lines(meta_lines, 3)

    lines: list[tuple[str, str, float]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", F(12.0)))
    if tag != "—":
        lines.append((tag, "Helvetica", F(8.6)))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", F(8.4)))

    def lh(sz: float) -> float:
        return sz * 1.18

    text_h = sum(lh(sz) for _, _, sz in lines) if lines else S(10)
    header_h = max(logo_h, text_h) + S(2)

    return header_h + S(2)


def _draw_branding_header(c: canvas.Canvas, branding: Optional[Any], x: float,
                          top_y: float, w: float, *, scale: float) -> float:
    b = branding or type("B", (), {})()

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    def F(pt: float) -> float:
        return max(7.2, pt * scale)

    INK = colors.black
    MUTED = colors.HexColor("#111827")  # govt-ish (almost black)

    logo_h = S(18)
    gutter = S(5)

    logo_col = min(max(S(62), w * 0.36), S(78))
    right_w = max(S(58), w - logo_col - gutter)

    org = _safe(_bget(b, "org_name", "name", "hospital_name"))
    tag = _safe(_bget(b, "org_tagline", "tagline"))
    addr = _safe(_bget(b, "org_address", "address"))
    phone = _safe(_bget(b, "org_phone", "phone", "mobile"))
    email = _safe(_bget(b, "org_email", "email"))
    website = _safe(_bget(b, "org_website", "website"))
    gstin = _safe(_bget(b, "org_gstin", "gstin"))

    contact_bits = []
    if phone != "—":
        contact_bits.append(f"Ph: {phone}")
    if email != "—":
        contact_bits.append(f"Email: {email}")
    contact_line = " | ".join(contact_bits)

    meta_lines: list[str] = []
    if addr != "—":
        meta_lines.extend(
            _cap_lines(simpleSplit(addr, "Helvetica", F(8.4), right_w), 2))
    if contact_line:
        meta_lines.extend(
            _cap_lines(simpleSplit(contact_line, "Helvetica", F(8.4), right_w),
                       1))

    extra_bits = []
    if website != "—":
        extra_bits.append(f"{website}")
    if gstin != "—":
        extra_bits.append(f"GSTIN: {gstin}")
    if extra_bits and len(meta_lines) < 3:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(" | ".join(extra_bits), "Helvetica", F(8.4),
                            right_w), 1))

    meta_lines = _cap_lines(meta_lines, 3)

    lines: list[tuple[str, str, float, Any]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", F(12.0), INK))
    if tag != "—":
        lines.append((tag, "Helvetica", F(8.6), MUTED))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", F(8.4), MUTED))

    def lh(sz: float) -> float:
        return sz * 1.18

    text_h = sum(lh(sz) for _, _, sz, _ in lines) if lines else S(10)
    header_h = max(logo_h, text_h) + S(2)

    # logo
    logo_reader = _read_logo_reader(b)
    if logo_reader:
        try:
            iw, ih = logo_reader.getSize()
            if iw and ih:
                scale_h = logo_h / float(ih)
                draw_w = float(iw) * scale_h
                draw_h = logo_h

                max_w = logo_col
                if draw_w > max_w:
                    scale_w = max_w / float(iw)
                    draw_w = max_w
                    draw_h = float(ih) * scale_w

                center_y = top_y - header_h / 2
                logo_x = x
                logo_y = center_y - (draw_h / 2)
                c.drawImage(
                    logo_reader,
                    logo_x,
                    logo_y,
                    width=draw_w,
                    height=draw_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
        except Exception:
            pass

    # right text
    text_right_x = x + w
    center_y = top_y - header_h / 2
    cur_y = center_y + (text_h / 2)

    for txt, font, sz, col in lines:
        cur_y -= lh(sz)
        c.setFont(font, sz)
        c.setFillColor(col)
        c.drawRightString(text_right_x, cur_y, txt)

    # govt rule (black)
    c.setStrokeColor(colors.black)
    c.setLineWidth(max(0.45, 0.60 * scale))
    c.line(x, top_y - header_h, x + w, top_y - header_h)

    return top_y - header_h - S(2)


# ----------------------------
# Patient header block (govt-form)
# ----------------------------
def _draw_lv_column(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    col_w: float,
    rows: list[tuple[str, str]],
    label_w: float,
    size: float,
    leading: float,
    scale: float,
) -> float:

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    colon_w = S(2.0)
    gap = S(2.0)
    value_x = x + label_w + colon_w + gap
    value_w = max(10, col_w - (label_w + colon_w + gap))

    for k, v in rows:
        k = _safe(k)
        v = _safe(v)

        c.setFont("Helvetica-Bold", size)
        c.setFillColor(colors.black)
        c.drawString(x, y, (k[:28] + "…") if len(k) > 29 else k)
        c.drawString(x + label_w + S(0.2), y, ":")

        c.setFont("Helvetica", size)
        lines = simpleSplit(v, "Helvetica", size, value_w) or ["—"]
        c.drawString(value_x, y, lines[0][:200])

        for ln in lines[1:]:
            y -= leading
            c.drawString(value_x, y, ln[:200])

        y -= leading

    return y


def _estimate_patient_drop(payload: Dict[str, Any], w: float, scale: float, *,
                           show_bill_number_row: bool) -> float:

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    def F(pt: float) -> float:
        return max(7.2, pt * scale)

    bill = payload.get("bill", {}) or {}
    pat = payload.get("patient", {}) or {}
    et = payload.get("encounter_type")
    enc = payload.get("encounter", {}) or {}
    payer = payload.get("payer", {}) or {}

    left_w = w * 0.60
    right_w = w - left_w

    age_gender = "—"
    if _safe(pat.get("Age")) != "—" or _safe(pat.get("Gender")) != "—":
        age_gender = f"{_safe(pat.get('Age'))} / {_safe(pat.get('Gender'))}"

    payer_mode = _safe(payer.get("Payer Mode"))
    payer_line = "SELF"
    if payer_mode != "SELF":
        payer_bits = []
        if _safe(payer.get("Payer")) != "—":
            payer_bits.append(_safe(payer.get("Payer")))
        if _safe(payer.get("TPA")) != "—":
            payer_bits.append(_safe(payer.get("TPA")))
        payer_line = " / ".join(payer_bits) if payer_bits else payer_mode

    left_rows = [
        ("Patient Name", _safe(pat.get("Patient Name"))),
        ("Patient ID", _safe(pat.get("UHID"))),
        ("Age / Gender", age_gender),
        ("Phone", _safe(pat.get("Phone"))),
        ("TPA / Comp", payer_line),
    ]

    if et == "OP":
        left_rows += [("Doctor", _safe(enc.get("Doctor"))),
                      ("Department", _safe(enc.get("Department")))]
    elif et == "IP":
        left_rows += [("Ward", _safe(enc.get("Ward"))),
                      ("Doctor", _safe(enc.get("Admission Doctor")))]

    left_rows += [("Patient Address", _safe(pat.get("Address")))]

    right_rows: list[tuple[str, str]] = []
    if show_bill_number_row:
        right_rows.append(("Bill Number", _safe(bill.get("Bill Number"))))
    right_rows += [("Bill Date", _safe(bill.get("Bill Date"))),
                   ("Encounter Type", _safe(et))]

    if et == "OP":
        right_rows += [("Visit ID", _safe(enc.get("Visit Id"))),
                       ("Appointment On", _safe(enc.get("Appointment On")))]
    elif et == "IP":
        right_rows += [
            ("IP Number", _safe(enc.get("IP Admission Number"))),
            ("Admitted On", _safe(enc.get("Admitted On"))),
            ("Discharged On", _safe(enc.get("Discharged On"))),
        ]

    label_w = S(30)
    size = F(8.8)
    leading = max(F(10.2), size + 1.6)

    # estimate consumed height per column using simpleSplit widths
    def col_consumed(col_w: float, rows: list[tuple[str, str]]) -> float:
        colon_w = S(2.0)
        gap = S(2.0)
        value_w = max(10, col_w - (label_w + colon_w + gap))
        total = 0.0
        for _, v in rows:
            lines = simpleSplit(_safe(v), "Helvetica", size, value_w) or ["—"]
            total += leading * max(1, len(lines))
        return total

    left_drop = col_consumed(left_w - S(2), left_rows)
    right_drop = col_consumed(right_w - S(6), right_rows)
    return max(left_drop, right_drop)


def _draw_patient_header_block(
        c: canvas.Canvas,
        payload: Dict[str, Any],
        x: float,
        y_top: float,
        w: float,
        *,
        scale: float,
        show_bill_number_row:
    bool = False,  # invoice: keep header clean; invoice no is at top-right
) -> float:

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    def F(pt: float) -> float:
        return max(7.2, pt * scale)

    bill = payload.get("bill", {}) or {}
    pat = payload.get("patient", {}) or {}
    et = payload.get("encounter_type")
    enc = payload.get("encounter", {}) or {}
    payer = payload.get("payer", {}) or {}

    left_w = w * 0.60
    right_w = w - left_w

    age_gender = "—"
    if _safe(pat.get("Age")) != "—" or _safe(pat.get("Gender")) != "—":
        age_gender = f"{_safe(pat.get('Age'))} / {_safe(pat.get('Gender'))}"

    payer_mode = _safe(payer.get("Payer Mode"))
    payer_line = "SELF"
    if payer_mode != "SELF":
        payer_bits = []
        if _safe(payer.get("Payer")) != "—":
            payer_bits.append(_safe(payer.get("Payer")))
        if _safe(payer.get("TPA")) != "—":
            payer_bits.append(_safe(payer.get("TPA")))
        payer_line = " / ".join(payer_bits) if payer_bits else payer_mode

    left_rows = [
        ("Patient Name", _safe(pat.get("Patient Name"))),
        ("Patient ID", _safe(pat.get("UHID"))),
        ("Age / Gender", age_gender),
        ("Phone", _safe(pat.get("Phone"))),
        ("TPA / Comp", payer_line),
    ]

    if et == "OP":
        left_rows += [("Doctor", _safe(enc.get("Doctor"))),
                      ("Department", _safe(enc.get("Department")))]
    elif et == "IP":
        left_rows += [("Ward", _safe(enc.get("Ward"))),
                      ("Doctor", _safe(enc.get("Admission Doctor")))]

    left_rows += [("Patient Address", _safe(pat.get("Address")))]

    right_rows: list[tuple[str, str]] = []
    if show_bill_number_row:
        right_rows.append(("Bill Number", _safe(bill.get("Bill Number"))))
    right_rows += [("Bill Date", _safe(bill.get("Bill Date"))),
                   ("Encounter Type", _safe(et))]

    if et == "OP":
        right_rows += [("Visit ID", _safe(enc.get("Visit Id"))),
                       ("Appointment On", _safe(enc.get("Appointment On")))]
    elif et == "IP":
        right_rows += [
            ("IP Number", _safe(enc.get("IP Admission Number"))),
            ("Admitted On", _safe(enc.get("Admitted On"))),
            ("Discharged On", _safe(enc.get("Discharged On"))),
        ]

    label_w = S(30)
    size = F(8.8)
    leading = max(F(10.2), size + 1.6)

    y1 = _draw_lv_column(
        c,
        x=x,
        y=y_top,
        col_w=left_w - S(2),
        rows=left_rows,
        label_w=label_w,
        size=size,
        leading=leading,
        scale=scale,
    )
    y2 = _draw_lv_column(
        c,
        x=x + left_w + S(6),
        y=y_top,
        col_w=right_w - S(6),
        rows=right_rows,
        label_w=label_w,
        size=size,
        leading=leading,
        scale=scale,
    )

    y_end = min(y1, y2)

    # separator rule (govt)
    line_y = y_end + S(1.2)
    c.setStrokeColor(colors.black)
    c.setLineWidth(max(0.70, 0.95 * scale))
    c.line(x, line_y, x + w, line_y)

    return y_end


# ----------------------------
# Column sets (responsive ratios)
# ----------------------------
@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width_ratio: float


DEFAULT_COLUMNS: List[Column] = [
    Column("description", "Particulars", 0.58),
    Column("service_date", "Date", 0.14),
    Column("qty", "Qty/Day(s)", 0.10),
    Column("net_amount", "Total Amount", 0.18),
]

PHARMACY_COLUMNS: List[Column] = [
    Column("service_date", "Service Date", 0.14),
    Column("description", "Item Name", 0.42),
    Column("meta.batch_no", "Batch No", 0.14),
    Column("meta.expiry_date", "Expiry", 0.12),
    Column("qty", "QTY", 0.06),
    Column("net_amount", "Total Amount", 0.12),
]


def _is_pharmacy_invoice(inv) -> bool:
    it = str(
        getattr(getattr(inv, "invoice_type", None), "value",
                getattr(inv, "invoice_type", "")) or "").upper()
    mod = str(getattr(inv, "module", "") or "").upper()
    return (it == "PHARMACY") or (mod in ("PHM", "PHC", "PHARM", "PHARMACY"))


def _pick_columns(inv) -> List[Column]:
    return PHARMACY_COLUMNS if _is_pharmacy_invoice(inv) else DEFAULT_COLUMNS


# ----------------------------
# Main: build invoice PDF
# ----------------------------
def build_invoice_pdf(
    *,
    invoice: Any,
    lines: List[Any],
    branding: Optional[Any] = None,
    patient: Optional[Any] = None,
    payer_label: str = "Patient",
    header_payload: Optional[Dict[str, Any]] = None,
    paper: str = "A4",
    orientation: str = "portrait",
) -> bytes:
    pagesize = _resolve_pagesize(paper, orientation)
    scale = _page_scale(pagesize)

    def S(mm_val: float) -> float:
        return mm_val * mm * scale

    def F(pt: float) -> float:
        return max(7.2, pt * scale)

    buf = BytesIO()

    # govt margins (tighter)
    left_margin = S(10)
    right_margin = S(10)
    bottom_margin = S(10)

    avail_w = pagesize[0] - left_margin - right_margin

    # ✅ Dynamic top margin based on actual header height (no extra blank, no overlap)
    top_padding = S(10.0)
    branding_drop = _estimate_branding_drop(branding, avail_w, scale)
    patient_drop = 0.0
    if isinstance(header_payload, dict) and header_payload:
        patient_drop = _estimate_patient_drop(header_payload,
                                              avail_w,
                                              scale,
                                              show_bill_number_row=False)
    else:
        # fallback minimal header height if payload missing
        patient_drop = S(24)

    gap_after_branding = S(2.0)
    gap_after_patient = S(2.6)  # ✅ tight so table starts right after separator
    top_margin = top_padding + branding_drop + gap_after_branding + patient_drop + gap_after_patient

    doc = SimpleDocTemplate(
        buf,
        pagesize=pagesize,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
        title=f"Invoice {getattr(invoice, 'invoice_number', '')}",
    )

    styles = getSampleStyleSheet()
    SMALL = ParagraphStyle(
        "SMALL",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=F(8.3),
        leading=F(10.0),
    )

    story: List[Any] = []

    # ---------------- Lines table ----------------
    cols = _pick_columns(invoice)
    header_row = [Paragraph(f"<b>{c.label}</b>", SMALL) for c in cols]
    data: List[List[Any]] = [header_row]

    cleaned = [ln for ln in (lines or []) if not _line_is_removed(ln)]


    for ln in cleaned:
        m = _meta(getattr(ln, "meta_json", None))
        row: List[Any] = []
        for ccol in cols:
            k = ccol.key

            if k == "service_date":
                row.append(
                    _fmt_date_dt(
                        getattr(ln, "service_date", None)
                        or getattr(invoice, "service_date", None)
                        or getattr(invoice, "created_at", None)))
                continue

            if k == "description":
                row.append(
                    Paragraph(_s(getattr(ln, "description", None)), SMALL))
                continue

            if k == "qty":
                row.append(_s(getattr(ln, "qty", None)))
                continue

            if k == "net_amount":
                row.append(_money(getattr(ln, "net_amount", 0)))
                continue

            # ✅ Batch: show only batch_no (NO batch_id fallback)
            if k == "meta.batch_no":
                row.append(
                    _meta_pick(
                        m,
                        [
                            "batch_no", "batchNo", "batch_number",
                            "batchNumber", "batch"
                        ],
                        "—",
                    ))
                continue

            if k == "meta.expiry_date":
                row.append(
                    _meta_pick(m, [
                        "expiry_date", "expiryDate", "expiry", "exp_date",
                        "expDate"
                    ], "—"))
                continue

            row.append("—")

        data.append(row)

    col_widths = [avail_w * c.width_ratio for c in cols]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)

    # compact padding (reduce table “over gap”)
    pad_v = max(0.85, 1.20 * scale)
    pad_h = max(1.50, 2.10 * scale)

    date_idx = next((i for i, c in enumerate(cols) if c.key == "service_date"),
                    None)
    qty_idx = next((i for i, c in enumerate(cols) if c.key == "qty"), None)
    amt_idx = next((i for i, c in enumerate(cols) if c.key == "net_amount"),
                   None)

    style_cmds = [
        ("GRID", (0, 0), (-1, -1), max(0.60, 0.75 * scale), colors.black),
        ("LINEABOVE", (0, 0), (-1, 0), max(0.95, 1.15 * scale), colors.black),
        ("LINEBELOW", (0, 0), (-1, 0), max(0.95, 1.15 * scale), colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), F(8.6)),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), F(8.6)),
        ("TOPPADDING", (0, 0), (-1, -1), pad_v),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad_v),
        ("LEFTPADDING", (0, 0), (-1, -1), pad_h),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad_h),
    ]

    if date_idx is not None:
        style_cmds += [
            ("ALIGN", (date_idx, 0), (date_idx, 0), "CENTER"),
            ("ALIGN", (date_idx, 1), (date_idx, -1), "CENTER"),
        ]
    if qty_idx is not None:
        style_cmds += [
            ("ALIGN", (qty_idx, 0), (qty_idx, 0), "RIGHT"),
            ("ALIGN", (qty_idx, 1), (qty_idx, -1), "RIGHT"),
        ]
    if amt_idx is not None:
        style_cmds += [
            ("ALIGN", (amt_idx, 0), (amt_idx, 0), "RIGHT"),
            ("ALIGN", (amt_idx, 1), (amt_idx, -1), "RIGHT"),
        ]

    tbl.setStyle(TableStyle(style_cmds))

    story.append(tbl)
    story.append(Spacer(1, S(2.2)))  # ✅ tight space before totals

    # ---------------- Totals (Govt form block) ----------------
    sub_total = _money(getattr(invoice, "sub_total", 0))
    disc_total = _money(getattr(invoice, "discount_total", 0))
    tax_total = _money(getattr(invoice, "tax_total", 0))
    round_off = _money(getattr(invoice, "round_off", 0))
    grand_total = _money(getattr(invoice, "grand_total", 0))

    TOT_L = ParagraphStyle("TOT_L",
                           parent=styles["Normal"],
                           fontName="Helvetica-Bold",
                           fontSize=F(9.0),
                           leading=F(10.5))
    TOT_S = ParagraphStyle("TOT_S",
                           parent=styles["Normal"],
                           fontName="Helvetica",
                           fontSize=F(8.8),
                           leading=F(10.2))

    mini_rows = [
        [
            Paragraph("Sub Total", TOT_S),
            Paragraph(f"<b>{sub_total}</b>", TOT_S)
        ],
        [
            Paragraph("Discount", TOT_S),
            Paragraph(f"<b>{disc_total}</b>", TOT_S)
        ],
        [
            Paragraph("Tax Total", TOT_S),
            Paragraph(f"<b>{tax_total}</b>", TOT_S)
        ],
        [
            Paragraph("Round Off", TOT_S),
            Paragraph(f"<b>{round_off}</b>", TOT_S)
        ],
        [
            Paragraph("Grand Total", TOT_S),
            Paragraph(f"<b>{grand_total}</b>", TOT_S)
        ],
    ]

    left_w = avail_w * 0.55
    right_w = avail_w - left_w

    mini = Table(mini_rows,
                 colWidths=[right_w * 0.60, right_w * 0.40],
                 hAlign="RIGHT")
    mini.setStyle(
        TableStyle([
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), max(0.8, 1.05 * scale)),
            ("BOTTOMPADDING", (0, 0), (-1, -1), max(0.8, 1.05 * scale)),
            ("LEFTPADDING", (0, 0), (-1, -1), max(0.8, 1.05 * scale)),
            ("RIGHTPADDING", (0, 0), (-1, -1), max(0.8, 1.05 * scale)),
            # ✅ thick rule before Grand Total (govt emphasis)
            ("LINEABOVE", (0, -1), (-1, -1), max(1.05,
                                                 1.35 * scale), colors.black),
            ("TOPPADDING", (0, -1), (-1, -1), max(1.6, 2.0 * scale)),
        ]))

    totals_block = Table([[Paragraph("Totals", TOT_L), mini]],
                         colWidths=[left_w, right_w],
                         hAlign="LEFT")
    totals_block.setStyle(
        TableStyle([
            # ✅ govt form thicker top border across whole totals block
            ("LINEABOVE", (0, 0), (-1, 0), max(1.25,
                                               1.60 * scale), colors.black),
            ("VALIGN", (0, 0), (-1, 0), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, 0), max(2.0, 3.0 * scale)),
            ("BOTTOMPADDING", (0, 0), (-1, 0), max(1.0, 1.5 * scale)),
            ("LEFTPADDING", (0, 0), (-1, 0), 0),
            ("RIGHTPADDING", (0, 0), (-1, 0), 0),
        ]))
    story.append(totals_block)

    # ---------------- Page header/footer ----------------
    inv_no = _s(getattr(invoice, "invoice_number", None))

    def _on_page(canv, doc_):
        canv.saveState()
        W, H = doc_.pagesize
        x0 = doc_.leftMargin
        w0 = W - doc_.leftMargin - doc_.rightMargin

        # ✅ top-right invoice number only (small)
        canv.setFont("Helvetica-Bold", F(8.2))
        canv.setFillColor(colors.black)
        canv.drawRightString(x0 + w0, H - S(7.0), inv_no)

        # header start
        top_y = H - S(10.0)
        y = _draw_branding_header(canv, branding, x0, top_y, w0, scale=scale)
        y -= S(2.0)

        if isinstance(header_payload, dict) and header_payload:
            _draw_patient_header_block(
                canv,
                header_payload,
                x0,
                y,
                w0,
                scale=scale,
                show_bill_number_row=False,  # ✅ no extra Bill Number row
            )
        else:
            # minimal fallback
            canv.setFont("Helvetica-Bold", F(8.8))
            canv.setFillColor(colors.black)
            pn = _s(_get(patient, "name", None))
            canv.drawString(x0, y, f"Patient Name : {pn}")
            canv.setFont("Helvetica", F(8.8))
            canv.drawRightString(x0 + w0, y, inv_no)

            canv.setStrokeColor(colors.black)
            canv.setLineWidth(max(0.70, 0.95 * scale))
            canv.line(x0, y - S(3.0), x0 + w0, y - S(3.0))

        # footer page number
        canv.setFont("Helvetica", max(7.0, 8.0 * scale))
        canv.setFillColor(colors.black)
        canv.drawRightString(W - doc_.rightMargin, max(S(8), 10 * mm),
                             f"Page {canv.getPageNumber()}")
        canv.restoreState()

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
