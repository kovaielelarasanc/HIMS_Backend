# FILE: app/services/pdf/ot_safety_checklist_premium.py
from __future__ import annotations

from io import BytesIO
from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics

from app.core.config import settings
from app.models.ui_branding import UiBranding

IST = ZoneInfo("Asia/Kolkata")

# ---- Exact Form Colors (match screenshot) ----
BLUE = colors.HexColor("#1F4DB4")  # border + header bar
BLUE_DARK = colors.HexColor("#1B43A3")  # slightly darker for bar feel
TEXT = colors.black


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


def _fmt_date(v: Any) -> str:
    dt = _to_ist(_to_dt(v))
    if dt:
        return dt.strftime("%d-%b-%Y")
    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")
    return ""


def _calc_age(dob: Optional[date]) -> str:
    if not dob:
        return ""
    today = date.today()
    years = today.year - dob.year - (
        (today.month, today.day) < (dob.month, dob.day))
    return str(years)


# -----------------------------
# Resolvers (case/schedule/patient)
# -----------------------------
def _resolve_schedule(case: Any) -> Any:
    return (_get(case, "schedule", "ot_schedule", "schedule_obj", default=None)
            or _get(case, "ot_case_schedule", default=None) or None)


def _resolve_patient_any(case: Any, payload: Dict[str, Any]) -> Any:
    p = payload.get("patient") if isinstance(payload, dict) else None
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


def _resolve_op_no(case: Any, schedule: Any) -> str:
    v = (_get(schedule, "op_no", "op_number", "visit_no", default=None)
         or _get(_get(schedule, "visit", "opd_visit", default=None),
                 "op_no",
                 "op_number",
                 default=None)
         or _get(_get(case, "visit", "opd_visit", default=None),
                 "op_no",
                 "op_number",
                 default=None) or None)
    return (str(v).strip() if v else "")


def _resolve_patient_fields(case: Any, payload: Dict[str,
                                                     Any]) -> Dict[str, str]:
    payload = payload or {}
    schedule = _resolve_schedule(case)
    patient = _resolve_patient_any(case, payload)

    admission = None
    if schedule:
        admission = _get(schedule,
                         "admission",
                         "ipd_admission",
                         "admission_obj",
                         default=None)
    if admission is None:
        admission = _get(case,
                         "admission",
                         "ipd_admission",
                         "ipd",
                         default=None)

    name = (
        _get(patient,
             "full_name",
             "display_name",
             "name",
             "patient_name",
             default="") or _get(payload, "patient_name", "name", default="")
        or
        (f"{_get(patient,'prefix','title',default='')}".strip() + " " +
         f"{_get(patient,'first_name','given_name',default='')}".strip() +
         " " + f"{_get(patient,'last_name','family_name',default='')}".strip()
         ).strip() or _get(
             case, "patient_name", "patient_full_name", default="")).strip()

    sex = str(
        _get(patient, "sex", "gender", "sex_label", default="")
        or _get(payload, "sex", "gender", default="")
        or _get(case, "sex", "gender", default="") or "").strip()

    age = str(
        _get(patient, "age_display", "age", "age_years", default="")
        or "").strip()
    if not age:
        dob = _get(patient, "dob", "date_of_birth", default=None) or _get(
            payload, "dob", default=None)
        if isinstance(dob, date):
            age = _calc_age(dob)

    age_sex = ""
    if age and sex:
        age_sex = f"{age} / {sex}"
    elif age:
        age_sex = age
    elif sex:
        age_sex = sex

    uhid = (str(
        _get(patient,
             "uhid",
             "uhid_number",
             "reg_no",
             "mrn",
             "patient_id",
             default="") or "").strip()
            or str(_get(payload, "uhid", "reg_no", "mrn", default="")
                   or "").strip()
            or str(
                _get(schedule, "patient_uhid", "uhid", "reg_no", default="")
                or "").strip() or
            str(_get(case, "uhid", "reg_no", "mrn", default="") or "").strip())

    ip_no = (str(
        _get(admission,
             "display_code",
             "ip_no",
             "ip_number",
             "ipd_no",
             "admission_no",
             default="") or "").strip()
             or str(_get(payload, "ip_no", "ip_number", default="")
                    or "").strip())

    op_no = _resolve_op_no(case, schedule)
    doa = _fmt_date(
        _get(admission,
             "admitted_at",
             "admission_at",
             "created_at",
             default=None))

    return {
        "name": name or "",
        "age_sex": age_sex or "",
        "uhid": uhid or "",
        "ip_no": ip_no or "",
        "op_no": op_no or "",
        "doa": doa or "",
    }


# -----------------------------
# Tiny drawing helpers
# -----------------------------
def _sw(txt: str, font: str, fs: float) -> float:
    return pdfmetrics.stringWidth(txt or "", font, fs)


def _norm_choice(v: Any) -> str:
    s = ("" if v is None else str(v)).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return "yes"
    if s in ("no", "n", "false", "0"):
        return "no"
    if s in ("na", "n/a", "not applicable", "not_applicable", "notapplicable"):
        return "na"
    return ""


def _checkbox(c: canvas.Canvas, x: float, y: float, size: float,
              checked: bool):
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1)
    c.rect(x, y, size, size, stroke=1, fill=0)
    if checked:
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", size * 0.8)
        c.drawCentredString(x + size / 2, y + size * 0.08, "✓")
    c.restoreState()


def _wrap(c: canvas.Canvas,
          txt: str,
          x: float,
          y_top: float,
          w: float,
          font="Helvetica",
          fs=7.6,
          leading=1.25) -> float:
    lines = simpleSplit(
        (txt or "").replace("\n", " ").strip(), font, fs, w) or [""]
    c.setFont(font, fs)
    c.setFillColor(TEXT)
    lead = fs * leading
    y = y_top
    for ln in lines:
        c.drawString(x, y, ln)
        y -= lead
    return y


def _checkbox_line(c: canvas.Canvas,
                   x: float,
                   y_top: float,
                   w: float,
                   text: str,
                   checked: bool,
                   fs=7.6) -> float:
    box = 3.6 * mm
    gap = 1.6 * mm
    # place checkbox slightly below baseline for better alignment
    _checkbox(c, x, y_top - box + 0.9 * mm, box, checked)
    tx = x + box + gap
    y_after = _wrap(c, text, tx, y_top, w - (box + gap), fs=fs)
    return y_after - 1.2 * mm


def _divider(c: canvas.Canvas, x: float, y: float, w: float):
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1)
    c.line(x, y, x + w, y)
    c.restoreState()


def _dotted_field(c: canvas.Canvas,
                  x: float,
                  y: float,
                  w: float,
                  label: str,
                  value: str,
                  fs=8):
    """
    Draws: LABEL : value ............ (dotted to end)  (exact screenshot feel)
    """
    label = (label or "").strip()
    value = (value or "").strip()

    c.setFillColor(TEXT)
    c.setFont("Helvetica", fs)
    c.drawString(x, y, label)

    lx = x + _sw(label, "Helvetica", fs) + 1.2 * mm

    c.setFont("Helvetica-Bold", fs)
    c.drawString(lx, y, value)

    vx = lx + _sw(value, "Helvetica-Bold", fs) + 1.5 * mm
    if vx < x + w:
        c.saveState()
        c.setStrokeColor(TEXT)
        c.setLineWidth(0.8)
        c.setDash(1, 2)
        c.line(vx, y - 1.2, x + w, y - 1.2)
        c.setDash()
        c.restoreState()


def _column_box(c: canvas.Canvas, x: float, y_top: float, w: float, h: float):
    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.2)
    c.roundRect(x, y_top - h, w, h, 8 * mm, stroke=1, fill=0)
    c.restoreState()


def _column_header_tab(c: canvas.Canvas, x: float, y_top: float, w: float,
                       title_lines: Tuple[str, ...], subtitle: str):
    tab_h = 16 * mm
    pad_x = 5 * mm
    tab_w = w - 2 * pad_x
    tab_x = x + pad_x
    tab_y_top = y_top - 5.5 * mm

    c.saveState()
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.2)
    c.roundRect(tab_x,
                tab_y_top - tab_h,
                tab_w,
                tab_h,
                6 * mm,
                stroke=1,
                fill=0)
    c.restoreState()

    # Title (center)
    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 8.4)
    ty = tab_y_top - 5.2 * mm
    for ln in title_lines:
        c.drawCentredString(tab_x + tab_w / 2, ty, ln)
        ty -= 4.1 * mm

    # Subtitle
    c.setFont("Helvetica", 7.0)
    c.drawCentredString(tab_x + tab_w / 2, tab_y_top - tab_h + 3.2 * mm,
                        subtitle)

    # content top y (below tab)
    return (tab_y_top - tab_h - 4.5 * mm)


# -----------------------------
# Main PDF builder (EXACT screenshot UI)
# -----------------------------
def build_ot_safety_checklist_pdf_bytes(
    *,
    branding: UiBranding,
    case: Any,
    safety_data: Dict[str, Any],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    safety_data = safety_data or {}
    fields = _resolve_patient_fields(case, safety_data)

    sign_in = safety_data.get("sign_in") or {}
    time_out = safety_data.get("time_out") or {}
    sign_out = safety_data.get("sign_out") or {}

    # margins
    mx = 10 * mm
    my = 10 * mm
    x0 = mx
    y = page_h - my
    w = page_w - 2 * mx

    # ---- Top Blue Bar ----
    bar_h = 10.5 * mm
    org = (str(_get(branding, "org_name", "hospital_name", default="")
               or "").strip() or "HOSPITAL").upper()

    c.saveState()
    c.setFillColor(BLUE_DARK)
    c.rect(x0, y - bar_h, w, bar_h, stroke=0, fill=1)
    c.restoreState()

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x0 + 5 * mm, y - bar_h + 3.1 * mm,
                 "SURGICAL SAFETY CHECK LIST")
    c.drawRightString(x0 + w - 5 * mm, y - bar_h + 3.1 * mm, org)

    y -= bar_h + 5.5 * mm

    # ---- Patient dotted lines (exact style) ----
    # Row 1: Name | Age & Sex
    row_fs = 8
    name_w = w * 0.70
    age_w = w - name_w - 6 * mm

    _dotted_field(c,
                  x0 + 2 * mm,
                  y,
                  name_w - 2 * mm,
                  "Name :",
                  fields["name"],
                  fs=row_fs)
    _dotted_field(c,
                  x0 + name_w + 4 * mm,
                  y,
                  age_w,
                  "Age & Sex :",
                  fields["age_sex"],
                  fs=row_fs)

    y -= 7.5 * mm

    # Row 2: UHID | DOA | IP No | Ward No
    seg1 = w * 0.33
    seg2 = w * 0.20
    seg3 = w * 0.22
    seg4 = w - seg1 - seg2 - seg3 - 8 * mm

    _dotted_field(c,
                  x0 + 2 * mm,
                  y,
                  seg1 - 2 * mm,
                  "UHID No. :",
                  fields["uhid"],
                  fs=row_fs)
    _dotted_field(c,
                  x0 + seg1 + 2 * mm,
                  y,
                  seg2,
                  "D.O.A :",
                  fields["doa"],
                  fs=row_fs)
    _dotted_field(c,
                  x0 + seg1 + seg2 + 4 * mm,
                  y,
                  seg3,
                  "IP No. :",
                  fields["ip_no"],
                  fs=row_fs)
    _dotted_field(c,
                  x0 + seg1 + seg2 + seg3 + 6 * mm,
                  y,
                  seg4,
                  "Ward No. :",
                  "",
                  fs=row_fs)

    y -= 10.0 * mm

    # ---- Three columns ----
    gap = 8 * mm
    col_w = (w - 2 * gap) / 3.0
    col_top = y
    sig_area = 34 * mm
    col_bottom = my + sig_area
    col_h = col_top - col_bottom

    cols_x = [x0, x0 + col_w + gap, x0 + 2 * (col_w + gap)]

    # outer boxes
    for cx in cols_x:
        _column_box(c, cx, col_top, col_w, col_h)

    # Column 1 header
    c1_top = _column_header_tab(
        c,
        cols_x[0],
        col_top,
        col_w,
        title_lines=("BEFORE INDUCTION", "OF ANAESTHESIA"),
        subtitle="(with at least nurse and anaesthetist)",
    )
    # Column 2 header
    c2_top = _column_header_tab(
        c,
        cols_x[1],
        col_top,
        col_w,
        title_lines=("BEFORE SKIN INCISION", ),
        subtitle="(with nurse, anaesthetist and surgeon)",
    )
    # Column 3 header
    c3_top = _column_header_tab(
        c,
        cols_x[2],
        col_top,
        col_w,
        title_lines=("BEFORE PATIENT LEAVES", "OPERATING ROOM"),
        subtitle="(with nurse, anaesthetist and surgeon)",
    )

    # ---------- Column 1 content ----------
    pad = 6 * mm
    x1 = cols_x[0] + pad
    w1 = col_w - 2 * pad
    y1 = c1_top

    y1 = _wrap(
        c,
        "Has the patient confirmed his/her identity, site,\nprocedure, and consent?",
        x1,
        y1,
        w1,
        fs=7.6)
    y1 -= 1.0 * mm
    y1 = _checkbox_line(
        c,
        x1,
        y1,
        w1,
        "Yes",
        _as_bool(sign_in.get("identity_site_procedure_consent_confirmed")),
        fs=7.6)

    y1 = _wrap(c, "Is the site marked?", x1, y1, w1, fs=7.6)
    site = _norm_choice(sign_in.get("site_marked"))
    y1 -= 1.0 * mm
    y1 = _checkbox_line(c, x1, y1, w1, "Yes", site == "yes", fs=7.6)
    y1 = _checkbox_line(c, x1, y1, w1, "Not applicable", site == "na", fs=7.6)

    y1 = _wrap(c,
               "Is the anaesthesia machine and medication check\ncomplete?",
               x1,
               y1,
               w1,
               fs=7.6)
    y1 -= 1.0 * mm
    y1 = _checkbox_line(
        c,
        x1,
        y1,
        w1,
        "Yes",
        _as_bool(sign_in.get("machine_and_medication_check_complete")),
        fs=7.6)

    y1 = _wrap(c, "Does the patient have a:", x1, y1, w1, fs=7.6)
    y1 = _wrap(c, "Known allergy?", x1, y1, w1, fs=7.6)
    allergy = _norm_choice(sign_in.get("known_allergy"))
    y1 -= 1.0 * mm
    y1 = _checkbox_line(c, x1, y1, w1, "No", allergy == "no", fs=7.6)
    y1 = _checkbox_line(c, x1, y1, w1, "Yes", allergy == "yes", fs=7.6)

    y1 = _wrap(c, "Difficult airway or aspiration risk?", x1, y1, w1, fs=7.6)
    daw = _norm_choice(sign_in.get("difficult_airway_or_aspiration_risk"))
    equip = _as_bool(sign_in.get("equipment_assistance_available"))
    y1 -= 1.0 * mm
    y1 = _checkbox_line(c, x1, y1, w1, "No", daw == "no", fs=7.6)
    y1 = _checkbox_line(c,
                        x1,
                        y1,
                        w1,
                        "Yes, and equipment /assistance available",
                        (daw == "yes") or (equip and daw == ""),
                        fs=7.6)

    y1 = _wrap(c,
               "Risk of >500ml blood loss (7ml/kg in children)?",
               x1,
               y1,
               w1,
               fs=7.6)
    bl = _norm_choice(sign_in.get("blood_loss_risk_gt500ml_or_7mlkg"))
    iv = _as_bool(sign_in.get("iv_central_access_and_fluids_planned"))
    y1 -= 1.0 * mm
    y1 = _checkbox_line(c, x1, y1, w1, "No", bl == "no", fs=7.6)
    y1 = _checkbox_line(c,
                        x1,
                        y1,
                        w1,
                        "Yes,and two IVs/central access and fluids planned",
                        (bl == "yes") or (iv and bl == ""),
                        fs=7.6)

    # ---------- Column 2 content ----------
    x2 = cols_x[1] + pad
    w2 = col_w - 2 * pad
    y2 = c2_top

    y2 = _checkbox_line(
        c,
        x2,
        y2,
        w2,
        "Confirm all team members have introduced\nthemselves by name and role.",
        _as_bool(time_out.get("team_members_introduced")),
        fs=7.6)
    y2 = _checkbox_line(
        c,
        x2,
        y2,
        w2,
        "Confirm the patient's name, procedure, and\nwhere the incision will be made.",
        _as_bool(
            time_out.get("patient_name_procedure_incision_site_confirmed")),
        fs=7.6)

    y2 = _wrap(
        c,
        "Has antibiotic prophylaxis been given within the\nlast 60 minutes?",
        x2,
        y2,
        w2,
        fs=7.6)
    abx = _norm_choice(time_out.get("antibiotic_prophylaxis_given"))
    y2 -= 1.0 * mm
    y2 = _checkbox_line(c, x2, y2, w2, "Yes", abx == "yes", fs=7.6)
    y2 = _checkbox_line(c, x2, y2, w2, "Not applicable", abx == "na", fs=7.6)

    y2 -= 1.5 * mm
    _divider(c, x2, y2, w2)
    y2 -= 4.0 * mm

    c.setFont("Helvetica-Bold", 7.8)
    c.setFillColor(TEXT)
    c.drawString(x2, y2, "Anticipated Critical Events")
    y2 -= 4.2 * mm

    c.setFont("Helvetica-Bold", 7.6)
    c.drawString(x2, y2, "To Surgeon :")
    y2 -= 3.6 * mm

    y2 = _checkbox_line(c,
                        x2,
                        y2,
                        w2,
                        "What are the critical or non-routine steps?",
                        bool((time_out.get("surgeon_critical_steps")
                              or "").strip()),
                        fs=7.6)
    y2 = _checkbox_line(c,
                        x2,
                        y2,
                        w2,
                        "How long will the case take?",
                        bool((time_out.get("surgeon_case_duration_estimate")
                              or "").strip()),
                        fs=7.6)
    y2 = _checkbox_line(c,
                        x2,
                        y2,
                        w2,
                        "What is the anticipated blood loss?",
                        bool((time_out.get("surgeon_anticipated_blood_loss")
                              or "").strip()),
                        fs=7.6)

    c.setFont("Helvetica-Bold", 7.6)
    c.drawString(x2, y2, "To Anaesthetist:")
    y2 -= 3.6 * mm

    y2 = _checkbox_line(
        c,
        x2,
        y2,
        w2,
        "Are there any patient-specific concerns?",
        bool((time_out.get("anaesthetist_patient_specific_concerns")
              or "").strip()),
        fs=7.6)

    c.setFont("Helvetica-Bold", 7.6)
    c.drawString(x2, y2, "To Nursing Team:")
    y2 -= 3.6 * mm

    y2 = _checkbox_line(
        c,
        x2,
        y2,
        w2,
        "Has sterility (including indicator results) been\nconfirmed?",
        _as_bool(time_out.get("sterility_confirmed")),
        fs=7.6)
    y2 = _checkbox_line(c,
                        x2,
                        y2,
                        w2,
                        "Are there equipment issues or any concerns?",
                        _as_bool(time_out.get("equipment_issues_or_concerns")),
                        fs=7.6)

    y2 = _wrap(c, "is essential imaging displayed?", x2, y2, w2, fs=7.6)
    img = _norm_choice(time_out.get("essential_imaging_displayed"))
    y2 -= 1.0 * mm
    y2 = _checkbox_line(c, x2, y2, w2, "Yes", img == "yes", fs=7.6)
    y2 = _checkbox_line(c, x2, y2, w2, "Not applicable", img == "na", fs=7.6)

    # ---------- Column 3 content ----------
    x3 = cols_x[2] + pad
    w3 = col_w - 2 * pad
    y3 = c3_top

    c.setFont("Helvetica-Bold", 7.7)
    c.setFillColor(TEXT)
    c.drawString(x3, y3, "Nurse Verbally Confirms:")
    y3 -= 4.0 * mm

    y3 = _checkbox_line(c,
                        x3,
                        y3,
                        w3,
                        "The name of the procedure",
                        _as_bool(sign_out.get("procedure_name_confirmed")),
                        fs=7.6)
    y3 = _checkbox_line(c,
                        x3,
                        y3,
                        w3,
                        "Completion of instrument, sponge and needle\ncounts",
                        _as_bool(sign_out.get("counts_complete")),
                        fs=7.6)
    y3 = _checkbox_line(
        c,
        x3,
        y3,
        w3,
        "specimen labelling(read specimen lables aloud,\nincluding patient name)",
        _as_bool(sign_out.get("specimens_labelled_correctly")),
        fs=7.6)

    # this item exists as free-text in your schema; tick if text is present (keeps exact UI)
    equip_txt = (sign_out.get("equipment_problems_to_be_addressed") or "")
    y3 = _checkbox_line(
        c,
        x3,
        y3,
        w3,
        "Whether there are any equipment problems to be\naddressed",
        bool(str(equip_txt).strip()),
        fs=7.6)

    y3 -= 1.5 * mm
    _divider(c, x3, y3, w3)
    y3 -= 4.0 * mm

    c.setFont("Helvetica-Bold", 7.6)
    c.drawString(x3, y3, "To Surgeon, Anaesthetist and Nurse:")
    y3 -= 4.0 * mm

    key_txt = (sign_out.get("key_concerns_for_recovery_and_management") or "")
    y3 = _checkbox_line(
        c,
        x3,
        y3,
        w3,
        "What are the key concerns for recovery and\nmanagement of this patient?",
        bool(str(key_txt).strip()),
        fs=7.6)

    # ---- Signature area (exact screenshot) ----
    # dotted line blocks
    sig_y = my + 22 * mm
    third = w / 3.0

    c.saveState()
    c.setStrokeColor(TEXT)
    c.setLineWidth(0.8)
    c.setDash(1, 2)
    for i in range(3):
        sx = x0 + i * third + 8 * mm
        ex = x0 + (i + 1) * third - 8 * mm
        c.line(sx, sig_y, ex, sig_y)
    c.setDash()
    c.restoreState()

    c.setFillColor(TEXT)
    c.setFont("Helvetica-Bold", 7.4)
    c.drawCentredString(x0 + third / 2, sig_y - 5.2 * mm, "Signature of")
    c.drawCentredString(x0 + third / 2, sig_y - 9.0 * mm, "Surgeon")

    c.drawCentredString(x0 + third + third / 2, sig_y - 5.2 * mm,
                        "Signature of")
    c.drawCentredString(x0 + third + third / 2, sig_y - 9.0 * mm,
                        "Anaesthetist")

    c.drawCentredString(x0 + 2 * third + third / 2, sig_y - 5.2 * mm,
                        "Signature")
    c.drawCentredString(x0 + 2 * third + third / 2, sig_y - 9.0 * mm,
                        "of Nurse")

    c.showPage()
    c.save()
    return buf.getvalue()
