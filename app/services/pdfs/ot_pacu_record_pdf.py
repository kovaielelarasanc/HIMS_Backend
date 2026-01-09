# FILE: app/services/pdfs/ot_pacu_record_pdf.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date
from typing import Any, Dict, List, Optional
import os
import re
import logging

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

# Reuse your existing "government form black" primitives
from app.services.pdfs.ot_anaesthesia_record_pdf import (
    IST,
    BLACK,
    TEXT,
    _get,
    _as_str,
    _fmt_date,
    _rect_form,
    _hline_form,
    _vline_form,
    _draw_brand_letterhead_template,
    _draw_footer,
    _ellipsize,
    _draw_tick,
    _block_text_form,  # ✅ import once
)

logger = logging.getLogger(__name__)


# -------------------------
# Deep getters / parsing
# -------------------------
def _deep_get(obj: Any, *paths: str, default: Any = None) -> Any:
    """
    Supports:
      - direct fields: "procedure_name"
      - nested paths: "procedure.name", "theatre_master.name", "patient.uhid"
    Works with dicts and objects.
    """
    if obj is None:
        return default

    for p in paths:
        if not p:
            continue

        if "." not in p:
            v = _get(obj, p, default=None)
            if v not in (None, ""):
                return v
            continue

        cur = obj
        ok = True
        for part in p.split("."):
            if cur is None:
                ok = False
                break
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = getattr(cur, part, None)
        if ok and cur not in (None, ""):
            return cur

    return default


def _as_name(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return _as_str(v)

    vv = _deep_get(
        v,
        "name",
        "title",
        "display_name",
        "label",
        "procedure_name",
        "theatre_name",
        "room_name",
        default=None,
    )
    if vv not in (None, "") and vv is not v:
        return _as_str(vv)

    return _as_str(v)


def _as_list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, dict):
        out: List[str] = []
        for k, vv in v.items():
            if vv in (True, 1, "1", "true", "True", "yes", "on", "checked"):
                ks = str(k).strip()
                if ks:
                    out.append(ks)
        return out
    if isinstance(v, str):
        parts = re.split(r"[,;\n]+", v.replace("\r", "\n"))
        return [p.strip() for p in parts if p.strip()]
    return [str(v).strip()] if str(v).strip() else []


def _norm_pick(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def _selected(items: Optional[List[str]], key: str) -> bool:
    if not items:
        return False
    kk = _norm_pick(key)
    if not kk:
        return False
    return any(_norm_pick(str(x)) == kk for x in items)


# -------------------------
# DOB → Age helper
# -------------------------
_DDMMYYYY_RE = re.compile(r"^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$")


def _to_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except Exception:
            pass
        m = _DDMMYYYY_RE.match(s)
        if m:
            dd, mm_, yy = m.groups()
            try:
                return date(int(yy), int(mm_), int(dd))
            except Exception:
                return None
    return None


def _age_years_from_dob(dob: Any) -> str:
    d = _to_date(dob)
    if not d:
        return ""
    today = datetime.now(IST).date()
    years = today.year - d.year - (1 if (today.month,
                                         today.day) < (d.month, d.day) else 0)
    return str(years) if years >= 0 else ""


# -------------------------
# Pickers
# -------------------------
def _pick_patient_obj(patient_fields: Dict[str, Any], case: Any) -> Any:
    if isinstance(patient_fields, dict):
        p = patient_fields.get("patient") or patient_fields.get(
            "patient_obj") or patient_fields.get("patient_details")
        if p:
            return p
    return _deep_get(
        case,
        "patient",
        "patient_obj",
        "patient_detail",
        "patient_details",
        "patient_info",
        "patient_profile",
        default=None,
    )


def _pick_patient_name(patient_fields: Dict[str, Any], case: Any) -> str:
    p = _pick_patient_obj(patient_fields, case)

    prefix = _as_str(
        patient_fields.get("patient_prefix") or patient_fields.get("prefix")
        or _deep_get(p, "prefix", "title", "salutation", default=""))

    name = _as_str(
        patient_fields.get("name") or patient_fields.get("patient_name")
        or patient_fields.get("full_name") or _deep_get(
            p, "full_name", "name", "patient_name", "display_name",
            default=""))

    if not name:
        fn = _as_str(_deep_get(p, "first_name", "firstname", default=""))
        ln = _as_str(_deep_get(p, "last_name", "lastname", default=""))
        name = (f"{fn} {ln}").strip()

    if prefix and name and not name.lower().startswith(prefix.lower()):
        return f"{prefix} {name}".strip()
    return name


def _pick_uhid(patient_fields: Dict[str, Any], case: Any) -> str:
    p = _pick_patient_obj(patient_fields, case)
    return _as_str(
        patient_fields.get("uhid") or patient_fields.get("uhid_no")
        or patient_fields.get("uhid_number") or _deep_get(p,
                                                          "uhid",
                                                          "uhid_no",
                                                          "uhid_number",
                                                          "patient_id",
                                                          "uid",
                                                          default=""))


def _pick_age_sex(patient_fields: Dict[str, Any], case: Any) -> str:
    v = _as_str(
        patient_fields.get("age_sex") or patient_fields.get("age/sex")
        or patient_fields.get("ageSex"))
    if v:
        return v

    p = _pick_patient_obj(patient_fields, case)

    age = _as_str(
        patient_fields.get("age")
        or _deep_get(p, "age", "age_years", default=""))

    if not age:
        dob = patient_fields.get("dob") or patient_fields.get(
            "date_of_birth") or _deep_get(
                p, "dob", "date_of_birth", "birth_date", default=None)
        age = _age_years_from_dob(dob)

    sex = _as_str(
        patient_fields.get("sex") or patient_fields.get("gender")
        or _deep_get(p, "sex", "gender", default=""))

    if age and sex:
        return f"{age}/{sex}"
    return age or sex


def _pick_case_no(patient_fields: Dict[str, Any], case: Any) -> str:
    return _as_str(
        patient_fields.get("case_no")
        or _deep_get(case, "case_no", "case_number", "id", default=""))


def _pick_or_no(patient_fields: Dict[str, Any], case: Any) -> str:
    v = (patient_fields.get("or_no") or patient_fields.get("or") or _deep_get(
        case,
        "or_no",
        "ot_room.name",
        "or_room.name",
        "theatre.name",
        "theatre_master.name",
        default="",
    ))
    return _as_name(v)


# -------------------------
# UI helpers
# -------------------------
def _box_label(c: canvas.Canvas,
               x: float,
               y: float,
               txt: str,
               fs: float = 8.4,
               bold: bool = True):
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", fs)
    c.drawString(x, y, txt)
    c.restoreState()


def _checkbox(c: canvas.Canvas,
              x: float,
              y: float,
              label: str,
              checked: bool,
              fs: float = 8.2):
    box = 3.2 * mm
    c.saveState()
    c.setStrokeColor(BLACK)
    c.setLineWidth(1.0)
    c.rect(x, y - 2.4, box, box, stroke=1, fill=0)
    if checked:
        _draw_tick(c, x, (y - 2.4), size=box, lw=1.15)
    c.setFillColor(TEXT)
    c.setFont("Helvetica", fs)
    c.drawString(x + box + 2.0 * mm, y, label)
    c.restoreState()


def _draw_patient_strip_simple(c: canvas.Canvas, x: float, y_top: float,
                               w: float, patient_fields: Dict[str,
                                                              Any], case: Any):
    h = 10 * mm
    _rect_form(c, x, y_top, w, h, lw=1.0)

    name = _pick_patient_name(patient_fields, case)
    uhid = _pick_uhid(patient_fields, case)
    age_sex = _pick_age_sex(patient_fields, case)
    case_no = _pick_case_no(patient_fields, case)
    or_no = _pick_or_no(patient_fields, case)

    cols = [("Patient", name), ("UHID", uhid), ("Age/Sex", age_sex),
            ("Case", case_no), ("OR", or_no)]
    col_w = [w * 0.34, w * 0.18, w * 0.14, w * 0.18, w * 0.16]

    y_lab = y_top - 3.8 * mm
    y_val = y_top - 8.0 * mm

    xx = x
    c.saveState()
    c.setFillColor(TEXT)
    for (lab, val), cw in zip(cols, col_w):
        c.setFont("Helvetica-Bold", 8.0)
        c.drawString(xx + 2 * mm, y_lab, lab)
        c.setFont("Helvetica", 8.4)
        c.drawString(xx + 2 * mm, y_val,
                     _ellipsize(_as_str(val), cw - 4 * mm, "Helvetica", 8.4))
        xx += cw
        if xx < x + w - 0.1:
            _vline_form(c, xx, y_top, y_top - h, lw=0.9)
    c.restoreState()
    return y_top - h


# -------------------------
# Main PDF builder (✅ GRAPH COLUMN REMOVED)
# -------------------------
def build_ot_pacu_record_pdf_bytes(
    *,
    branding: Any,
    case: Any,
    patient_fields: Dict[str, Any],
    anaesthesia_record: Any,
    pacu_record: Any,
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    mx, my = 10 * mm, 10 * mm
    x = mx
    w = page_w - 2 * mx

    debug_on = str(os.getenv("PDF_DEBUG_PACU",
                             "")).strip().lower() in ("1", "true", "yes", "on")

    # Letterhead
    y_line = _draw_brand_letterhead_template(c, page_w, page_h, mx, my,
                                             branding)

    # Title
    y_title = y_line - 6 * mm
    c.saveState()
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 14.0)
    c.drawCentredString(x + w / 2, y_title, "POST OPERATIVE RECOVERY RECORD")
    c.restoreState()
    _hline_form(c, x, x + w, y_title - 4.5 * mm, lw=1.0)

    # Patient strip
    y = _draw_patient_strip_simple(c, x, y_title - 7.5 * mm, w, patient_fields,
                                   case) - 4 * mm

    # Outer form box
    bottom_y = my + 12 * mm
    form_top = y
    form_h = form_top - bottom_y
    _rect_form(c, x, form_top, w, form_h, lw=1.2)

    # ------------------------------------------------------------
    # Row 1: Date + times
    # ------------------------------------------------------------
    row1_h = 14 * mm
    y1 = form_top
    y1b = y1 - row1_h
    _hline_form(c, x, x + w, y1b, lw=1.0)

    x_split = x + w * 0.55
    _vline_form(c, x_split, y1, y1b, lw=1.0)

    sched = getattr(case, "schedule", None)

    date_val = _fmt_date(
        patient_fields.get("date")
        or _get(sched, "date", "scheduled_date", "scheduled_at", default=None)
        or _get(case, "ot_date", "scheduled_date", "created_at", default=None))
    _box_label(c, x + 2.5 * mm, y1 - 5.2 * mm, f"DATE : {date_val}", fs=9.0)

    t_recovery = _as_str(_get(pacu_record, "time_to_recovery", default=""))
    t_ward = _as_str(
        _get(pacu_record,
             "time_to_ward_icu",
             "time_to_ward",
             "time_to_icu",
             default=""))

    _box_label(c,
               x_split + 2.5 * mm,
               y1 - 5.2 * mm,
               f"Time to RECOVERY : {t_recovery}",
               fs=9.0)
    _box_label(c,
               x_split + 2.5 * mm,
               y1 - 10.3 * mm,
               f"Time to WARD / ICU : {t_ward}",
               fs=9.0,
               bold=False)

    # ------------------------------------------------------------
    # Row 2: Operation/Theatre/Anaesthesia | Airway Support | Monitoring
    # ------------------------------------------------------------
    row2_h = 28 * mm
    y2 = y1b
    y2b = y2 - row2_h
    _hline_form(c, x, x + w, y2b, lw=1.0)

    x_left_end = x + w * 0.62
    x_mid_end = x + w * 0.84
    _vline_form(c, x_left_end, y2, y2b, lw=1.0)
    _vline_form(c, x_mid_end, y2, y2b, lw=1.0)

    theater = getattr(sched, "theater", None) if sched else None

    operation = _as_str(
        patient_fields.get("proposed_operation")
        or patient_fields.get("operation")
        or _get(sched, "procedure_name", default=None)
        or _get(case, "final_procedure_name", "procedure_name", default=""))

    theatre = _as_str(
        patient_fields.get("or_no") or _get(sched, "or_no", default=None)
        or _get(theater, "name", "theater_no", default=None)
        or _get(case, "theatre", "ot_room", "or_no", default=""))

    _box_label(c,
               x + 2.5 * mm,
               y2 - 5.0 * mm,
               f"OPERATION : {operation}",
               fs=9.0)
    _box_label(c,
               x + 2.5 * mm,
               y2 - 10.2 * mm,
               f"THEATRE : {theatre}",
               fs=9.0,
               bold=False)

    _box_label(c, x + 2.5 * mm, y2 - 15.5 * mm, "ANAESTHESIA :", fs=9.0)

    methods = _as_list(
        _get(pacu_record,
             "anaesthesia_methods",
             "anaesthesia_method",
             default=None))
    _checkbox(c, x + 28 * mm, y2 - 15.5 * mm, "GA / MAC",
              _selected(methods, "GA/MAC"))
    _checkbox(c, x + 28 * mm, y2 - 20.2 * mm, "SPINAL / EPIDURAL",
              _selected(methods, "Spinal/Epidural"))
    _checkbox(c, x + 28 * mm, y2 - 24.9 * mm, "NERVE / PLEXUS BLOCK",
              _selected(methods, "Nerve/Plexus Block"))

    _box_label(c,
               x_left_end + 2.5 * mm,
               y2 - 5.0 * mm,
               "AIRWAY SUPPORT :",
               fs=9.0)
    airway = _as_list(_get(pacu_record, "airway_support", default=None))
    _checkbox(c, x_left_end + 2.5 * mm, y2 - 10.0 * mm, "None",
              _selected(airway, "None"))
    _checkbox(c, x_left_end + 2.5 * mm, y2 - 14.7 * mm, "Face Mask / Airway",
              _selected(airway, "Face Mask/Airway"))
    _checkbox(c, x_left_end + 2.5 * mm, y2 - 19.4 * mm, "LMA",
              _selected(airway, "LMA"))
    _checkbox(c, x_left_end + 2.5 * mm, y2 - 24.1 * mm, "Intubated",
              _selected(airway, "Intubated"))
    _checkbox(c, x_left_end + 2.5 * mm + 32 * mm, y2 - 24.1 * mm, "O2",
              _selected(airway, "O2"))

    _box_label(c, x_mid_end + 2.5 * mm, y2 - 5.0 * mm, "MONITORING :", fs=9.0)
    mon = _as_list(_get(pacu_record, "monitoring", default=None))
    _checkbox(c, x_mid_end + 2.5 * mm, y2 - 10.0 * mm, "SPO2",
              _selected(mon, "SpO2"))
    _checkbox(c, x_mid_end + 2.5 * mm, y2 - 14.7 * mm, "NIBP",
              _selected(mon, "NIBP"))
    _checkbox(c, x_mid_end + 2.5 * mm, y2 - 19.4 * mm, "ECG",
              _selected(mon, "ECG"))
    _checkbox(c, x_mid_end + 2.5 * mm, y2 - 24.1 * mm, "CVP",
              _selected(mon, "CVP"))

    # ------------------------------------------------------------
    # BODY (FULL WIDTH) ✅ (Graph removed completely)
    # ------------------------------------------------------------
    body_top = y2b
    body_bottom = bottom_y

    avail = body_top - body_bottom

    nurse_h = 16 * mm
    checks_h = 44 * mm
    iv_h = 28 * mm
    min_instr = 30 * mm

    instr_h = avail - (nurse_h + checks_h + iv_h)
    if instr_h < min_instr:
        deficit = min_instr - instr_h
        # reduce checks first (keep minimum)
        min_checks = 34 * mm
        can_reduce_checks = max(0, checks_h - min_checks)
        r1 = min(deficit, can_reduce_checks)
        checks_h -= r1
        deficit -= r1
        # reduce IV next (keep minimum)
        min_iv = 22 * mm
        can_reduce_iv = max(0, iv_h - min_iv)
        r2 = min(deficit, can_reduce_iv)
        iv_h -= r2
        deficit -= r2
        instr_h = avail - (nurse_h + checks_h + iv_h)

    # ---- Nurse row
    yN_top = body_top
    yN_bot = yN_top - nurse_h
    _hline_form(c, x, x + w, yN_bot, lw=1.0)

    nurse_obj = _get(pacu_record, "nurse", default=None)
    nurse_name = _as_str(
        _deep_get(nurse_obj, "full_name", "name", "display_name", default=""))

    _box_label(c, x + 2.5 * mm, yN_top - 6.0 * mm, "NURSE :", fs=9.0)
    c.saveState()
    c.setFont("Helvetica", 8.6)
    c.setFillColor(TEXT)
    c.drawString(x + 22 * mm, yN_top - 6.0 * mm,
                 _ellipsize(nurse_name, w - 40 * mm, "Helvetica", 8.6))
    c.drawRightString(x + w - 2.5 * mm, yN_top - 11.2 * mm, "Signature")
    c.restoreState()

    # ---- Checklists row (2 columns)
    yC_top = yN_bot
    yC_bot = yC_top - checks_h
    _hline_form(c, x, x + w, yC_bot, lw=1.0)

    mid = x + w * 0.50
    _vline_form(c, mid, yC_top, yC_bot, lw=1.0)

    # Left column: POST OP CHARTS
    _box_label(c, x + 2.5 * mm, yC_top - 6.0 * mm, "POST OP CHARTS :", fs=9.0)
    charts = _as_list(_get(pacu_record, "post_op_charts", default=None))

    lx = x + 4.0 * mm
    ly = yC_top - 12.0 * mm
    _checkbox(c, lx, ly, "1. DIABETIC CHART",
              _selected(charts, "Diabetic Chart"))
    _checkbox(c, lx, ly - 4.7 * mm, "2. I.V. FLUIDS",
              _selected(charts, "I.V. Fluids"))
    _checkbox(c, lx, ly - 9.4 * mm, "3. ANALGESIA",
              _selected(charts, "Analgesia"))
    _checkbox(c, lx, ly - 14.1 * mm, "4. PCA CHART",
              _selected(charts, "PCA Chart"))

    # Right column: TUBES / DRAINS
    _box_label(c,
               mid + 2.5 * mm,
               yC_top - 6.0 * mm,
               "TUBES / DRAINS :",
               fs=9.0)
    td = _as_list(
        _get(pacu_record, "tubes_drains", "tubes_and_drains", default=None))

    rx = mid + 4.0 * mm
    ry = yC_top - 12.0 * mm
    _checkbox(c, rx, ry, "WOUND DRAINS", _selected(td, "Wound Drains"))
    _checkbox(c, rx, ry - 5.0 * mm, "URINARY CATHETER",
              _selected(td, "Urinary Catheter"))
    _checkbox(c, rx, ry - 10.0 * mm, "NG TUBE", _selected(td, "NG Tube"))
    _checkbox(c, rx, ry - 15.0 * mm, "IRRIGATION", _selected(td, "Irrigation"))

    # ---- IV Fluids section (full width)
    yIV_top = yC_bot
    yIV_bot = yIV_top - iv_h
    _hline_form(c, x, x + w, yIV_bot, lw=1.0)

    _box_label(c, x + 2.5 * mm, yIV_top - 6.0 * mm, "I.V. FLUIDS :", fs=9.0)
    _rect_form(c,
               x + 2.5 * mm,
               yIV_top - 9.0 * mm,
               w - 5.0 * mm, (yIV_top - yIV_bot) - 12.0 * mm,
               lw=0.9)

    iv_txt = _as_str(
        _get(pacu_record, "iv_fluids_orders", "iv_fluids", default=""))
    if iv_txt:
        _block_text_form(
            c,
            x + 2.5 * mm,
            yIV_top - 9.0 * mm,
            w - 5.0 * mm,
            (yIV_top - yIV_bot) - 12.0 * mm,
            iv_txt,
            fs=8.2,
            pad_top=5.0 * mm,
            pad_bottom=2.6 * mm,
            pad_x=2.6 * mm,
        )

    # ---- POST OP INSTRUCTIONS (full width, remaining area)
    yI_top = yIV_bot
    yI_bot = body_bottom

    _box_label(c,
               x + 2.5 * mm,
               yI_top - 6.0 * mm,
               "POST OP INSTRUCTIONS :",
               fs=9.0)
    _rect_form(c,
               x + 2.5 * mm,
               yI_top - 9.0 * mm,
               w - 5.0 * mm, (yI_top - yI_bot) - 10.0 * mm,
               lw=0.9)

    instr = _as_str(
        _get(pacu_record, "post_op_instructions", "instructions", default=""))
    if instr:
        _block_text_form(
            c,
            x + 2.5 * mm,
            yI_top - 9.0 * mm,
            w - 5.0 * mm,
            (yI_top - yI_bot) - 10.0 * mm,
            instr,
            fs=8.2,
            pad_top=5.0 * mm,
            pad_bottom=2.6 * mm,
            pad_x=2.6 * mm,
        )

    # server log (confirm values)
    try:
        picked = {
            "patient": _pick_patient_name(patient_fields, case),
            "uhid": _pick_uhid(patient_fields, case),
            "age_sex": _pick_age_sex(patient_fields, case),
            "case_no": _pick_case_no(patient_fields, case),
            "or_no": _pick_or_no(patient_fields, case),
            "operation": operation,
            "theatre": theatre,
        }
        logger.info("PACU PDF resolved fields: %s", picked)
    except Exception:
        pass

    # Footer
    _draw_footer(c, page_w, my, page_no=1)
    c.save()
    return buf.getvalue()
