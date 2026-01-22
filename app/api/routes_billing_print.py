# FILE: app/api/routes_billing_print.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Path as FPath
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload, joinedload
from sqlalchemy.inspection import inspect as sa_inspect

from reportlab.lib.pagesizes import A3, A4, A5, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit, ImageReader
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

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

# Optional: Department lookup fallback
try:
    from app.models.department import Department  # type: ignore
except Exception:
    Department = None  # type: ignore

# Encounter models (safe)
try:
    from app.models.opd import Visit  # type: ignore
except Exception:
    Visit = None

try:
    from app.models.ipd import IpdAdmission, IpdBed, IpdRoom  # type: ignore
    try:
        from app.models.ipd import IpdWard  # type: ignore
    except Exception:
        IpdWard = None  # type: ignore
except Exception:
    IpdAdmission = None
    IpdBed = None
    IpdRoom = None
    IpdWard = None  # type: ignore

# ✅ try load BillingAdvanceApplication safely (some installations may not have it)
try:
    from app.models.billing import BillingAdvanceApplication  # type: ignore
except Exception:
    BillingAdvanceApplication = None  # type: ignore

# Optional legacy HTML/Weasy helpers (ignore if missing)
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

# Optional external invoice builder (ignore if missing)
build_invoice_pdf = None
try:
    from app.services.pdfs.billing_invoice_export import build_invoice_pdf  # type: ignore
except Exception:
    build_invoice_pdf = None

# ✅ PDF merge (optional dependency)
PdfReader = PdfWriter = None  # type: ignore
try:
    from pypdf import PdfReader, PdfWriter  # type: ignore
except Exception:
    try:
        from PyPDF2 import PdfReader, PdfWriter  # type: ignore
    except Exception:
        PdfReader = PdfWriter = None  # type: ignore

# ✅ Insurance models (safe import)
try:
    from app.models.billing import BillingInsuranceCase, BillingPreauthRequest, BillingClaim  # type: ignore
except Exception:
    BillingInsuranceCase = None  # type: ignore
    BillingPreauthRequest = None  # type: ignore
    BillingClaim = None  # type: ignore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing/print", tags=["Billing Print"])


# =========================================================
# Permissions (safe fallback)
# =========================================================
def _perm_code(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, str):
        return x.strip()
    return (getattr(x, "code", None) or getattr(x, "name", None)
            or "").strip() or None


def _need_any(user: User, perms: Union[str, Iterable[str]]) -> None:
    if isinstance(perms, str):
        perms = [perms]
    else:
        perms = list(perms)

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

    codes: set[str] = set()
    try:
        for item in (getattr(user, "permissions", None) or []):
            c = _perm_code(item)
            if c:
                codes.add(c)
    except Exception:
        pass

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


# =========================================================
# Small utils
# =========================================================
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
               keys: List[str],
               default: str = "—") -> str:
    for k in keys:
        if k in meta:
            val = meta.get(k)
            if val not in (None, "", []):
                return str(val).strip()
    return default


def _has_rel(model: Any, rel_name: str) -> bool:
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


def _try_parse_date_str(s: str) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y",
                "%d/%b/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _fmt_ddmmyyyy(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.date().strftime("%d-%m-%Y")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, str):
        d = _try_parse_date_str(v)
        return d.strftime("%d-%m-%Y") if d else _safe(v)
    return _safe(v)


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


# =========================================================
# Amount in words (INR) – Indian system
# =========================================================
_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen"
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
    return _TENS[t] if o == 0 else f"{_TENS[t]} {_ONES[o]}"


def _three_digits(n: int) -> str:
    if n == 0:
        return ""
    h = n // 100
    r = n % 100
    parts: List[str] = []
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

    parts: List[str] = []
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
    return out or "Zero"


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


# =========================================================
# Loaders
# =========================================================
def _load_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).order_by(UiBranding.id.desc()).first()


def _load_case(db: Session, case_id: int) -> BillingCase:
    case = (db.query(BillingCase).options(
        selectinload(BillingCase.patient).selectinload(
            Patient.addresses)).filter(BillingCase.id == case_id).first())
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return case


def _list_case_invoices(
    db: Session,
    case_id: int,
    *,
    include_draft_invoices: bool = True,
) -> List[BillingInvoice]:
    q = (db.query(BillingInvoice).options(selectinload(
        BillingInvoice.lines)).filter(
            BillingInvoice.billing_case_id == case_id,
            BillingInvoice.status != DocStatus.VOID,
        ).order_by(BillingInvoice.created_at.asc()))
    if not include_draft_invoices:
        q = q.filter(BillingInvoice.status != DocStatus.DRAFT)
    return q.all()


def _load_invoice(db: Session, invoice_id: int) -> BillingInvoice:
    inv = (db.query(BillingInvoice).options(
        selectinload(BillingInvoice.lines),
        joinedload(BillingInvoice.billing_case)).filter(
            BillingInvoice.id == int(invoice_id)).first())
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


# =========================================================
# Patient helpers
# =========================================================
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


def _pick_best_address(addresses: List[PatientAddress]) -> str:
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


# =========================================================
# Payer lookups
# =========================================================
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
    pm = getattr(case, "payer_mode", None)
    out: Dict[str, str] = {"Payer Mode": _safe(_val(pm) if pm else None)}

    if pm is None or _eq_enum(pm, PayerMode.SELF) or str(
            _val(pm)).upper() == "SELF":
        return out

    patient: Patient = case.patient
    effective_payer_id = getattr(case, "default_payer_id", None) or getattr(
        patient, "credit_payer_id", None)
    effective_tpa_id = getattr(case, "default_tpa_id", None) or getattr(
        patient, "credit_tpa_id", None)
    effective_plan_id = getattr(case,
                                "default_credit_plan_id", None) or getattr(
                                    patient, "credit_plan_id", None)

    out["Default Bill Type"] = _safe(getattr(case, "default_payer_type", None))
    out["Payer"] = _payer_name(db, effective_payer_id)
    out["TPA"] = _tpa_name(db, effective_tpa_id)
    out["Credit Plan"] = _plan_name(db, effective_plan_id)
    return out


# =========================================================
# Encounter context loaders (OP/IP) – extra fallbacks
# =========================================================
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

    appt = getattr(v, "appointment", None) if _has_rel(Visit,
                                                       "appointment") else None
    if appt is not None:
        appt_date = getattr(appt, "date", None)
        slot_start = getattr(appt, "slot_start", None) or getattr(
            appt, "start_time", None)
        if appt_date and slot_start:
            out["Appointment On"] = f"{_fmt_date(appt_date)} {str(slot_start)[:5]}"
        elif appt_date:
            out["Appointment On"] = _fmt_date(appt_date)

    doc_obj = getattr(v, "doctor", None) if _has_rel(Visit, "doctor") else None
    if doc_obj is not None:
        out["Doctor"] = _safe(
            getattr(doc_obj, "name", None)
            or getattr(doc_obj, "full_name", None))
    else:
        doc_id = getattr(v, "doctor_id", None) or getattr(
            v, "practitioner_user_id", None)
        if doc_id:
            doc = db.query(User).filter(User.id == int(doc_id)).first()
            if doc:
                out["Doctor"] = _safe(
                    getattr(doc, "name", None)
                    or getattr(doc, "full_name", None))

    dept_obj = getattr(v, "department", None) if _has_rel(
        Visit, "department") else None
    if dept_obj is not None:
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

    # relationship name possibilities (different installations)
    bed_rel_names = ("current_bed", "bed", "ipd_bed", "active_bed",
                     "current_ipd_bed")
    room_rel_names = ("room", "ipd_room")
    ward_rel_names = ("ward", "ipd_ward")

    # Try to eager-load bed->room->ward chain if relations exist
    for bed_rel in bed_rel_names:
        if _has_rel(IpdAdmission, bed_rel):
            bed_opt = selectinload(getattr(IpdAdmission, bed_rel))
            if IpdBed is not None:
                for rr in room_rel_names:
                    if _has_rel(IpdBed, rr):
                        room_opt = bed_opt.selectinload(getattr(IpdBed, rr))
                        if IpdRoom is not None:
                            for wr in ward_rel_names:
                                if _has_rel(IpdRoom, wr):
                                    room_opt = room_opt.selectinload(
                                        getattr(IpdRoom, wr))
                                    break
                        bed_opt = room_opt
                        break
            opts.append(bed_opt)
            break

    if _has_rel(IpdAdmission, "department"):
        opts.append(selectinload(IpdAdmission.department))
    if _has_rel(IpdAdmission, "doctor"):
        opts.append(selectinload(IpdAdmission.doctor))
    if _has_rel(IpdAdmission, "practitioner"):
        opts.append(selectinload(IpdAdmission.practitioner))

    if opts:
        q = q.options(*opts)

    adm = q.filter(IpdAdmission.id == int(encounter_id)).first()
    if not adm:
        return out

    out["IP Admission Number"] = _safe(
        getattr(adm, "admission_code", None)
        or getattr(adm, "ip_number", None) or getattr(adm, "ip_no", None)
        or getattr(adm, "display_code", None)
        or getattr(adm, "admission_no", None)
        or getattr(adm, "ip_admission_no", None))

    out["Admitted On"] = _fmt_dt(
        getattr(adm, "admitted_at", None)
        or getattr(adm, "admission_at", None))
    out["Discharged On"] = _fmt_dt(
        getattr(adm, "discharge_at", None)
        or getattr(adm, "discharged_at", None))

    # Doctor
    doc_obj = None
    if _has_rel(IpdAdmission, "doctor"):
        doc_obj = getattr(adm, "doctor", None)
    if doc_obj is None and _has_rel(IpdAdmission, "practitioner"):
        doc_obj = getattr(adm, "practitioner", None)

    if doc_obj is not None:
        out["Admission Doctor"] = _safe(
            getattr(doc_obj, "name", None)
            or getattr(doc_obj, "full_name", None))
    else:
        practitioner_id = (getattr(adm, "practitioner_user_id", None)
                           or getattr(adm, "doctor_id", None)
                           or getattr(adm, "practitioner_id", None))
        if practitioner_id:
            doc = db.query(User).filter(
                User.id == int(practitioner_id)).first()
            if doc:
                out["Admission Doctor"] = _safe(
                    getattr(doc, "name", None)
                    or getattr(doc, "full_name", None))

    # Department
    dept_name = "—"
    if _has_rel(IpdAdmission, "department"):
        dept_obj = getattr(adm, "department", None)
        if dept_obj is not None:
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

    # First: direct text columns (many schemas store them directly)
    out["Ward"] = _pick_attr(adm,
                             "ward_name",
                             "ward_display_name",
                             "ward_label",
                             "ward",
                             "ward_no",
                             "ward_code",
                             default=out["Ward"])
    out["Room"] = _pick_attr(adm,
                             "room_display_name",
                             "room_label",
                             "room_no",
                             "room_number",
                             "room",
                             "room_code",
                             "room_name",
                             default=out["Room"])
    out["Bed"] = _pick_attr(adm,
                            "bed_display_name",
                            "bed_label",
                            "bed_no",
                            "bed_number",
                            "bed_code",
                            "bed",
                            "bed_name",
                            default=out["Bed"])

    # Second: relationship / id fallback
    bed_obj = None
    for bed_rel in bed_rel_names:
        if _has_rel(IpdAdmission, bed_rel):
            bed_obj = getattr(adm, bed_rel, None)
            if bed_obj:
                break

    if bed_obj is None:
        bed_id = (getattr(adm, "current_bed_id", None)
                  or getattr(adm, "bed_id", None)
                  or getattr(adm, "ipd_bed_id", None)
                  or getattr(adm, "current_ipd_bed_id", None))
        if bed_id and IpdBed is not None:
            bed_obj = db.query(IpdBed).filter(IpdBed.id == int(bed_id)).first()

    if bed_obj is not None:
        # Bed name (most common actual fields)
        out["Bed"] = _pick_attr(
            bed_obj,
            "display_name",
            "displayLabel",
            "bed_display_name",
            "name",
            "label",
            "code",
            "bed_code",
            "bed_number",
            "number",
            default=out["Bed"],
        )

        room_obj = None
        if IpdBed is not None:
            for rr in room_rel_names:
                if _has_rel(IpdBed, rr):
                    room_obj = getattr(bed_obj, rr, None)
                    if room_obj:
                        break

        if room_obj is None:
            room_id = getattr(bed_obj, "room_id", None) or getattr(
                bed_obj, "ipd_room_id", None)
            if room_id and IpdRoom is not None:
                room_obj = db.query(IpdRoom).filter(
                    IpdRoom.id == int(room_id)).first()

        if room_obj is not None:
            out["Room"] = _pick_attr(
                room_obj,
                "display_name",
                "displayLabel",
                "room_display_name",
                "name",
                "label",
                "number",
                "room_no",
                "room_number",
                "code",
                default=out["Room"],
            )

            ward_name = out["Ward"]
            ward_obj = None
            if IpdRoom is not None:
                for wr in ward_rel_names:
                    if _has_rel(IpdRoom, wr):
                        ward_obj = getattr(room_obj, wr, None)
                        if ward_obj:
                            break

            if ward_obj is not None:
                ward_name = _pick_attr(
                    ward_obj,
                    "display_name",
                    "displayLabel",
                    "ward_display_name",
                    "name",
                    "label",
                    "ward_name",
                    "code",
                    default=ward_name,
                )
            else:
                ward_name = _pick_attr(room_obj,
                                       "ward_display_name",
                                       "ward_name",
                                       "ward",
                                       "ward_no",
                                       "ward_code",
                                       default=ward_name)
                ward_id = getattr(room_obj, "ward_id", None) or getattr(
                    room_obj, "ipd_ward_id", None)
                if ward_name == "—" and ward_id and IpdWard is not None:
                    wobj = db.query(IpdWard).filter(
                        IpdWard.id == int(ward_id)).first()
                    if wobj:
                        ward_name = _pick_attr(wobj,
                                               "display_name",
                                               "ward_display_name",
                                               "name",
                                               "label",
                                               "ward_name",
                                               "code",
                                               default=ward_name)

            out["Ward"] = ward_name

    # Third: meta_json fallback (some IPD stores ward/room/bed in JSON)
    try:
        meta = _meta(
            getattr(adm, "meta_json", None) or getattr(adm, "meta", None)
            or getattr(adm, "extra_json", None))
        if out["Ward"] == "—":
            out["Ward"] = _meta_pick(meta, [
                "ward", "ward_name", "wardName", "ward_display_name",
                "wardDisplayName"
            ], out["Ward"])
        if out["Room"] == "—":
            out["Room"] = _meta_pick(meta, [
                "room", "room_no", "roomNo", "room_name", "roomName",
                "room_display_name"
            ], out["Room"])
        if out["Bed"] == "—":
            out["Bed"] = _meta_pick(meta, [
                "bed", "bed_no", "bedNo", "bed_name", "bedName",
                "bed_display_name"
            ], out["Bed"])
    except Exception:
        pass

    return out


def _draw_lv_column_cap(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    col_w: float,
    rows: List[Tuple[str, str]],
    label_w: float,
    size: float,
    leading: float,
    max_value_lines: int = 1,
) -> float:
    colon_w = 2.0 * mm
    gap = 2.0 * mm
    value_x = x + label_w + colon_w + gap
    value_w = max(10, col_w - (label_w + colon_w + gap))

    for k, v in rows:
        k = _safe(k)
        v = _safe(v)

        c.setFont("Helvetica-Bold", size)
        c.setFillColor(INK)
        c.drawString(x, y, _clip_text(k, "Helvetica-Bold", size, label_w))
        c.drawString(x + label_w + 0.2 * mm, y, ":")

        c.setFont("Helvetica", size)
        c.setFillColor(INK)
        lines = simpleSplit(v, "Helvetica", size, value_w) or ["—"]
        lines = _cap_lines_with_ellipsis(lines, max_value_lines, "Helvetica",
                                         size, value_w)

        c.drawString(value_x, y, lines[0][:200])
        for ln in lines[1:]:
            y -= leading
            c.drawString(value_x, y, ln[:200])

        y -= leading

    return y


def _draw_patient_header_block_a5(
    c: canvas.Canvas,
    payload: Dict[str, Any],
    x: float,
    y_top: float,
    w: float,
    *,
    scale: float = 1.0,
) -> float:
    """
    ✅ Pixel-fit compact header for A5 Bill Summary.
    Keeps 2 columns, avoids address overflow, and fits in ~40-46mm.
    """
    bill = payload.get("bill", {}) or {}
    pat = payload.get("patient", {}) or {}
    et = payload.get("encounter_type")
    enc = payload.get("encounter", {}) or {}
    payer = payload.get("payer", {}) or {}

    left_w = w * 0.62
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

    # Compact rows (A5)
    left_rows = [
        ("Patient", _safe(pat.get("Patient Name"))),
        ("UHID", _safe(pat.get("UHID"))),
        ("Age/Gender", age_gender),
        ("Phone", _safe(pat.get("Phone"))),
        ("Payer", payer_line),
    ]

    if et == "OP":
        left_rows += [
            ("Doctor", _safe(enc.get("Doctor"))),
            ("Dept", _safe(enc.get("Department"))),
        ]
    elif et == "IP":
        # combine to keep compact
        ward = _safe(enc.get("Ward"))
        room = _safe(enc.get("Room"))
        bed = _safe(enc.get("Bed"))
        left_rows += [
            ("Ward/Room/Bed", f"{ward} / {room} / {bed}"),
            ("Doctor", _safe(enc.get("Admission Doctor"))),
        ]

    # Address 1-line only (avoid pushing content)
    left_rows += [("Address", _safe(pat.get("Address")))]

    right_rows = [
        ("Bill No", _safe(bill.get("Bill Number"))),
        ("Bill Date", _safe(bill.get("Bill Date"))),
        ("Type", _safe(et)),
    ]
    if et == "OP":
        right_rows += [
            ("Visit", _safe(enc.get("Visit Id"))),
            ("Appt", _safe(enc.get("Appointment On"))),
        ]
    elif et == "IP":
        right_rows += [
            ("IP No", _safe(enc.get("IP Admission Number"))),
            ("Admit", _safe(enc.get("Admitted On"))),
        ]

    label_w = min(24 * mm, left_w * 0.34)
    base_size = 7.6 * scale
    leading = 8.8 * scale

    y1 = _draw_lv_column_cap(
        c,
        x=x,
        y=y_top,
        col_w=left_w - 1 * mm,
        rows=left_rows,
        label_w=label_w,
        size=base_size,
        leading=leading,
        max_value_lines=1,  # key for A5
    )
    y2 = _draw_lv_column_cap(
        c,
        x=x + left_w + 4 * mm,
        y=y_top,
        col_w=right_w - 4 * mm,
        rows=right_rows,
        label_w=min(22 * mm, right_w * 0.42),
        size=base_size,
        leading=leading,
        max_value_lines=1,
    )

    y_end = min(y1, y2)

    line_y = y_end + 1.0 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.9)
    c.line(x, line_y, x + w, line_y)

    return y_end


def _draw_balance_pill_a5(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    w: float,
    label: str,
    value: str,
    words: str,
    scale: float = 1.0,
) -> float:
    """
    ✅ A5 bottom "pill" card: Balance + words (pixel-fit)
    """
    h = (14.0 * scale) * mm
    r = 3.2 * mm
    c.setFillColor(HEAD_FILL)
    c.setStrokeColor(GRID_SOFT)
    c.setLineWidth(0.8)
    c.roundRect(x, y - h, w, h, radius=r, stroke=1, fill=1)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9.2 * scale)
    c.drawString(x + 3 * mm, y - 5.2 * mm, label)

    c.setFont("Helvetica-Bold", 9.4 * scale)
    c.drawRightString(x + w - 3 * mm, y - 5.2 * mm, value)

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.7 * scale)
    ww = w - 6 * mm
    line = _clip_text(words, "Helvetica", 7.7 * scale, ww)
    c.drawString(x + 3 * mm, y - 10.7 * mm, line)

    return y - h - 2.5 * mm


# =========================================================
# Invoice bill number/date helpers
# =========================================================
def _invoice_bill_no(inv: BillingInvoice) -> str:
    return _safe(
        getattr(inv, "bill_number", None) or getattr(inv, "bill_no", None)
        or getattr(inv, "doc_no", None)
        or getattr(inv, "invoice_number", None))


def _invoice_bill_date_obj(inv: BillingInvoice) -> Optional[date]:
    v = (getattr(inv, "bill_date", None) or getattr(inv, "invoice_date", None)
         or getattr(inv, "doc_date", None) or getattr(inv, "created_at", None))
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return _try_parse_date_str(v)
    return None


def _invoice_bill_date(inv: BillingInvoice) -> str:
    d = _invoice_bill_date_obj(inv)
    if d:
        return d.strftime("%d-%m-%Y")
    # fallback
    v = getattr(inv, "created_at", None)
    return _fmt_ddmmyyyy(v)


# =========================================================
# Header payload (Case + Patient + Encounter + Payer)
# =========================================================
def _build_header_payload(db: Session, case: BillingCase,
                          doc_no: Optional[str],
                          doc_date: Optional[date]) -> Dict[str, Any]:
    patient: Patient = case.patient
    enc_type_val = _safe(_val(getattr(case, "encounter_type", None))).upper()

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
        "encounter_type": enc_type_val if enc_type_val != "—" else "—",
        "encounter": {},
        "payer": {},
    }

    try:
        enc_id = int(getattr(case, "encounter_id", None) or 0)
    except Exception:
        enc_id = 0

    if enc_id > 0 and (_eq_enum(case.encounter_type, EncounterType.OP)
                       or enc_type_val == "OP"):
        payload["encounter"] = _load_op_context(db, enc_id)
    elif enc_id > 0 and (_eq_enum(case.encounter_type, EncounterType.IP)
                         or enc_type_val == "IP"):
        payload["encounter"] = _load_ip_context(db, enc_id)
    else:
        payload["encounter"] = {}

    payload["payer"] = _payer_block(db, case)
    return payload


# =========================================================
# Overview payload (module summary + payments + advances)
# =========================================================
def _advance_consumed_from_applications(
        db: Session, case_id: int) -> Tuple[Decimal, Optional[datetime]]:
    if BillingAdvanceApplication is None:
        return Decimal("0"), None
    try:
        rows = (db.query(BillingAdvanceApplication).filter(
            BillingAdvanceApplication.billing_case_id == case_id).all())
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
    MODULE_ORDER: List[str] = [
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

    payment_rows: List[Dict[str, Any]] = []
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


# =========================================================
# Ledger payloads
# =========================================================
def _build_payments_ledger_payload(db: Session,
                                   case: BillingCase) -> Dict[str, Any]:
    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case.id).order_by(
            BillingPayment.received_at.asc()).all())

    rows: List[Dict[str, Any]] = []
    total_in = Decimal("0")
    total_out = Decimal("0")

    for p in pays:
        status = getattr(p, "status", None)
        if status is not None and not _eq_enum(status, ReceiptStatus.ACTIVE):
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

    rows: List[Dict[str, Any]] = []
    net = Decimal("0")

    for a in advs:
        et = getattr(a, "entry_type", None)
        amt = _dec(getattr(a, "amount", 0))

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
            "net_words": _amount_in_words_inr(net)
        },
    }


def _try_load_insurance_payload(db: Session,
                                case: BillingCase) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "insurance_case": None,
        "preauths": [],
        "claims": []
    }

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


# =========================================================
# ReportLab UI tokens (Govt-style + premium clarity)
# =========================================================
INK = colors.black
MUTED = colors.HexColor("#222222")  # near-black (optional)
GRID = colors.black  # outer borders
GRID_SOFT = colors.HexColor("#4b4b4b")  # inner grid lines
HEAD_FILL = colors.white  # kept for compatibility (not used)
ZEBRA_FILL = colors.white


def _pagesize_for(paper: str, orientation: str):
    paper = (paper or "A4").upper()
    orientation = (orientation or "portrait").lower()
    if paper == "A3":
        size = A3
    elif paper == "A5":
        size = A5
    else:
        size = A4
    if orientation == "landscape":
        size = landscape(size)
    return size


def _layout_for(paper: str, orientation: str) -> Dict[str, Any]:
    size = _pagesize_for(paper, orientation)
    W, H = size
    baseW = A4[0]

    paper_u = (paper or "A4").upper()

    # ✅ IMPORTANT:
    # A5 width is ~0.707 of A4. Do NOT clamp it up to 0.84 (it becomes congested).
    raw_scale = float(W) / float(baseW)

    if paper_u == "A5":
        scale = max(0.72, min(0.86, raw_scale))  # keep A5 crisp + airy
        M = 7.0 * mm
        bottom = 10.5 * mm
    elif paper_u == "A3":
        scale = max(1.05, min(1.22, raw_scale))
        M = 12.0 * mm
        bottom = 14.0 * mm
    else:
        scale = max(0.90, min(1.10, raw_scale))
        M = 10.0 * mm
        bottom = 14.0 * mm

    border_pad = max(5 * mm, M * 0.60)

    return {
        "size": size,
        "W": W,
        "H": H,
        "scale": scale,
        "M": M,
        "bottom": bottom,
        "x0": M,
        "w0": W - 2 * M,
        "border_pad": border_pad,
    }


def _is_number_like(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    s = s.replace(",", "")
    try:
        Decimal(s)
        return True
    except Exception:
        return False


def _clip_text(txt: str, font: str, size: float, max_w: float) -> str:
    t = (txt or "").strip()
    if not t:
        return "—"
    if stringWidth(t, font, size) <= max_w:
        return t
    ell = "…"
    cut = t
    while cut and stringWidth(cut + ell, font, size) > max_w:
        cut = cut[:-1]
    return (cut + ell) if cut else ell


def _cap_lines(lines: List[str], max_lines: int) -> List[str]:
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


def _cap_lines_with_ellipsis(lines: List[str], max_lines: int, font: str,
                             size: float, max_w: float) -> List[str]:
    if not lines:
        return ["—"]
    if len(lines) <= max_lines:
        return lines
    keep = lines[:max_lines]
    keep[-1] = _clip_text(keep[-1], font, size, max_w)
    if not keep[-1].endswith("…"):
        keep[-1] = _clip_text(keep[-1] + "…", font, size, max_w)
    return keep


def _draw_page_border(c: canvas.Canvas, *, W: float, H: float,
                      pad: float) -> None:
    c.setStrokeColor(GRID)
    c.setLineWidth(0.8)
    c.rect(pad, pad, W - 2 * pad, H - 2 * pad, stroke=1, fill=0)


# =========================================================
# Branding header (ReportLab)
# =========================================================
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


def _draw_branding_header(c: canvas.Canvas,
                          branding: Optional[UiBranding],
                          x: float,
                          top_y: float,
                          w: float,
                          *,
                          scale: float = 1.0) -> float:
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

    logo_h = (18.0 * scale) * mm
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

    meta_lines: List[str] = []
    if addr != "—":
        meta_lines.extend(
            _cap_lines(simpleSplit(addr, "Helvetica", 8.4 * scale, right_w),
                       2))
    if contact_line:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(contact_line, "Helvetica", 8.4 * scale, right_w),
                1))

    extra_bits = []
    if website != "—":
        extra_bits.append(f"{website}")
    if gstin != "—":
        extra_bits.append(f"GSTIN: {gstin}")
    if extra_bits and len(meta_lines) < 3:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(" | ".join(extra_bits), "Helvetica", 8.4 * scale,
                            right_w), 1))
    meta_lines = _cap_lines(meta_lines, 3)

    lines: List[Tuple[str, str, float, Any]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", 12.0 * scale, INK))
    if tag != "—":
        lines.append((tag, "Helvetica", 8.6 * scale, MUTED))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", 8.4 * scale, MUTED))

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
                c.drawImage(
                    logo_reader,
                    x,
                    center_y - (draw_h / 2),
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

    c.setStrokeColor(GRID_SOFT)
    c.setLineWidth(0.6)
    c.line(x, top_y - header_h, x + w, top_y - header_h)

    return top_y - header_h - (2 * mm)


def _draw_branding_header_small(c: canvas.Canvas,
                                branding: Optional[UiBranding],
                                x: float,
                                top_y: float,
                                w: float,
                                *,
                                scale: float = 1.0) -> float:
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

    logo_h = (14.0 * scale) * mm
    gutter = 4 * mm

    logo_col = min(max(48 * mm, w * 0.38), 64 * mm)
    right_w = max(46 * mm, w - logo_col - gutter)

    org = _safe(_bget(b, "org_name", "name", "hospital_name"))
    tag = _safe(_bget(b, "org_tagline", "tagline"))
    addr = _safe(_bget(b, "org_address", "address"))
    phone = _safe(_bget(b, "org_phone", "phone", "mobile"))

    meta_lines: List[str] = []
    if addr != "—":
        meta_lines.extend(
            _cap_lines(simpleSplit(addr, "Helvetica", 7.7 * scale, right_w),
                       1))
    if phone != "—" and len(meta_lines) < 2:
        meta_lines.extend(
            _cap_lines(
                simpleSplit(f"Ph: {phone}", "Helvetica", 7.7 * scale, right_w),
                1))
    meta_lines = _cap_lines(meta_lines, 2)

    lines: List[Tuple[str, str, float, Any]] = []
    if org != "—":
        lines.append((org, "Helvetica-Bold", 11.0 * scale, INK))
    if tag != "—":
        lines.append((tag, "Helvetica", 8.0 * scale, MUTED))
    for ln in meta_lines:
        lines.append((ln, "Helvetica", 7.7 * scale, MUTED))

    def lh(sz: float) -> float:
        return sz * 1.16

    text_h = sum(lh(sz) for _, _, sz, _ in lines) if lines else (8 * mm)
    header_h = max(logo_h, text_h) + (1.8 * mm)

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
                c.drawImage(
                    logo_reader,
                    x,
                    center_y - (draw_h / 2),
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

    c.setStrokeColor(GRID_SOFT)
    c.setLineWidth(0.6)
    c.line(x, top_y - header_h, x + w, top_y - header_h)

    return top_y - header_h - (2 * mm)


# =========================================================
# Patient header block (Govt form alignment)
# =========================================================
def _draw_lv_column(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    col_w: float,
    rows: List[Tuple[str, str]],
    label_w: float,
    size: float,
    leading: float,
) -> float:
    colon_w = 2.0 * mm
    gap = 2.0 * mm
    value_x = x + label_w + colon_w + gap
    value_w = max(10, col_w - (label_w + colon_w + gap))

    for k, v in rows:
        k = _safe(k)
        v = _safe(v)

        c.setFont("Helvetica-Bold", size)
        c.setFillColor(INK)
        c.drawString(x, y, (k[:28] + "…") if len(k) > 29 else k)
        c.drawString(x + label_w + 0.2 * mm, y, ":")

        c.setFont("Helvetica", size)
        c.setFillColor(INK)
        lines = simpleSplit(v, "Helvetica", size, value_w) or ["—"]
        c.drawString(value_x, y, lines[0][:200])

        for ln in lines[1:]:
            y -= leading
            c.drawString(value_x, y, ln[:200])

        y -= leading

    return y


def _draw_patient_header_block(
    c: canvas.Canvas,
    payload: Dict[str, Any],
    x: float,
    y_top: float,
    w: float,
    *,
    scale: float = 1.0,
) -> float:
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
        left_rows += [
            ("Ward", _safe(enc.get("Ward"))),
            ("Room", _safe(enc.get("Room"))),
            ("Bed", _safe(enc.get("Bed"))),
            ("Doctor", _safe(enc.get("Admission Doctor"))),
        ]

    left_rows += [("Patient Address", _safe(pat.get("Address")))]

    right_rows = [
        ("Bill Number", _safe(bill.get("Bill Number"))),
        ("Bill Date", _safe(bill.get("Bill Date"))),
        ("Encounter Type", _safe(et)),
    ]
    if et == "OP":
        right_rows += [("Visit ID", _safe(enc.get("Visit Id"))),
                       ("Appointment On", _safe(enc.get("Appointment On")))]
    elif et == "IP":
        right_rows += [
            ("IP Number", _safe(enc.get("IP Admission Number"))),
            ("Admitted On", _safe(enc.get("Admitted On"))),
            ("Discharged On", _safe(enc.get("Discharged On"))),
        ]

    label_w = 30 * mm
    base_size = 8.8 * scale
    leading = 10.2 * scale

    y1 = _draw_lv_column(c,
                         x=x,
                         y=y_top,
                         col_w=left_w - 2 * mm,
                         rows=left_rows,
                         label_w=label_w,
                         size=base_size,
                         leading=leading)
    y2 = _draw_lv_column(c,
                         x=x + left_w + 6 * mm,
                         y=y_top,
                         col_w=right_w - 6 * mm,
                         rows=right_rows,
                         label_w=label_w,
                         size=base_size,
                         leading=leading)

    y_end = min(y1, y2)

    line_y = y_end + 1.2 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.9)
    c.line(x, line_y, x + w, line_y)

    return y_end


# =========================================================
# Simple table (with proper padding, line breaks, page breaks)
# =========================================================
def _draw_simple_table(
    c: canvas.Canvas,
    *,
    x: float,
    y: float,
    w: float,
    cols: List[Tuple[str, float]],
    rows: List[List[str]],
    row_h: float,
    bottom_margin: float,
    new_page_fn=None,
    aligns: Optional[List[str]] = None,
    max_lines: int = 2,
    zebra: bool = False,  # ✅ force off (govt)
    head_size: float = 9.2,
    body_size: float = 9.1,
    pad_x: float = 2.0 * mm,
    pad_y: float = 1.6 * mm,
    lead: float = 11.0,
) -> float:
    col_widths = [w * r for _, r in cols]
    head_font = ("Helvetica-Bold", head_size)
    body_font = ("Helvetica", body_size)

    def draw_header(cur_y: float) -> float:
        h = row_h

        # Header border only (no fill)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.8)
        c.rect(x, cur_y - h, w, h, stroke=1, fill=0)

        c.setFillColor(INK)
        c.setFont(*head_font)

        xx = x
        for (title, _), cw in zip(cols, col_widths):
            tw = cw - 2 * pad_x
            t = _clip_text(str(title or ""), head_font[0], head_font[1], tw)
            c.drawString(xx + pad_x, cur_y - h + (h - head_font[1]) / 2 - 0.5,
                         t)
            xx += cw

        # Vertical splits
        c.setStrokeColor(GRID_SOFT)
        c.setLineWidth(0.6)
        xx = x
        for cw in col_widths[:-1]:
            xx += cw
            c.line(xx, cur_y - h, xx, cur_y)

        return cur_y - h

    def ensure(cur_y: float, need_h: float) -> float:
        if cur_y - need_h < bottom_margin:
            if callable(new_page_fn):
                c.showPage()
                cur_y = new_page_fn()
            else:
                c.showPage()
                cur_y = A4[1] - 12 * mm
            cur_y = draw_header(cur_y)
        return cur_y

    def cell_align(j: int, txt: Any) -> str:
        if aligns and j < len(aligns) and aligns[j] in ("left", "right",
                                                        "center"):
            return aligns[j]
        return "right" if _is_number_like(txt) else "left"

    cur_y = draw_header(y)
    c.setFont(*body_font)

    for r in (rows or []):
        cell_lines: List[List[str]] = []
        max_needed_lines = 1

        for j, cw in enumerate(col_widths):
            raw = "" if j >= len(r) else ("" if r[j] is None else str(r[j]))
            raw = raw.strip() if raw.strip() else "—"
            available_w = max(8.0, cw - 2 * pad_x)

            lines = simpleSplit(raw, body_font[0], body_font[1],
                                available_w) or [raw]
            lines = _cap_lines_with_ellipsis(lines, max_lines, body_font[0],
                                             body_font[1], available_w)

            cell_lines.append(lines)
            max_needed_lines = max(max_needed_lines, len(lines))

        dyn_h = max(row_h, (pad_y * 2) + (max_needed_lines * lead))
        cur_y = ensure(cur_y, dyn_h + 2 * mm)

        # Row border only (no fill)
        c.setStrokeColor(GRID_SOFT)
        c.setLineWidth(0.6)
        c.rect(x, cur_y - dyn_h, w, dyn_h, stroke=1, fill=0)

        # Vertical splits
        xx = x
        for cw in col_widths[:-1]:
            xx += cw
            c.line(xx, cur_y - dyn_h, xx, cur_y)

        baseline_top = cur_y - pad_y - body_font[1]

        xx = x
        c.setFillColor(INK)
        c.setFont(*body_font)

        for j, cw in enumerate(col_widths):
            al = cell_align(j, (r[j] if j < len(r) else ""))
            lines = cell_lines[j]
            for li, line in enumerate(lines):
                yy = baseline_top - (li * lead)
                if al == "right":
                    c.drawRightString(xx + cw - pad_x, yy, line)
                elif al == "center":
                    c.drawCentredString(xx + cw / 2, yy, line)
                else:
                    c.drawString(xx + pad_x, yy, line)
            xx += cw

        cur_y -= dyn_h

    return cur_y


def _draw_totals_box_right(
    c: canvas.Canvas,
    *,
    x_right: float,
    y_top: float,
    rows: List[Tuple[str, str, bool]],  # (label, value, bold)
    box_w: float,
    scale: float = 1.0,
) -> float:
    pad_x = 2.2 * mm
    pad_y = 1.8 * mm
    row_h = (5.0 * scale) * mm

    box_h = pad_y * 2 + row_h * len(rows)
    x0 = x_right - box_w
    y0 = y_top

    # Outer border
    c.setStrokeColor(GRID)
    c.setLineWidth(0.8)
    c.rect(x0, y0 - box_h, box_w, box_h, stroke=1, fill=0)

    # Inner horizontal lines
    c.setStrokeColor(GRID_SOFT)
    c.setLineWidth(0.6)
    for i in range(1, len(rows)):
        yy = y0 - pad_y - (row_h * i) + (row_h * 0.15)
        c.line(x0, yy, x0 + box_w, yy)

    # Text rows
    yy = y0 - pad_y - (row_h * 0.75)
    for (label, value, bold) in rows:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.0 * scale)
        c.drawString(x0 + pad_x, yy, f"{label} :")
        c.drawRightString(x0 + box_w - pad_x, yy, str(value))
        yy -= row_h

    return y0 - box_h - 3.0 * mm


def _draw_section_bar(c: canvas.Canvas,
                      *,
                      x: float,
                      y: float,
                      w: float,
                      title: str,
                      scale: float = 1.0) -> float:
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9.4 * scale)
    c.drawString(x, y, (title or "").strip().upper())

    y2 = y - (2.2 * mm)
    c.setStrokeColor(GRID)
    c.setLineWidth(0.7)
    c.line(x, y2, x + w, y2)

    return y2 - (3.2 * mm)


class _NumberedCanvas(canvas.Canvas):
    """
    Govt-form footer: Printed Date/Time, Printed By, Page X of Y
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

        self.setFont("Helvetica", 6.2)
        self.setFillColor(colors.black)

        if self._printed_at:
            self.drawString(M, y, f"Printed Date / Time : {self._printed_at}")

        if self._printed_by:
            self.drawCentredString(W / 2, y,
                                   f"Printed By : {self._printed_by}")

        self.drawRightString(W - M, y,
                             f"Page {self.getPageNumber()} of {total_pages}")


# =========================================================
# Invoice KV card (compact govt format)
# =========================================================
def _draw_invoice_kv_card(
    c: canvas.Canvas,
    header_payload: Dict[str, Any],
    x: float,
    y_top: float,
    w: float,
    payer_label: str,
    *,
    scale: float = 1.0,
) -> float:
    bill = (header_payload or {}).get("bill", {}) or {}
    pat = (header_payload or {}).get("patient", {}) or {}
    et = _safe((header_payload or {}).get("encounter_type"))
    enc = (header_payload or {}).get("encounter", {}) or {}
    payer = (header_payload or {}).get("payer", {}) or {}

    card_h = (30.0 * scale) * mm
    pad = 2.2 * mm

    # Outer box (no fill)
    c.setStrokeColor(GRID)
    c.setLineWidth(0.8)
    c.rect(x, y_top - card_h, w, card_h, stroke=1, fill=0)

    # Column split (55/45)
    split_x = x + w * 0.55
    c.setStrokeColor(GRID_SOFT)
    c.setLineWidth(0.6)
    c.line(split_x, y_top - card_h, split_x, y_top)

    def draw_pair(xx: float, yy: float, k: str, v: str, max_w: float):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.2 * scale)
        c.drawString(xx, yy, f"{k}:")
        c.setFont("Helvetica", 8.2 * scale)
        c.drawString(xx + 20 * mm, yy,
                     _clip_text(_safe(v), "Helvetica", 8.2 * scale, max_w))

    left_x = x + pad
    right_x = split_x + pad
    row_y = y_top - (6.4 * scale) * mm

    # Left side
    draw_pair(left_x, row_y, "Patient", pat.get("Patient Name"),
              (w * 0.55) - 22 * mm)
    row_y -= (4.6 * scale) * mm
    draw_pair(left_x, row_y, "UHID", pat.get("UHID"), (w * 0.55) - 22 * mm)
    row_y -= (4.6 * scale) * mm
    draw_pair(left_x, row_y, "Phone", pat.get("Phone"), (w * 0.55) - 22 * mm)
    row_y -= (4.6 * scale) * mm

    if et == "IP":
        draw_pair(left_x, row_y, "Ward", enc.get("Ward"), (w * 0.55) - 22 * mm)
        row_y -= (4.6 * scale) * mm
        draw_pair(left_x, row_y, "Room/Bed",
                  f"{_safe(enc.get('Room'))} / {_safe(enc.get('Bed'))}",
                  (w * 0.55) - 22 * mm)
    elif et == "OP":
        draw_pair(left_x, row_y, "Doctor", enc.get("Doctor"),
                  (w * 0.55) - 22 * mm)
        row_y -= (4.6 * scale) * mm
        draw_pair(left_x, row_y, "Dept", enc.get("Department"),
                  (w * 0.55) - 22 * mm)
    else:
        draw_pair(left_x, row_y, "Encounter", et, (w * 0.55) - 22 * mm)

    # Right side
    ry = y_top - (6.4 * scale) * mm
    draw_pair(right_x, ry, "Bill No", bill.get("Bill Number"),
              (w * 0.45) - 22 * mm)
    ry -= (4.6 * scale) * mm
    draw_pair(right_x, ry, "Bill Date", bill.get("Bill Date"),
              (w * 0.45) - 22 * mm)
    ry -= (4.6 * scale) * mm

    payer_mode = _safe(payer.get("Payer Mode"))
    payer_name = _safe(payer.get("Payer"))
    show_payer = payer_name if payer_name != "—" else (
        payer_mode if payer_mode != "—" else payer_label)
    draw_pair(right_x, ry, "Payer", show_payer, (w * 0.45) - 22 * mm)
    ry -= (4.6 * scale) * mm
    draw_pair(right_x, ry, "Type", et, (w * 0.45) - 22 * mm)

    return y_top - card_h - 3 * mm


# =========================================================
# Detail collection + Pharmacy split-up (Bill No removed)
# =========================================================
def _collect_detail_rows(
        invoices: List[BillingInvoice]) -> Dict[str, List[Dict[str, Any]]]:
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

    out: Dict[str, List[Dict[str, Any]]] = {}

    for inv in invoices:
        if getattr(inv, "status", None) == DocStatus.VOID:
            continue

        mod = (getattr(inv, "module", None) or "MISC").strip().upper()
        grp = label_for_module(mod)

        for ln in list(getattr(inv, "lines", []) or []):
            meta = _meta(getattr(ln, "meta_json", None))
            if meta.get("is_void") is True or meta.get("is_deleted") is True:
                continue

            dt = getattr(ln, "service_date", None) or getattr(
                inv, "service_date", None) or getattr(inv, "created_at", None)
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
        invoices: List[BillingInvoice]) -> List[List[str]]:
    """
    Bill Date | Item Name | Batch No | Expiry Date | Qty | Item Amount
    ✅ Bill No removed (shown in header).
    """
    rows: List[List[str]] = []

    for inv in invoices:
        mod = (getattr(inv, "module", "") or "").upper()
        if mod not in ("PHM", "PHARM", "PHARMACY", "RX", "MED"):
            continue

        bill_date = _invoice_bill_date(inv)

        for ln in list(getattr(inv, "lines", []) or []):
            meta = _meta(getattr(ln, "meta_json", None))

            batch_no = _safe(
                getattr(ln, "batch_no", None)
                or getattr(ln, "batch_number", None)
                or getattr(ln, "batch", None))
            if batch_no == "—":
                batch_no = _meta_pick(meta, [
                    "batch_no", "batchNo", "batch_number", "batchNumber",
                    "batch", "batch_id", "batchId"
                ], "—")

            exp_raw = _safe(
                getattr(ln, "expiry_date", None)
                or getattr(ln, "exp_date", None)
                or getattr(ln, "expiry", None))
            if exp_raw == "—":
                exp_raw = _meta_pick(meta, [
                    "expiry_date", "expiryDate", "expiry", "exp_date",
                    "expDate", "exp"
                ], "—")
            exp = _fmt_ddmmyyyy(exp_raw) if exp_raw != "—" else "—"

            qty = _safe(getattr(ln, "qty", None))
            amt = _money(getattr(ln, "net_amount", 0))

            rows.append([
                bill_date,
                _safe(getattr(ln, "description", None)), batch_no, exp, qty,
                amt
            ])

    return rows


# =========================================================
# Insurance + Advance rows (for full history)
# =========================================================
def _load_insurance_block(db: Session, case: BillingCase,
                          invoices: List[BillingInvoice]) -> Dict[str, Any]:
    payer_company = "—"
    approval_no = "—"

    payer = _payer_block(db, case) or {}
    if _safe(payer.get("Payer")) != "—":
        payer_company = _safe(payer.get("Payer"))

    if BillingInsuranceCase is not None:
        try:
            ins = (db.query(BillingInsuranceCase).filter(
                BillingInsuranceCase.billing_case_id == case.id).order_by(
                    BillingInsuranceCase.id.desc()).first())
            if ins is not None:
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

    insurer_sum = Decimal("0")
    for inv in invoices:
        for ln in list(getattr(inv, "lines", []) or []):
            v1 = _dec(getattr(ln, "insurer_pay_amount", 0))
            v2 = _dec(getattr(ln, "approved_amount", 0))
            insurer_sum += (v1 if v1 > 0 else v2)

    return {
        "company": payer_company,
        "approval_no": approval_no,
        "amount": _money(insurer_sum)
    }


def _advance_ledger_rows(db: Session, case_id: int,
                         overview_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deposit Summary:
    Deposit Date | Reference No | Actual Amt | Consumed Amt | Refund Amt | Balance Amt
    ✅ Columns fixed (previously mismatch).
    """
    adv_rows = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case_id).order_by(
            BillingAdvance.entry_at.asc()).all())

    total_in = Decimal("0")
    total_ref = Decimal("0")

    consumed = _dec((overview_payload.get("totals")
                     or {}).get("advance_consumed", 0))
    available = _dec((overview_payload.get("advance_summary_row")
                      or {}).get("available", 0))

    rows: List[List[str]] = []
    running_balance = Decimal("0")

    for a in adv_rows:
        dt = _fmt_dt(
            getattr(a, "entry_at", None) or getattr(a, "created_at", None))
        ref = _safe(
            getattr(a, "reference_no", None) or getattr(a, "ref_no", None)
            or getattr(a, "receipt_number", None)
            or getattr(a, "txn_ref", None)
            or getattr(a, "advance_number", None)
            or getattr(a, "voucher_number", None) or getattr(a, "id", None))
        et = getattr(a, "entry_type", None)
        amt = _dec(getattr(a, "amount", 0))

        actual_amt = Decimal("0")
        refund_amt = Decimal("0")
        if _eq_enum(et, AdvanceType.ADVANCE):
            total_in += amt
            actual_amt = amt
            running_balance += amt
        elif _eq_enum(et, AdvanceType.REFUND):
            total_ref += amt
            refund_amt = amt
            running_balance -= amt
        else:
            # adjustment treated as signed
            running_balance += amt
            actual_amt = amt if amt > 0 else Decimal("0")
            refund_amt = abs(amt) if amt < 0 else Decimal("0")

        # We show overall consumed/available summary below; per-row consumed/balance as running wallet only
        rows.append([
            dt,
            ref,
            _money(actual_amt),
            "—",
            _money(refund_amt),
            _money(running_balance),
        ])

    # Add a final summary row (govt-style)
    if rows:
        rows.append([
            "—",
            "TOTAL",
            _money(total_in),
            _money(consumed),
            _money(total_ref),
            _money(available),
        ])

    return {
        "rows": rows,
        "total_in": _money(total_in),
        "total_refund": _money(total_ref),
        "consumed": _money(consumed),
        "available": _money(available),
    }


# =========================================================
# Titles
# =========================================================
def _bill_kind_title(case: BillingCase) -> Tuple[str, str]:
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


# =========================================================
# Invoice PDF renderer (Govt bill style) – supports A3/A4/A5 + landscape
# =========================================================
def _render_invoices_by_case_pdf_reportlab(
    *,
    db: Session,
    case: BillingCase,
    invoices: List[BillingInvoice],
    branding: Optional[UiBranding],
    paper: str,
    orientation: str,
    printed_by: str = "",
) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    printed_at = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    c = _NumberedCanvas(buf,
                        pagesize=size,
                        printed_at=printed_at,
                        printed_by=printed_by)

    def new_page(title: str, header_payload: Dict[str, Any]) -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)
        y = H - M
        y = _draw_branding_header_small(c, branding, x0, y, w0, scale=scale)
        y -= 1.5 * mm

        c.setFont("Helvetica-Bold", 10.2 * scale)
        c.setFillColor(colors.black)
        c.drawCentredString(x0 + w0 / 2, y, (title or "").strip().upper())
        y -= 4.8 * mm

        y = _draw_invoice_kv_card(c,
                                  header_payload,
                                  x0,
                                  y,
                                  w0,
                                  payer_label="—",
                                  scale=scale)
        return y

    if not invoices:
        # still produce a valid PDF with a message
        hp = _build_header_payload(db,
                                   case,
                                   doc_no=_safe(
                                       getattr(case, "case_number", None)),
                                   doc_date=None)
        y = new_page("INVOICES (NO DATA)", hp)
        c.setFont("Helvetica", 9.6 * scale)
        c.setFillColor(INK)
        c.drawString(x0, y - 6 * mm, "No invoices found for this case.")
        c.save()
        return buf.getvalue()

    for idx, inv in enumerate(invoices, 1):
        if idx > 1:
            c.showPage()

        doc_no = _invoice_bill_no(inv)
        doc_date = _invoice_bill_date_obj(inv)

        # header payload from case but with invoice doc_no/doc_date
        hp = _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)

        mod = (getattr(inv, "module", None) or "GENERAL").strip().upper()
        title = f"TAX INVOICE / BILL OF SUPPLY  ( {mod} )"

        y = new_page(title, hp)

        # build item rows
        item_rows: List[List[str]] = []
        lines = list(getattr(inv, "lines", []) or [])
        for i, ln in enumerate(lines, 1):
            desc = _safe(getattr(ln, "description", None))
            qty = _safe(getattr(ln, "qty", None))

            rate = getattr(ln, "unit_price", None) or getattr(
                ln, "rate", None) or getattr(ln, "price", None)
            rate_s = _money(rate) if rate not in (None, "", "—") else "—"

            amt = getattr(ln, "net_amount", None)
            if amt is None:
                amt = getattr(ln, "amount", None)
            amt_s = _money(amt or 0)

            item_rows.append([str(i), desc, qty, rate_s, amt_s])

        if not item_rows:
            item_rows = [["1", "—", "—", "—", "0.00"]]

        # table sizes per page
        row_h = (6.5 * scale) * mm
        head_size = 8.8 * scale
        body_size = 8.7 * scale
        pad_x = 1.8 * mm
        pad_y = (1.2 * scale) * mm
        lead = (9.6 * scale)

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[
                ("S.No", 0.08),
                ("Item / Service", 0.52),
                ("Qty", 0.12),
                ("Rate", 0.14),
                ("Amount", 0.14),
            ],
            rows=item_rows,
            row_h=row_h,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title, hp),
            aligns=["center", "left", "right", "right", "right"],
            max_lines=2,
            head_size=head_size,
            body_size=body_size,
            pad_x=pad_x,
            pad_y=pad_y,
            lead=lead,
        )
        y -= 2.5 * mm

        sub = _dec(getattr(inv, "sub_total", 0))
        disc = _dec(getattr(inv, "discount_total", 0))
        tax = _dec(getattr(inv, "tax_total", 0))
        roff = _dec(getattr(inv, "round_off", 0))
        grand = _dec(getattr(inv, "grand_total", (sub - disc + tax + roff)))

        tot_rows = [
            ("Sub Total", _money(sub), False),
            ("Discount", _money(disc), False),
            ("Tax", _money(tax), False),
            ("Round Off", _money(roff), False),
            ("Grand Total", _money(grand), True),
        ]

        need_h = (2.2 * mm * 2) + (5.2 * scale * mm * len(tot_rows)) + 10 * mm
        if y - need_h < bottom:
            c.showPage()
            y = new_page(title, hp)

        y = _draw_totals_box_right(
            c,
            x_right=x0 + w0,
            y_top=y,
            rows=tot_rows,
            box_w=min(88 * mm, w0 * 0.58),
            scale=scale,
        )

        # words
        words = _amount_in_words_inr(grand)
        if y - 14 * mm < bottom:
            c.showPage()
            y = new_page(title, hp)

        c.setFont("Helvetica-Bold", 9.0 * scale)
        c.setFillColor(INK)
        c.drawString(x0, y, "In Words :")
        c.setFont("Helvetica", 9.0 * scale)
        c.setFillColor(MUTED)

        yy = y
        for ln in simpleSplit(words, "Helvetica", 9.0 * scale,
                              w0 - 18 * mm)[:3]:
            c.drawString(x0 + 16 * mm, yy, ln)
            yy -= (4.4 * scale) * mm
        y = yy - 2.0 * mm

        # Signature area (govt-style)
        if y - 22 * mm < bottom:
            c.showPage()
            y = new_page(title, hp)

        c.setStrokeColor(GRID)
        c.setLineWidth(0.8)
        sig_h = 18 * mm
        c.rect(x0, y - sig_h, w0 * 0.52, sig_h, stroke=1, fill=0)
        c.rect(x0 + w0 * 0.52 + 6 * mm,
               y - sig_h,
               w0 * 0.48 - 6 * mm,
               sig_h,
               stroke=1,
               fill=0)

        c.setFont("Helvetica", 9.0 * scale)
        c.setFillColor(INK)
        c.drawString(x0 + 3 * mm, y - 6 * mm, "Patient / Attender Signature")
        c.drawString(x0 + 3 * mm, y - 12 * mm, "Name & Relationship")

        c.setFont("Helvetica", 9.0 * scale)
        c.drawString(x0 + w0 * 0.52 + 9 * mm, y - 6 * mm,
                     "For Hospital / Authorized Signatory")

    c.save()
    return buf.getvalue()


# =========================================================
# Full History PDF (Summary + Detail + Deposit + Pharmacy)
# =========================================================
def _render_full_history_pdf_reportlab(
    *,
    db: Session,
    case: BillingCase,
    invoices: List[BillingInvoice],
    branding: Optional[UiBranding],
    overview_payload: Dict[str, Any],
    paper: str,
    orientation: str,
    printed_by: str = "",
) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    printed_at = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    c = _NumberedCanvas(buf,
                        pagesize=size,
                        printed_at=printed_at,
                        printed_by=printed_by)

    base, kind = _bill_kind_title(case)
    bill_no = _safe(getattr(case, "case_number", None))

    header_payload = _build_header_payload(db,
                                           case,
                                           doc_no=bill_no,
                                           doc_date=None)

    def new_page(title: str) -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)
        y = H - M
        y = _draw_branding_header(c, branding, x0, y, w0, scale=scale)
        y -= 1.5 * mm

        c.setFont("Helvetica-Bold", 10.5 * scale)
        c.setFillColor(colors.black)
        c.drawCentredString(x0 + w0 / 2, y, (title or "").strip().upper())
        y -= 4.8 * mm

        y = _draw_patient_header_block(c,
                                       header_payload,
                                       x0,
                                       y,
                                       w0,
                                       scale=scale)
        y -= 3.0 * mm
        return y

    # =======================
    # SUMMARY PAGE
    # =======================
    title1 = f"{base} SUMMARY BILL - {kind}"
    y = new_page(title1)

    modules = overview_payload.get("modules") or []
    sum_rows = [[_safe(m.get("label")),
                 _money(m.get("total"))] for m in modules] or [["—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[("Particulars", 0.72), ("Amount", 0.28)],
        rows=sum_rows,
        row_h=(7.0 * scale) * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page(title1),
        aligns=["left", "right"],
        max_lines=2,
        head_size=9.2 * scale,
        body_size=9.1 * scale,
        lead=11.2 * scale,
    )
    y -= 3.0 * mm

    totals = overview_payload.get("totals") or {}
    total_bill = _dec(totals.get("total_bill", 0))
    taxable = _dec(totals.get("taxable_value", 0))
    gst = _dec(totals.get("gst", 0))
    round_off = _dec(totals.get("round_off", 0))
    total_amt = total_bill

    pre_round = total_amt - round_off
    exempted = pre_round if (taxable == 0 and gst == 0) else Decimal("0")

    tot_rows = [
        ("Exempted Value", _money(exempted), False),
        ("Taxable Value", _money(taxable), False),
        ("GST", _money(gst), False),
        ("Round Off", _money(round_off), False),
        ("Total Bill Amount", _money(total_amt), True),
    ]

    need_h = (2.2 * mm * 2) + ((5.2 * scale) * mm * len(tot_rows)) + 6 * mm
    if y - need_h < bottom:
        c.showPage()
        y = new_page(title1)

    y = _draw_totals_box_right(c,
                               x_right=x0 + w0,
                               y_top=y,
                               rows=tot_rows,
                               box_w=min(92 * mm, w0 * 0.58),
                               scale=scale)

    # Payment details block
    pay_rows = overview_payload.get("payment_details") or []
    if pay_rows:
        if y - 28 * mm < bottom:
            c.showPage()
            y = new_page(title1)

        y -= 3.5 * mm
        y = _draw_section_bar(c,
                              x=x0,
                              y=y,
                              w=w0,
                              title="Payment Details",
                              scale=scale)

        pr = []
        total_pay = Decimal("0")
        for p in pay_rows:
            amt = _dec(p.get("amount", 0))
            total_pay += amt
            pr.append([
                _safe(p.get("receipt_number")),
                _safe(p.get("mode")),
                _safe(p.get("date")),
                _money(amt)
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Receipt No", 0.30), ("Paymode", 0.18), ("Date", 0.22),
                  ("Amount", 0.30)],
            rows=pr,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title1),
            aligns=["left", "left", "left", "right"],
            max_lines=2,
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )

        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.2 * scale)
        c.setFillColor(INK)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Payment Received :")
        c.drawRightString(x0 + w0, y, _money(total_pay))

    # Insurance details
    ins = _load_insurance_block(db, case, invoices)
    if _safe(ins.get("company")) != "—" and _safe(
            ins.get("amount")) not in ("0.00", "—"):
        if y - 22 * mm < bottom:
            c.showPage()
            y = new_page(title1)

        y -= 4.0 * mm
        c.setFont("Helvetica-Bold", 9.8 * scale)
        c.setFillColor(INK)
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
            cols=[("Company", 0.54), ("Approval No", 0.26), ("Amount", 0.20)],
            rows=ins_rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title1),
            aligns=["left", "left", "right"],
            max_lines=2,
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )

    # =======================
    # DETAIL BILL
    # =======================
    c.showPage()
    title2 = f"{base} DETAIL BILL OF SUPPLY - {kind}"
    y = new_page(title2)

    col_part = 0.62
    col_date = 0.14
    col_qty = 0.10
    col_amt = 0.14

    def draw_detail_header(yy: float) -> float:
        h = (7.0 * scale) * mm
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.9)
        c.rect(x0, yy - h, w0, h, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 9.2 * scale)
        c.setFillColor(INK)

        c.drawString(x0 + 2 * mm, yy - h + 2.2 * mm, "Particulars")
        c.drawString(x0 + w0 * col_part + 2 * mm, yy - h + 2.2 * mm, "Date")
        c.drawRightString(x0 + w0 * (col_part + col_date + col_qty) - 2 * mm,
                          yy - h + 2.2 * mm, "Quantity")
        c.drawRightString(x0 + w0 - 2 * mm, yy - h + 2.2 * mm, "Total Amount")

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

        y = ensure_space(y, 10 * mm)
        y -= 2.8 * mm
        c.setFont("Helvetica-Bold", 9.6 * scale)
        c.setFillColor(INK)
        c.drawString(x0 + 1.0 * mm, y, grp)
        y -= 2.0 * mm
        c.setLineWidth(0.6)
        c.line(x0, y, x0 + 55 * mm, y)
        y -= 2.5 * mm

        row_h = (6.0 * scale) * mm
        for r in items:
            y = ensure_space(y, row_h + 2 * mm)

            c.setStrokeColor(colors.black)
            c.setLineWidth(0.5)
            c.rect(x0, y - row_h, w0, row_h, stroke=1, fill=0)

            vx1 = x0 + w0 * col_part
            vx2 = x0 + w0 * (col_part + col_date)
            vx3 = x0 + w0 * (col_part + col_date + col_qty)
            c.line(vx1, y - row_h, vx1, y)
            c.line(vx2, y - row_h, vx2, y)
            c.line(vx3, y - row_h, vx3, y)

            c.setFont("Helvetica", 9.0 * scale)
            c.setFillColor(INK)

            desc = (r.get("desc") or "—")
            max_w = w0 * col_part - 4 * mm
            desc = _clip_text(desc, "Helvetica", 9.0 * scale, max_w)
            c.drawString(x0 + 2 * mm, y - row_h + 2.0 * mm, desc)

            c.drawString(vx1 + 2 * mm, y - row_h + 2.0 * mm,
                         _safe(r.get("date")))
            c.drawRightString(vx3 - 2 * mm, y - row_h + 2.0 * mm,
                              _safe(r.get("qty")))
            c.drawRightString(x0 + w0 - 2 * mm, y - row_h + 2.0 * mm,
                              _safe(r.get("amt")))

            y -= row_h

        y -= 2.0 * mm

    # =======================
    # FINAL PAGE: Deposit + Bill Abstract
    # =======================
    c.showPage()
    title3 = f"{base} BILL ABSTRACT - {kind}"
    y = new_page(title3)

    adv = _advance_ledger_rows(db, case.id, overview_payload)
    rows = adv.get("rows") or []
    if rows:
        c.setFont("Helvetica-Bold", 9.8 * scale)
        c.setFillColor(INK)
        c.drawString(x0, y, "DEPOSIT SUMMARY")
        y -= 4.0 * mm

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[
                ("Deposit Date", 0.20),
                ("Reference No", 0.20),
                ("Actual Amt", 0.14),
                ("Consumed Amt", 0.14),
                ("Refund Amt", 0.14),
                ("Balance Amt", 0.18),
            ],
            rows=rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title3),
            aligns=["left", "left", "right", "right", "right", "right"],
            max_lines=2,
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )
        y -= 4.0 * mm

    effective_paid = _dec((overview_payload.get("totals")
                           or {}).get("effective_paid", 0))
    balance = _dec((overview_payload.get("totals") or {}).get("balance", 0))
    insurer_amt = _dec((_load_insurance_block(db, case, invoices)
                        or {}).get("amount", 0))

    if y - 40 * mm < bottom:
        c.showPage()
        y = new_page(title3)

    box_w = w0 * 0.52
    box_h = 26 * mm
    c.setLineWidth(0.8)
    c.rect(x0, y - box_h, box_w, box_h, stroke=1, fill=0)

    c.setFont("Helvetica", 9.0 * scale)
    c.setFillColor(INK)
    c.drawString(x0 + 3 * mm, y - 6 * mm, "Patient / Attender signature")
    c.drawString(x0 + 3 * mm, y - 12 * mm, "Name & Relationship")
    c.drawString(x0 + 3 * mm, y - 18 * mm, "Contact Number")

    rx = x0 + box_w + 8 * mm
    rw = w0 - (box_w + 8 * mm)
    c.rect(rx, y - box_h, rw, box_h, stroke=1, fill=0)

    c.setFont("Helvetica-Bold", 9.2 * scale)
    c.drawString(rx + 3 * mm, y - 6 * mm, "Bill Abstract :")
    c.setFont("Helvetica", 9.0 * scale)
    c.drawRightString(rx + rw - 3 * mm, y - 6 * mm, f"{_money(total_amt)}")

    c.setFont("Helvetica-Bold", 9.0 * scale)
    c.drawString(rx + 3 * mm, y - 12 * mm, "Less Payment Received :")
    c.drawRightString(rx + rw - 3 * mm, y - 12 * mm, _money(effective_paid))

    c.drawString(rx + 3 * mm, y - 18 * mm, "Balance Amount :")
    c.drawRightString(rx + rw - 3 * mm, y - 18 * mm, _money(balance))

    if insurer_amt > 0:
        c.drawString(rx + 3 * mm, y - 24 * mm,
                     "Net Payable by Insurance Company :")
        c.drawRightString(rx + rw - 3 * mm, y - 24 * mm, _money(insurer_amt))

    y -= (box_h + 10 * mm)

    bal_words = _amount_in_words_inr(balance)
    c.setFont("Helvetica-Bold", 9.0 * scale)
    c.drawString(x0, y, "Balance Amount in Words :")
    c.setFont("Helvetica", 9.0 * scale)
    c.drawString(x0 + 46 * mm, y,
                 _clip_text(bal_words, "Helvetica", 9.0 * scale, w0 - 48 * mm))
    y -= 8 * mm

    # =======================
    # PHARMACY SPLIT UP
    # =======================
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
                ("Bill Date", 0.14),
                ("Item Name", 0.44),
                ("Batch No", 0.14),
                ("Expiry Date", 0.12),
                ("Qty", 0.06),
                ("Item Amount", 0.10),
            ],
            rows=ph_rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page(title4),
            aligns=["left", "left", "left", "left", "right", "right"],
            max_lines=2,
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )

    c.save()
    return buf.getvalue()


# =========================================================
# Other PDFs (Overview / Ledgers / Insurance) – also responsive
# =========================================================
def _render_common_header_pdf_reportlab(payload: Dict[str, Any],
                                        branding: Optional[UiBranding],
                                        paper: str, orientation: str) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)

    _draw_page_border(c, W=W, H=H, pad=border_pad)

    y = H - M
    y = _draw_branding_header(c, branding, x0, y, w0, scale=scale)
    y -= 3 * mm
    _draw_patient_header_block(c, payload, x0, y, w0, scale=scale)

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_overview_pdf_reportlab(
    payload: Dict[str, Any],
    branding: Optional[UiBranding],
    paper: str,
    orientation: str,
) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    paper_u = (paper or "A4").upper()
    is_a5 = (paper_u == "A5"
             and (orientation or "portrait").lower() != "landscape")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)

    def new_page() -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)

        y0 = H - M

        # ✅ A5 uses compact branding header (more vertical space, cleaner)
        if is_a5:
            y0 = _draw_branding_header_small(c,
                                             branding,
                                             x0,
                                             y0,
                                             w0,
                                             scale=scale)
            y0 -= 1.0 * mm
            c.setFont("Helvetica-Bold", 10.0 * scale)
            c.setFillColor(INK)
            c.drawCentredString(x0 + w0 / 2, y0, "BILL SUMMARY")
            y0 -= 4.2 * mm
            y0 = _draw_patient_header_block_a5(c,
                                               payload,
                                               x0,
                                               y0,
                                               w0,
                                               scale=scale)
            y0 -= 2.4 * mm
            return y0

        # A4/A3 keep your richer header block
        y0 = _draw_branding_header(c, branding, x0, y0, w0, scale=scale)
        y0 -= 2 * mm
        c.setFont("Helvetica-Bold", 10.0 * scale)
        c.setFillColor(colors.black)
        c.drawCentredString(x0 + w0 / 2, y0, "BILL SUMMARY")
        y0 -= 5 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0, scale=scale)
        y0 -= 4 * mm
        return y0

    y = new_page()

    # Particulars rows
    modules = payload.get("modules") or []
    part_rows = [[_safe(m.get("label")),
                  _money(m.get("total"))] for m in modules] or [["—", "0.00"]]

    # ✅ A5 uses tighter table geometry (pixel-fit)
    if is_a5:
        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Particulars", 0.70), ("Amount", 0.30)],
            rows=part_rows,
            row_h=(6.2 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=new_page,
            aligns=["left", "right"],
            max_lines=1,
            head_size=8.6 * scale,
            body_size=8.4 * scale,
            pad_x=2.0 * mm,
            pad_y=1.2 * mm,
            lead=9.6 * scale,
        )
        y -= 2.5 * mm
    else:
        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Particulars", 0.72), ("Amount", 0.28)],
            rows=part_rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=new_page,
            aligns=["left", "right"],
            max_lines=2,
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )
        y -= 3 * mm

    totals = payload.get("totals") or {}
    total_bill_d = _dec(totals.get("total_bill", 0))
    taxable_d = _dec(totals.get("taxable_value", 0))
    gst_d = _dec(totals.get("gst", 0))
    round_d = _dec(totals.get("round_off", 0))

    pre_round = total_bill_d - round_d
    exempted_d = pre_round if (taxable_d == 0 and gst_d == 0) else Decimal("0")

    tot_rows = [
        ("Exempted Value", _money(exempted_d), False),
        ("Taxable Value", _money(taxable_d), False),
        ("GST", _money(gst_d), False),
        ("Round Off", _money(round_d), False),
        ("Total Bill Amount", _money(total_bill_d), True),
    ]

    # space check
    need_h = (2.2 * mm * 2) + ((5.0 * scale) * mm * len(tot_rows)) + 10 * mm
    if y - need_h < bottom:
        c.showPage()
        y = new_page()

    # ✅ A5: totals box uses full width for perfect alignment
    if is_a5:
        y = _draw_totals_box_right(
            c,
            x_right=x0 + w0,
            y_top=y,
            rows=tot_rows,
            box_w=w0,
            scale=scale,
        )
        y -= 1.5 * mm
    else:
        y = _draw_totals_box_right(
            c,
            x_right=x0 + w0,
            y_top=y,
            rows=tot_rows,
            box_w=min(92 * mm, w0 * 0.58),
            scale=scale,
        )

    # ✅ A5: Balance pill (clean + readable)
    balance_d = _dec(totals.get("balance", 0))
    bal_words = _amount_in_words_inr(balance_d)

    if is_a5:
        if y - (18 * mm) < bottom:
            c.showPage()
            y = new_page()
        y = _draw_balance_pill_a5(
            c,
            x=x0,
            y=y,
            w=w0,
            label="Balance Payable",
            value=_money(balance_d),
            words=bal_words,
            scale=scale,
        )
    else:
        # A4/A3 keep your simple divider style
        if y - 16 * mm < bottom:
            c.showPage()
            y = new_page()
        y -= 3.0 * mm
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.8)
        c.line(x0, y, x0 + w0, y)
        y -= 5.0 * mm
        c.setFont("Helvetica-Bold", 9.8 * scale)
        c.setFillColor(INK)
        c.drawRightString(x0 + w0 - 42 * mm, y, "Total Balance Amount :")
        c.drawRightString(x0 + w0, y, _money(balance_d))

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_payments_ledger_pdf_reportlab(payload: Dict[str, Any],
                                          branding: Optional[UiBranding],
                                          payments_payload: Dict[str, Any],
                                          paper: str,
                                          orientation: str) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)

    def new_page(title: str) -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0, scale=scale)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0, scale=scale)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0 * scale)
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
        cols=[("Receipt No", 0.18), ("Mode", 0.12), ("Kind", 0.18),
              ("Dir", 0.08), ("Date/Time", 0.26), ("Amount", 0.18)],
        rows=rows,
        row_h=(7.0 * scale) * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page("PAYMENT RECEIPTS LEDGER (Cont.)"),
        head_size=9.2 * scale,
        body_size=9.1 * scale,
        lead=11.2 * scale,
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
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4 * scale)
        c.drawRightString(x0 + w0 - 42 * mm, y, label)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.4 * scale)
        c.drawRightString(x0 + w0, y, val)
        y -= 5 * mm

    line("Total Received :", _money(t.get("received")), bold=True)
    line("Total Refunds :", _money(t.get("refunds")))
    line("Net Received :", _money(t.get("net")), bold=True)

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_advance_ledger_pdf_reportlab(payload: Dict[str, Any],
                                         branding: Optional[UiBranding],
                                         adv_payload: Dict[str,
                                                           Any], paper: str,
                                         orientation: str) -> bytes:
    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)

    def new_page(title: str) -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0, scale=scale)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0, scale=scale)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0 * scale)
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
            _money(r.get("amount"))
        ])
    if not rows:
        rows = [["—", "—", "—", "—", "0.00"]]

    y = _draw_simple_table(
        c,
        x=x0,
        y=y,
        w=w0,
        cols=[("Date/Time", 0.22), ("Type", 0.14), ("Reference", 0.20),
              ("Note", 0.28), ("Amount", 0.16)],
        rows=rows,
        row_h=(7.0 * scale) * mm,
        bottom_margin=bottom,
        new_page_fn=lambda: new_page("ADVANCE LEDGER (Cont.)"),
        head_size=9.2 * scale,
        body_size=9.1 * scale,
        lead=11.2 * scale,
    )

    c.showPage()
    c.save()
    return buf.getvalue()


def _render_insurance_pdf_reportlab(payload: Dict[str, Any],
                                    branding: Optional[UiBranding],
                                    ins_payload: Dict[str, Any], paper: str,
                                    orientation: str) -> bytes:
    if not ins_payload or (not ins_payload.get("insurance_case")
                           and not ins_payload.get("preauths")
                           and not ins_payload.get("claims")):
        return b""

    lay = _layout_for(paper, orientation)
    size = lay["size"]
    W, H = lay["W"], lay["H"]
    M, x0, w0 = lay["M"], lay["x0"], lay["w0"]
    bottom = lay["bottom"]
    scale = lay["scale"]
    border_pad = lay["border_pad"]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=size)

    def new_page(title: str) -> float:
        _draw_page_border(c, W=W, H=H, pad=border_pad)
        y0 = H - M
        y0 = _draw_branding_header(c, branding, x0, y0, w0, scale=scale)
        y0 -= 2 * mm
        y0 = _draw_patient_header_block(c, payload, x0, y0, w0, scale=scale)
        y0 -= 3 * mm
        c.setFont("Helvetica-Bold", 10.0 * scale)
        c.drawCentredString(x0 + w0 / 2, y0, title)
        y0 -= 5 * mm
        return y0

    y = new_page("INSURANCE")

    ic = ins_payload.get("insurance_case")
    if ic:
        c.setFont("Helvetica-Bold", 9.6 * scale)
        c.setFillColor(INK)
        c.drawString(x0, y, "Insurance Case")
        y -= 4.5 * mm

        def kv(label: str, val: str):
            nonlocal y
            c.setFont("Helvetica-Bold", 9.0 * scale)
            c.drawString(x0, y, f"{label} :")
            c.setFont("Helvetica", 9.0 * scale)
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
        c.setFont("Helvetica-Bold", 9.8 * scale)
        c.drawString(x0, y, "PREAUTH REQUESTS")
        y -= 4 * mm

        rows = []
        for p in preauths:
            rows.append([
                _safe(p.get("preauth_no")),
                _safe(p.get("status")),
                _safe(p.get("date")),
                _safe(p.get("requested")),
                _safe(p.get("approved"))
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Preauth No", 0.22), ("Status", 0.16), ("Date", 0.26),
                  ("Requested", 0.18), ("Approved", 0.18)],
            rows=rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page("INSURANCE (Preauth Cont.)"),
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )

    claims = ins_payload.get("claims") or []
    if claims:
        if y - 20 * mm < bottom:
            c.showPage()
            y = new_page("INSURANCE (Claims)")
        c.setFont("Helvetica-Bold", 9.8 * scale)
        c.drawString(x0, y, "CLAIMS")
        y -= 4 * mm

        rows = []
        for cl in claims:
            rows.append([
                _safe(cl.get("claim_no")),
                _safe(cl.get("status")),
                _safe(cl.get("date")),
                _safe(cl.get("claimed")),
                _safe(cl.get("approved"))
            ])

        y = _draw_simple_table(
            c,
            x=x0,
            y=y,
            w=w0,
            cols=[("Claim No", 0.22), ("Status", 0.16), ("Date", 0.26),
                  ("Claimed", 0.18), ("Approved", 0.18)],
            rows=rows,
            row_h=(7.0 * scale) * mm,
            bottom_margin=bottom,
            new_page_fn=lambda: new_page("INSURANCE (Claims Cont.)"),
            head_size=9.2 * scale,
            body_size=9.1 * scale,
            lead=11.2 * scale,
        )

    c.showPage()
    c.save()
    return buf.getvalue()


# =========================================================
# Endpoints
# =========================================================
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
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_header_payload(db, case, doc_no=doc_no, doc_date=doc_date)
    pdf_bytes = _render_common_header_pdf_reportlab(payload, branding, paper,
                                                    orientation)

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
        include_draft_invoices=include_draft_invoices)


@router.get("/overview")
def billing_overview_pdf(
        case_id: int = Query(..., gt=0),
        doc_no: Optional[str] = Query(None),
        doc_date: Optional[date] = Query(None),
        include_draft_invoices: bool = Query(True),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
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
        include_draft_invoices=include_draft_invoices)
    pdf_bytes = _render_overview_pdf_reportlab(payload, branding, paper,
                                               orientation)

    filename = f"Billing_Overview_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ✅ FIX: invoice_id is PATH param + supports A3/A4/A5 + landscape
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

    inv = _load_invoice(db, invoice_id)

    # Branding (tenant safe)
    branding_q = db.query(UiBranding)
    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)
    if tenant_id is not None and hasattr(UiBranding, "tenant_id"):
        branding_q = branding_q.filter(UiBranding.tenant_id == tenant_id)
    if hasattr(UiBranding, "is_active"):
        branding_q = branding_q.filter(UiBranding.is_active.is_(True))
    branding = branding_q.order_by(UiBranding.id.desc()).first()

    # Load case + patient for header
    cid = getattr(inv, "billing_case_id", None) or getattr(
        getattr(inv, "billing_case", None), "id", None)
    if not cid:
        raise HTTPException(status_code=409,
                            detail="Invoice is not linked to a billing case")
    case = _load_case(db, int(cid))

    invoices = [inv]
    pdf_bytes = _render_invoices_by_case_pdf_reportlab(
        db=db,
        case=case,
        invoices=invoices,
        branding=branding,
        paper=paper,
        orientation=orientation,
        printed_by=_safe(getattr(user, "name", None)),
    )

    filename = f"Invoice_{_invoice_bill_no(inv)}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ✅ NEW (IMPORTANT): Invoices by Case (this was missing)
@router.get("/cases/{case_id}/invoices/pdf")
def billing_case_invoices_pdf(
        case_id: int = FPath(..., gt=0),
        include_draft_invoices: bool = Query(True),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])

    case = _load_case(db, int(case_id))

    # Branding (tenant safe)
    branding_q = db.query(UiBranding)
    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)
    if tenant_id is not None and hasattr(UiBranding, "tenant_id"):
        branding_q = branding_q.filter(UiBranding.tenant_id == tenant_id)
    if hasattr(UiBranding, "is_active"):
        branding_q = branding_q.filter(UiBranding.is_active.is_(True))
    branding = branding_q.order_by(UiBranding.id.desc()).first()

    invoices = _list_case_invoices(
        db, case.id, include_draft_invoices=include_draft_invoices)

    pdf_bytes = _render_invoices_by_case_pdf_reportlab(
        db=db,
        case=case,
        invoices=invoices,
        branding=branding,
        paper=paper,
        orientation=orientation,
        printed_by=_safe(getattr(user, "name", None)),
    )

    filename = f"Invoices_Case_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ✅ Full History (Govt form): Summary + Detail + Deposit + Pharmacy split-up
@router.get("/cases/{case_id}/history/pdf")
def billing_case_full_history_pdf(
        case_id: int = FPath(..., gt=0),
        include_draft_invoices: bool = Query(True),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])

    case = _load_case(db, int(case_id))

    branding_q = db.query(UiBranding)
    tenant_id = getattr(user, "tenant_id", None) or getattr(
        user, "hospital_id", None)
    if tenant_id is not None and hasattr(UiBranding, "tenant_id"):
        branding_q = branding_q.filter(UiBranding.tenant_id == tenant_id)
    if hasattr(UiBranding, "is_active"):
        branding_q = branding_q.filter(UiBranding.is_active.is_(True))
    branding = branding_q.order_by(UiBranding.id.desc()).first()

    invoices = _list_case_invoices(
        db, case.id, include_draft_invoices=include_draft_invoices)
    overview_payload = _build_overview_payload(
        db,
        case,
        doc_no=_safe(getattr(case, "case_number", None)),
        doc_date=None,
        include_draft_invoices=include_draft_invoices)

    pdf_bytes = _render_full_history_pdf_reportlab(
        db=db,
        case=case,
        invoices=invoices,
        branding=branding,
        overview_payload=overview_payload,
        paper=paper,
        orientation=orientation,
        printed_by=_safe(getattr(user, "name", None)),
    )

    filename = f"Billing_History_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


# ✅ Ledgers + Insurance (optional, but consistent govt-print UI)
@router.get("/cases/{case_id}/payments-ledger/pdf")
def billing_case_payments_ledger_pdf(
        case_id: int = FPath(..., gt=0),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, int(case_id))
    branding = _load_branding(db)

    header_payload = _build_header_payload(db,
                                           case,
                                           doc_no=_safe(
                                               getattr(case, "case_number",
                                                       None)),
                                           doc_date=None)
    pay_payload = _build_payments_ledger_payload(db, case)
    pdf_bytes = _render_payments_ledger_pdf_reportlab(header_payload, branding,
                                                      pay_payload, paper,
                                                      orientation)

    filename = f"Payments_Ledger_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.get("/cases/{case_id}/advance-ledger/pdf")
def billing_case_advance_ledger_pdf(
        case_id: int = FPath(..., gt=0),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, int(case_id))
    branding = _load_branding(db)

    header_payload = _build_header_payload(db,
                                           case,
                                           doc_no=_safe(
                                               getattr(case, "case_number",
                                                       None)),
                                           doc_date=None)
    adv_payload = _build_advance_ledger_payload(db, case)
    pdf_bytes = _render_advance_ledger_pdf_reportlab(header_payload, branding,
                                                     adv_payload, paper,
                                                     orientation)

    filename = f"Advance_Ledger_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.get("/cases/{case_id}/insurance/pdf")
def billing_case_insurance_pdf(
        case_id: int = FPath(..., gt=0),
        disposition: str = Query("inline", pattern="^(inline|attachment)$"),
        paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
        orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["billing.view"])
    case = _load_case(db, int(case_id))
    branding = _load_branding(db)

    header_payload = _build_header_payload(db,
                                           case,
                                           doc_no=_safe(
                                               getattr(case, "case_number",
                                                       None)),
                                           doc_date=None)
    ins_payload = _try_load_insurance_payload(db, case)
    pdf_bytes = _render_insurance_pdf_reportlab(header_payload, branding,
                                                ins_payload, paper,
                                                orientation)

    if not pdf_bytes:
        raise HTTPException(status_code=404,
                            detail="No insurance data available for this case")

    filename = f"Insurance_{_safe(getattr(case, 'case_number', None))}.pdf"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


def _pdf_response(data: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/cases/{case_id}/bill-summary")
def bill_summary(
        case_id: int,
        paper: str = Query(default="A5"),
        orientation: str = Query(default="portrait"),
        include_draft_invoices: bool = Query(default=True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, [
        "billing.print", "billing.view", "billing.cases.view",
        "billing.case.view", "billing.invoices.view"
    ])
    case = _load_case(db, case_id)
    branding = _load_branding(db)

    payload = _build_overview_payload(
        db,
        case,
        include_draft_invoices=include_draft_invoices,
        doc_no=_safe(getattr(case, "case_number", None)),
        doc_date=None,
    )

    try:
        pdf = _render_overview_pdf_reportlab(payload, branding, paper,
                                             orientation)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bill_summary render failed case_id=%s", case_id)
        raise HTTPException(status_code=500,
                            detail=f"Failed to render Bill Summary: {e}")

    return _pdf_response(
        pdf, f"bill_summary_case_{case_id}_{paper}_{orientation}.pdf")
