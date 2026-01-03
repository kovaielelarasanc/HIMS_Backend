# FILE: app/services/pdf/pharmacy_schedule_medicine_report_pdf.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date, time, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.pdfbase import pdfmetrics

from sqlalchemy.orm import Session, selectinload

from app.models.ui_branding import UiBranding
from app.models.pharmacy_inventory import StockTransaction, InventoryItem, ItemBatch
from app.models.pharmacy_prescription import PharmacyPrescriptionLine, PharmacyPrescription
from app.models.user import User

# ✅ Schedule meta helper (safe import)
try:
    from app.services.drug_schedules import get_schedule_meta
except Exception:  # keep PDF working even if module missing
    def get_schedule_meta(system: Optional[str], code: Optional[str]) -> dict:  # type: ignore
        return {"system": system or "", "code": code or ""}

# ✅ import your actual Patient model (as you shared)
try:
    from app.models.patient import Patient
except Exception:  # fallback if your project uses different module name
    from app.models.patients import Patient  # type: ignore

IST = ZoneInfo("Asia/Kolkata")

# -------------------------
# Colors / Styles
# -------------------------
BROWN = colors.HexColor("#6B4F2A")
ROW_ALT = colors.Color(0, 0, 0, alpha=0.035)
GRID = colors.Color(0, 0, 0, alpha=0.12)
TEXT = colors.HexColor("#111827")
SUBT = colors.HexColor("#334155")


# -------------------------
# Helpers
# -------------------------
def _get(obj: Any, *names: str, default: Any = "") -> Any:
    if obj is None:
        return default
    for n in names:
        if isinstance(obj, dict) and n in obj:
            v = obj.get(n)
            return default if v is None else v
        if hasattr(obj, n):
            v = getattr(obj, n)
            return default if v is None else v
    return default


def _fmt_exp(d: Optional[date]) -> str:
    return d.strftime("%m/%Y") if d else ""


def _fmt_dt_as_date(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d/%m/%Y")


def _safe_name(u: Optional[User]) -> str:
    if not u:
        return ""
    for key in ("full_name", "name", "display_name", "username", "email"):
        v = getattr(u, key, None)
        if v:
            return str(v)
    return ""


def _patient_display(p: Optional[Patient]) -> Dict[str, str]:
    """
    ✅ Build patient display exactly from your model:
    prefix + first_name + last_name
    uhid
    """
    if not p:
        return {"name": "", "uhid": ""}

    prefix = str(getattr(p, "prefix", "") or "").strip()
    first_name = str(getattr(p, "first_name", "") or "").strip()
    last_name = str(getattr(p, "last_name", "") or "").strip()
    uhid = str(getattr(p, "uhid", "") or "").strip()

    parts = []
    if prefix:
        parts.append(prefix)
    if first_name:
        parts.append(first_name)
    if last_name:
        parts.append(last_name)

    name = " ".join([x for x in parts if x]).strip()
    return {"name": name, "uhid": uhid}


def _fmt_qty(v: Any) -> str:
    if v is None:
        return ""
    try:
        dv = Decimal(str(v))
    except Exception:
        return str(v)
    dv = abs(dv)
    s = f"{dv:f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _schedule_for_item(item: Optional[InventoryItem]) -> str:
    """
    ✅ Return SHORT schedule code for PDF column:
      - India (IN_DCA): H / H1 / X / G / C / C1 ...
      - US (US_CSA): I / II / III / IV / V
    If schedule_code is not present:
      - If prescription_status is SCHEDULED -> return "H" (fallback)
      - Else -> return "" (DO NOT return RX/OTC)
    Supports BOTH:
      - New fields: schedule_system + schedule_code
      - Legacy fields: schedule / drug_schedule / schedule_code (old)
    """
    if not item:
        return ""

    # Only apply to DRUG items (ignore consumables/equipment)
    it = (str(getattr(item, "item_type", "") or "").strip().upper() or "DRUG")
    if it not in ("DRUG", "MEDICINE"):
        return ""

    def _clean(v: object) -> str:
        s = ("" if v is None else str(v)).strip()
        if not s:
            return ""
        s = s.upper().strip()
        # normalize: "Schedule H1" / "SCHEDULE-H1" / "H-1" -> "H1"
        s = s.replace("SCHEDULE", "").replace(" ", "").replace("-", "")
        return s

    def _norm_us_code(code: str) -> str:
        """US: accept 1-5 or I-V and return roman I-V."""
        if not code:
            return ""
        code = code.strip().upper()
        # strip "US" prefix if present
        if code.startswith("US"):
            code = code[2:]
        # numeric to roman
        if code in ("1", "2", "3", "4", "5"):
            return {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V"}[code]
        # already roman
        if code in ("I", "II", "III", "IV", "V"):
            return code
        return code  # keep whatever is provided (best-effort)

    # 1) New model fields
    sysv = _clean(getattr(item, "schedule_system", None))
    code = _clean(getattr(item, "schedule_code", None))
    if code:
        # if drug_schedules.get_schedule_meta exists, use it, else use raw code
        try:
            meta = get_schedule_meta(sysv or None, code)
        except Exception:
            meta = {}

        sch_code = _clean(meta.get("code")) if isinstance(meta, dict) else ""
        if not sch_code:
            sch_code = code

        # US normalization to I-V / II / III
        if (sysv or "").startswith("US"):
            sch_code = _norm_us_code(sch_code)

        # For India keep as H/H1/X/C/C1 etc
        return sch_code

    # 2) Legacy/alternate fields
    for attr in ("schedule", "drug_schedule", "schedule_code"):
        if hasattr(item, attr):
            raw = _clean(getattr(item, attr, None))
            if not raw:
                continue

            # handle legacy "USII" -> "II", "US2" -> "II"
            if raw.startswith("US") or (sysv or "").startswith("US"):
                return _norm_us_code(raw)

            return raw

    # 3) Fallback mapping (IMPORTANT CHANGE)
    # If scheduled -> show as H; else show blank (NOT RX/OTC)
    ps = _clean(getattr(item, "prescription_status", None))
    if ps in ("SCHEDULED", "SCHEDULE"):
        return "H"

    return ""



def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_asset_path(p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    p = str(p).strip()
    if not p:
        return None

    root = _project_root()
    raw = Path(p)

    candidates: List[Path] = []
    if raw.is_absolute():
        candidates.append(raw)

    candidates.extend(
        [
            root / raw,
            root / "uploads" / raw,
            root / "media" / raw,
            root / "static" / raw,
            root / "public" / raw,
            root / p,
        ]
    )

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c
        except Exception:
            pass
    return None


def _read_bytes(path: Optional[str]) -> Optional[bytes]:
    fp = _resolve_asset_path(path)
    if not fp:
        return None
    try:
        return fp.read_bytes()
    except Exception:
        return None


def _draw_image_bytes(
    c: canvas.Canvas,
    img_bytes: Optional[bytes],
    x: float,
    y: float,
    w: float,
    h: float,
    preserve_aspect: bool = True,
) -> None:
    if not img_bytes:
        return
    try:
        img = ImageReader(BytesIO(img_bytes))
        c.drawImage(
            img,
            x,
            y,
            width=w,
            height=h,
            mask="auto",
            preserveAspectRatio=preserve_aspect,
            anchor="sw",
        )
    except Exception:
        return


def _is_dark(col: colors.Color) -> bool:
    r, g, b = float(col.red), float(col.green), float(col.blue)
    lum = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
    return lum < 0.55


def _fit_text(txt: str, font: str, size: float, max_w: float) -> str:
    s = str(txt or "")
    if not s:
        return ""
    if pdfmetrics.stringWidth(s, font, size) <= max_w:
        return s
    ell = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi) // 2
        cand = s[:mid].rstrip() + ell
        if pdfmetrics.stringWidth(cand, font, size) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    return s[: max(0, lo - 1)].rstrip() + ell


def _wrap_text_hard(text: str, font: str, size: float, max_w: float) -> List[str]:
    """
    ✅ Wrap text to width.
    If a single word is too long (UHID / invoice / batch), hard-break by characters.
    Prevents spill into next column.
    """
    t = str(text or "").strip()
    if not t:
        return [""]

    out: List[str] = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue

        lines = simpleSplit(para, font, size, max_w) or [para]
        for line in lines:
            if pdfmetrics.stringWidth(line, font, size) <= max_w:
                out.append(line)
                continue

            seg = ""
            for ch in line:
                if pdfmetrics.stringWidth(seg + ch, font, size) <= max_w:
                    seg += ch
                else:
                    if seg:
                        out.append(seg)
                    seg = ch
            if seg:
                out.append(seg)

    return out if out else [""]


class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        super().showPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            cb = getattr(self, "_page_number_cb", None)
            if cb:
                try:
                    cb(self._pageNumber, num_pages)
                except Exception:
                    pass
            super().showPage()
        super().save()


def build_schedule_medicine_report_pdf(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    location_id: Optional[int] = None,
    only_outgoing: bool = True,
    report_title: str = "Schedule Medicine Report",
) -> bytes:
    branding: Optional[UiBranding] = (
        db.query(UiBranding).order_by(UiBranding.id.asc()).first()
    )

    org_name = _get(branding, "org_name", default="QnQ Pharmacy")
    org_tagline = _get(branding, "org_tagline", default="")
    org_address = _get(branding, "org_address", default="")
    org_phone = _get(branding, "org_phone", default="")
    org_email = _get(branding, "org_email", default="")
    org_website = _get(branding, "org_website", default="")
    org_gstin = _get(branding, "org_gstin", default="")

    logo_bytes = _read_bytes(_get(branding, "logo_path", default=""))
    header_img_bytes = _read_bytes(_get(branding, "pdf_header_path", default=""))
    footer_img_bytes = _read_bytes(_get(branding, "pdf_footer_path", default=""))

    header_h_mm = int(_get(branding, "pdf_header_height_mm", default=46) or 46)
    footer_h_mm = int(_get(branding, "pdf_footer_height_mm", default=12) or 12)
    show_page_no = bool(_get(branding, "pdf_show_page_number", default=True))

    primary_color = str(_get(branding, "primary_color", default="") or "").strip()
    header_color = colors.HexColor(primary_color) if primary_color else colors.HexColor("#0F766E")
    header_text = colors.white if _is_dark(header_color) else TEXT
    header_muted = colors.Color(1, 1, 1, alpha=0.85) if _is_dark(header_color) else SUBT
    header_link = colors.Color(1, 1, 1, alpha=0.95) if _is_dark(header_color) else colors.HexColor("#0B3B8C")

    # ✅ date range in UTC naive (txn_time stored with utcnow)
    start_ist = datetime.combine(date_from, time.min).replace(tzinfo=IST)
    end_ist = datetime.combine(date_to, time.max).replace(tzinfo=IST)
    start_utc_naive = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc_naive = end_ist.astimezone(timezone.utc).replace(tzinfo=None)

    q = (
        db.query(StockTransaction)
        .options(
            selectinload(StockTransaction.item),
            selectinload(StockTransaction.batch),
            selectinload(StockTransaction.user),
        )
        .filter(
            StockTransaction.txn_time >= start_utc_naive,
            StockTransaction.txn_time <= end_utc_naive,
        )
        .order_by(StockTransaction.txn_time.asc(), StockTransaction.id.asc())
    )

    if location_id:
        q = q.filter(StockTransaction.location_id == location_id)
    if only_outgoing:
        q = q.filter(StockTransaction.quantity_change < 0)

    txns: List[StockTransaction] = q.all()

    # -------------------------
    # Enrichment maps (RX)
    # -------------------------
    rx_line_ids = [int(t.ref_id) for t in txns if (t.ref_type == "PHARMACY_RX" and t.ref_id)]
    rx_line_map: Dict[int, PharmacyPrescriptionLine] = {}
    rx_map: Dict[int, PharmacyPrescription] = {}

    if rx_line_ids:
        rx_lines = (
            db.query(PharmacyPrescriptionLine)
            .options(
                selectinload(PharmacyPrescriptionLine.prescription),
                selectinload(PharmacyPrescriptionLine.item),
                selectinload(PharmacyPrescriptionLine.batch),
            )
            .filter(PharmacyPrescriptionLine.id.in_(rx_line_ids))
            .all()
        )
        rx_line_map = {int(ln.id): ln for ln in rx_lines if ln.id is not None}
        rx_ids = sorted({int(ln.prescription_id) for ln in rx_lines if ln.prescription_id})
        if rx_ids:
            rxs = (
                db.query(PharmacyPrescription)
                .options(
                    selectinload(PharmacyPrescription.doctor),
                    selectinload(PharmacyPrescription.patient),
                )
                .filter(PharmacyPrescription.id.in_(rx_ids))
                .all()
            )
            rx_map = {int(r.id): r for r in rxs if r.id is not None}

    # doctor users
    doctor_user_ids = set()
    for t in txns:
        if getattr(t, "doctor_id", None):
            doctor_user_ids.add(int(t.doctor_id))
    for rx in rx_map.values():
        if getattr(rx, "doctor_user_id", None):
            doctor_user_ids.add(int(rx.doctor_user_id))

    doctors_map: Dict[int, User] = {}
    if doctor_user_ids:
        docs = db.query(User).filter(User.id.in_(sorted(doctor_user_ids))).all()
        doctors_map = {int(u.id): u for u in docs}

    # patients
    patient_ids = set()
    for t in txns:
        if getattr(t, "patient_id", None):
            patient_ids.add(int(t.patient_id))
    for rx in rx_map.values():
        if getattr(rx, "patient_id", None):
            patient_ids.add(int(rx.patient_id))

    patient_map: Dict[int, Dict[str, str]] = {}
    if patient_ids:
        pats = db.query(Patient).filter(Patient.id.in_(sorted(patient_ids))).all()
        for p in pats:
            pid = int(getattr(p, "id", 0) or 0)
            patient_map[pid] = _patient_display(p)

    # -------------------------
    # Build rows
    # -------------------------
    rows: List[Dict[str, str]] = []

    for t in txns:
        item: Optional[InventoryItem] = getattr(t, "item", None)
        batch: Optional[ItemBatch] = getattr(t, "batch", None)

        rx_line: Optional[PharmacyPrescriptionLine] = None
        rx: Optional[PharmacyPrescription] = None

        if t.ref_type == "PHARMACY_RX" and t.ref_id:
            rx_line = rx_line_map.get(int(t.ref_id))
            if rx_line and rx_line.prescription_id:
                rx = rx_map.get(int(rx_line.prescription_id))

            # prefer snapshots for stable display
            if rx_line and getattr(rx_line, "item", None):
                item = rx_line.item
            if rx_line and getattr(rx_line, "batch", None):
                batch = rx_line.batch

        invoice_date = _fmt_dt_as_date(getattr(t, "txn_time", None))

        # ✅ Invoice No like your screenshot: PHARMACY_RX-104
        ref_type = str(getattr(t, "ref_type", "") or "").strip()
        ref_id = getattr(t, "ref_id", None)
        invoice_no = f"{ref_type}-{ref_id}" if (ref_type and ref_id) else (ref_type or (str(ref_id) if ref_id else ""))

        # doctor name
        doctor_name = ""
        if getattr(t, "doctor_id", None):
            doctor_name = _safe_name(doctors_map.get(int(t.doctor_id)))
        elif rx and getattr(rx, "doctor_user_id", None):
            doctor_name = _safe_name(doctors_map.get(int(rx.doctor_user_id)))

        # ✅ Client Name: Name + UHID (2 lines)
        client_name = ""
        pid = None
        if getattr(t, "patient_id", None):
            pid = int(t.patient_id)
        elif rx and getattr(rx, "patient_id", None):
            pid = int(rx.patient_id)

        if pid and pid in patient_map:
            pname = (patient_map[pid].get("name") or "").strip()
            puhid = (patient_map[pid].get("uhid") or "").strip()
            if pname and puhid:
                client_name = f"{pname}\nUHID: {puhid}"
            elif puhid:
                client_name = f"UHID:\n{puhid}"
            else:
                client_name = pname
        else:
            client_name = str(getattr(t, "remark", "") or "").strip()

        # brand name
        brand_name = ""
        if rx_line:
            brand_name = str(getattr(rx_line, "item_name", "") or "").strip()
        if not brand_name:
            brand_name = (getattr(item, "brand_name", "") or "").strip()
        if not brand_name:
            brand_name = (getattr(item, "name", "") or "").strip()

        qty_str = _fmt_qty(getattr(t, "quantity_change", 0))
        mfg_name = (getattr(item, "manufacturer", "") or "").strip()

        batch_no = ""
        exp_date = ""
        if batch:
            batch_no = (getattr(batch, "batch_no", "") or "").strip()
            exp_date = _fmt_exp(getattr(batch, "expiry_date", None))

        if rx_line and not batch_no:
            batch_no = str(getattr(rx_line, "batch_no_snapshot", "") or "").strip()
        if rx_line and not exp_date:
            exp_date = _fmt_exp(getattr(rx_line, "expiry_date_snapshot", None))

        schedule = _schedule_for_item(item)

        rows.append(
            dict(
                invoice_date=invoice_date,
                invoice_no=invoice_no,
                doctor_name=doctor_name,
                client_name=client_name,
                brand_name=brand_name,
                qty=qty_str,
                mfg_name=mfg_name,
                batch_no=batch_no,
                exp_date=exp_date,
                schedule=schedule,
                pharmacist_sig="",
            )
        )

    # -------------------------
    # PDF Layout
    # -------------------------
    buf = BytesIO()
    c = NumberedCanvas(buf, pagesize=A4)
    W, H = A4

    margin_x = 10 * mm
    header_h = max(40 * mm, header_h_mm * mm)
    footer_h = max(10 * mm, footer_h_mm * mm)

    col_titles = [
        "Invoice Date",
        "Invoice No",
        "Doctor Name",
        "Client Name",
        "Brand Name",
        "Qty",
        "Mfg Name",
        "Batch No",
        "Exp Date",
        "Schedule",
        "Pharmacist Signature",
    ]
    # ✅ sum=190mm (fits A4 with 10mm margins)
    col_w_mm = [18, 20, 16, 18, 32, 8, 18, 14, 12, 8, 26]
    col_w = [w * mm for w in col_w_mm]
    table_w = sum(col_w)

    def draw_footer(page_no: int, total_pages: int):
        if footer_img_bytes:
            _draw_image_bytes(c, footer_img_bytes, 0, 0, W, footer_h, preserve_aspect=True)

        if show_page_no:
            c.setFillColor(SUBT)
            c.setFont("Helvetica", 8)
            c.drawRightString(W - margin_x, 6 * mm, f"Page {page_no} of {total_pages}")

    c._page_number_cb = draw_footer

    def draw_header() -> float:
        if header_img_bytes:
            _draw_image_bytes(c, header_img_bytes, 0, H - header_h, W, header_h, preserve_aspect=True)
        else:
            c.setFillColor(header_color)
            c.rect(0, H - header_h, W, header_h, stroke=0, fill=1)

        pad_top = 8 * mm
        pad_lr = margin_x
        logo_w = 22 * mm
        logo_h = 22 * mm

        if logo_bytes:
            _draw_image_bytes(c, logo_bytes, pad_lr, H - pad_top - logo_h, logo_w, logo_h, preserve_aspect=True)

        text_x = pad_lr + (logo_w + 6 * mm if logo_bytes else 0)
        y = H - pad_top - 2 * mm

        c.setFillColor(header_text)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(text_x, y, _fit_text(org_name, "Helvetica-Bold", 16, 110 * mm))

        y -= 7 * mm
        c.setFont("Helvetica", 9)
        c.setFillColor(header_muted)

        if org_tagline:
            c.drawString(text_x, y, _fit_text(org_tagline, "Helvetica", 9, 110 * mm))
            y -= 5 * mm

        if org_address:
            for line in simpleSplit(str(org_address), "Helvetica", 9, 110 * mm):
                c.drawString(text_x, y, line)
                y -= 5 * mm

        if org_phone:
            c.drawString(text_x, y, f"MobileNo: {org_phone}")
            y -= 5 * mm
        if org_email:
            c.drawString(text_x, y, f"Email: {org_email}")
            y -= 5 * mm
        if org_website:
            c.setFillColor(header_link)
            c.drawString(text_x, y, _fit_text(org_website, "Helvetica", 9, 110 * mm))
            y -= 5 * mm
            c.setFillColor(header_muted)
        if org_gstin:
            c.drawString(text_x, y, f"GST No: {org_gstin}")
            y -= 5 * mm

        # right title + date
        c.setFillColor(header_text)
        c.setFont("Helvetica-Bold", 18)
        c.drawRightString(W - pad_lr, H - pad_top - 4 * mm, report_title)

        c.setFont("Helvetica-Bold", 10.5)
        if date_from == date_to:
            date_txt = date_from.strftime("%d-%m-%Y")
        else:
            date_txt = f"{date_from.strftime('%d-%m-%Y')}  to  {date_to.strftime('%d-%m-%Y')}"
        c.drawRightString(W - pad_lr, H - pad_top - 18 * mm, date_txt)

        c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.22) if _is_dark(header_color) else GRID)
        c.setLineWidth(0.7)
        c.line(margin_x, H - header_h, W - margin_x, H - header_h)

        return H - header_h - 4 * mm

    def draw_table_header(y: float) -> float:
        font = "Helvetica-Bold"
        size = 8.0
        pad_x = 1.4 * mm
        line_h = 3.6 * mm

        wrapped_cols: List[List[str]] = []
        max_lines = 1
        for i, title in enumerate(col_titles):
            w = col_w[i] - (2 * pad_x)
            lines = _wrap_text_hard(title, font, size, w)
            lines = [_fit_text(ln, font, size, w) for ln in lines]
            wrapped_cols.append(lines)
            max_lines = max(max_lines, len(lines))

        header_hh = max(10 * mm, (max_lines * line_h) + (3.2 * mm))

        x = margin_x
        for i in range(len(col_w)):
            c.setFillColor(BROWN)
            c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.25))
            c.setLineWidth(0.5)
            c.rect(x, y - header_hh, col_w[i], header_hh, stroke=1, fill=1)

            c.setFillColor(colors.white)
            c.setFont(font, size)

            lines = wrapped_cols[i]
            total_text_h = len(lines) * line_h
            baseline = y - ((header_hh - total_text_h) / 2) - (0.75 * line_h)

            for li, line in enumerate(lines):
                c.drawCentredString(x + (col_w[i] / 2), baseline - (li * line_h), line)

            x += col_w[i]

        c.setStrokeColor(GRID)
        c.setLineWidth(0.45)
        c.line(margin_x, y - header_hh, margin_x + table_w, y - header_hh)

        return y - header_hh

    def draw_row(y: float, row: Dict[str, str], alt: bool) -> float:
        font = "Helvetica"
        size = 8.2
        line_h = 3.8 * mm
        pad_x = 1.4 * mm
        pad_y = 1.1 * mm

        c.setFont(font, size)

        vals = [
            row["invoice_date"],
            row["invoice_no"],
            row["doctor_name"],
            row["client_name"],
            row["brand_name"],
            row["qty"],
            row["mfg_name"],
            row["batch_no"],
            row["exp_date"],
            row["schedule"],
            row["pharmacist_sig"],
        ]

        wrapped: List[List[str]] = []
        max_lines = 1
        for i, v in enumerate(vals):
            max_w = col_w[i] - (2 * pad_x)
            lines = _wrap_text_hard(str(v or ""), font, size, max_w) or [""]
            lines = [_fit_text(ln, font, size, max_w) for ln in lines]
            wrapped.append(lines)
            max_lines = max(max_lines, len(lines))

        row_h = max(7.6 * mm, (max_lines * line_h) + (2 * pad_y) + 2.0 * mm)

        if alt:
            c.setFillColor(ROW_ALT)
            c.rect(margin_x, y - row_h, table_w, row_h, stroke=0, fill=1)

        c.setStrokeColor(GRID)
        c.setLineWidth(0.35)
        c.setFillColor(TEXT)

        center_cols = {0, 5, 8, 9}  # date, qty, exp, schedule

        x = margin_x
        for i in range(len(col_w)):
            c.rect(x, y - row_h, col_w[i], row_h, stroke=1, fill=0)

            lines = wrapped[i]
            total_text_h = len(lines) * line_h
            baseline = y - ((row_h - total_text_h) / 2) - (0.75 * line_h)

            for li, line in enumerate(lines):
                yy = baseline - (li * line_h)
                if i in center_cols:
                    c.drawCentredString(x + (col_w[i] / 2), yy, line)
                else:
                    c.drawString(x + pad_x, yy, line)

            x += col_w[i]

        return y - row_h

    def new_page():
        c.showPage()
        return draw_header()

    # Start
    y = draw_header()
    y = draw_table_header(y)

    usable_bottom = footer_h + 10 * mm
    alt = False

    for r in rows:
        if y <= usable_bottom + 14 * mm:
            y = new_page()
            y = draw_table_header(y)
        y = draw_row(y, r, alt)
        alt = not alt

    c.setFillColor(SUBT)
    c.setFont("Helvetica", 8)
    c.drawString(
        margin_x,
        usable_bottom - 4 * mm,
        f"Generated: {datetime.now(IST).strftime('%d/%m/%Y %I:%M %p')}",
    )

    c.save()
    return buf.getvalue()
