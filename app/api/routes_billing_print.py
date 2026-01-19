# FILE: app/api/routes_billing_print.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Path as FPath
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload, joinedload
import json
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.pdfgen import canvas
from typing import Iterable, Union
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

# ✅ try load BillingAdvanceApplication safely (some installations may not have it)
try:
    from app.models.billing import BillingAdvanceApplication  # type: ignore
except Exception:
    BillingAdvanceApplication = None  # type: ignore

from app.models.ui_branding import UiBranding

# ✅ payer masters
from app.models.payer import Payer, Tpa, CreditPlan  # type: ignore
from sqlalchemy.inspection import inspect as sa_inspect

# Optional: Department lookup fallback
try:
    from app.models.department import Department  # type: ignore
except Exception:
    Department = None  # type: ignore

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

from app.services.pdfs.billing_invoice_export import build_invoice_pdf

# ✅ PDF merge (Overview + Invoices + Ledgers + Insurance => one PDF)
PdfReader = PdfWriter = None  # type: ignore
try:
    from pypdf import PdfReader, PdfWriter  # type: ignore
except Exception:
    try:
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore
    except Exception:
        PdfReader = PdfWriter = None  # type: ignore

logger = logging.getLogger(__name__)

# ✅ Insurance models (safe import)
try:
    from app.models.billing import BillingInsuranceCase, BillingPreauthRequest, BillingClaim  # type: ignore
except Exception:
    BillingInsuranceCase = None  # type: ignore
    BillingPreauthRequest = None  # type: ignore
    BillingClaim = None  # type: ignore

router = APIRouter(prefix="/billing/print", tags=["Billing Print"])

# ---------------------------
# Permissions (safe fallback)
# ---------------------------

from typing import Iterable, Union


def _perm_code(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, str):
        return x.strip()
    return (getattr(x, "code", None) or getattr(x, "name", None)
            or "").strip() or None


def _need_any(user: User, perms: Union[str, Iterable[str]]) -> None:
    # accept "billing.view" or ["billing.view", ...]
    if isinstance(perms, str):
        perms = [perms]
    else:
        perms = list(perms)

    if getattr(user, "is_admin", False):
        return

    # 1) preferred method if your User has it
    fn = getattr(user, "has_perm", None)
    if callable(fn):
        for p in perms:
            try:
                if fn(p):
                    return
            except Exception:
                pass

    # 2) Collect codes from user.permissions (if any)
    codes: set[str] = set()
    try:
        for item in (getattr(user, "permissions", None) or []):
            c = _perm_code(item)
            if c:
                codes.add(c)
    except Exception:
        pass

    # 3) ✅ Collect codes from roles -> role.permissions (THIS is what your /me/permissions uses)
    try:
        for role in (getattr(user, "roles", None) or []):
            for item in (getattr(role, "permissions", None) or []):
                c = _perm_code(item)
                if c:
                    codes.add(c)
    except Exception:
        pass

    if any(p in codes for p in perms):
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


def _up(v: Any) -> str:
    return str(v or "").strip().upper()


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


def _meta_pick(meta: Dict[str, Any],
               keys: list[str],
               default: str = "—") -> str:
    for k in keys:
        if k in meta:
            val = meta.get(k)
            if val not in (None, "", []):
                return str(val).strip()
    return default


def _safe(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).strip()
    return s if s else "—"


def _has_rel(model: Any, rel_name: str) -> bool:
    """True only if rel_name is a SQLAlchemy relationship on model."""
    try:
        return rel_name in sa_inspect(model).relationships
    except Exception:
        return False


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
    d = _dec(v).quantize(Decimal("0.01"))
    return f"{d}"


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
        key=lambda a: pref.get((getattr(a, "type", None) or "").lower(), 99),
    )
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


def _pick_attr(obj: Any, *names: str, default: str = "—") -> str:
    for n in names:
        try:
            v = getattr(obj, n, None)
            s = _safe(v)
            if s != "—":
                return s
        except Exception:
            pass
    return default


def _merge_pdf_bytes(parts: list[bytes]) -> bytes:
    """
    Merge multiple PDF byte blobs into one PDF (preserve page order).
    Requires pypdf (preferred) or PyPDF2.
    """
    if not parts:
        return b""

    if PdfReader is None or PdfWriter is None:
        raise HTTPException(
            status_code=500,
            detail="PDF merge dependency missing. Install: pip install pypdf",
        )

    writer = PdfWriter()
    for b in parts:
        if not b:
            continue
        reader = PdfReader(BytesIO(b))
        for page in reader.pages:
            writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _list_case_invoices(
    db: Session,
    case_id: int,
    *,
    include_draft_invoices: bool = True,
) -> list[BillingInvoice]:
    q = (db.query(BillingInvoice).options(selectinload(
        BillingInvoice.lines)).filter(
            BillingInvoice.billing_case_id == case_id,
            BillingInvoice.status != DocStatus.VOID,
        ).order_by(BillingInvoice.created_at.asc()))
    if not include_draft_invoices:
        q = q.filter(BillingInvoice.status != DocStatus.DRAFT)
    return q.all()


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
    # ✅ latest branding
    return db.query(UiBranding).order_by(UiBranding.id.desc()).first()


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

    q = db.query(Visit)

    # Apply loader options ONLY if these are relationships
    opts = []
    if _has_rel(Visit, "appointment"):
        opts.append(selectinload(Visit.appointment))
    if _has_rel(Visit, "doctor"):
        opts.append(selectinload(Visit.doctor))
    if _has_rel(Visit, "department"):
        opts.append(selectinload(Visit.department))

    if opts:
        q = q.options(*opts)

    v = q.filter(Visit.id == encounter_id).first()
    if not v:
        return out

    visit_no = getattr(v, "episode_id", None) or getattr(
        v, "op_no", None) or getattr(v, "visit_no", None)
    out["Visit Id"] = _safe(visit_no)

    # Appointment date/time
    appt = getattr(v, "appointment", None) if _has_rel(Visit,
                                                       "appointment") else None
    if appt is not None:
        appt_date = getattr(appt, "date", None)
        slot_start = getattr(appt, "slot_start", None)
        if appt_date and slot_start:
            out["Appointment On"] = f"{_fmt_date(appt_date)} {str(slot_start)[:5]}"
        elif appt_date:
            out["Appointment On"] = _fmt_date(appt_date)

    # Doctor (relationship OR id fallback)
    doc_obj = getattr(v, "doctor", None) if _has_rel(Visit, "doctor") else None
    if doc_obj is not None and hasattr(doc_obj, "name"):
        out["Doctor"] = _safe(getattr(doc_obj, "name", None))
    else:
        doc_id = getattr(v, "doctor_id", None) or getattr(
            v, "practitioner_user_id", None)
        if doc_id:
            doc = db.query(User).filter(User.id == int(doc_id)).first()
            if doc:
                out["Doctor"] = _safe(getattr(doc, "name", None))

    # Department (relationship OR id fallback)
    dept_obj = getattr(v, "department", None) if _has_rel(
        Visit, "department") else None
    if dept_obj is not None and hasattr(dept_obj, "name"):
        out["Department"] = _safe(getattr(dept_obj, "name", None))
    else:
        dept_id = getattr(v, "department_id", None) or getattr(
            v, "dept_id", None)
        if dept_id and Department is not None:
            d = db.query(Department).filter(
                Department.id == int(dept_id)).first()
            if d:
                out["Department"] = _safe(getattr(d, "name", None))

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

    q = db.query(IpdAdmission)

    opts = []

    # ✅ Bed → Room → Ward (apply only if they are relationships)
    if _has_rel(IpdAdmission, "current_bed"):
        bed_opt = selectinload(IpdAdmission.current_bed)

        if IpdBed is not None and _has_rel(IpdBed, "room"):
            room_opt = bed_opt.selectinload(IpdBed.room)

            if IpdRoom is not None and _has_rel(IpdRoom, "ward"):
                room_opt = room_opt.selectinload(IpdRoom.ward)

            bed_opt = room_opt

        opts.append(bed_opt)

    # ✅ Department relationship (only if relationship exists)
    if _has_rel(IpdAdmission, "department"):
        opts.append(selectinload(IpdAdmission.department))

    if opts:
        q = q.options(*opts)

    adm = q.filter(IpdAdmission.id == encounter_id).first()
    if not adm:
        return out

    out["IP Admission Number"] = _safe(
        getattr(adm, "admission_code", None)
        or getattr(adm, "display_code", None))
    out["Admitted On"] = _fmt_dt(getattr(adm, "admitted_at", None))
    out["Discharged On"] = _fmt_dt(getattr(adm, "discharge_at", None))

    # Doctor fallback
    practitioner_id = getattr(adm, "practitioner_user_id", None) or getattr(
        adm, "doctor_id", None)
    if practitioner_id:
        doc = db.query(User).filter(User.id == int(practitioner_id)).first()
        if doc:
            out["Admission Doctor"] = _safe(getattr(doc, "name", None))

    # Department name (relationship OR department_id fallback)
    dept_name = "—"
    if _has_rel(IpdAdmission, "department"):
        dept_obj = getattr(adm, "department", None)
        if dept_obj is not None and hasattr(dept_obj, "name"):
            dept_name = _safe(getattr(dept_obj, "name", None))

    if dept_name == "—":
        dept_id = getattr(adm, "department_id", None) or getattr(
            adm, "dept_id", None)
        if dept_id and Department is not None:
            d = db.query(Department).filter(
                Department.id == int(dept_id)).first()
            if d:
                dept_name = _safe(getattr(d, "name", None))

    out["Department"] = dept_name

    # Bed / Room / Ward (relationship chain)
    bed = getattr(adm, "current_bed", None) if _has_rel(
        IpdAdmission, "current_bed") else None
    if bed is not None:
        out["Bed"] = _safe(getattr(bed, "code", None))

        room = getattr(bed, "room", None) if (
            IpdBed is not None and _has_rel(IpdBed, "room")) else None
        if room is not None:
            out["Room"] = _safe(getattr(room, "number", None))

            ward = getattr(room, "ward", None) if (
                IpdRoom is not None and _has_rel(IpdRoom, "ward")) else None
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
# Branding header (ReportLab) – MEDIUM size + perfect alignment
# ---------------------------
def _bget(b: Any, *names: str) -> str:
    for n in names:
        try:
            v = getattr(b, n, None)
            if v not in (None, "", []):
                return str(v).strip()
        except Exception:
            pass
    return ""


def _read_logo_reader(branding: Any) -> Optional[ImageReader]:
    rel = (_bget(branding, "logo_path", "logo_file", "logo", "logo_rel_path")
           or "").strip()
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


def _draw_branding_header(c: canvas.Canvas, branding: Optional[UiBranding],
                          x: float, top_y: float, w: float) -> float:
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

    logo_h = 18 * mm
    gutter = 5 * mm

    logo_col = min(max(62 * mm, w * 0.36), 78 * mm)
    right_w = max(58 * mm, w - logo_col - gutter)

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
            _cap_lines(simpleSplit(addr, "Helvetica", 8.4, right_w), 2))
    if contact_line:
        meta_lines.extend(
            _cap_lines(simpleSplit(contact_line, "Helvetica", 8.4, right_w),
                       1))

    extra_bits = []
    if website != "—":
        extra_bits.append(f"{website}")
    if gstin != "—":
        extra_bits.append(f"GSTIN: {gstin}")
    if extra_bits and len(meta_lines) < 3:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(" | ".join(extra_bits), "Helvetica", 8.4, right_w),
                1))

    meta_lines = _cap_lines(meta_lines, 3)

    lines: list[tuple[str, str, float, Any]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", 12.0, INK))
    if tag != "—":
        lines.append((tag, "Helvetica", 8.6, MUTED))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", 8.4, MUTED))

    def lh(sz: float) -> float:
        return sz * 1.18

    text_h = sum(lh(sz) for _, _, sz, _ in lines) if lines else (10 * mm)
    header_h = max(logo_h, text_h) + (2 * mm)

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

    text_right_x = x + w
    center_y = top_y - header_h / 2
    cur_y = center_y + (text_h / 2)

    for txt, font, sz, col in lines:
        cur_y -= lh(sz)
        c.setFont(font, sz)
        c.setFillColor(col)
        c.drawRightString(text_right_x, cur_y, txt)

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
# Payload: Overview (Module-wise compact) + FIX payments
# ---------------------------
def _advance_consumed_from_applications(
        db: Session, case_id: int) -> tuple[Decimal, Optional[datetime]]:
    if BillingAdvanceApplication is None:
        return Decimal("0"), None
    try:
        q = db.query(BillingAdvanceApplication).filter(
            BillingAdvanceApplication.billing_case_id == case_id)
        rows = q.all()
        total = Decimal("0")
        last_dt: Optional[datetime] = None
        for r in rows:
            amt = _dec(getattr(r, "amount", 0))
            total += amt
            dt = getattr(r, "applied_at", None) or getattr(
                r, "created_at", None) or getattr(r, "entry_at", None)
            if isinstance(dt, datetime):
                last_dt = dt if (last_dt is None or dt > last_dt) else last_dt
        return total, last_dt
    except Exception:
        return Decimal("0"), None


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

    # keep your existing mapping tables exactly as-is (unchanged)
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
        "ADM", "ROOM", "DOC", "LAB", "BLOOD", "DIET", "PHM", "PHC", "PROC",
        "SCAN", "XRAY", "SURG", "MISC"
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

    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case.id).order_by(
            BillingPayment.received_at.asc()).all())

    receipts_total = Decimal("0")
    adv_adjusted_total = Decimal("0")
    refunds_total = Decimal("0")

    payment_rows: list[Dict[str, Any]] = []
    has_adv_row = False

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
            if _eq_enum(kind, PaymentKind.ADVANCE_ADJUSTMENT):
                adv_adjusted_total += amt
                has_adv_row = True
                payment_rows.append({
                    "receipt_number":
                    _safe(getattr(p, "receipt_number", None)) if _safe(
                        getattr(p, "receipt_number", None)) != "—" else
                    "ADV-ADJ",
                    "mode":
                    "ADVANCE",
                    "date":
                    _fmt_date(getattr(p, "received_at", None)),
                    "amount":
                    float(amt),
                })
                continue

            receipts_total += amt
            receipt_no = _safe(getattr(p, "receipt_number", None))
            if receipt_no == "—":
                receipt_no = _safe(getattr(p, "txn_ref", None))
            payment_rows.append({
                "receipt_number":
                receipt_no if receipt_no != "—" else "RCPT",
                "mode":
                _safe(_val(getattr(p, "mode", None))),
                "date":
                _fmt_date(getattr(p, "received_at", None)),
                "amount":
                float(amt),
            })

    adv_app_total, adv_app_last_dt = _advance_consumed_from_applications(
        db, case.id)
    consumed_advance = max(adv_adjusted_total, adv_app_total)

    if consumed_advance > 0 and not has_adv_row and adv_adjusted_total == 0:
        payment_rows.append({
            "receipt_number":
            "ADV-ADJ",
            "mode":
            "ADVANCE",
            "date":
            _fmt_date(adv_app_last_dt) if adv_app_last_dt else "—",
            "amount":
            float(consumed_advance),
        })

    payment_received_total = receipts_total + consumed_advance
    effective_paid = payment_received_total - refunds_total
    balance = total_bill - effective_paid

    adv_rows = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case.id).order_by(
            BillingAdvance.entry_at.asc()).all())

    total_adv_in = Decimal("0")
    total_adv_refund = Decimal("0")
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
    available = net_advance - consumed_advance
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
            "advance_consumed": float(consumed_advance),
            "total_bill_words": _amount_in_words_inr(total_bill),
            "payment_received_words":
            _amount_in_words_inr(payment_received_total),
            "balance_words": _amount_in_words_inr(balance),
        },
        "payment_details": payment_rows,
        "advance_summary_row": {
            "as_on": _fmt_date(last_adv_dt) if last_adv_dt else "—",
            "type": "Advance Wallet",
            "total_advance": float(total_adv_in),
            "net_advance": float(net_advance),
            "consumed": float(consumed_advance),
            "available": float(available),
            "advance_refunded": float(total_adv_refund),
        },
    }
    return payload


def _build_payments_ledger_payload(db: Session,
                                   case: BillingCase) -> Dict[str, Any]:
    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case.id).order_by(
            BillingPayment.received_at.asc()).all())

    rows: list[dict[str, Any]] = []
    total_in = Decimal("0")
    total_out = Decimal("0")

    for p in pays:
        status = getattr(p, "status", None)
        if status is not None and not _eq_enum(status, ReceiptStatus.ACTIVE):
            # govt print: show only ACTIVE receipts
            continue

        direction = getattr(p, "direction", None)
        kind = getattr(p, "kind", None)
        amt = _dec(getattr(p, "amount", 0))

        is_out = _eq_enum(direction, PaymentDirection.OUT) or _eq_enum(
            kind, PaymentKind.REFUND)
        if is_out:
            total_out += amt
        else:
            total_in += amt

        receipt_no = _safe(getattr(p, "receipt_number", None))
        if receipt_no == "—":
            receipt_no = _safe(getattr(p, "txn_ref", None))
        if receipt_no == "—":
            receipt_no = "RCPT"

        rows.append({
            "receipt_number": receipt_no,
            "mode": _safe(_val(getattr(p, "mode", None))),
            "kind": _safe(_val(kind)),
            "direction": "OUT" if is_out else "IN",
            "date": _fmt_dt(getattr(p, "received_at", None)),
            "amount": float(amt),
        })

    return {
        "rows": rows,
        "totals": {
            "received": float(total_in),
            "refunds": float(total_out),
            "net": float(total_in - total_out),
            "received_words": _amount_in_words_inr(total_in),
            "net_words": _amount_in_words_inr(total_in - total_out),
        },
    }


def _build_advance_ledger_payload(db: Session,
                                  case: BillingCase) -> Dict[str, Any]:
    advs = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case.id).order_by(
            BillingAdvance.entry_at.asc()).all())

    rows: list[dict[str, Any]] = []
    net = Decimal("0")

    for a in advs:
        et = getattr(a, "entry_type", None)
        amt = _dec(getattr(a, "amount", 0))

        # Sign logic (govt ledger style)
        # Advance = +, Refund = -, Adjustment = +/- (keep as stored)
        sign_amt = amt
        if _eq_enum(et, AdvanceType.REFUND):
            sign_amt = -abs(amt)
        elif _eq_enum(et, AdvanceType.ADVANCE):
            sign_amt = abs(amt)

        net += sign_amt

        ref = _pick_attr(
            a,
            "advance_number",
            "voucher_number",
            "ref_number",
            "reference_number",
            "receipt_number",
            "txn_ref",
            default="—",
        )

        note = _pick_attr(a, "notes", "note", "remark", "remarks", default="—")

        rows.append({
            "date":
            _fmt_dt(
                getattr(a, "entry_at", None)
                or getattr(a, "created_at", None)),
            "type":
            _safe(_val(et)),
            "reference":
            ref,
            "note":
            note,
            "amount":
            float(sign_amt),
        })

    return {
        "rows": rows,
        "totals": {
            "net": float(net),
            "net_words": _amount_in_words_inr(net),
        },
    }


def _try_load_insurance_payload(db: Session,
                                case: BillingCase) -> Dict[str, Any]:
    """
    Optional: Insurance tables may not exist in every installation.
    We'll try safely and return empty if missing.
    """
    out: Dict[str, Any] = {
        "insurance_case": None,
        "preauths": [],
        "claims": []
    }

    BillingInsuranceCase = BillingPreauthRequest = BillingClaim = None  # type: ignore
    try:
        from app.models.billing import BillingInsuranceCase, BillingPreauthRequest, BillingClaim  # type: ignore
    except Exception:
        return out

    try:
        ic = (db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == case.id).order_by(
                BillingInsuranceCase.id.desc()).first())
        if ic:
            out["insurance_case"] = {
                "payer":
                _safe(
                    getattr(ic, "payer_name", None)
                    or getattr(ic, "payer", None)),
                "tpa":
                _safe(
                    getattr(ic, "tpa_name", None) or getattr(ic, "tpa", None)),
                "policy_no":
                _safe(
                    getattr(ic, "policy_no", None)
                    or getattr(ic, "policy_number", None)),
                "member_id":
                _safe(getattr(ic, "member_id", None)),
                "uhid":
                _safe(getattr(case.patient, "uhid", None)) if getattr(
                    case, "patient", None) else "—",
                "status":
                _safe(_val(getattr(ic, "status", None))),
                "approved_limit":
                _money(getattr(ic, "approved_limit", None)),
            }

            preauths = (db.query(BillingPreauthRequest).filter(
                BillingPreauthRequest.billing_case_id == case.id).order_by(
                    BillingPreauthRequest.created_at.asc()).all())
            for p in preauths:
                out["preauths"].append({
                    "preauth_no":
                    _safe(
                        getattr(p, "preauth_number", None)
                        or getattr(p, "request_no", None)),
                    "status":
                    _safe(_val(getattr(p, "status", None))),
                    "requested":
                    _money(getattr(p, "requested_amount", None)),
                    "approved":
                    _money(getattr(p, "approved_amount", None)),
                    "date":
                    _fmt_dt(getattr(p, "created_at", None)),
                })

            claims = (db.query(BillingClaim).filter(
                BillingClaim.billing_case_id == case.id).order_by(
                    BillingClaim.created_at.asc()).all())
            for cl in claims:
                out["claims"].append({
                    "claim_no":
                    _safe(
                        getattr(cl, "claim_number", None)
                        or getattr(cl, "claim_no", None)),
                    "status":
                    _safe(_val(getattr(cl, "status", None))),
                    "claimed":
                    _money(getattr(cl, "claimed_amount", None)),
                    "approved":
                    _money(getattr(cl, "approved_amount", None)),
                    "date":
                    _fmt_dt(getattr(cl, "created_at", None)),
                })

        return out
    except Exception:
        return out


# ---------------------------
# ReportLab: Patient header (clean alignment)
# ---------------------------
def _draw_lv_column(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    col_w: float,
    rows: list[tuple[str, str]],
    label_w: float,
    size: float = 8.8,
    leading: float = 10.2,
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
        c.drawString(value_x, y, lines[0][:200])

        for ln in lines[1:]:
            y -= leading
            c.drawString(value_x, y, ln[:200])

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

    label_w = 30 * mm
    y1 = _draw_lv_column(c,
                         x=x,
                         y=y_top,
                         col_w=left_w - 2 * mm,
                         rows=left_rows,
                         label_w=label_w)
    y2 = _draw_lv_column(c,
                         x=x + left_w + 6 * mm,
                         y=y_top,
                         col_w=right_w - 6 * mm,
                         rows=right_rows,
                         label_w=label_w)

    y_end = min(y1, y2)

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
            c.drawString(xx + 2.0 * mm, cur_y - row_h + 2.0 * mm, txt[:200])
            xx += cw

        cur_y -= row_h

    return cur_y


from reportlab.pdfbase.pdfmetrics import stringWidth


class _NumberedCanvas(canvas.Canvas):
    """
    Page X of Y + Printed Date/Time + Printed By (Govt form footer)
    """

    def __init__(self,
                 *args,
                 printed_at: str = "",
                 printed_by: str = "",
                 **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self._printed_at = printed_at
        self._printed_by = printed_by

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        self._saved_page_states.append(dict(self.__dict__))
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def _draw_footer(self, total_pages: int):
        W, H = self._pagesize
        M = 10 * mm
        y = 8.5 * mm

        self.setFont("Helvetica", 8.2)
        self.setFillColor(colors.black)

        # left: printed date/time
        if self._printed_at:
            self.drawString(M, y, f"Printed Date / Time : {self._printed_at}")

        # center: printed by
        if self._printed_by:
            mid = W / 2
            txt = f"Printed By : {self._printed_by}"
            self.drawCentredString(mid, y, txt)

        # right: page x of y
        self.drawRightString(W - M, y,
                             f"Page {self.getPageNumber()} of {total_pages}")


def _bill_kind_title(case: BillingCase) -> tuple[str, str]:
    """
    Titles like sample:
    - INPATIENT SUMMARY BILL - CREDIT
    - INPATIENT DETAIL BILL OF SUPPLY - CREDIT
    """
    et = _safe(
        _val(case.encounter_type) if case.encounter_type else None).upper()
    pm = case.payer_mode
    is_credit = (pm is not None and pm != PayerMode.SELF)

    if et == "IP":
        base = "INPATIENT"
    elif et == "OP":
        base = "OUTPATIENT"
    else:
        base = "PATIENT"

    kind = "CREDIT" if is_credit else "CASH"
    return base, kind


def _collect_detail_rows(
        invoices: list[BillingInvoice]) -> dict[str, list[dict[str, Any]]]:
    MODULE_LABELS: Dict[str, str] = {
        "ADM": "ADMISSIONS",
        "ADMISSION": "ADMISSIONS",
        "ROOM": "BED CHARGES",
        "BED": "BED CHARGES",
        "WARD": "BED CHARGES",
        "BLOOD": "BLOOD BANK",
        "BB": "BLOOD BANK",
        "LAB": "CLINICAL LAB CHARGES",
        "LIS": "CLINICAL LAB CHARGES",
        "DIET": "DIETARY CHARGES",
        "DOC": "DOCTOR FEES",
        "DOCTOR": "DOCTOR FEES",
        "PHM": "PHARMACY CHARGES",
        "PHARM": "PHARMACY CHARGES",
        "PHARMACY": "PHARMACY CHARGES",
        "PHC": "CONSUMABLES & DISPOSABLES",
        "CONSUMABLES": "CONSUMABLES & DISPOSABLES",
        "PROC": "PROCEDURES",
        "PROCEDURE": "PROCEDURES",
        "SCAN": "SCAN CHARGES",
        "RIS": "SCAN CHARGES",
        "RAD": "SCAN CHARGES",
        "XRAY": "X RAY CHARGES",
        "X-RAY": "X RAY CHARGES",
        "SURG": "SURGERY",
        "SURGERY": "SURGERY",
        "OT": "SURGERY",
        "MISC": "MISCELLANEOUS",
        "OTHER": "MISCELLANEOUS",
    }

    def label_for_module(mod: str) -> str:
        m = (mod or "MISC").strip().upper()
        return MODULE_LABELS.get(m, "MISCELLANEOUS")

    out: dict[str, list[dict[str, Any]]] = {}

    for inv in invoices:
        if getattr(inv, "status", None) == DocStatus.VOID:
            continue

        mod = (getattr(inv, "module", None) or "MISC").strip().upper()
        grp = label_for_module(mod)

        for ln in list(getattr(inv, "lines", []) or []):
            meta = _meta(getattr(ln, "meta_json", None))

            # skip deleted/void lines ONLY by meta flags (safe for your model)
            if meta.get("is_void") is True or meta.get("is_deleted") is True:
                continue

            dt = (getattr(ln, "service_date", None)
                  or getattr(inv, "service_date", None)
                  or getattr(inv, "created_at", None))

            out.setdefault(grp, []).append({
                "desc":
                _safe(getattr(ln, "description", None)),
                "date":
                _fmt_date(dt),
                "qty":
                _safe(getattr(ln, "qty", None)),
                "amt":
                _money(getattr(ln, "net_amount", 0)),
            })

    return out


def _collect_pharmacy_split_rows(
        invoices: list[BillingInvoice]) -> list[list[str]]:
    rows: list[list[str]] = []
    for inv in invoices:
        mod = (getattr(inv, "module", "") or "").upper()
        is_ph = mod in ("PHM", "PHARM", "PHARMACY")
        if not is_ph:
            continue

        bill_no = _safe(getattr(inv, "invoice_number", None))
        bill_date = _fmt_date(getattr(inv, "created_at", None))

        for ln in list(getattr(inv, "lines", []) or []):
            meta = _meta(getattr(ln, "meta_json", None))
            batch_no = _meta_pick(meta, [
                "batch_no", "batchNo", "batch_number", "batchNumber", "batch"
            ], "—")
            exp = _meta_pick(
                meta,
                ["expiry_date", "expiryDate", "expiry", "exp_date", "expDate"],
                "—")

            rows.append([
                bill_no,
                bill_date,
                _safe(getattr(ln, "description", None)),
                batch_no,
                exp,
                _safe(getattr(ln, "qty", None)),
                _money(getattr(ln, "net_amount", 0)),
            ])
    return rows


def _load_insurance_block(db: Session, case: BillingCase,
                          invoices: list[BillingInvoice]) -> dict[str, Any]:
    """
    Insurance block like sample:
    Company | Approval Number | Amount | Total
    """
    payer_company = "—"
    approval_no = "—"

    payer = _payer_block(db, case) or {}
    if _safe(payer.get("Payer")) != "—":
        payer_company = _safe(payer.get("Payer"))

    # try insurance tables
    if BillingInsuranceCase is not None:
        try:
            ins = db.query(BillingInsuranceCase).filter(
                BillingInsuranceCase.billing_case_id == case.id).order_by(
                    BillingInsuranceCase.id.desc()).first()
            if ins is not None:
                # best-effort fields
                approval_no = _safe(
                    getattr(ins, "approval_number", None)
                    or getattr(ins, "preauth_number", None)
                    or getattr(ins, "policy_number", None))
                payer_company = _safe(getattr(
                    ins, "insurer_name", None)) if _safe(
                        getattr(ins, "insurer_name",
                                None)) != "—" else payer_company
        except Exception:
            pass

    # compute insurer payable from lines
    insurer_sum = Decimal("0")
    for inv in invoices:
        for ln in list(getattr(inv, "lines", []) or []):
            insurer_sum += _dec(getattr(ln, "insurer_pay_amount", 0))
            if insurer_sum == 0:
                insurer_sum += _dec(getattr(ln, "approved_amount", 0))

    return {
        "company": payer_company,
        "approval_no": approval_no,
        "amount": _money(insurer_sum),
    }


def _advance_ledger_rows(db: Session, case_id: int,
                         overview_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Deposit Summary table like sample:
    Deposit Date | Reference No | Actual Amt | Consumed Amt | Refund Amt | Balance Amt
    """
    adv_rows = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case_id).order_by(
            BillingAdvance.entry_at.asc()).all())

    total_in = Decimal("0")
    total_ref = Decimal("0")

    # consumed from overview totals
    consumed = _dec((overview_payload.get("totals")
                     or {}).get("advance_consumed", 0))
    available = _dec(((overview_payload.get("advance_summary_row")
                       or {}).get("available", 0)))

    rows: list[list[str]] = []
    for a in adv_rows:
        dt = _fmt_dt(getattr(a, "entry_at", None))
        ref = _safe(
            getattr(a, "reference_no", None) or getattr(a, "ref_no", None)
            or getattr(a, "receipt_number", None)
            or getattr(a, "txn_ref", None) or getattr(a, "id", None))
        et = getattr(a, "entry_type", None)
        amt = _dec(getattr(a, "amount", 0))

        if _eq_enum(et, AdvanceType.ADVANCE):
            total_in += amt
            rows.append([dt, ref, _money(amt), "0.00", "0.00", "—"])
        elif _eq_enum(et, AdvanceType.REFUND):
            total_ref += amt
            rows.append([dt, ref, "0.00", "0.00", _money(amt), "—"])
        else:
            # adjustment (rare)
            rows.append([dt, ref, _money(amt), "0.00", "0.00", "—"])

    return {
        "rows": rows,
        "total_in": _money(total_in),
        "total_refund": _money(total_ref),
        "consumed": _money(consumed),
        "available": _money(available),
    }


def _render_full_history_pdf_reportlab(
    *,
    db: Session,
    case: BillingCase,
    invoices: list[BillingInvoice],
    branding: Optional[UiBranding],
    overview_payload: dict[str, Any],
    printed_by: str = "",
) -> bytes:
    """
    ✅ One single PDF (Govt form style) -> Summary + Detail lines + Payments + Insurance + Deposit summary + Pharmacy split up.
    No PDF merge, no external dependency, no 500.
    """
    buf = BytesIO()

    printed_at = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    c = _NumberedCanvas(buf,
                        pagesize=A4,
                        printed_at=printed_at,
                        printed_by=printed_by)
    W, H = A4

    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    bottom = 14 * mm

    base, kind = _bill_kind_title(case)
    bill_no = _safe(getattr(case, "case_number", None))
    bill_date = _fmt_dt(getattr(case, "created_at", None))

    header_payload = _build_header_payload(db,
                                           case,
                                           doc_no=bill_no,
                                           doc_date=None)

    # ---------- page header ----------
    def new_page(title: str) -> float:
        y = H - M
        y = _draw_branding_header(c, branding, x0, y, w0)
        y -= 2 * mm

        # title (govt style)
        c.setFont("Helvetica-Bold", 10.5)
        c.setFillColor(colors.black)
        c.drawCentredString(x0 + w0 / 2, y, title)
        y -= 5.0 * mm

        # patient block
        y = _draw_patient_header_block(c, header_payload, x0, y, w0)
        y -= 3.0 * mm
        return y

    # ---------- SUMMARY PAGE (like sample page 1) ----------
    title1 = f"{base} SUMMARY BILL - {kind}"
    y = new_page(title1)

    # Particulars summary (module totals)
    modules = (overview_payload.get("modules") or [])
    sum_rows = [[_safe(m.get("label")),
                 _money(m.get("total"))] for m in modules] or [["—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[("Particulars", 0.76), ("Total Amount", 0.24)],
        rows=sum_rows,
        row_h=7 * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page(title1),
    )
    y -= 3.0 * mm

    # Totals block (match sample: Exempted/Taxable/GST/RoundOff/Total)
    totals = overview_payload.get("totals") or {}
    total_bill = _dec(totals.get("total_bill", 0))
    taxable = _dec(totals.get("taxable_value", 0))
    gst = _dec(totals.get("gst", 0))
    round_off = _dec(totals.get("round_off", 0))
    total_amt = total_bill

    pre_round = total_amt - round_off  # matches sample math
    exempted = pre_round if (taxable == 0 and gst == 0) else Decimal("0")

    # Right aligned totals (govt style)
    def draw_right_total(label: str, val: str, yy: float, bold=False) -> float:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.2)
        c.drawRightString(x0 + w0 - 42 * mm, yy, f"{label} :")
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.2)
        c.drawRightString(x0 + w0, yy, val)
        return yy - 4.8 * mm

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.9)
    c.line(x0, y, x0 + w0, y)
    y -= 4.0 * mm

    y = draw_right_total("Exempted Value", _money(exempted), y)
    y = draw_right_total("Taxable Value", _money(taxable), y)
    y = draw_right_total("GST", _money(gst), y)
    y = draw_right_total("Round Off", _money(round_off), y)
    c.setLineWidth(1.1)
    c.line(x0 + w0 - 70 * mm, y + 2.0 * mm, x0 + w0, y + 2.0 * mm)
    y = draw_right_total("Total Bill Amount", _money(total_amt), y, bold=True)

    # PAYMENT DETAILS (like sample)
    pay_rows = overview_payload.get("payment_details") or []
    if pay_rows:
        if y - 28 * mm < bottom:
            c.showPage()
            y = new_page(title1)

        y -= 3.5 * mm
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "PAYMENT DETAILS")
        y -= 4.0 * mm

        pr = []
        total_pay = Decimal("0")
        for p in pay_rows:
            amt = _dec(p.get("amount", 0))
            total_pay += amt
            pr.append([
                _safe(p.get("receipt_number")),
                _safe(p.get("mode")),
                _safe(p.get("date")),
                _money(amt),
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Receipt No", 0.30), ("Paymode", 0.18), ("Date", 0.22),
                  ("Amount", 0.30)],
            rows=pr,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title1),
        )
        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.2)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Payment Received :")
        c.drawRightString(x0 + w0, y, _money(total_pay))

    # INSURANCE DETAILS (like sample)
    ins = _load_insurance_block(db, case, invoices)
    if _safe(ins.get("company")) != "—" and _safe(ins.get("amount")) != "0.00":
        if y - 22 * mm < bottom:
            c.showPage()
            y = new_page(title1)

        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "INSURANCE DETAILS")
        y -= 4.0 * mm

        ins_rows = [[
            _safe(ins.get("company")),
            _safe(ins.get("approval_no")),
            _safe(ins.get("amount"))
        ]]
        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Company", 0.56), ("Approval Number", 0.26),
                  ("Amount", 0.18)],
            rows=ins_rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title1),
        )

        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.2)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Total :")
        c.drawRightString(x0 + w0, y, _safe(ins.get("amount")))

    # ---------- DETAIL BILL (like sample pages 2..n) ----------
    c.showPage()
    title2 = f"{base} DETAIL BILL OF SUPPLY - {kind}"
    y = new_page(title2)

    # table header for details
    col_part = 0.62
    col_date = 0.14
    col_qty = 0.10
    col_amt = 0.14

    def draw_detail_header(yy: float) -> float:
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.9)
        h = 7 * mm
        c.rect(x0, yy - h, w0, h, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 9.2)
        c.drawString(x0 + 2 * mm, yy - h + 2.2 * mm, "Particulars")
        c.drawString(x0 + w0 * col_part + 2 * mm, yy - h + 2.2 * mm, "Date")
        c.drawRightString(x0 + w0 * (col_part + col_date + col_qty) - 2 * mm,
                          yy - h + 2.2 * mm, "Quantity")
        c.drawRightString(x0 + w0 - 2 * mm, yy - h + 2.2 * mm, "Total Amount")

        # vertical lines
        vx1 = x0 + w0 * col_part
        vx2 = x0 + w0 * (col_part + col_date)
        vx3 = x0 + w0 * (col_part + col_date + col_qty)
        c.setLineWidth(0.7)
        c.line(vx1, yy - h, vx1, yy)
        c.line(vx2, yy - h, vx2, yy)
        c.line(vx3, yy - h, vx3, yy)
        return yy - h

    def ensure_space(yy: float, need: float) -> float:
        if yy - need < bottom:
            c.showPage()
            yy = new_page(title2)
            yy = draw_detail_header(yy)
        return yy

    y = draw_detail_header(y)

    grouped = _collect_detail_rows(invoices)

    # order groups like sample (common hospital order)
    GROUP_ORDER = [
        "ADMISSIONS",
        "BED CHARGES",
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

    def group_key(g: str) -> int:
        try:
            return GROUP_ORDER.index(g)
        except Exception:
            return 999

    for grp in sorted(grouped.keys(), key=group_key):
        items = grouped[grp]
        if not items:
            continue

        # group header
        y = ensure_space(y, 10 * mm)
        y -= 2.8 * mm
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(x0 + 1.0 * mm, y, grp)
        y -= 2.0 * mm
        c.setLineWidth(0.6)
        c.line(x0, y, x0 + 55 * mm, y)  # underline left (like sample)
        y -= 2.5 * mm

        # rows
        row_h = 6.0 * mm
        for r in items:
            y = ensure_space(y, row_h + 2 * mm)

            # row border (thin)
            c.setStrokeColor(colors.black)
            c.setLineWidth(0.5)
            c.rect(x0, y - row_h, w0, row_h, stroke=1, fill=0)

            # verticals
            vx1 = x0 + w0 * col_part
            vx2 = x0 + w0 * (col_part + col_date)
            vx3 = x0 + w0 * (col_part + col_date + col_qty)
            c.line(vx1, y - row_h, vx1, y)
            c.line(vx2, y - row_h, vx2, y)
            c.line(vx3, y - row_h, vx3, y)

            c.setFont("Helvetica", 9.0)

            # particulars (clip if too long)
            desc = r["desc"] or "—"
            max_w = w0 * col_part - 4 * mm
            while stringWidth(desc, "Helvetica",
                              9.0) > max_w and len(desc) > 4:
                desc = desc[:-1]
            c.drawString(x0 + 2 * mm, y - row_h + 2.0 * mm, desc)

            # date
            c.drawString(vx1 + 2 * mm, y - row_h + 2.0 * mm, r["date"])

            # qty (right)
            c.drawRightString(vx3 - 2 * mm, y - row_h + 2.0 * mm, r["qty"])

            # amt (right)
            c.drawRightString(x0 + w0 - 2 * mm, y - row_h + 2.0 * mm, r["amt"])

            y -= row_h

        y -= 2.0 * mm

    # ---------- FINAL PAGE: Deposit + Bill Abstract (like sample last page) ----------
    c.showPage()
    title3 = f"{base} BILL ABSTRACT - {kind}"
    y = new_page(title3)

    # Deposit Summary
    adv = _advance_ledger_rows(db, case.id, overview_payload)
    rows = adv.get("rows") or []
    if rows:
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "DEPOSIT SUMMARY")
        y -= 4.0 * mm

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[
                ("Deposit Date", 0.22),
                ("Reference No", 0.22),
                ("Actual Amt", 0.14),
                ("Consumed Amt", 0.14),
                ("Refund Amt", 0.14),
                ("Balance Amt", 0.14),
            ],
            rows=rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title3),
        )
        y -= 4.0 * mm

        # totals line
        c.setFont("Helvetica-Bold", 9.0)
        c.drawRightString(
            x0 + w0, y,
            f"Total Advance : {adv.get('total_in')}    Consumed : {adv.get('consumed')}    Available : {adv.get('available')}"
        )
        y -= 6.0 * mm

    # Bill Abstract block (like sample bottom-right)
    effective_paid = _dec((overview_payload.get("totals")
                           or {}).get("effective_paid", 0))
    balance = _dec((overview_payload.get("totals") or {}).get("balance", 0))
    insurer_amt = _dec((_load_insurance_block(db, case, invoices)
                        or {}).get("amount", 0))

    if y - 40 * mm < bottom:
        c.showPage()
        y = new_page(title3)

    # left signature box
    box_w = w0 * 0.52
    box_h = 26 * mm
    c.setLineWidth(0.8)
    c.rect(x0, y - box_h, box_w, box_h, stroke=1, fill=0)
    c.setFont("Helvetica", 9.0)
    c.drawString(x0 + 3 * mm, y - 6 * mm, "Patient / Attender signature")
    c.drawString(x0 + 3 * mm, y - 12 * mm, "Name & Relationship")
    c.drawString(x0 + 3 * mm, y - 18 * mm, "Contact Number")

    # right abstract box
    rx = x0 + box_w + 8 * mm
    rw = w0 - (box_w + 8 * mm)
    c.rect(rx, y - box_h, rw, box_h, stroke=1, fill=0)

    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(rx + 3 * mm, y - 6 * mm, "Bill Abstract :")
    c.setFont("Helvetica", 9.0)
    c.drawRightString(rx + rw - 3 * mm, y - 6 * mm, f"{_money(total_amt)}")

    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(rx + 3 * mm, y - 12 * mm, "Less Payment Received :")
    c.drawRightString(rx + rw - 3 * mm, y - 12 * mm, _money(effective_paid))

    c.drawString(rx + 3 * mm, y - 18 * mm, "Balance Amount :")
    c.drawRightString(rx + rw - 3 * mm, y - 18 * mm, _money(balance))

    # Insurance net payable line (if credit)
    if insurer_amt > 0:
        c.drawString(rx + 3 * mm, y - 24 * mm,
                     "Net Payable by Insurance Company :")
        c.drawRightString(rx + rw - 3 * mm, y - 24 * mm, _money(insurer_amt))

    y -= (box_h + 10 * mm)

    # Balance in words (like sample)
    bal_words = _amount_in_words_inr(balance)
    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(x0, y, "Balance Amount in Words :")
    c.setFont("Helvetica", 9.0)
    c.drawString(x0 + 46 * mm, y, bal_words[:170])
    y -= 8 * mm

    # ---------- Pharmacy Split Up (optional, like sample separate report) ----------
    ph_rows = _collect_pharmacy_split_rows(invoices)
    if ph_rows:
        c.showPage()
        title4 = "PHARMACY SPLIT UP REPORT"
        y = new_page(title4)

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[
                ("Bill No", 0.16),
                ("Bill Date", 0.12),
                ("Item Name", 0.36),
                ("Batch No", 0.12),
                ("Expiry Date", 0.10),
                ("Qty", 0.06),
                ("Item Amount", 0.08),
            ],
            rows=ph_rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title4),
        )

    c.save()
    return buf.getvalue()


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

    y -= 3.5 * mm
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    bill_word_lines = simpleSplit(total_bill_words, "Helvetica", 9.2,
                                  w0 - 22 * mm)
    x_words = x0 + 18 * mm
    yy = y
    for ln in bill_word_lines[:3]:
        c.drawString(x_words, yy, ln)
        yy -= 4.6 * mm
    y = yy - 2.0 * mm

    pay_rows = payload.get("payment_details") or []
    if pay_rows:
        if y - 22 * mm < bottom:
            c.showPage()
            y = new_page()
            y = _draw_patient_header_block(c, payload, x0, y, w0)
            y -= 4 * mm

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

        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.4)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Total Payment Received :")
        c.drawRightString(x0 + w0, y, payment_received)

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

    adv = payload.get("advance_summary_row") or {}
    if adv and (_dec(adv.get("total_advance")) != Decimal("0")
                or _dec(adv.get("consumed")) != Decimal("0")):
        if y - 18 * mm < bottom:
            c.showPage()
            y = new_page()
            y = _draw_patient_header_block(c, payload, x0, y, w0)
            y -= 4 * mm

        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "ADVANCE SUMMARY")
        y -= 4.0 * mm

        rows = [[
            _safe(adv.get("as_on")),
            _safe(adv.get("type") or "Advance Wallet"),
            _money(adv.get("total_advance")),
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

    if y - 18 * mm < bottom:
        c.showPage()
        y = new_page()

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(x0, y, x0 + w0, y)
    y -= 5.0 * mm

    c.setFont("Helvetica-Bold", 9.8)
    c.drawRightString(x0 + w0 - 42 * mm, y, "Total Balance Amount :")
    c.drawRightString(x0 + w0, y, balance)

    y -= 5.5 * mm
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    bal_lines = simpleSplit(balance_words, "Helvetica", 9.2, w0 - 22 * mm)
    yy = y
    for ln in bal_lines[:3]:
        c.drawString(x_words, yy, ln)
        yy -= 4.6 * mm

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_payments_ledger_pdf_reportlab(
    payload: Dict[str, Any],
    branding: Optional[UiBranding],
    payments_payload: Dict[str, Any],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    bottom = 14 * mm

    def new_page(title: str) -> float:
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0)
        c.drawCentredString(x0 + w0 / 2, y0, title)
        y0 -= 5 * mm
        return y0

    y = new_page("PAYMENT RECEIPTS LEDGER")

    rows = []
    for r in payments_payload.get("rows") or []:
        rows.append([
            _safe(r.get("receipt_number")),
            _safe(r.get("mode")),
            _safe(r.get("kind")),
            _safe(r.get("direction")),
            _safe(r.get("date")),
            _money(r.get("amount")),
        ])

    if not rows:
        rows = [["—", "—", "—", "—", "—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[
            ("Receipt No", 0.18),
            ("Mode", 0.12),
            ("Kind", 0.18),
            ("Dir", 0.08),
            ("Date/Time", 0.26),
            ("Amount", 0.18),
        ],
        rows=rows,
        row_h=7 * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page("PAYMENT RECEIPTS LEDGER (Cont.)"),
    )

    y -= 4 * mm
    if y - 18 * mm < bottom:
        c.showPage()
        y = new_page("PAYMENT RECEIPTS LEDGER (Summary)")

    t = payments_payload.get("totals") or {}
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(x0, y, x0 + w0, y)
    y -= 5 * mm

    def line(label: str, val: str, bold: bool = False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4)
        c.drawRightString(x0 + w0 - 42 * mm, y, label)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4)
        c.drawRightString(x0 + w0, y, val)
        y -= 5 * mm

    line("Total Received :", _money(t.get("received")), bold=True)
    line("Total Refunds :", _money(t.get("refunds")))
    line("Net Received :", _money(t.get("net")), bold=True)

    y -= 2 * mm
    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    words = _safe(t.get("net_words") or "")
    for ln in simpleSplit(words, "Helvetica", 9.2, w0 - 22 * mm)[:3]:
        c.drawString(x0 + 18 * mm, y, ln)
        y -= 4.6 * mm

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_advance_ledger_pdf_reportlab(
    payload: Dict[str, Any],
    branding: Optional[UiBranding],
    adv_payload: Dict[str, Any],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    bottom = 14 * mm

    def new_page(title: str) -> float:
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0)
        c.drawCentredString(x0 + w0 / 2, y0, title)
        y0 -= 5 * mm
        return y0

    y = new_page("ADVANCE LEDGER")

    rows = []
    for r in adv_payload.get("rows") or []:
        rows.append([
            _safe(r.get("date")),
            _safe(r.get("type")),
            _safe(r.get("reference")),
            _safe(r.get("note")),
            _money(r.get("amount")),
        ])
    if not rows:
        rows = [["—", "—", "—", "—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[
            ("Date/Time", 0.22),
            ("Type", 0.14),
            ("Reference", 0.20),
            ("Note", 0.28),
            ("Amount", 0.16),
        ],
        rows=rows,
        row_h=7 * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page("ADVANCE LEDGER (Cont.)"),
    )

    y -= 4 * mm
    if y - 16 * mm < bottom:
        c.showPage()
        y = new_page("ADVANCE LEDGER (Summary)")

    t = adv_payload.get("totals") or {}
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(x0, y, x0 + w0, y)
    y -= 5 * mm

    c.setFont("Helvetica-Bold", 9.6)
    c.drawRightString(x0 + w0 - 42 * mm, y, "Net Advance Balance :")
    c.drawRightString(x0 + w0, y, _money(t.get("net")))
    y -= 6 * mm

    c.setFont("Helvetica-Bold", 9.2)
    c.drawString(x0, y, "In Words :")
    c.setFont("Helvetica", 9.2)
    words = _safe(t.get("net_words") or "")
    for ln in simpleSplit(words, "Helvetica", 9.2, w0 - 22 * mm)[:3]:
        c.drawString(x0 + 18 * mm, y, ln)
        y -= 4.6 * mm

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_insurance_pdf_reportlab(
    payload: Dict[str, Any],
    branding: Optional[UiBranding],
    ins_payload: Dict[str, Any],
) -> bytes:
    # If no insurance at all, return empty bytes (we will skip it)
    if not ins_payload or (not ins_payload.get("insurance_case")
                           and not ins_payload.get("preauths")
                           and not ins_payload.get("claims")):
        return b""

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    M = 10 * mm
    x0 = M
    w0 = W - 2 * M
    bottom = 14 * mm

    def new_page(title: str) -> float:
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0)
        c.drawCentredString(x0 + w0 / 2, y0, title)
        y0 -= 5 * mm
        return y0

    y = new_page("INSURANCE")

    ic = ins_payload.get("insurance_case")
    if ic:
        # mini govt key/value block
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(x0, y, "Insurance Case")
        y -= 4.5 * mm

        def kv(label: str, val: str):
            nonlocal y
            c.setFont("Helvetica-Bold", 9.0)
            c.drawString(x0, y, f"{label} :")
            c.setFont("Helvetica", 9.0)
            c.drawString(x0 + 32 * mm, y, _safe(val))
            y -= 4.8 * mm

        kv("Payer", ic.get("payer"))
        kv("TPA", ic.get("tpa"))
        kv("Policy No", ic.get("policy_no"))
        kv("Member ID", ic.get("member_id"))
        kv("Status", ic.get("status"))
        kv("Approved Limit", ic.get("approved_limit"))

        y -= 2 * mm
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.8)
        c.line(x0, y, x0 + w0, y)
        y -= 5 * mm

    preauths = ins_payload.get("preauths") or []
    if preauths:
        if y - 20 * mm < bottom:
            c.showPage()
            y = new_page("INSURANCE (Preauth)")
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "PREAUTH REQUESTS")
        y -= 4 * mm

        rows = []
        for p in preauths:
            rows.append([
                _safe(p.get("preauth_no")),
                _safe(p.get("status")),
                _safe(p.get("date")),
                _safe(p.get("requested")),
                _safe(p.get("approved")),
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Preauth No", 0.22), ("Status", 0.16), ("Date", 0.26),
                  ("Requested", 0.18), ("Approved", 0.18)],
            rows=rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page("INSURANCE (Preauth Cont.)"),
        )
        y -= 5 * mm

    claims = ins_payload.get("claims") or []
    if claims:
        if y - 20 * mm < bottom:
            c.showPage()
            y = new_page("INSURANCE (Claims)")
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x0, y, "CLAIMS")
        y -= 4 * mm

        rows = []
        for cl in claims:
            rows.append([
                _safe(cl.get("claim_no")),
                _safe(cl.get("status")),
                _safe(cl.get("date")),
                _safe(cl.get("claimed")),
                _safe(cl.get("approved")),
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Claim No", 0.22), ("Status", 0.16), ("Date", 0.26),
                  ("Claimed", 0.18), ("Approved", 0.18)],
            rows=rows,
            row_h=7 * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page("INSURANCE (Claims Cont.)"),
        )

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_common_header_pdf(payload: Dict[str, Any],
                              branding: Optional[UiBranding]) -> bytes:
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
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)
    return _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)


@router.get("/common-header")
def billing_common_header_pdf(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)
    pdf_bytes = _render_common_header_pdf(payload, branding)

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
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)
    return _build_overview_payload(
        db,
        case,
        doc_no=doc_no,
        doc_date=doc_date,
        include_draft_invoices=include_draft_invoices,
    )


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
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_overview_payload(
        db,
        case,
        doc_no=doc_no,
        doc_date=doc_date,
        include_draft_invoices=include_draft_invoices,
    )
    pdf_bytes = _render_overview_pdf_reportlab(payload, branding)

    filename = f"Billing_Overview_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ✅ FIX 1: ADD ROUTE DECORATOR
# ✅ FIX 2: make invoice_id a PATH param
@router.get("/invoices/{invoice_id}/pdf")
def billing_invoice_pdf(
        invoice_id: int = FPath(..., gt=0),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])

    inv = (db.query(BillingInvoice).options(
        selectinload(BillingInvoice.lines),
        joinedload(BillingInvoice.billing_case),
    ).filter(BillingInvoice.id == int(invoice_id)).first())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # UiBranding (safe tenant filter if column exists)
    branding_q = db.query(UiBranding)
    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)
    if tenant_id is not None and hasattr(UiBranding, "tenant_id"):
        branding_q = branding_q.filter(UiBranding.tenant_id == tenant_id)
    if hasattr(UiBranding, "is_active"):
        branding_q = branding_q.filter(UiBranding.is_active.is_(True))
    branding = branding_q.order_by(UiBranding.id.desc()).first()

    # Load full case + patient + addresses for the patient header block
    case = None
    try:
        cid = getattr(inv, "billing_case_id", None) or getattr(
            getattr(inv, "billing_case", None), "id", None)
        if cid:
            case = _load_case(db, int(cid))
    except Exception:
        case = None

    patient = getattr(case, "patient", None) if case else None

    payer_type = str(
        getattr(getattr(inv, "payer_type", None), "value",
                getattr(inv, "payer_type", "")) or "")
    payer_label = payer_type.title() if payer_type else "Patient"

    doc_no = _safe(getattr(inv, "invoice_number", None) or f"INV-{invoice_id}")
    created_at = getattr(inv, "created_at", None)
    doc_date = created_at.date() if isinstance(created_at, datetime) else None

    header_payload = None
    if case is not None:
        header_payload = _build_header_payload(db,
                                               case,
                                               doc_no=doc_no,
                                               doc_date=doc_date)

    # ✅ paper + orientation are passed through to build_invoice_pdf (your pdf builder must accept them)
    pdf_bytes = build_invoice_pdf(
        invoice=inv,
        lines=list(getattr(inv, "lines", []) or []),
        branding=branding,
        patient=patient,
        payer_label=payer_label,
        header_payload=header_payload,
        paper=paper,
        orientation=orientation,
    )

    filename = f"Invoice_{getattr(inv, 'invoice_number', invoice_id)}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.get("/full-history/data")
def billing_full_history_data(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        include_draft_invoices: bool = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)

    overview = _build_overview_payload(
        db,
        case,
        doc_no=doc_no,
        doc_date=doc_date,
        include_draft_invoices=include_draft_invoices,
    )

    invoices = _list_case_invoices(
        db, case.id, include_draft_invoices=include_draft_invoices)
    payments_ledger = _build_payments_ledger_payload(db, case)
    advance_ledger = _build_advance_ledger_payload(db, case)
    insurance = _try_load_insurance_payload(db, case)

    inv_rows = []
    for inv in invoices:
        inv_rows.append({
            "invoice_number":
            _safe(getattr(inv, "invoice_number", None)),
            "module":
            _safe(getattr(inv, "module", None)),
            "invoice_type":
            _safe(_val(getattr(inv, "invoice_type", None))),
            "status":
            _safe(_val(getattr(inv, "status", None))),
            "created_at":
            _fmt_dt(getattr(inv, "created_at", None)),
            "grand_total":
            float(_dec(getattr(inv, "grand_total", 0))),
        })

    return {
        "kind": "FULL_HISTORY",
        "case_id": int(case.id),
        "case_number": _safe(getattr(case, "case_number", None)),
        "overview": overview,
        "invoices": inv_rows,
        "payments_ledger": payments_ledger,
        "advance_ledger": advance_ledger,
        "insurance": insurance,
    }


@router.get("/full-history")
def billing_full_history_pdf(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        include_draft_invoices: bool = Query(True),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ Govt form Full Bill History:
    Summary + Detail Lines + Payments + Deposits + Insurance + Pharmacy Split-Up
    """
    _need_any(user, ["billing.view"])
    try:
        case = _load_case(db, case_id)
        branding = _load_branding(db)

        inv_q = (db.query(BillingInvoice).options(
            selectinload(BillingInvoice.lines)).filter(
                BillingInvoice.billing_case_id == case.id,
                BillingInvoice.status
                != DocStatus.VOID).order_by(BillingInvoice.created_at.asc()))
        if not include_draft_invoices:
            inv_q = inv_q.filter(BillingInvoice.status != DocStatus.DRAFT)
        invoices = inv_q.all()

        overview_payload = _build_overview_payload(
            db,
            case,
            doc_no=doc_no,
            doc_date=doc_date,
            include_draft_invoices=include_draft_invoices,
        )

        printed_by = _safe(
            getattr(user, "name", None) or getattr(user, "full_name", None)
            or getattr(user, "username", None))

        pdf_bytes = _render_full_history_pdf_reportlab(
            db=db,
            case=case,
            invoices=invoices,
            branding=branding,
            overview_payload=overview_payload,
            printed_by=printed_by if printed_by != "—" else "",
        )

        filename = f"Billing_FullHistory_{_safe(getattr(case, 'case_number', None))}.pdf"
        headers = {
            "Content-Disposition": f'{disposition}; filename="{filename}"'
        }
        return StreamingResponse(BytesIO(pdf_bytes),
                                 media_type="application/pdf",
                                 headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        # ✅ shows real reason instead of silent 500
        raise HTTPException(status_code=500,
                            detail=f"Full history PDF failed: {str(e)}")
