# FILE: app/api/routes_billing_print.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.pdfgen import canvas

from app.api.deps import get_db, current_user
from app.core.config import settings

from app.models.user import User
from app.models.patient import Patient, PatientAddress
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingPayment,
    BillingAdvance,
    EncounterType,
    PayerMode,
    DocStatus,
    ReceiptStatus,
    PaymentKind,
    PaymentDirection,
    AdvanceType,
)
from app.models.ui_branding import UiBranding

# ✅ payer masters
from app.models.payer import Payer, Tpa, CreditPlan  # type: ignore

# (Optional) Weasy helpers (only if installed)
brand_header_css = None
render_brand_header_html = None
try:
    from app.services.pdf_branding import brand_header_css, render_brand_header_html  # type: ignore
except Exception:
    try:
        from app.service.pdf_branding import brand_header_css, render_brand_header_html  # type: ignore
    except Exception:
        brand_header_css = None
        render_brand_header_html = None

# Encounter models
try:
    from app.models.opd import Visit  # type: ignore
except Exception:
    Visit = None

try:
    from app.models.ipd import IpdAdmission, IpdBed, IpdRoom  # type: ignore
except Exception:
    IpdAdmission = None
    IpdBed = None
    IpdRoom = None

router = APIRouter(prefix="/billing/print", tags=["Billing Print"])


# ---------------------------
# Permissions (safe fallback)
# ---------------------------
def _need_any(user: User, perms: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return

    fn = getattr(user, "has_perm", None)
    if callable(fn):
        for p in perms:
            try:
                if fn(p):
                    return
            except Exception:
                pass

    perms_list = getattr(user, "permissions", None) or []
    perms_set = set(perms_list) if isinstance(perms_list,
                                              (list, tuple, set)) else set()
    if any(p in perms_set for p in perms):
        return

    raise HTTPException(status_code=403, detail="Not permitted")


# ---------------------------
# Small utils
# ---------------------------
def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _eq_enum(v: Any, enum_member: Any) -> bool:
    try:
        return v == enum_member or _val(v) == _val(enum_member)
    except Exception:
        return False


def _safe(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).strip()
    return s if s else "—"


def _dec(v: Any) -> Decimal:
    try:
        if v is None:
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _money(v: Any) -> str:
    d = _dec(v)
    return f"{d.quantize(Decimal('0.01'))}"


def _fmt_date(d: Any) -> str:
    if isinstance(d, (datetime, date)):
        return d.strftime("%d-%b-%Y")
    return _safe(d)


def _fmt_dt(d: Any) -> str:
    if isinstance(d, datetime):
        return d.strftime("%d-%b-%Y %I:%M %p")
    return _fmt_date(d)


def _calc_age(dob: Optional[date], on: Optional[date] = None) -> str:
    if not dob:
        return "—"
    on = on or date.today()
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    if years >= 1:
        return f"{years} Y"
    months = (on.year - dob.year) * 12 + (on.month - dob.month)
    if on.day < dob.day:
        months = max(0, months - 1)
    return f"{months} M"


def _human_gender(g: Optional[str]) -> str:
    if not g:
        return "—"
    x = g.strip().lower()
    if x in ("m", "male"):
        return "Male"
    if x in ("f", "female"):
        return "Female"
    if x in ("o", "other"):
        return "Other"
    return g


def _patient_display_name(p: Patient) -> str:
    prefix = (getattr(p, "prefix", None) or "").strip()
    if prefix and not prefix.endswith("."):
        prefix += "."
    name = " ".join([
        x for x in [
            (getattr(p, "first_name", None) or "").strip(),
            (getattr(p, "last_name", None) or "").strip(),
        ] if x
    ]).strip()
    full = " ".join([x for x in [prefix, name] if x]).strip()
    return full.upper() if full else "—"


def _pick_best_address(addresses: list[PatientAddress]) -> str:
    if not addresses:
        return "—"
    pref = {"current": 0, "permanent": 1, "office": 2, "other": 3}
    addresses_sorted = sorted(
        addresses,
        key=lambda a: pref.get((getattr(a, "type", None) or "").lower(), 99))
    a = addresses_sorted[0]
    parts = [
        (getattr(a, "line1", None) or "").strip(),
        (getattr(a, "line2", None) or "").strip(),
        (getattr(a, "city", None) or "").strip(),
        (getattr(a, "state", None) or "").strip(),
        (getattr(a, "pincode", None) or "").strip(),
        (getattr(a, "country", None) or "").strip(),
    ]
    parts = [x for x in parts if x]
    return ", ".join(parts) if parts else "—"


# ---------------------------
# Amount in words (INR)
# ---------------------------
_ONES = [
    "",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty",
    "Ninety"
]


def _two_digits(n: int) -> str:
    if n == 0:
        return ""
    if n < 20:
        return _ONES[n]
    t = n // 10
    o = n % 10
    if o == 0:
        return _TENS[t]
    return f"{_TENS[t]} {_ONES[o]}"


def _three_digits(n: int) -> str:
    if n == 0:
        return ""
    h = n // 100
    r = n % 100
    parts: list[str] = []
    if h:
        parts.append(f"{_ONES[h]} Hundred")
        if r:
            parts.append("and")
    if r:
        parts.append(_two_digits(r))
    return " ".join([p for p in parts if p])


def _int_to_words_indian(n: int) -> str:
    if n == 0:
        return "Zero"
    if n < 0:
        return f"Minus {_int_to_words_indian(abs(n))}"

    parts: list[str] = []

    crore = n // 10000000
    n = n % 10000000
    if crore:
        parts.append(f"{_three_digits(crore)} Crore")

    lakh = n // 100000
    n = n % 100000
    if lakh:
        parts.append(f"{_three_digits(lakh)} Lakh")

    thousand = n // 1000
    n = n % 1000
    if thousand:
        parts.append(f"{_three_digits(thousand)} Thousand")

    if n:
        parts.append(_three_digits(n))

    out = " ".join([p for p in parts if p]).strip()
    return out if out else "Zero"


def _amount_in_words_inr(amount: Any) -> str:
    """
    ✅ Rupees / Paise words
    Example: 2390.00 => "Rupees Two Thousand Three Hundred and Ninety Only"
    """
    d = _dec(amount).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    d = abs(d)

    rupees = int(d)
    paise = int((d - Decimal(rupees)) * 100)

    r_words = _int_to_words_indian(rupees)
    if paise > 0:
        p_words = _int_to_words_indian(paise)
        return f"{sign}Rupees {r_words} and Paise {p_words} Only"
    return f"{sign}Rupees {r_words} Only"


# ---------------------------
# Loaders
# ---------------------------
def _load_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).order_by(UiBranding.id.asc()).first()


def _load_case(db: Session, case_id: int) -> BillingCase:
    case = (db.query(BillingCase).options(
        selectinload(BillingCase.patient).selectinload(
            Patient.addresses)).filter(BillingCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return case


def _load_op_context(db: Session, encounter_id: int) -> Dict[str, str]:
    out = {
        "Visit Id": "—",
        "Appointment On": "—",
        "Doctor": "—",
        "Department": "—"
    }
    if Visit is None:
        return out

    v = (db.query(Visit).options(selectinload(Visit.appointment),
                                 selectinload(Visit.doctor),
                                 selectinload(Visit.department)).filter(
                                     Visit.id == encounter_id).first())
    if not v:
        return out

    visit_no = getattr(v, "episode_id", None) or getattr(v, "op_no",
                                                         None) or None
    out["Visit Id"] = _safe(visit_no)

    appt = getattr(v, "appointment", None)
    if appt is not None:
        appt_date = getattr(appt, "date", None)
        slot_start = getattr(appt, "slot_start", None)
        if appt_date and slot_start:
            out["Appointment On"] = f"{_fmt_date(appt_date)} {str(slot_start)[:5]}"
        elif appt_date:
            out["Appointment On"] = _fmt_date(appt_date)

        doc = getattr(appt, "doctor", None) or getattr(v, "doctor", None)
        dept = getattr(appt, "department", None) or getattr(
            v, "department", None)
        out["Doctor"] = _safe(getattr(doc, "name", None))
        out["Department"] = _safe(getattr(dept, "name", None))
    else:
        doc = getattr(v, "doctor", None)
        dept = getattr(v, "department", None)
        out["Doctor"] = _safe(getattr(doc, "name", None))
        out["Department"] = _safe(getattr(dept, "name", None))

    return out


def _load_ip_context(db: Session, encounter_id: int) -> Dict[str, str]:
    out = {
        "IP Admission Number": "—",
        "Ward": "—",
        "Room": "—",
        "Bed": "—",
        "Admitted On": "—",
        "Discharged On": "—",
        "Admission Doctor": "—",
        "Department": "—",
    }
    if IpdAdmission is None:
        return out

    q = db.query(IpdAdmission).options(
        selectinload(IpdAdmission.current_bed).selectinload(
            IpdBed.room).selectinload(IpdRoom.ward),
        selectinload(IpdAdmission.department),
    )
    adm = q.filter(IpdAdmission.id == encounter_id).first()
    if not adm:
        return out

    out["IP Admission Number"] = _safe(
        getattr(adm, "admission_code", None)
        or getattr(adm, "display_code", None))
    out["Admitted On"] = _fmt_dt(getattr(adm, "admitted_at", None))
    out["Discharged On"] = _fmt_dt(getattr(adm, "discharge_at", None))

    practitioner_id = getattr(adm, "practitioner_user_id", None)
    if practitioner_id:
        doc = db.query(User).filter(User.id == practitioner_id).first()
        out["Admission Doctor"] = _safe(getattr(doc, "name", None))

    dept = getattr(adm, "department", None)
    out["Department"] = _safe(getattr(dept, "name", None))

    bed = getattr(adm, "current_bed", None)
    if bed is not None:
        out["Bed"] = _safe(getattr(bed, "code", None))
        room = getattr(bed, "room", None)
        if room is not None:
            out["Room"] = _safe(getattr(room, "number", None))
            ward = getattr(room, "ward", None)
            if ward is not None:
                out["Ward"] = _safe(getattr(ward, "name", None))

    return out


# ---------------------------
# Payer lookups
# ---------------------------
def _payer_name(db: Session, payer_id: Optional[int]) -> str:
    if not payer_id:
        return "—"
    obj = db.query(Payer).filter(Payer.id == payer_id).first()
    return _safe(obj.name) if obj else "—"


def _tpa_name(db: Session, tpa_id: Optional[int]) -> str:
    if not tpa_id:
        return "—"
    obj = db.query(Tpa).filter(Tpa.id == tpa_id).first()
    return _safe(obj.name) if obj else "—"


def _plan_name(db: Session, plan_id: Optional[int]) -> str:
    if not plan_id:
        return "—"
    obj = db.query(CreditPlan).filter(CreditPlan.id == plan_id).first()
    return _safe(obj.name) if obj else "—"


def _payer_block(db: Session, case: BillingCase) -> Dict[str, str]:
    pm = case.payer_mode
    out: Dict[str, str] = {"Payer Mode": _safe(_val(pm) if pm else None)}

    if pm == PayerMode.SELF:
        return out

    patient: Patient = case.patient
    effective_payer_id = case.default_payer_id or getattr(
        patient, "credit_payer_id", None)
    effective_tpa_id = case.default_tpa_id or getattr(patient, "credit_tpa_id",
                                                      None)
    effective_plan_id = case.default_credit_plan_id or getattr(
        patient, "credit_plan_id", None)

    out["Default Bill Type"] = _safe(getattr(case, "default_payer_type", None))
    out["Payer"] = _payer_name(db, effective_payer_id)
    out["TPA"] = _tpa_name(db, effective_tpa_id)
    out["Credit Plan"] = _plan_name(db, effective_plan_id)
    return out


# ---------------------------
# Branding header (ReportLab) – MEDIUM size
# ---------------------------
def _read_logo_reader(branding: Any) -> Optional[ImageReader]:
    rel = (getattr(branding, "logo_path", None) or "").strip()
    if not rel:
        return None
    abs_path = Path(getattr(settings, "STORAGE_DIR", ".")).joinpath(rel)
    if not abs_path.exists() or not abs_path.is_file():
        return None
    try:
        return ImageReader(str(abs_path))
    except Exception:
        return None


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


def _draw_branding_header(
    c: canvas.Canvas,
    branding: Optional[UiBranding],
    x: float,
    top_y: float,
    w: float,
) -> float:
    b = branding or SimpleNamespace(
        org_name="",
        org_tagline="",
        org_address="",
        org_phone="",
        org_email="",
        org_website="",
        org_gstin="",
        logo_path="",
    )

    INK = colors.black
    MUTED = colors.HexColor("#334155")

    # medium header
    logo_h = 22 * mm
    logo_col = max(70 * mm, min(92 * mm, w * 0.45))
    right_w = max(40 * mm, w - logo_col)

    org = _safe(getattr(b, "org_name", None))
    tag = _safe(getattr(b, "org_tagline", None))
    addr = _safe(getattr(b, "org_address", None))
    phone = _safe(getattr(b, "org_phone", None))
    email = _safe(getattr(b, "org_email", None))
    website = _safe(getattr(b, "org_website", None))
    gstin = _safe(getattr(b, "org_gstin", None))

    # contact (single line)
    contact_bits = []
    if phone != "—":
        contact_bits.append(f"Ph: {phone}")
    if email != "—":
        contact_bits.append(f"Email: {email}")
    contact_line = " | ".join(contact_bits) if contact_bits else ""

    meta_lines: list[str] = []
    if addr != "—":
        meta_lines.extend(
            _cap_lines(
                simpleSplit(f"Address: {addr}", "Helvetica", 8.6, right_w), 2))
    if contact_line:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(f"Contact: {contact_line}", "Helvetica", 8.6,
                            right_w), 1))

    extras = []
    if website != "—":
        extras.append(f"Website: {website}")
    if gstin != "—":
        extras.append(f"GSTIN: {gstin}")
    if extras and len(meta_lines) < 3:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(" | ".join(extras), "Helvetica", 8.6, right_w), 1))

    meta_lines = _cap_lines(meta_lines, 3)

    lines: list[tuple[str, str, float, Any]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", 12.5, INK))
    if tag != "—":
        lines.append((tag, "Helvetica", 8.9, MUTED))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", 8.6, MUTED))

    def lh(sz: float) -> float:
        return sz * 1.20

    text_h = sum(lh(sz) for _, _, sz, _ in lines) if lines else 10
    header_h = max(logo_h, text_h) + (1.5 * mm)

    # logo (left, vertically centered)
    logo_reader = _read_logo_reader(b)
    if logo_reader:
        try:
            iw, ih = logo_reader.getSize()
            if iw and ih:
                scale_h = logo_h / float(ih)
                draw_w = float(iw) * scale_h
                draw_h = logo_h

                max_w = logo_col - (2 * mm)
                if draw_w > max_w:
                    scale_w = max_w / float(iw)
                    draw_w = max_w
                    draw_h = float(ih) * scale_w

                logo_x = x
                logo_y = top_y - (header_h / 2) - (draw_h / 2)
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

    # org details (right aligned, vertically centered)
    text_right_x = x + w
    cur_y = top_y - (header_h / 2) + (text_h / 2)
    for txt, font, sz, col in lines:
        cur_y -= lh(sz)
        c.setFont(font, sz)
        c.setFillColor(col)
        c.drawRightString(text_right_x, cur_y, txt)

    # rule
    c.setStrokeColor(colors.HexColor("#cbd5e1"))
    c.setLineWidth(0.6)
    c.line(x, top_y - header_h, x + w, top_y - header_h)

    return top_y - header_h - (2 * mm)


# ---------------------------
# Payload: Header
# ---------------------------
def _build_header_payload(db: Session, case: BillingCase,
                          doc_no: Optional[str],
                          doc_date: Optional[date]) -> Dict[str, Any]:
    patient: Patient = case.patient

    payload: Dict[str, Any] = {
        "bill": {
            "Bill Number":
            doc_no or _safe(getattr(case, "case_number", None)),
            "Bill Date":
            _fmt_date(doc_date or getattr(case, "created_at", None)
                      or date.today()),
        },
        "patient": {
            "Patient Name": _patient_display_name(patient),
            "UHID": _safe(getattr(patient, "uhid", None)),
            "Phone": _safe(getattr(patient, "phone", None)),
            "Address":
            _pick_best_address(getattr(patient, "addresses", []) or []),
            "Age": _calc_age(getattr(patient, "dob", None)),
            "Gender": _human_gender(getattr(patient, "gender", None)),
        },
        "encounter_type":
        _safe(_val(case.encounter_type) if case.encounter_type else None),
        "encounter": {},
        "payer": {},
    }

    if case.encounter_type == EncounterType.OP:
        payload["encounter"] = _load_op_context(db, int(case.encounter_id))
    elif case.encounter_type == EncounterType.IP:
        payload["encounter"] = _load_ip_context(db, int(case.encounter_id))
    else:
        payload["encounter"] = {}

    payload["payer"] = _payer_block(db, case)
    return payload


# ---------------------------
# Module label mapping
# ---------------------------
MODULE_LABELS: Dict[str, str] = {
    "DOC": "Doctor Fees",
    "DOCTOR": "Doctor Fees",
    "CONSULT": "Doctor Fees",
    "CONSULTATION": "Doctor Fees",
    "LAB": "Clinical Lab Charges",
    "LIS": "Clinical Lab Charges",
    "LABORATORY": "Clinical Lab Charges",
    "PHM": "Pharmacy Charges (Medicines)",
    "PHARM": "Pharmacy Charges (Medicines)",
    "PHARMACY": "Pharmacy Charges (Medicines)",
    "RX": "Pharmacy Charges (Medicines)",
    "MED": "Pharmacy Charges (Medicines)",
    "ADM": "Admission Charges",
    "ADMISSION": "Admission Charges",
    "ROOM": "Observation / Room Charges",
    "BED": "Observation / Room Charges",
    "WARD": "Observation / Room Charges",
    "BLOOD": "Blood Bank Charges",
    "BLOODBANK": "Blood Bank Charges",
    "BB": "Blood Bank Charges",
    "DIET": "Dietary Charges",
    "DIETARY": "Dietary Charges",
    "PHC": "Pharmacy Charges (Consumables)",
    "CONSUMABLES": "Pharmacy Charges (Consumables)",
    "CONSUMABLE": "Pharmacy Charges (Consumables)",
    "CONSUM": "Pharmacy Charges (Consumables)",
    "PROC": "Procedure Charges",
    "PROCEDURE": "Procedure Charges",
    "SCAN": "Scan Charges",
    "RAD": "Scan Charges",
    "RIS": "Scan Charges",
    "SURG": "Surgery Charges",
    "SURGERY": "Surgery Charges",
    "OT": "Surgery Charges",
    "XRAY": "X-Ray Charges",
    "X-RAY": "X-Ray Charges",
    "MISC": "Miscellaneous Charges",
    "OTHER": "Miscellaneous Charges",
}

MODULE_ORDER: list[str] = [
    "ADM",
    "ROOM",
    "DOC",
    "LAB",
    "BLOOD",
    "DIET",
    "PHM",
    "PHC",
    "PROC",
    "SCAN",
    "XRAY",
    "SURG",
    "MISC",
]


def _module_label(code: str) -> str:
    c = (code or "").strip().upper()
    return MODULE_LABELS.get(c, "Miscellaneous Charges")


def _module_order_key(code: str) -> int:
    c = (code or "").strip().upper()
    try:
        return MODULE_ORDER.index(c)
    except Exception:
        return 999


# ---------------------------
# Payload: Overview (Module-wise compact)
# ---------------------------
def _build_overview_payload(
    db: Session,
    case: BillingCase,
    *,
    doc_no: Optional[str] = None,
    doc_date: Optional[date] = None,
    include_draft_invoices: bool = True,
) -> Dict[str, Any]:
    base = _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)

    inv_q = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case.id,
        BillingInvoice.status != DocStatus.VOID,
    )
    if not include_draft_invoices:
        inv_q = inv_q.filter(BillingInvoice.status != DocStatus.DRAFT)

    invoices = inv_q.order_by(BillingInvoice.created_at.asc()).all()

    modules_map: Dict[Tuple[str, str], Decimal] = {}
    total_bill = Decimal("0")
    taxable_value = Decimal("0")
    gst_total = Decimal("0")
    round_off_total = Decimal("0")

    for inv in invoices:
        mod_code = (getattr(inv, "module", None) or "MISC").strip().upper()
        label = _module_label(mod_code)

        amt = _dec(getattr(inv, "grand_total", 0))
        total_bill += amt

        sub_total = _dec(getattr(inv, "sub_total", None))
        disc_total = _dec(getattr(inv, "discount_total", None))
        tax_total = _dec(getattr(inv, "tax_total", None))
        roff = _dec(getattr(inv, "round_off", None))

        if sub_total != Decimal("0") or disc_total != Decimal("0"):
            taxable_value += (sub_total - disc_total)
        gst_total += tax_total
        round_off_total += roff

        key = (mod_code, label)
        modules_map[key] = modules_map.get(key, Decimal("0")) + amt

    if taxable_value == Decimal("0") and total_bill > Decimal(
            "0") and gst_total == Decimal("0"):
        taxable_value = total_bill

    modules = [{
        "code": k[0],
        "label": k[1],
        "total": float(v)
    } for k, v in modules_map.items()]
    modules.sort(key=lambda m: _module_order_key(m["code"]))

    # ---- payments (include advance adjustment into "Payment Received")
    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case.id).order_by(
            BillingPayment.received_at.asc()).all())

    receipts_total = Decimal("0")
    adv_adjusted_total = Decimal("0")
    refunds_total = Decimal("0")

    payment_rows: list[Dict[str, Any]] = []

    for p in pays:
        status = getattr(p, "status", None)
        direction = getattr(p, "direction", None)
        kind = getattr(p, "kind", None)
        amt = _dec(getattr(p, "amount", 0))

        is_active = _eq_enum(status, ReceiptStatus.ACTIVE)
        is_in = _eq_enum(direction, PaymentDirection.IN)
        is_out = _eq_enum(direction, PaymentDirection.OUT)

        if not is_active:
            continue

        if is_out or _eq_enum(kind, PaymentKind.REFUND):
            refunds_total += amt
            continue

        if is_in:
            if _eq_enum(kind, PaymentKind.RECEIPT) or kind is None:
                receipts_total += amt
                receipt_no = _safe(getattr(p, "receipt_number", None))
                if receipt_no == "—":
                    receipt_no = _safe(getattr(p, "txn_ref", None))
                payment_rows.append({
                    "receipt_number":
                    receipt_no,
                    "mode":
                    _safe(_val(getattr(p, "mode", None))),
                    "date":
                    _fmt_date(getattr(p, "received_at", None)),
                    "amount":
                    float(amt),
                })
            elif _eq_enum(kind, PaymentKind.ADVANCE_ADJUSTMENT):
                adv_adjusted_total += amt
                payment_rows.append({
                    "receipt_number":
                    "ADV-ADJ",
                    "mode":
                    "ADVANCE",
                    "date":
                    _fmt_date(getattr(p, "received_at", None)),
                    "amount":
                    float(amt),
                })
            else:
                receipts_total += amt
                payment_rows.append({
                    "receipt_number":
                    _safe(getattr(p, "receipt_number", None)) if _safe(
                        getattr(p, "receipt_number", None)) != "—" else "RCPT",
                    "mode":
                    _safe(_val(getattr(p, "mode", None))),
                    "date":
                    _fmt_date(getattr(p, "received_at", None)),
                    "amount":
                    float(amt),
                })

    payment_received_total = receipts_total + adv_adjusted_total
    effective_paid = payment_received_total - refunds_total
    balance = total_bill - effective_paid

    # ---- advances summary
    adv_rows = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case.id).order_by(
            BillingAdvance.entry_at.asc()).all())

    total_adv_in = Decimal("0")  # ✅ gross advance paid (example: 10000)
    total_adv_refund = Decimal("0")  # ✅ refund (example: 5000)
    total_adv_adjust = Decimal("0")
    last_adv_dt: Optional[datetime] = None

    for a in adv_rows:
        last_adv_dt = getattr(a, "entry_at", None) or last_adv_dt
        et = getattr(a, "entry_type", None)
        amt = _dec(getattr(a, "amount", 0))
        if _eq_enum(et, AdvanceType.ADVANCE):
            total_adv_in += amt
        elif _eq_enum(et, AdvanceType.REFUND):
            total_adv_refund += amt
        else:
            total_adv_adjust += amt

    net_advance = total_adv_in - total_adv_refund + total_adv_adjust
    consumed = adv_adjusted_total
    available = net_advance - consumed
    if available < 0:
        available = Decimal("0")

    payload: Dict[str, Any] = {
        **base,
        "case_number": _safe(getattr(case, "case_number", None)),
        "generated_on": _fmt_date(datetime.utcnow()),
        "modules": [{
            "label": m["label"],
            "total": m["total"]
        } for m in modules],
        "totals": {
            "total_bill": float(total_bill),
            "taxable_value": float(taxable_value),
            "gst": float(gst_total),
            "round_off": float(round_off_total),
            "payment_received": float(payment_received_total),
            "refunds": float(refunds_total),
            "effective_paid": float(effective_paid),
            "balance": float(balance),

            # ✅ NEW: words for all requested
            "total_bill_words": _amount_in_words_inr(total_bill),
            "payment_received_words":
            _amount_in_words_inr(payment_received_total),
            "balance_words": _amount_in_words_inr(balance),
        },
        "payment_details": payment_rows,
        "advance_summary_row": {
            "as_on": _fmt_date(last_adv_dt) if last_adv_dt else "—",
            "type": "Advance Wallet",
            # ✅ show gross paid, not net after refund
            "total_advance": float(total_adv_in),
            "net_advance": float(net_advance),
            "consumed": float(consumed),
            "available": float(available),
            "advance_refunded": float(total_adv_refund),
        },
    }
    return payload


# ---------------------------
# ReportLab: Patient header (FIXED alignment)
# ---------------------------
def _draw_lv_column(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    col_w: float,
    rows: list[tuple[str, str]],
    label_w: float,
    size: float = 8.9,
    leading: float = 10.8,
) -> float:
    colon_w = 2.0 * mm
    gap = 2.0 * mm
    value_x = x + label_w + colon_w + gap
    value_w = max(10, col_w - (label_w + colon_w + gap))

    for k, v in rows:
        k = _safe(k)
        v = _safe(v)

        c.setFont("Helvetica-Bold", size)
        c.setFillColor(colors.black)

        c.drawString(x, y, (k[:28] + "…") if len(k) > 29 else k)
        c.drawString(x + label_w + 0.2 * mm, y, ":")

        c.setFont("Helvetica", size)
        lines = simpleSplit(v, "Helvetica", size, value_w) or ["—"]
        c.drawString(value_x, y, lines[0][:160])

        for ln in lines[1:]:
            y -= leading
            c.drawString(value_x, y, ln[:160])

        y -= leading

    return y


def _draw_patient_header_block(c: canvas.Canvas, payload: Dict[str, Any],
                               x: float, y_top: float, w: float) -> float:
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
        left_rows += [
            ("Doctor", _safe(enc.get("Doctor"))),
            ("Department", _safe(enc.get("Department"))),
        ]
    elif et == "IP":
        left_rows += [
            ("Ward", _safe(enc.get("Ward"))),
            ("Doctor", _safe(enc.get("Admission Doctor"))),
        ]

    left_rows += [("Patient Address", _safe(pat.get("Address")))]

    right_rows = [
        ("Bill Number", _safe(bill.get("Bill Number"))),
        ("Bill Date", _safe(bill.get("Bill Date"))),
        ("Encounter Type", _safe(et)),
    ]
    if et == "OP":
        right_rows += [
            ("Visit ID", _safe(enc.get("Visit Id"))),
            ("Appointment On", _safe(enc.get("Appointment On"))),
        ]
    elif et == "IP":
        right_rows += [
            ("IP Number", _safe(enc.get("IP Admission Number"))),
            ("Admitted On", _safe(enc.get("Admitted On"))),
            ("Discharged On", _safe(enc.get("Discharged On"))),
        ]

    y1 = _draw_lv_column(
        c,
        x=x,
        y=y_top,
        col_w=left_w - 2 * mm,
        rows=left_rows,
        label_w=28 * mm,
    )
    y2 = _draw_lv_column(
        c,
        x=x + left_w + 6 * mm,
        y=y_top,
        col_w=right_w - 6 * mm,
        rows=right_rows,
        label_w=28 * mm,
    )

    y_end = min(y1, y2)

    # ✅ PATCH: line padding (avoid overlap with address)
    line_y = y_end + 1.2 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.9)
    c.line(x, line_y, x + w, line_y)

    return y_end


# ---------------------------
# ReportLab: Simple table
# ---------------------------
def _draw_simple_table(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    w: float,
    cols: list[tuple[str, float]],
    rows: list[list[str]],
    row_h: float = 7 * mm,
    bottom_margin: float = 14 * mm,
    new_page_fn=None,
) -> float:
    col_widths = [w * r for _, r in cols]
    head_h = row_h

    def draw_header(cur_y: float) -> float:
        c.setFont("Helvetica-Bold", 9.2)
        c.setFillColor(colors.black)
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.9)
        c.rect(x, cur_y - head_h, w, head_h, stroke=1, fill=0)

        xx = x
        for (title, _), cw in zip(cols, col_widths):
            c.drawString(xx + 2.0 * mm, cur_y - head_h + 2.2 * mm, title)
            xx += cw

        xx = x
        for cw in col_widths[:-1]:
            xx += cw
            c.line(xx, cur_y - head_h, xx, cur_y)

        return cur_y - head_h

    def ensure(cur_y: float, need: float) -> float:
        if cur_y - need < bottom_margin:
            if callable(new_page_fn):
                c.showPage()
                cur_y = new_page_fn()
            else:
                c.showPage()
                cur_y = A4[1] - 12 * mm
            cur_y = draw_header(cur_y)
        return cur_y

    cur_y = draw_header(y)
    c.setFont("Helvetica", 9.2)

    for r in rows:
        cur_y = ensure(cur_y, row_h + 2 * mm)

        c.setStrokeColor(colors.black)
        c.setLineWidth(0.7)
        c.rect(x, cur_y - row_h, w, row_h, stroke=1, fill=0)

        xx = x
        for cw in col_widths[:-1]:
            xx += cw
            c.line(xx, cur_y - row_h, xx, cur_y)

        xx = x
        for j, cw in enumerate(col_widths):
            txt = "" if j >= len(r) else ("" if r[j] is None else str(r[j]))
            c.drawString(xx + 2.0 * mm, cur_y - row_h + 2.0 * mm, txt[:160])
            xx += cw

        cur_y -= row_h

    return cur_y


# ---------------------------
# PDFs
# ---------------------------
def _render_common_header_pdf_reportlab(
        payload: Dict[str, Any], branding: Optional[UiBranding]) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    y = H - M

    y = _draw_branding_header(c, branding, x0, y, w0)
    y -= 3 * mm
    _draw_patient_header_block(c, payload, x0, y, w0)

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_overview_pdf_reportlab(payload: Dict[str, Any],
                                   branding: Optional[UiBranding]) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    bottom = 14 * mm

    def new_page() -> float:
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0)
        y0 -= 2 * mm

        c.setFont("Helvetica-Bold", 10.0)
        c.setFillColor(colors.black)
        c.drawCentredString(x0 + w0 / 2, y0, "BILL SUMMARY")
        y0 -= 5 * mm
        return y0

    y = new_page()

    y = _draw_patient_header_block(c, payload, x0, y, w0)
    y -= 3 * mm

    c.setFont("Helvetica-Bold", 9.8)
    c.drawString(x0, y, "Particulars")
    c.drawRightString(x0 + w0, y, "Total Amount")
    y -= 4 * mm

    modules = payload.get("modules") or []
    part_rows = [[_safe(m.get("label")),
                  _money(m.get("total"))] for m in modules] or [["—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[("Particulars", 0.72), ("Amount", 0.28)],
        rows=part_rows,
        row_h=7 * mm,
        bottom_margin=bottom,
        new_page_fn=new_page,
    )
    y -= 3 * mm

    totals = payload.get("totals") or {}

    total_bill = _money(totals.get("total_bill"))
    taxable_val = _money(totals.get("taxable_value"))
    gst_val = _money(totals.get("gst"))
    round_off = _money(totals.get("round_off"))

    payment_received = _money(totals.get("payment_received"))
    balance = _money(totals.get("balance"))

    total_bill_words = _safe(
        totals.get("total_bill_words")
        or _amount_in_words_inr(totals.get("total_bill")))
    pay_recv_words = _safe(
        totals.get("payment_received_words")
        or _amount_in_words_inr(totals.get("payment_received")))
    balance_words = _safe(
        totals.get("balance_words")
        or _amount_in_words_inr(totals.get("balance")))

    def total_line(label: str,
                   value: str,
                   yy: float,
                   bold: bool = False) -> float:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4)
        c.drawRightString(x0 + w0 - 42 * mm, yy, label)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4)
        c.drawRightString(x0 + w0, yy, value)
        return yy - 5.0 * mm

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(x0, y, x0 + w0, y)
    y -= 4.3 * mm

    y = total_line("Taxable Value :", taxable_val, y)
    y = total_line("GST :", gst_val, y)
    y = total_line("Round Off :", round_off, y)
    y = total_line("Total Bill Amount :", total_bill, y, bold=True)

    # ✅ NEW: Total Bill Amount in words
    y -= 3.5 * mm
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    bill_word_lines = simpleSplit(total_bill_words, "Helvetica", 9.2,
                                  w0 - 22 * mm)
    yy = y
    x_words = x0 + 18 * mm
    for ln in bill_word_lines[:3]:
        c.drawString(x_words, yy, ln)
        yy -= 4.6 * mm
    y = yy - 2.0 * mm

    # PAYMENT DETAILS
    pay_rows = payload.get("payment_details") or []
    if pay_rows:
        if y - 22 * mm < bottom:
            c.showPage()
            y = new_page()
            y = _draw_patient_header_block(c, payload, x0, y, w0)
            y -= 3 * mm

        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "PAYMENT DETAILS")
        y -= 4.0 * mm

        rows = []
        for p in pay_rows:
            rows.append([
                _safe(p.get("receipt_number")),
                _safe(p.get("mode")),
                _safe(p.get("date")),
                _money(p.get("amount")),
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Receipt No", 0.32), ("Paymode", 0.18), ("Date", 0.22),
                  ("Amount", 0.28)],
            rows=rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=new_page,
        )

        # ✅ padding + totals + words
        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.4)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Payment Received :")
        c.drawRightString(x0 + w0, y, payment_received)

        # ✅ NEW: Payment received words
        y -= 5.5 * mm
        c.setFont("Helvetica-Bold", 9.2)
        c.drawString(x0, y, "In Words :")
        c.setFont("Helvetica", 9.2)
        pr_lines = simpleSplit(pay_recv_words, "Helvetica", 9.2, w0 - 22 * mm)
        yy = y
        for ln in pr_lines[:2]:
            c.drawString(x_words, yy, ln)
            yy -= 4.6 * mm
        y = yy - 2.0 * mm

    # ADVANCE SUMMARY
    adv = payload.get("advance_summary_row") or {}
    if adv and (_dec(adv.get("total_advance")) != Decimal("0")
                or _dec(adv.get("consumed")) != Decimal("0")):
        if y - 18 * mm < bottom:
            c.showPage()
            y = new_page()
            y = _draw_patient_header_block(c, payload, x0, y, w0)
            y -= 3 * mm

        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "ADVANCE SUMMARY")
        y -= 4.0 * mm

        rows = [[
            _safe(adv.get("as_on")),
            _safe(adv.get("type") or "Advance Wallet"),
            _money(adv.get("total_advance")),  # ✅ gross
            _money(adv.get("consumed")),
            _money(adv.get("available")),
            _money(adv.get("advance_refunded")),
        ]]

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Date", 0.16), ("Type", 0.22), ("Total", 0.16),
                  ("Consumed", 0.16), ("Available", 0.16), ("Refund", 0.14)],
            rows=rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=new_page,
        )
        y -= 3.0 * mm

    # Balance amount + words
    if y - 18 * mm < bottom:
        c.showPage()
        y = new_page()

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(x0, y, x0 + w0, y)
    y -= 5.0 * mm

    c.setFont("Helvetica-Bold", 9.8)
    c.drawRightString(x0 + w0 - 42 * mm, y, "Balance Amount :")
    c.drawRightString(x0 + w0, y, balance)

    # ✅ Balance words
    y -= 5.5 * mm
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    bal_lines = simpleSplit(balance_words, "Helvetica", 9.2, w0 - 22 * mm)
    yy = y
    for ln in bal_lines[:3]:
        c.drawString(x_words, yy, ln)
        yy -= 4.6 * mm
    y = yy - 1.0 * mm

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------
# Optional WeasyPrint: Common Header only
# ---------------------------
def _render_common_header_pdf_weasy(payload: Dict[str, Any],
                                    branding: UiBranding) -> bytes:
    if not (brand_header_css and render_brand_header_html):
        raise RuntimeError("pdf_branding html helpers not available")

    try:
        from weasyprint import HTML  # type: ignore
    except Exception as e:
        raise RuntimeError(f"WeasyPrint not available: {e}")

    def esc(x: Any) -> str:
        import html as _html
        return _html.escape(_safe(x))

    bill = payload.get("bill", {})
    pat = payload.get("patient", {})
    enc_type = payload.get("encounter_type")
    enc = payload.get("encounter") or {}

    css = f"""
    @page{{ size:A4; margin:10mm 10mm 14mm 10mm; }}
    html,body{{ font-family: Arial, sans-serif; font-size: 11px; color:#111827; }}
    {brand_header_css()}
    .grid{{ display:flex; gap:12px; margin-top:4px; }}
    .col{{ flex:1; }}
    .row{{ margin:2px 0; }}
    .k{{ font-weight:700; display:inline-block; width:120px; }}
    .sep{{ border-top:1px solid #111827; margin:8px 0; }}
    """

    left = []
    left.append(
        f"<div class='row'><span class='k'>Patient Name</span>: {esc(pat.get('Patient Name'))}</div>"
    )
    left.append(
        f"<div class='row'><span class='k'>Patient ID</span>: {esc(pat.get('UHID'))}</div>"
    )
    left.append(
        f"<div class='row'><span class='k'>Age / Gender</span>: {esc(pat.get('Age'))} / {esc(pat.get('Gender'))}</div>"
    )
    left.append(
        f"<div class='row'><span class='k'>Phone</span>: {esc(pat.get('Phone'))}</div>"
    )
    left.append(
        f"<div class='row'><span class='k'>Address</span>: {esc(pat.get('Address'))}</div>"
    )

    right = []
    right.append(
        f"<div class='row'><span class='k'>Bill Number</span>: {esc(bill.get('Bill Number'))}</div>"
    )
    right.append(
        f"<div class='row'><span class='k'>Bill Date</span>: {esc(bill.get('Bill Date'))}</div>"
    )
    right.append(
        f"<div class='row'><span class='k'>Encounter Type</span>: {esc(enc_type)}</div>"
    )
    if enc_type == "OP":
        right.append(
            f"<div class='row'><span class='k'>Visit ID</span>: {esc(enc.get('Visit Id'))}</div>"
        )
        right.append(
            f"<div class='row'><span class='k'>Appointment On</span>: {esc(enc.get('Appointment On'))}</div>"
        )
        right.append(
            f"<div class='row'><span class='k'>Doctor</span>: {esc(enc.get('Doctor'))}</div>"
        )

    html_doc = f"""
    <html>
      <head><meta charset="utf-8"><style>{css}</style></head>
      <body>
        {render_brand_header_html(branding)}
        <div class="grid">
          <div class="col">{''.join(left)}</div>
          <div class="col">{''.join(right)}</div>
        </div>
        <div class="sep"></div>
      </body>
    </html>
    """

    return HTML(string=html_doc,
                base_url=str(getattr(settings, "STORAGE_DIR",
                                     "."))).write_pdf()


def _render_common_header_pdf(payload: Dict[str, Any],
                              branding: Optional[UiBranding], *,
                              engine: str) -> bytes:
    engine = (engine or "reportlab").strip().lower()
    if engine == "reportlab":
        return _render_common_header_pdf_reportlab(payload, branding)
    if engine in ("weasy", "auto"):
        if not branding:
            raise HTTPException(status_code=500,
                                detail="UiBranding not configured")
        try:
            return _render_common_header_pdf_weasy(payload, branding)
        except Exception:
            if engine == "weasy":
                raise
            return _render_common_header_pdf_reportlab(payload, branding)
    return _render_common_header_pdf_reportlab(payload, branding)


# ---------------------------
# Endpoints
# ---------------------------
@router.get("/common-header/data")
def billing_common_header_data(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view", "billing.cases.view", "billing.print"])
    case = _load_case(db, case_id)
    return _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)


@router.get("/common-header")
def billing_common_header_pdf(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        engine: str = Query("reportlab", pattern="^(reportlab|weasy|auto)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view", "billing.cases.view", "billing.print"])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)
    pdf_bytes = _render_common_header_pdf(payload, branding, engine=engine)

    filename = f"Billing_Header_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.get("/overview/data")
def billing_overview_data(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        include_draft_invoices: bool = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view", "billing.cases.view", "billing.print"])
    case = _load_case(db, case_id)
    return _build_overview_payload(
        db,
        case,
        doc_no=doc_no,
        doc_date=doc_date,
        include_draft_invoices=include_draft_invoices)


@router.get("/overview")
def billing_overview_pdf(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        include_draft_invoices: bool = Query(True),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view", "billing.cases.view", "billing.print"])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_overview_payload(
        db,
        case,
        doc_no=doc_no,
        doc_date=doc_date,
        include_draft_invoices=include_draft_invoices)
    pdf_bytes = _render_overview_pdf_reportlab(payload, branding)

    filename = f"Billing_Overview_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)
