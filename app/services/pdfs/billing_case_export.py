# app/pdfs/billing_case_export.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from reportlab.graphics.barcode import code128


# ============================================================
# Safe getters (dict or ORM)
# ============================================================
def _get(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _upper(v: Any) -> str:
    return str(v or "").strip().upper()


def _s(v: Any, dash: str = "—") -> str:
    if v is None:
        return dash
    s = str(v).strip()
    return s if s else dash


def _d(v: Any, with_time: bool = True) -> str:
    if not v:
        return "—"
    try:
        if isinstance(v, datetime):
            return v.strftime(
                "%d-%m-%Y %I:%M %p") if with_time else v.strftime("%d-%m-%Y")
        if isinstance(v, date):
            return v.strftime("%d-%m-%Y")
        # ISO-ish fallback
        s = str(v)
        if "T" in s:
            s = s.replace("T", " ")
        return s[:19]
    except Exception:
        return _s(v)


def _money(v: Any) -> str:
    try:
        n = Decimal(str(v if v is not None else "0"))
    except Exception:
        n = Decimal("0")
    return f"{n:,.2f}"


def _to_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v if v is not None else "0"))
    except Exception:
        return Decimal("0")


def _num_fallback(prefix: str, raw_id: Any, pad: int = 6) -> str:
    try:
        rid = int(raw_id or 0)
    except Exception:
        rid = 0
    return f"{prefix}-{rid:0{pad}d}" if rid > 0 else f"{prefix}-{'0'*pad}"


def _is_void(status: Any) -> bool:
    s = _upper(status)
    return s in {"VOID", "CANCELLED", "CANCELED"}


def _line_deleted_like(line: Any) -> bool:
    # supports dict/orm
    if _get(line, "is_deleted", False):
        return True
    if _get(line, "deleted_at", None):
        return True
    if _is_void(_get(line, "doc_status", None)):
        return True
    if _is_void(_get(line, "status", None)):
        return True
    return False


def _meta(line: Any) -> dict:
    m = _get(line, "meta_json", None)
    return m if isinstance(m, dict) else {}


# ============================================================
# Branding snapshot (supports your UiBranding field names)
# ============================================================
@dataclass
class BrandingSnapshot:
    org_name: str = "Hospital"
    org_tagline: str = ""
    org_address: str = ""
    org_phone: str = ""
    org_email: str = ""
    org_gstin: str = ""
    logo_path: Optional[str] = None


def snapshot_branding(branding_obj: Any) -> BrandingSnapshot:
    if not branding_obj:
        return BrandingSnapshot()
    # supports both old/new keys if any
    return BrandingSnapshot(
        org_name=_s(_get(branding_obj, "org_name", None), "Hospital"),
        org_tagline=_s(_get(branding_obj, "org_tagline", None), ""),
        org_address=_s(_get(branding_obj, "org_address", None), ""),
        org_phone=_s(_get(branding_obj, "org_phone", None), ""),
        org_email=_s(_get(branding_obj, "org_email", None), ""),
        org_gstin=_s(_get(branding_obj, "org_gstin", None), ""),
        logo_path=_get(branding_obj, "logo_path", None)
        or _get(branding_obj, "logo_file_path", None),
    )


# ============================================================
# Layout helpers (hospital print)
# ============================================================
PAGE_W, PAGE_H = A4

PURPLE_BAND = colors.HexColor("#D8CBE8")  # close to sample
INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#374151")
LINE = colors.HexColor("#111827")
LIGHT_LINE = colors.HexColor("#D1D5DB")
WATER = colors.HexColor("#9CA3AF")

LEFT = 12 * mm
RIGHT = PAGE_W - 12 * mm
TOP = PAGE_H - 12 * mm
BOTTOM = 12 * mm

FONT = "Helvetica"
FONT_B = "Helvetica-Bold"


def _fit_text(c: canvas.Canvas, text: str, max_w: float, font: str,
              size: int) -> str:
    if not text:
        return ""
    c.setFont(font, size)
    if c.stringWidth(text, font, size) <= max_w:
        return text
    # ellipsis shrink
    ell = "…"
    lo, hi = 0, len(text)
    best = ell
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip() + ell
        if c.stringWidth(cand, font, size) <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _wm(c: canvas.Canvas, branding: BrandingSnapshot):
    """light watermark like sample (center)."""
    try:
        c.saveState()
        # transparency if available
        if hasattr(c, "setFillAlpha"):
            c.setFillAlpha(0.06)
        c.setFillColor(WATER)
        c.setFont(FONT_B, 46)
        c.translate(PAGE_W / 2, PAGE_H / 2)
        c.rotate(12)
        text = (branding.org_name or "Hospital").strip()
        c.drawCentredString(0, 0, text)
        c.restoreState()
    except Exception:
        pass


def _draw_brand_band(c: canvas.Canvas, branding: BrandingSnapshot):
    """Top purple band with logo + org details (like sample)."""
    band_h = 18 * mm
    y0 = PAGE_H - band_h

    c.setFillColor(PURPLE_BAND)
    c.rect(LEFT, y0, RIGHT - LEFT, band_h, stroke=0, fill=1)

    # logo on left inside band
    x = LEFT + 4 * mm
    y = y0 + 2 * mm
    if branding.logo_path:
        try:
            img = ImageReader(branding.logo_path)
            c.drawImage(img,
                        x,
                        y,
                        width=14 * mm,
                        height=14 * mm,
                        preserveAspectRatio=True,
                        mask="auto")
        except Exception:
            pass

    # org name centered / slightly left like sample
    c.setFillColor(colors.HexColor("#4B2E83"))
    c.setFont(FONT_B, 12)
    c.drawCentredString((LEFT + RIGHT) / 2, y0 + 11 * mm, branding.org_name
                        or "Hospital")
    c.setFillColor(colors.HexColor("#4B2E83"))
    c.setFont(FONT, 8)
    # address line (small)
    addr = branding.org_address
    if addr and addr != "—":
        c.drawCentredString((LEFT + RIGHT) / 2, y0 + 5.5 * mm,
                            _fit_text(c, addr, (RIGHT - LEFT) - 30 * mm, FONT,
                                      8))

    # right small GSTIN
    if branding.org_gstin and branding.org_gstin != "—":
        c.setFont(FONT, 7)
        c.drawRightString(RIGHT - 3 * mm, y0 + 3.5 * mm,
                          f"GSTIN No : {branding.org_gstin}")


def _draw_doc_title(c: canvas.Canvas,
                    title: str,
                    subtitle: str,
                    duplicate: bool = True):
    y = PAGE_H - 34 * mm
    c.setFillColor(INK)
    c.setFont(FONT_B, 11)
    c.drawCentredString((LEFT + RIGHT) / 2, y, title)
    c.setFont(FONT_B, 9)
    c.drawCentredString((LEFT + RIGHT) / 2, y - 5 * mm, subtitle)
    if duplicate:
        c.setFont(FONT, 8)
        c.drawCentredString((LEFT + RIGHT) / 2, y - 10 * mm, "(Duplicate)")


def _draw_footer(c: canvas.Canvas,
                 printed_dt: datetime,
                 page_label: str,
                 billed_by: Optional[str] = None,
                 checked_by: Optional[str] = None):
    c.setStrokeColor(LIGHT_LINE)
    c.setLineWidth(0.6)
    c.line(LEFT, BOTTOM + 6 * mm, RIGHT, BOTTOM + 6 * mm)

    c.setFillColor(MUTED)
    c.setFont(FONT, 8)
    c.drawString(LEFT, BOTTOM + 2 * mm,
                 f"Printed Date / Time : {_d(printed_dt)}")

    if billed_by:
        c.drawCentredString((LEFT + RIGHT) / 2, BOTTOM + 2 * mm,
                            f"Billed By : {billed_by}")

    # right page
    c.drawRightString(RIGHT, BOTTOM + 2 * mm, page_label)

    # checked_by is typically signature area on last pages; use there, not in every footer


def _kv_block(
    c: canvas.Canvas,
    x: float,
    y_top: float,
    rows: List[Tuple[str, str]],
    col1_w: float,
    col2_w: float,
    line_h: float = 4.6 * mm,
    font_size: int = 8,
):
    c.setFillColor(INK)
    c.setFont(FONT, font_size)
    y = y_top
    for k, v in rows:
        c.setFont(FONT_B, font_size)
        c.drawString(x, y, k)
        c.setFont(FONT, font_size)
        c.drawString(x + col1_w, y, v)
        y -= line_h
    return y


def _box(c: canvas.Canvas,
         x: float,
         y_top: float,
         w: float,
         h: float,
         lw: float = 1.0):
    c.setStrokeColor(LINE)
    c.setLineWidth(lw)
    c.rect(x, y_top - h, w, h, stroke=1, fill=0)


def _table_header_line(c: canvas.Canvas, y: float):
    c.setStrokeColor(LINE)
    c.setLineWidth(1.0)
    c.line(LEFT, y, RIGHT, y)


def _amount_in_words_inr(n: Decimal) -> str:
    """
    Simple INR words (enough for bills). No paise detail for simplicity.
    """
    n = n.quantize(Decimal("1"))
    if n <= 0:
        return "Rupees Zero Only"

    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
        "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
        "Sixteen", "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = [
        "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
        "Eighty", "Ninety"
    ]

    def two_digits(x: int) -> str:
        if x < 20:
            return ones[x]
        return (tens[x // 10] + (" " + ones[x % 10] if x % 10 else "")).strip()

    def three_digits(x: int) -> str:
        h = x // 100
        r = x % 100
        out = ""
        if h:
            out += ones[h] + " Hundred"
            if r:
                out += " "
        if r:
            out += two_digits(r)
        return out.strip()

    num = int(n)
    parts = []
    crore = num // 10000000
    num %= 10000000
    lakh = num // 100000
    num %= 100000
    thousand = num // 1000
    num %= 1000
    hundred = num

    if crore:
        parts.append(two_digits(crore) + " Crore")
    if lakh:
        parts.append(two_digits(lakh) + " Lakh")
    if thousand:
        parts.append(two_digits(thousand) + " Thousand")
    if hundred:
        parts.append(three_digits(hundred))

    return "Rupees " + " ".join([p for p in parts if p]).strip() + " Only"


# ============================================================
# Data shaping: map lines -> hospital categories like sample
# ============================================================
CATEGORY_ORDER = [
    "ADMISSIONS",
    "BED CHARGES(INCLUDING GST, IF APPLICABLE)",
    "BLOOD BANK",
    "CLINICAL LAB CHARGES",
    "CONSUMABLES & DISPOSABLES",
    "DIETARY CHARGES",
    "DOCTOR FEES",
    "PHARMACY CHARGES",
    "PROCEDURES",
    "SCAN CHARGES",
    "SURGERY",
    "X RAY CHARGES",
    "MISCELLANEOUS",
]


def _pick_bill_invoice(invoices: List[Any]) -> Optional[Any]:
    """Choose the main bill invoice_number for header/barcode (like sample Bill Number)."""
    alive = [i for i in invoices if not _is_void(_get(i, "status", None))]
    if not alive:
        return None

    # prefer patient invoice approved/posted
    pref = []
    for i in alive:
        it = _upper(_get(i, "invoice_type", None))
        st = _upper(_get(i, "status", None))
        if it == "PATIENT" and st in {"APPROVED", "POSTED"}:
            pref.append(i)
    if pref:
        pref.sort(key=lambda x: (_get(x, "approved_at", None) or _get(
            x, "created_at", None) or datetime.min))
        return pref[-1]

    # else most recent non-void
    alive.sort(key=lambda x: (_get(x, "created_at", None) or datetime.min))
    return alive[-1]


def _encounter_label_and_number(case: Any,
                                payload: Dict[str, Any]) -> Tuple[str, str]:
    et = _upper(_get(case, "encounter_type", None))
    enc_id = _s(_get(case, "encounter_id", None), "—")

    meta = payload.get("encounter_meta") or {}
    if et == "OP":
        vno = _s(
            meta.get("op_visit_no") or meta.get("visit_no")
            or meta.get("visit_number"), "")
        return ("OP Number", vno if vno and vno != "—" else enc_id)
    if et == "IP":
        ano = _s(
            meta.get("ip_admission_no") or meta.get("admission_no")
            or meta.get("admission_number"), "")
        return ("IP Number", ano if ano and ano != "—" else enc_id)
    return ("Encounter", enc_id)


def _patient_block(case: Any, patient: Any,
                   payload: Dict[str, Any]) -> Dict[str, str]:
    meta = payload.get("encounter_meta") or {}

    name = _s(_get(patient, "name", None) or _get(case, "patient_name", None))
    uhid = _s(
        _get(patient, "uhid", None) or _get(patient, "patient_number", None)
        or _get(case, "uhid", None))
    age = _s(_get(patient, "age", None))
    gender = _s(_get(patient, "gender", None))
    phone = _s(_get(patient, "phone", None))
    addr = _s(_get(patient, "address", None))

    doctor = _s(
        meta.get("doctor") or meta.get("doctor_name")
        or _get(payload.get("doctor"), "name", None))
    ward = _s(meta.get("ward"))
    room = _s(meta.get("room"))
    admitted_on = _d(meta.get("admitted_on"), with_time=True)
    discharged_on = _d(meta.get("discharged_on"), with_time=True)

    return {
        "patient_name": name,
        "patient_id": uhid,
        "age_gender": f"{age} / {gender}".strip(" /"),
        "phone": phone,
        "address": addr,
        "doctor": doctor,
        "ward": ward,
        "room": room,
        "admitted_on": admitted_on,
        "discharged_on": discharged_on,
    }


def _category_for_line(line: Any) -> str:
    """
    Convert your ServiceGroup + description heuristics into hospital headings like sample.
    """
    sg = _upper(_get(line, "service_group", None))
    desc = _upper(_get(line, "description", None) or "")
    if "ADMISSION" in desc:
        return "ADMISSIONS"
    if "BLOOD" in desc:
        return "BLOOD BANK"
    if "DIET" in desc or "FOOD" in desc:
        return "DIETARY CHARGES"

    if sg == "ROOM":
        return "BED CHARGES(INCLUDING GST, IF APPLICABLE)"
    if sg == "CONSULT":
        return "DOCTOR FEES"
    if sg == "LAB":
        return "CLINICAL LAB CHARGES"
    if sg == "PHARM":
        return "PHARMACY CHARGES"
    if sg == "PROC":
        return "PROCEDURES"
    if sg == "OT":
        return "SURGERY"
    if sg == "RAD":
        # try split XRAY vs SCAN like sample
        if "X RAY" in desc or "XRAY" in desc or "X-RAY" in desc:
            return "X RAY CHARGES"
        if "CT" in desc or "MRI" in desc or "SCAN" in desc or "USG" in desc or "ULTRA" in desc:
            return "SCAN CHARGES"
        # default radiology -> scan
        return "SCAN CHARGES"

    # consumables heuristics
    if "CONSUM" in desc or "DISPOS" in desc or "DRESS" in desc or "GAUZE" in desc or "SYRINGE" in desc:
        return "CONSUMABLES & DISPOSABLES"

    if sg == "NURSING":
        return "CONSUMABLES & DISPOSABLES"

    return "MISCELLANEOUS"


def _collect_printable_lines(invoices: List[Any]) -> List[Tuple[Any, Any]]:
    """
    Return list of (invoice, line) excluding VOID invoices and deleted/void lines.
    """
    out = []
    for inv in invoices:
        if _is_void(_get(inv, "status", None)):
            continue
        for ln in (_get(inv, "lines", None) or []):
            if _line_deleted_like(ln):
                continue
            out.append((inv, ln))
    return out


def _is_pharmacy_invoice(inv: Any) -> bool:
    it = _upper(_get(inv, "invoice_type", None))
    mod = _upper(_get(inv, "module", None))
    return it == "PHARMACY" or mod in {"PHARM", "PHARMACY", "RX", "PHM"}


def _pharmacy_items_from_invoices(invoices: List[Any]) -> List[Dict[str, Any]]:
    """
    Build Pharmacy Split Up rows from pharmacy invoices / pharmacy lines.
    Expected meta_json keys:
      batch_id/batch_no, expiry/expiry_date, hsn/hsn_code, qty, net_amount
    """
    rows: List[Dict[str, Any]] = []
    for inv in invoices:
        if _is_void(_get(inv, "status", None)):
            continue
        if not _is_pharmacy_invoice(inv) and _upper(_get(
                inv, "module", None)) not in {"PHARM", "PHARMACY"}:
            continue

        bill_no = _s(_get(inv, "invoice_number", None),
                     _num_fallback("INV", _get(inv, "id", None)))
        bill_dt = _d(_get(inv, "created_at", None), with_time=False)

        for ln in (_get(inv, "lines", None) or []):
            if _line_deleted_like(ln):
                continue
            m = _meta(ln)
            item = _s(_get(ln, "description", None))
            batch = _s(
                m.get("batch_id") or m.get("batch_no") or m.get("batch")
                or m.get("batch_number"))
            exp = _s(
                m.get("expiry") or m.get("expiry_date") or m.get("exp_date"))
            qty = _s(_get(ln, "qty", None))
            amt = _money(_get(ln, "net_amount", None))

            rows.append({
                "bill_no": bill_no,
                "bill_date": bill_dt,
                "item_name": item,
                "batch": batch,
                "expiry": exp,
                "qty": qty,
                "amount": amt,
            })
    return rows


def _sum_by_category(lines: List[Tuple[Any, Any]]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for inv, ln in lines:
        cat = _category_for_line(ln)
        amt = _to_decimal(_get(ln, "net_amount", None))
        out[cat] = out.get(cat, Decimal("0")) + amt
    return out


def _payments_receipts(payments: List[Any]) -> List[Any]:
    """
    Print like sample "PAYMENT DETAILS": only ACTIVE RECEIPT IN (exclude void, refunds, advance adjustments).
    """
    out = []
    for p in payments or []:
        if _is_void(_get(p, "status", None)):
            continue
        if _upper(_get(p, "kind", None)) not in {"RECEIPT"}:
            continue
        if _upper(_get(p, "direction", None)) not in {"IN"}:
            continue
        out.append(p)
    out.sort(key=lambda x: (_get(x, "received_at", None) or _get(
        x, "created_at", None) or datetime.min))
    return out


def _advances_for_deposit(advances: List[Any]) -> List[Any]:
    """
    Deposit summary typically from advances with type ADVANCE.
    """
    out = []
    for a in advances or []:
        if _is_void(_get(a, "status", None)):
            continue
        if _upper(_get(a, "entry_type", None)) != "ADVANCE":
            continue
        out.append(a)
    out.sort(key=lambda x: (_get(x, "entry_at", None) or _get(
        x, "created_at", None) or datetime.min))
    return out


# ============================================================
# Rendering: SUMMARY (2 pages) like sample
# ============================================================
def _render_summary_page1(
    c: canvas.Canvas,
    branding: BrandingSnapshot,
    payload: Dict[str, Any],
    page_no: int,
    total_pages: int,
):
    case = payload.get("case")
    patient = payload.get("patient")
    invoices = payload.get("invoices") or []
    payments = payload.get("payments") or []
    insurance = payload.get("insurance")
    preauths = payload.get("preauths") or []

    printed_dt: datetime = payload.get("printed_dt") or datetime.now()

    bill_inv = _pick_bill_invoice(invoices)
    bill_no = _s(_get(bill_inv, "invoice_number", None),
                 _num_fallback("INV", _get(bill_inv, "id", None)))
    bill_date = _d(_get(bill_inv, "created_at", None), with_time=True)

    enc_label, enc_no = _encounter_label_and_number(case, payload)

    pblk = _patient_block(case, patient, payload)
    payer_mode = _s(_get(case, "payer_mode", None))

    # header band
    _draw_brand_band(c, branding)
    _draw_doc_title(
        c,
        "INPATIENT SUMMARY BILL - CREDIT" if _upper(
            _get(case, "encounter_type", None)) != "OP" else
        "OP SUMMARY BILL - CREDIT",
        "INVOICE - CUM - BILL OF SUPPLY<br/>(RULE 46A and 49)".replace(
            "<br/>", " "),
        duplicate=True,
    )
    _wm(c, branding)

    # Patient + Bill boxes
    top_y = PAGE_H - 48 * mm
    left_x = LEFT
    right_x = PAGE_W - 80 * mm - 12 * mm

    # left patient info
    rows_left = [
        ("Patient Name", f":  {_s(pblk['patient_name'])}"),
        ("Patient ID", f":  {_s(pblk['patient_id'])}"),
        ("Age / Gender", f":  {_s(pblk['age_gender'])}"),
        ("TPA / Comp",
         f":  {_s(_get(payload.get('payer'), 'name', None) or _s(_get(insurance, 'payer_kind', None)))}"
         ),
        ("Insurance Co",
         f":  {_s(_get(payload.get('insurance_company'), 'name', None) or _s(_get(payload.get('insurer'), 'name', None)))}"
         ),
        ("ID Card No.",
         f":  {_s(_get(insurance, 'member_id', None) or _get(insurance, 'policy_no', None))}"
         ),
        ("Ward", f":  {_s(pblk['ward'])}"),
        ("Doctor", f":  {_s(pblk['doctor'])}"),
        ("Patient Address", f":  {_s(pblk['address'])}"),
    ]
    _kv_block(c,
              left_x,
              top_y,
              rows_left,
              col1_w=28 * mm,
              col2_w=70 * mm,
              line_h=4.6 * mm,
              font_size=8)

    # right bill info box
    box_w = 78 * mm
    box_h = 26 * mm
    _box(c, right_x, top_y + 2 * mm, box_w, box_h, lw=1.0)

    rows_right = [
        (enc_label, f":  {enc_no}"),
        ("Bill Number", f":  {bill_no}"),
        ("Bill Date", f":  {bill_date}"),
        ("Admitted On", f":  {_s(pblk['admitted_on'])}"),
        ("Discharged On", f":  {_s(pblk['discharged_on'])}"),
        ("Room", f":  {_s(pblk['room'])}"),
    ]
    _kv_block(c,
              right_x + 3 * mm,
              top_y,
              rows_right,
              col1_w=24 * mm,
              col2_w=48 * mm,
              line_h=4.4 * mm,
              font_size=8)

    # barcode on far right (like sample)
    try:
        b = code128.Code128(bill_no, barHeight=10 * mm, barWidth=0.4)
        b.drawOn(c, right_x + 40 * mm, top_y + 6 * mm)
    except Exception:
        pass

    # Particulars summary
    y = PAGE_H - 104 * mm
    _table_header_line(c, y)
    y -= 7 * mm

    c.setFillColor(INK)
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "Particulars")
    c.drawRightString(RIGHT, y, "Total Amount")
    y -= 4 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    # totals by category
    all_lines = _collect_printable_lines(invoices)
    totals = _sum_by_category(all_lines)

    # ensure ordered like sample
    c.setFont(FONT, 8.5)
    c.setFillColor(INK)

    printed_categories = []
    for cat in CATEGORY_ORDER:
        if cat in totals and totals[cat] != 0:
            printed_categories.append(cat)

    # print even if empty: show at least something
    if not printed_categories:
        printed_categories = ["MISCELLANEOUS"]

    for cat in printed_categories:
        c.setFont(FONT, 8.5)
        c.drawString(LEFT + 2 * mm, y, cat)
        c.drawRightString(RIGHT, y, _money(totals.get(cat, Decimal("0"))))
        y -= 5 * mm

    # tax / totals block right side like sample
    finance = payload.get("finance") or {}
    total_bill = _to_decimal(finance.get("total_billed")) if finance.get(
        "total_billed") is not None else sum(totals.values())
    total_tax = _to_decimal(finance.get("gst_total")) if finance.get(
        "gst_total") is not None else Decimal("0")
    round_off = _to_decimal(finance.get("round_off")) if finance.get(
        "round_off") is not None else Decimal("0")

    # compute exempt/taxable based on gst_rate
    exempt = Decimal("0")
    taxable = Decimal("0")
    gst_amt = Decimal("0")
    for inv, ln in all_lines:
        gr = _to_decimal(_get(ln, "gst_rate", 0))
        net = _to_decimal(_get(ln, "net_amount", 0))
        tax = _to_decimal(_get(ln, "tax_amount", 0))
        if gr <= 0:
            exempt += net
        else:
            taxable += (net - tax)
        gst_amt += tax

    # like sample uses "Exempted Value / Taxable Value / GST / Round Off / Total Bill Amount"
    y_tax = y - 2 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(LEFT, y_tax, RIGHT, y_tax)
    y_tax -= 6 * mm

    right_box_x = RIGHT - 78 * mm
    c.setFont(FONT_B, 8.5)
    c.drawRightString(right_box_x + 52 * mm, y_tax, "Exempted Value :")
    c.setFont(FONT, 8.5)
    c.drawRightString(RIGHT, y_tax, _money(exempt))
    y_tax -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(right_box_x + 52 * mm, y_tax, "Taxable Value :")
    c.setFont(FONT, 8.5)
    c.drawRightString(RIGHT, y_tax, _money(taxable))
    y_tax -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(right_box_x + 52 * mm, y_tax, "GST :")
    c.setFont(FONT, 8.5)
    c.drawRightString(RIGHT, y_tax, _money(gst_amt))
    y_tax -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(right_box_x + 52 * mm, y_tax, "Round Off :")
    c.setFont(FONT, 8.5)
    c.drawRightString(RIGHT, y_tax, _money(round_off))
    y_tax -= 6 * mm

    c.setFont(FONT_B, 9)
    c.drawRightString(right_box_x + 52 * mm, y_tax, "Total Bill Amount :")
    c.drawRightString(RIGHT, y_tax, _money(total_bill))
    y_tax -= 6 * mm

    # Payment Details (table like sample)
    y_pay = y_tax - 4 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(LEFT, y_pay, RIGHT, y_pay)
    y_pay -= 6 * mm

    c.setFillColor(INK)
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y_pay, "PAYMENT DETAILS")
    y_pay -= 5 * mm

    pay_rows = _payments_receipts(payments)
    # headers
    c.setFont(FONT_B, 8.5)
    c.drawString(LEFT, y_pay, "Receipt No")
    c.drawString(LEFT + 46 * mm, y_pay, "Paymode")
    c.drawString(LEFT + 72 * mm, y_pay, "Date")
    c.drawRightString(RIGHT, y_pay, "Amount")
    y_pay -= 3.5 * mm
    c.setLineWidth(0.6)
    c.line(LEFT, y_pay, RIGHT, y_pay)
    y_pay -= 4.5 * mm

    total_recv = Decimal("0")
    c.setFont(FONT, 8.5)
    if not pay_rows:
        c.drawString(LEFT, y_pay, "—")
        y_pay -= 5 * mm
    else:
        for p in pay_rows[:3]:  # sample shows 2-3 rows usually
            rcpt = _s(_get(p, "receipt_number", None),
                      _num_fallback("RCPT", _get(p, "id", None)))
            mode = _upper(_get(p, "mode", None))
            dt = _d(_get(p, "received_at", None), with_time=False)
            amt = _to_decimal(_get(p, "amount", 0))
            total_recv += amt

            c.drawString(LEFT, y_pay, rcpt)
            c.drawString(LEFT + 46 * mm, y_pay, mode)
            c.drawString(LEFT + 72 * mm, y_pay, dt)
            c.drawRightString(RIGHT, y_pay, _money(amt))
            y_pay -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT - 28 * mm, y_pay, "Payment Received :")
    c.drawRightString(RIGHT, y_pay, _money(total_recv))
    y_pay -= 8 * mm

    # Insurance Details (like sample)
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y_pay, "INSURANCE DETAILS")
    y_pay -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawString(LEFT, y_pay, "Company")
    c.drawString(LEFT + 84 * mm, y_pay, "Approval Number")
    c.drawRightString(RIGHT, y_pay, "Amount")
    y_pay -= 3.5 * mm
    c.setLineWidth(0.6)
    c.line(LEFT, y_pay, RIGHT, y_pay)
    y_pay -= 4.5 * mm

    insurer_name = _s(
        _get(payload.get("insurer"), "name", None)
        or _get(payload.get("insurance_company"), "name", None)
        or _get(payload.get("payer"), "name", None))
    approval_no = _s(
        payload.get("insurance_approval_no")
        or _get(insurance, "policy_no", None)
        or _get(insurance, "member_id", None))
    # prefer preauth reference if exists
    if preauths:
        approval_no = _s(_get(preauths[-1], "remarks", None) or approval_no)

    # approved amount
    ins_amt = _to_decimal(_get(insurance, "approved_limit", None))
    if ins_amt <= 0:
        ins_amt = _to_decimal(
            finance.get("insurance_payable") or finance.get("insurer_due")
            or 0)

    c.setFont(FONT, 8.5)
    if not (insurance or insurer_name.strip() != "—"):
        c.drawString(LEFT, y_pay, "—")
    else:
        c.drawString(LEFT, y_pay, _fit_text(c, insurer_name, 80 * mm, FONT,
                                            8.5))
        c.drawString(LEFT + 84 * mm, y_pay,
                     _fit_text(c, approval_no, 60 * mm, FONT, 8.5))
        c.drawRightString(RIGHT, y_pay, _money(ins_amt))
    y_pay -= 7 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT - 30 * mm, y_pay, "Total")
    c.drawRightString(RIGHT, y_pay, _money(ins_amt))

    # footer
    _draw_footer(c,
                 printed_dt,
                 f"Page {page_no} of {total_pages}",
                 billed_by=_s(
                     payload.get("billed_by") or payload.get("printed_by")
                     or ""))


def _render_summary_page2(
    c: canvas.Canvas,
    branding: BrandingSnapshot,
    payload: Dict[str, Any],
    page_no: int,
    total_pages: int,
):
    """
    Page-2 of Summary: tax/payment/insurance + deposit summary + bill abstract + signature blocks
    (matches the kind of content seen on your sample last page format).
    """
    case = payload.get("case")
    invoices = payload.get("invoices") or []
    payments = payload.get("payments") or []
    advances = payload.get("advances") or []
    insurance = payload.get("insurance")
    finance = payload.get("finance") or {}

    printed_dt: datetime = payload.get("printed_dt") or datetime.now()
    billed_by = _s(payload.get("billed_by") or payload.get("printed_by") or "")
    checked_by = _s(payload.get("checked_by") or "")

    bill_inv = _pick_bill_invoice(invoices)
    bill_no = _s(_get(bill_inv, "invoice_number", None),
                 _num_fallback("INV", _get(bill_inv, "id", None)))

    _draw_brand_band(c, branding)
    _draw_doc_title(
        c,
        "INPATIENT SUMMARY BILL - CREDIT" if _upper(
            _get(case, "encounter_type", None)) != "OP" else
        "OP SUMMARY BILL - CREDIT",
        "INVOICE - CUM - BILL OF SUPPLY (RULE 46A and 49)",
        duplicate=True,
    )
    _wm(c, branding)

    y = PAGE_H - 60 * mm

    # Tax summary table (like sample)
    c.setFont(FONT_B, 9)
    c.setFillColor(INK)
    c.drawString(LEFT, y, "TAX SUMMARY")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.setStrokeColor(LINE)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    headers = [
        "Taxable Service", "HSN / SAC Code", "Taxable Value", "CGST", "SGST",
        "Total Tax"
    ]
    colx = [
        LEFT, LEFT + 56 * mm, LEFT + 92 * mm, LEFT + 126 * mm, LEFT + 152 * mm,
        LEFT + 176 * mm
    ]
    c.setFont(FONT_B, 8.2)
    for i, h in enumerate(headers):
        if i == 0:
            c.drawString(colx[i], y, h)
        elif i == 5:
            c.drawRightString(RIGHT, y, h)
        else:
            c.drawString(colx[i], y, h)
    y -= 3.5 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    # build from lines where gst > 0
    all_lines = _collect_printable_lines(invoices)
    taxable_value = Decimal("0")
    cgst = Decimal("0")
    sgst = Decimal("0")
    total_tax = Decimal("0")

    # naive split cgst/sgst = tax/2
    for inv, ln in all_lines:
        gr = _to_decimal(_get(ln, "gst_rate", 0))
        tax = _to_decimal(_get(ln, "tax_amount", 0))
        net = _to_decimal(_get(ln, "net_amount", 0))
        if gr > 0 and tax > 0:
            base = net - tax
            taxable_value += base
            cgst += (tax / 2)
            sgst += (tax / 2)
            total_tax += tax

    c.setFont(FONT, 8.2)
    c.drawString(colx[0], y, "—" if taxable_value == 0 else "Taxable Items")
    c.drawString(colx[1], y, "—")
    c.drawString(colx[2], y, _money(taxable_value))
    c.drawString(colx[3], y, _money(cgst))
    c.drawString(colx[4], y, _money(sgst))
    c.drawRightString(RIGHT, y, _money(total_tax))
    y -= 10 * mm

    # Payment details small block
    pay_rows = _payments_receipts(payments)
    total_recv = sum((_to_decimal(_get(p, "amount", 0)) for p in pay_rows),
                     Decimal("0"))

    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "PAYMENT DETAILS")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    c.setFont(FONT_B, 8.3)
    c.drawString(LEFT, y, "Receipt No")
    c.drawString(LEFT + 46 * mm, y, "Paymode")
    c.drawString(LEFT + 72 * mm, y, "Date")
    c.drawRightString(RIGHT, y, "Amount")
    y -= 4.5 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    c.setFont(FONT, 8.3)
    if not pay_rows:
        c.drawString(LEFT, y, "—")
        y -= 6 * mm
    else:
        for p in pay_rows[:4]:
            rcpt = _s(_get(p, "receipt_number", None),
                      _num_fallback("RCPT", _get(p, "id", None)))
            mode = _upper(_get(p, "mode", None))
            dt = _d(_get(p, "received_at", None), with_time=False)
            amt = _to_decimal(_get(p, "amount", 0))
            c.drawString(LEFT, y, rcpt)
            c.drawString(LEFT + 46 * mm, y, mode)
            c.drawString(LEFT + 72 * mm, y, dt)
            c.drawRightString(RIGHT, y, _money(amt))
            y -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT - 28 * mm, y, "Payment Received :")
    c.drawRightString(RIGHT, y, _money(total_recv))
    y -= 10 * mm

    # Deposit summary (advances)
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "DEPOSIT SUMMARY")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    headers = [
        "Deposit Date", "Reference No", "Actual Amt", "Consumed Amt",
        "Refund Amt", "Balance Amt"
    ]
    cx = [
        LEFT, LEFT + 38 * mm, LEFT + 78 * mm, LEFT + 112 * mm, LEFT + 144 * mm,
        LEFT + 170 * mm
    ]
    c.setFont(FONT_B, 8.1)
    c.drawString(cx[0], y, headers[0])
    c.drawString(cx[1], y, headers[1])
    c.drawRightString(cx[2] + 24 * mm, y, headers[2])
    c.drawRightString(cx[3] + 24 * mm, y, headers[3])
    c.drawRightString(cx[4] + 24 * mm, y, headers[4])
    c.drawRightString(RIGHT, y, headers[5])
    y -= 4.2 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    adv_rows = _advances_for_deposit(advances)
    adv_total = Decimal("0")
    # consumed/refund/balance ideally from finance payload
    adv_balance = _to_decimal(finance.get("advance_balance") or 0)
    adv_consumed = _to_decimal(
        finance.get("advance_consumed") or (max(
            Decimal("0"),
            sum((_to_decimal(_get(a, "amount", 0))
                 for a in adv_rows), Decimal("0")) - adv_balance)))
    adv_refund = _to_decimal(finance.get("advance_refund") or 0)

    c.setFont(FONT, 8.1)
    if not adv_rows:
        c.drawString(LEFT, y, "—")
        y -= 6 * mm
    else:
        # print only first deposit row like sample
        a = adv_rows[0]
        dt = _d(_get(a, "entry_at", None), with_time=True)
        ref = _s(
            _get(a, "receipt_number", None) or _get(a, "txn_ref", None)
            or _num_fallback("ADV", _get(a, "id", None)))
        amt = _to_decimal(_get(a, "amount", 0))
        adv_total += amt

        c.drawString(cx[0], y, dt)
        c.drawString(cx[1], y, ref)
        c.drawRightString(cx[2] + 24 * mm, y, _money(amt))
        c.drawRightString(
            cx[3] + 24 * mm, y,
            _money(-adv_consumed if adv_consumed else Decimal("0")))
        c.drawRightString(cx[4] + 24 * mm, y, _money(adv_refund))
        c.drawRightString(RIGHT, y, _money(adv_balance))
        y -= 8 * mm

        c.setFont(FONT_B, 8.2)
        c.drawString(cx[1], y, "Total")
        c.drawRightString(cx[2] + 24 * mm, y, _money(amt))
        c.drawRightString(
            cx[3] + 24 * mm, y,
            _money(-adv_consumed if adv_consumed else Decimal("0")))
        c.drawRightString(cx[4] + 24 * mm, y, _money(adv_refund))
        c.drawRightString(RIGHT, y, _money(adv_balance))
        y -= 12 * mm

    # Bill Abstract (like sample right box)
    total_amount = _to_decimal(finance.get("total_billed")) if finance.get(
        "total_billed") is not None else Decimal("0")
    if total_amount <= 0:
        total_amount = sum((_to_decimal(_get(ln, "net_amount", 0))
                            for _, ln in _collect_printable_lines(invoices)),
                           Decimal("0"))

    due = _to_decimal(finance.get("due") or (total_amount - total_recv))

    box_w = 80 * mm
    box_h = 22 * mm
    bx = RIGHT - box_w
    by = y + 18 * mm
    _box(c, bx, by, box_w, box_h, lw=1.0)

    c.setFont(FONT_B, 9)
    c.drawString(bx + 3 * mm, by - 5 * mm, "Bill Abstract :")
    c.setFont(FONT_B, 8.5)
    c.drawString(bx + 3 * mm, by - 10 * mm, "Total Amount :")
    c.drawRightString(bx + box_w - 3 * mm, by - 10 * mm, _money(total_amount))
    c.drawString(bx + 3 * mm, by - 15 * mm, "Less Payment Received :")
    c.drawRightString(bx + box_w - 3 * mm, by - 15 * mm, _money(total_recv))
    c.drawString(bx + 3 * mm, by - 20 * mm, "Balance Amount :")
    c.drawRightString(bx + box_w - 3 * mm, by - 20 * mm, _money(due))

    # Signature blocks (left)
    sig_x = LEFT
    sig_y = BOTTOM + 34 * mm
    _box(c, sig_x, sig_y, 90 * mm, 22 * mm, lw=1.0)
    c.setFont(FONT, 8.3)
    c.drawString(sig_x + 3 * mm, sig_y - 6 * mm,
                 "Patient / Attender signature")
    c.drawString(sig_x + 3 * mm, sig_y - 12 * mm, "Name & Relationship")
    c.drawString(sig_x + 3 * mm, sig_y - 18 * mm, "Contact Number")

    # Amount in words
    words = _amount_in_words_inr(due if due > 0 else total_amount)
    c.setFont(FONT, 8.4)
    c.drawString(LEFT, BOTTOM + 20 * mm, f"Balance Amount in Words : {words}")

    # Right authorisation
    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT, BOTTOM + 24 * mm, f"For {branding.org_name}")
    c.setFont(FONT, 8.2)
    c.drawRightString(RIGHT, BOTTOM + 14 * mm, "Authorised signatory")

    # footer
    _draw_footer(c,
                 printed_dt,
                 f"Page {page_no} of {total_pages}",
                 billed_by=billed_by)


# ============================================================
# Rendering: DETAIL BILL (N item pages + last summary page)
# ============================================================
def _build_detail_rows(invoices: List[Any]) -> List[Dict[str, Any]]:
    """
    Build rows as seen in sample:
      - section headers like "CLINICAL LAB CHARGES"
      - rows: description, date, qty, amount
      - For PHARMACY CHARGES and CONSUMABLES: show "MEDICINES"/"CONSUMABLES" daily totals like sample.
    """
    pairs = _collect_printable_lines(invoices)
    # group lines by category
    cat_map: Dict[str, List[Tuple[Any, Any]]] = {}
    for inv, ln in pairs:
        cat = _category_for_line(ln)
        cat_map.setdefault(cat, []).append((inv, ln))

    rows: List[Dict[str, Any]] = []
    for cat in CATEGORY_ORDER:
        if cat not in cat_map:
            continue
        items = cat_map[cat]

        # section header
        rows.append({"kind": "header", "text": cat})

        # special grouping like sample
        if cat in {"PHARMACY CHARGES", "CONSUMABLES & DISPOSABLES"}:
            # group by service_date (line.service_date or invoice.service_date or invoice.created_at date)
            day_tot: Dict[str, Decimal] = {}
            for inv, ln in items:
                dt = _get(ln, "service_date", None) or _get(
                    inv, "service_date", None) or _get(inv, "created_at", None)
                day = _d(dt, with_time=False)
                day_tot[day] = day_tot.get(day, Decimal("0")) + _to_decimal(
                    _get(ln, "net_amount", 0))

            label = "MEDICINES" if cat == "PHARMACY CHARGES" else "CONSUMABLES"
            for day in sorted(day_tot.keys()):
                rows.append({
                    "kind": "row",
                    "desc": label,
                    "date": day,
                    "qty": "—",
                    "amt": day_tot[day],
                })

        else:
            # each line
            for inv, ln in items:
                dt = _get(ln, "service_date", None) or _get(
                    inv, "service_date", None) or _get(inv, "created_at", None)
                rows.append({
                    "kind": "row",
                    "desc": _s(_get(ln, "description", None)),
                    "date": _d(dt, with_time=False),
                    "qty": _s(_get(ln, "qty", None)),
                    "amt": _to_decimal(_get(ln, "net_amount", 0)),
                })

        # section subtotal (like sample right side total)
        sub = sum((_to_decimal(_get(ln, "net_amount", 0)) for _, ln in items),
                  Decimal("0"))
        rows.append({"kind": "subtotal", "amt": sub})

        # spacing line
        rows.append({"kind": "spacer"})
    return rows


def _render_detail_item_pages(
    c: canvas.Canvas,
    branding: BrandingSnapshot,
    payload: Dict[str, Any],
    rows: List[Dict[str, Any]],
    page_no: int,
    total_pages: int,
):
    case = payload.get("case")
    patient = payload.get("patient")
    invoices = payload.get("invoices") or []
    printed_dt: datetime = payload.get("printed_dt") or datetime.now()

    bill_inv = _pick_bill_invoice(invoices)
    bill_no = _s(_get(bill_inv, "invoice_number", None),
                 _num_fallback("INV", _get(bill_inv, "id", None)))
    bill_date = _d(_get(bill_inv, "created_at", None), with_time=True)

    pblk = _patient_block(case, patient, payload)
    enc_label, enc_no = _encounter_label_and_number(case, payload)

    _draw_brand_band(c, branding)
    _draw_doc_title(
        c,
        "INPATIENT DETAIL BILL OF SUPPLY - CREDIT" if _upper(
            _get(case, "encounter_type", None)) != "OP" else
        "OP DETAIL BILL OF SUPPLY - CREDIT",
        "(Duplicate)",
        duplicate=False,
    )
    _wm(c, branding)

    # small header area like sample
    top_y = PAGE_H - 48 * mm
    rows_left = [
        ("Patient Name", f":  {_s(pblk['patient_name'])}"),
        ("Patient ID", f":  {_s(pblk['patient_id'])}"),
        ("TPA / Comp",
         f":  {_s(_get(payload.get('payer'), 'name', None) or _s(_get(payload.get('insurer'), 'name', None)))}"
         ),
    ]
    _kv_block(c,
              LEFT,
              top_y,
              rows_left,
              col1_w=28 * mm,
              col2_w=70 * mm,
              line_h=4.6 * mm,
              font_size=8)

    # right bill box
    bx = RIGHT - 78 * mm
    _box(c, bx, top_y + 2 * mm, 78 * mm, 16 * mm, lw=1.0)
    rows_right = [
        (enc_label, f":  {enc_no}"),
        ("Bill Number", f":  {bill_no}"),
        ("Bill Date", f":  {bill_date}"),
    ]
    _kv_block(c,
              bx + 3 * mm,
              top_y,
              rows_right,
              col1_w=24 * mm,
              col2_w=48 * mm,
              line_h=4.4 * mm,
              font_size=8)

    # table header line
    y = PAGE_H - 78 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(1.0)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    c.setFont(FONT_B, 8.5)
    c.setFillColor(INK)
    c.drawString(LEFT, y, "Particulars")
    c.drawString(LEFT + 110 * mm, y, "Date")
    c.drawString(LEFT + 140 * mm, y, "Quantity")
    c.drawRightString(RIGHT, y, "Total Amount")

    y -= 4 * mm
    c.setLineWidth(0.6)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    # rows
    row_h = 5 * mm
    c.setFont(FONT, 8.5)

    for r in rows:
        kind = r.get("kind")
        if kind == "header":
            c.setFont(FONT_B, 8.8)
            c.drawString(LEFT, y, r["text"])
            y -= row_h
            c.setFont(FONT, 8.5)
            continue
        if kind == "row":
            desc = _fit_text(c, r["desc"], 105 * mm, FONT, 8.5)
            c.drawString(LEFT, y, desc)
            c.drawString(LEFT + 110 * mm, y, r["date"])
            c.drawString(LEFT + 146 * mm, y, r["qty"])
            c.drawRightString(RIGHT, y, _money(r["amt"]))
            y -= row_h
            continue
        if kind == "subtotal":
            # draw underline + subtotal on right
            c.setLineWidth(0.6)
            c.line(RIGHT - 42 * mm, y + 2 * mm, RIGHT, y + 2 * mm)
            c.setFont(FONT_B, 8.5)
            c.drawRightString(RIGHT, y - 1 * mm, _money(r["amt"]))
            c.setFont(FONT, 8.5)
            y -= (row_h + 2 * mm)
            continue
        if kind == "spacer":
            y -= 2 * mm
            continue

    _draw_footer(c,
                 printed_dt,
                 f"Page {page_no} of {total_pages}",
                 billed_by=_s(
                     payload.get("billed_by") or payload.get("printed_by")
                     or ""))


def _render_detail_last_page(
    c: canvas.Canvas,
    branding: BrandingSnapshot,
    payload: Dict[str, Any],
    page_no: int,
    total_pages: int,
):
    """
    Last page of detail bill: tax table + payment + insurance + deposit + abstract + signatures (like your sample page 4 of 4).
    """
    case = payload.get("case")
    invoices = payload.get("invoices") or []
    payments = payload.get("payments") or []
    advances = payload.get("advances") or []
    insurance = payload.get("insurance")
    finance = payload.get("finance") or {}

    printed_dt: datetime = payload.get("printed_dt") or datetime.now()
    billed_by = _s(payload.get("billed_by") or payload.get("printed_by") or "")
    checked_by = _s(payload.get("checked_by") or "")

    bill_inv = _pick_bill_invoice(invoices)
    bill_no = _s(_get(bill_inv, "invoice_number", None),
                 _num_fallback("INV", _get(bill_inv, "id", None)))
    _draw_brand_band(c, branding)
    _draw_doc_title(
        c,
        "INPATIENT DETAIL BILL OF SUPPLY - CREDIT" if _upper(
            _get(case, "encounter_type", None)) != "OP" else
        "OP DETAIL BILL OF SUPPLY - CREDIT",
        "INVOICE - CUM - BILL OF SUPPLY (RULE 46A and 49)",
        duplicate=True,
    )
    _wm(c, branding)

    y = PAGE_H - 58 * mm

    # Tax table header like sample
    headers = [
        "Taxable Service", "HSN / SAC Code", "Taxable Value", "CGST", "SGST",
        "Total Tax"
    ]
    cx = [
        LEFT, LEFT + 52 * mm, LEFT + 92 * mm, LEFT + 126 * mm, LEFT + 152 * mm,
        LEFT + 176 * mm
    ]
    c.setFont(FONT_B, 8.2)
    for i, h in enumerate(headers):
        if i == 0:
            c.drawString(cx[i], y, h)
        elif i == 5:
            c.drawRightString(RIGHT, y, h)
        else:
            c.drawString(cx[i], y, h)

    y -= 4 * mm
    c.setLineWidth(0.7)
    c.setStrokeColor(LINE)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    # compute totals
    all_lines = _collect_printable_lines(invoices)
    taxable_value = Decimal("0")
    total_tax = Decimal("0")
    for inv, ln in all_lines:
        gr = _to_decimal(_get(ln, "gst_rate", 0))
        tax = _to_decimal(_get(ln, "tax_amount", 0))
        net = _to_decimal(_get(ln, "net_amount", 0))
        if gr > 0 and tax > 0:
            taxable_value += (net - tax)
            total_tax += tax

    cgst = total_tax / 2
    sgst = total_tax / 2

    c.setFont(FONT, 8.2)
    c.drawString(cx[0], y, "—" if taxable_value == 0 else "Taxable Items")
    c.drawString(cx[1], y, "—")
    c.drawString(cx[2], y, _money(taxable_value))
    c.drawString(cx[3], y, _money(cgst))
    c.drawString(cx[4], y, _money(sgst))
    c.drawRightString(RIGHT, y, _money(total_tax))
    y -= 12 * mm

    # Payment details
    pay_rows = _payments_receipts(payments)
    total_recv = sum((_to_decimal(_get(p, "amount", 0)) for p in pay_rows),
                     Decimal("0"))

    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "PAYMENT DETAILS")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    c.setFont(FONT_B, 8.3)
    c.drawString(LEFT, y, "Receipt No")
    c.drawString(LEFT + 46 * mm, y, "Paymode")
    c.drawString(LEFT + 72 * mm, y, "Date")
    c.drawRightString(RIGHT, y, "Amount")
    y -= 4.5 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    c.setFont(FONT, 8.3)
    for p in pay_rows[:4]:
        rcpt = _s(_get(p, "receipt_number", None),
                  _num_fallback("RCPT", _get(p, "id", None)))
        mode = _upper(_get(p, "mode", None))
        dt = _d(_get(p, "received_at", None), with_time=False)
        amt = _to_decimal(_get(p, "amount", 0))
        c.drawString(LEFT, y, rcpt)
        c.drawString(LEFT + 46 * mm, y, mode)
        c.drawString(LEFT + 72 * mm, y, dt)
        c.drawRightString(RIGHT, y, _money(amt))
        y -= 5 * mm

    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT - 28 * mm, y, "Payment Received :")
    c.drawRightString(RIGHT, y, _money(total_recv))
    y -= 10 * mm

    # Insurance details
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "INSURANCE DETAILS")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    c.setFont(FONT_B, 8.3)
    c.drawString(LEFT, y, "Company")
    c.drawString(LEFT + 86 * mm, y, "Approval Number")
    c.drawRightString(RIGHT, y, "Amount")
    y -= 4.5 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    insurer_name = _s(
        _get(payload.get("insurer"), "name", None)
        or _get(payload.get("insurance_company"), "name", None)
        or _get(payload.get("payer"), "name", None))
    approval_no = _s(
        payload.get("insurance_approval_no")
        or _get(insurance, "policy_no", None)
        or _get(insurance, "member_id", None))
    ins_amt = _to_decimal(_get(insurance, "approved_limit", None))
    if ins_amt <= 0:
        ins_amt = _to_decimal(finance.get("insurance_payable") or 0)

    c.setFont(FONT, 8.3)
    c.drawString(LEFT, y, _fit_text(c, insurer_name, 80 * mm, FONT, 8.3))
    c.drawString(LEFT + 86 * mm, y,
                 _fit_text(c, approval_no, 60 * mm, FONT, 8.3))
    c.drawRightString(RIGHT, y, _money(ins_amt))
    y -= 10 * mm

    # Deposit summary
    c.setFont(FONT_B, 9)
    c.drawString(LEFT, y, "DEPOSIT SUMMARY")
    y -= 6 * mm
    c.setLineWidth(0.7)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    headers = [
        "Deposit Date", "Reference No", "Actual Amt", "Consumed Amt",
        "Refund Amt", "Balance Amt"
    ]
    cx = [
        LEFT, LEFT + 38 * mm, LEFT + 78 * mm, LEFT + 112 * mm, LEFT + 144 * mm,
        LEFT + 170 * mm
    ]
    c.setFont(FONT_B, 8.1)
    c.drawString(cx[0], y, headers[0])
    c.drawString(cx[1], y, headers[1])
    c.drawRightString(cx[2] + 24 * mm, y, headers[2])
    c.drawRightString(cx[3] + 24 * mm, y, headers[3])
    c.drawRightString(cx[4] + 24 * mm, y, headers[4])
    c.drawRightString(RIGHT, y, headers[5])
    y -= 4.2 * mm
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)
    y -= 5 * mm

    adv_rows = _advances_for_deposit(advances)
    adv_balance = _to_decimal(finance.get("advance_balance") or 0)
    adv_consumed = _to_decimal(
        finance.get("advance_consumed") or (max(
            Decimal("0"),
            sum((_to_decimal(_get(a, "amount", 0))
                 for a in adv_rows), Decimal("0")) - adv_balance)))
    adv_refund = _to_decimal(finance.get("advance_refund") or 0)

    c.setFont(FONT, 8.1)
    if adv_rows:
        a = adv_rows[0]
        dt = _d(_get(a, "entry_at", None), with_time=True)
        ref = _s(
            _get(a, "receipt_number", None) or _get(a, "txn_ref", None)
            or _num_fallback("ADV", _get(a, "id", None)))
        amt = _to_decimal(_get(a, "amount", 0))
        c.drawString(cx[0], y, dt)
        c.drawString(cx[1], y, ref)
        c.drawRightString(cx[2] + 24 * mm, y, _money(amt))
        c.drawRightString(
            cx[3] + 24 * mm, y,
            _money(-adv_consumed if adv_consumed else Decimal("0")))
        c.drawRightString(cx[4] + 24 * mm, y, _money(adv_refund))
        c.drawRightString(RIGHT, y, _money(adv_balance))
        y -= 10 * mm
    else:
        c.drawString(LEFT, y, "—")
        y -= 10 * mm

    # Abstract + signatures (bottom)
    total_amount = _to_decimal(finance.get("total_billed") or 0)
    if total_amount <= 0:
        total_amount = sum(
            (_to_decimal(_get(ln, "net_amount", 0)) for _, ln in all_lines),
            Decimal("0"))
    due = _to_decimal(finance.get("due") or (total_amount - total_recv))

    # signature box left
    sig_x = LEFT
    sig_y = BOTTOM + 34 * mm
    _box(c, sig_x, sig_y, 90 * mm, 22 * mm, lw=1.0)
    c.setFont(FONT, 8.3)
    c.drawString(sig_x + 3 * mm, sig_y - 6 * mm,
                 "Patient / Attender signature")
    c.drawString(sig_x + 3 * mm, sig_y - 12 * mm, "Name & Relationship")
    c.drawString(sig_x + 3 * mm, sig_y - 18 * mm, "Contact Number")

    # abstract box right
    box_w = 80 * mm
    box_h = 22 * mm
    bx = RIGHT - box_w
    by = sig_y
    _box(c, bx, by, box_w, box_h, lw=1.0)

    c.setFont(FONT_B, 9)
    c.drawString(bx + 3 * mm, by - 5 * mm, "Bill Abstract :")
    c.setFont(FONT_B, 8.5)
    c.drawString(bx + 3 * mm, by - 10 * mm, "Total Amount :")
    c.drawRightString(bx + box_w - 3 * mm, by - 10 * mm, _money(total_amount))
    c.drawString(bx + 3 * mm, by - 15 * mm, "Less Payment Received :")
    c.drawRightString(bx + box_w - 3 * mm, by - 15 * mm, _money(total_recv))
    c.drawString(bx + 3 * mm, by - 20 * mm, "Balance Amount :")
    c.drawRightString(bx + box_w - 3 * mm, by - 20 * mm, _money(due))

    # Amount in words
    words = _amount_in_words_inr(due if due > 0 else total_amount)
    c.setFont(FONT, 8.4)
    c.drawString(LEFT, BOTTOM + 20 * mm, f"Balance Amount in Words : {words}")

    # billed/checked + authorisation
    c.setFont(FONT, 8.4)
    c.drawString(LEFT, BOTTOM + 12 * mm, f"Billed By : {billed_by}")
    if checked_by and checked_by != "—":
        c.drawString(LEFT + 50 * mm, BOTTOM + 12 * mm,
                     f"Checked By : {checked_by}")

    c.setFont(FONT_B, 8.5)
    c.drawRightString(RIGHT, BOTTOM + 24 * mm, f"For {branding.org_name}")
    c.setFont(FONT, 8.2)
    c.drawRightString(RIGHT, BOTTOM + 14 * mm, "Authorised signatory")

    _draw_footer(c,
                 printed_dt,
                 f"Page {page_no} of {total_pages}",
                 billed_by=billed_by)


# ============================================================
# Rendering: PHARMACY SPLIT UP (N pages)
# ============================================================
def _render_pharmacy_page(
    c: canvas.Canvas,
    branding: BrandingSnapshot,
    payload: Dict[str, Any],
    items: List[Dict[str, Any]],
    page_no: int,
    total_pages: int,
    slice_rows: List[Dict[str, Any]],
):
    case = payload.get("case")
    patient = payload.get("patient")
    printed_dt: datetime = payload.get("printed_dt") or datetime.now()

    pblk = _patient_block(case, patient, payload)
    enc_label, enc_no = _encounter_label_and_number(case, payload)

    _draw_brand_band(c, branding)
    _draw_doc_title(c, "PHARMACY SPLIT UP REPORT", "", duplicate=False)
    _wm(c, branding)

    # top info like sample
    y = PAGE_H - 48 * mm
    left_rows = [
        ("Patient Name", f":  {_s(pblk['patient_name'])}"),
        (enc_label, f":  {enc_no}"),
        ("Payor",
         f":  {_s(_get(payload.get('payer'), 'name', None) or _get(payload.get('insurer'), 'name', None))}"
         ),
    ]
    _kv_block(c,
              LEFT,
              y,
              left_rows,
              col1_w=26 * mm,
              col2_w=70 * mm,
              line_h=4.6 * mm,
              font_size=8)

    right_rows = [
        ("Patient ID", f":  {_s(pblk['patient_id'])}"),
        ("Bill Date", f":  {_d(printed_dt, with_time=True)}"),
    ]
    _kv_block(c,
              RIGHT - 78 * mm,
              y,
              right_rows,
              col1_w=22 * mm,
              col2_w=52 * mm,
              line_h=4.6 * mm,
              font_size=8)

    # table
    y = PAGE_H - 78 * mm
    c.setLineWidth(0.8)
    c.setStrokeColor(LINE)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    cols = [
        "Bill No", "Bill Date", "Item Name", "Batch ID", "Expiry Date", "Qty",
        "Item Amount"
    ]
    cx = [
        LEFT, LEFT + 26 * mm, LEFT + 50 * mm, LEFT + 120 * mm, LEFT + 146 * mm,
        LEFT + 168 * mm, RIGHT
    ]
    c.setFont(FONT_B, 8.0)
    c.drawString(cx[0], y, cols[0])
    c.drawString(cx[1], y, cols[1])
    c.drawString(cx[2], y, cols[2])
    c.drawString(cx[3], y, cols[3])
    c.drawString(cx[4], y, cols[4])
    c.drawString(cx[5], y, cols[5])
    c.drawRightString(cx[6], y, cols[6])

    y -= 4.2 * mm
    c.setLineWidth(0.6)
    c.line(LEFT, y, RIGHT, y)
    y -= 6 * mm

    c.setFont(FONT, 8.0)
    row_h = 5 * mm
    for it in slice_rows:
        c.drawString(cx[0], y, _fit_text(c, it["bill_no"], 24 * mm, FONT, 8.0))
        c.drawString(cx[1], y, it["bill_date"])
        c.drawString(cx[2], y, _fit_text(c, it["item_name"], 68 * mm, FONT,
                                         8.0))
        c.drawString(cx[3], y, _fit_text(c, it["batch"], 24 * mm, FONT, 8.0))
        c.drawString(cx[4], y, _fit_text(c, it["expiry"], 20 * mm, FONT, 8.0))
        c.drawString(cx[5], y, _fit_text(c, it["qty"], 10 * mm, FONT, 8.0))
        c.drawRightString(cx[6], y, it["amount"])
        y -= row_h

    _draw_footer(
        c,
        printed_dt,
        f"Page {page_no} of {total_pages}",
        billed_by=_s(
            payload.get("billed_by") or payload.get("printed_by") or ""),
    )


# ============================================================
# PUBLIC API
# ============================================================
def build_full_case_pdf(payload: Dict[str, Any]) -> bytes:
    """
    ✅ Full Case PDF output EXACT structure:
      1) Summary Bill - Credit (2 pages)
      2) Detail Bill of Supply - Credit (N pages incl. last summary page)
      3) Pharmacy Split Up Report (N pages)

    payload recommended keys:
      branding: UiBranding
      case: BillingCase (or dict)
      patient: Patient (or dict)
      invoices: list[BillingInvoice] each with .lines
      payments: list[BillingPayment]
      advances: list[BillingAdvance]
      insurance: BillingInsuranceCase (optional)
      preauths: list[BillingPreauthRequest] (optional)
      claims: list[BillingClaim] (optional)
      finance: dict with totals (optional)
      encounter_meta: dict (recommended) with visit/admission numbers + doctor/ward/room/dates

      printed_by / billed_by / checked_by (optional)
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    branding = snapshot_branding(payload.get("branding"))
    invoices = payload.get("invoices") or []

    # -------------------------
    # Section 1: Summary (2 pages)
    # -------------------------
    summary_total_pages = 2
    _render_summary_page1(c,
                          branding,
                          payload,
                          page_no=1,
                          total_pages=summary_total_pages)
    c.showPage()
    _render_summary_page2(c,
                          branding,
                          payload,
                          page_no=2,
                          total_pages=summary_total_pages)
    c.showPage()

    # -------------------------
    # Section 2: Detail (N item pages + 1 last page)
    # -------------------------
    detail_rows = _build_detail_rows(invoices)

    # paginate detail_rows into item pages based on vertical space
    # approximate rows per page; headers/subtotals consume same space as normal rows
    ROWS_PER_PAGE = 38
    item_pages = []
    cur = []
    count = 0
    for r in detail_rows:
        # spacer not counted heavily
        if r.get("kind") == "spacer":
            # treat as half row
            if count + 0.5 > ROWS_PER_PAGE and cur:
                item_pages.append(cur)
                cur = []
                count = 0
            cur.append(r)
            count += 0.5
            continue

        if count + 1 > ROWS_PER_PAGE and cur:
            item_pages.append(cur)
            cur = []
            count = 0
        cur.append(r)
        count += 1
    if cur:
        item_pages.append(cur)

    # last page is summary/signature
    detail_total_pages = len(item_pages) + 1
    for idx, page_rows in enumerate(item_pages, start=1):
        _render_detail_item_pages(c,
                                  branding,
                                  payload,
                                  page_rows,
                                  page_no=idx,
                                  total_pages=detail_total_pages)
        c.showPage()

    _render_detail_last_page(c,
                             branding,
                             payload,
                             page_no=detail_total_pages,
                             total_pages=detail_total_pages)
    c.showPage()

    # -------------------------
    # Section 3: Pharmacy Split Up (N pages)
    # -------------------------
    ph_items = _pharmacy_items_from_invoices(invoices)
    PH_ROWS_PER_PAGE = 28
    ph_pages = ceil(len(ph_items) / PH_ROWS_PER_PAGE) if ph_items else 1

    if not ph_items:
        # still output 1 page with empty table, like report exists but no items
        _render_pharmacy_page(c,
                              branding,
                              payload,
                              ph_items,
                              page_no=1,
                              total_pages=1,
                              slice_rows=[])
        c.showPage()
    else:
        for p in range(ph_pages):
            start = p * PH_ROWS_PER_PAGE
            end = start + PH_ROWS_PER_PAGE
            _render_pharmacy_page(
                c,
                branding,
                payload,
                ph_items,
                page_no=p + 1,
                total_pages=ph_pages,
                slice_rows=ph_items[start:end],
            )
            c.showPage()

    c.save()
    return buf.getvalue()
