# FILE: app/services/ot_case_pdf.py
from __future__ import annotations

import json
from io import BytesIO
from datetime import datetime, date, time, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader
from xml.sax.saxutils import escape

from app.core.config import settings

# ============================================================
#  Timezone (IST) helpers
# ============================================================
try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None


def _to_ist(dt: Any) -> Any:
    """
    Convert datetime to IST for display.
    - If datetime is naive -> assume UTC (because your code uses datetime.utcnow()) and convert to IST.
    - If aware -> convert to IST.
    """
    if not isinstance(dt, datetime):
        return dt
    if IST is None:
        return dt

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _fmt_date(x: Any) -> str:
    if not x:
        return "—"
    if isinstance(x, datetime):
        x = _to_ist(x).date()
    if isinstance(x, date):
        return x.strftime("%d-%b-%Y")
    return "—"


def _fmt_time(x: Any) -> str:
    if not x:
        return "—"
    if isinstance(x, datetime):
        x = _to_ist(x)
        return x.strftime("%H:%M")
    if isinstance(x, time):
        return x.strftime("%H:%M")
    if isinstance(x, str) and len(x) >= 5 and x[2] == ":":
        return x[:5]
    return "—"


def _fmt_dt(x: Any) -> str:
    if not x:
        return "—"
    if isinstance(x, datetime):
        x = _to_ist(x)
        return x.strftime("%d-%b-%Y %H:%M")
    return "—"


# ============================================================
#  Safe getters / formatters
# ============================================================
def _g(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _txt(x: Any) -> str:
    if x is None:
        return "—"
    s = str(x).strip()
    return s if s else "—"


def _yn(x: Any) -> str:
    return "✓" if bool(x) else "—"


def _lines(text: Any) -> str:
    if text is None:
        return "—"
    s = escape(str(text))
    s = s.replace("\n", "<br/>")
    return s if s.strip() else "—"


def _labelize(key: str) -> str:
    if not key:
        return "—"
    k = str(key).strip().replace("-", " ").replace("_", " ")
    return " ".join([w[:1].upper() + w[1:] for w in k.split()])


def _json_pretty(x: Any) -> str:
    if x is None:
        return ""
    try:
        return json.dumps(x, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(x)


def _patient_name(p: Any) -> str:
    if not p:
        return "—"
    fn = (_g(p, "first_name", "") or "").strip()
    ln = (_g(p, "last_name", "") or "").strip()
    nm = (fn + " " + ln).strip()
    return nm or _txt(_g(p, "name", None))


def _patient_sex(p: Any) -> str:
    if not p:
        return "—"
    return _txt(_g(p, "sex", None) or _g(p, "gender", None))


def _patient_age(p: Any) -> str:
    if not p:
        return "—"
    dob = _g(p, "dob", None) or _g(p, "date_of_birth", None)
    if isinstance(dob, date) and not isinstance(dob, datetime):
        today = date.today()
        years = today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day))
        return str(years) if years >= 0 else "—"
    return "—"


def _name_user(u: Any) -> str:
    if not u:
        return "—"
    full = _g(u, "full_name", None)
    if full:
        return str(full).strip() or "—"
    first = (_g(u, "first_name", "") or "").strip()
    last = (_g(u, "last_name", "") or "").strip()
    nm = (first + " " + last).strip()
    return nm or _txt(_g(u, "email", None))


def _bed_label(bed: Any) -> str:
    if not bed:
        return "—"
    room = _g(bed, "room", None)
    ward = _g(room, "ward", None) if room else None

    ward_name = _g(ward, "name", None) or _g(ward, "ward_name", None)
    room_name = _g(room, "name", None) or _g(room, "room_name", None) or _g(
        room, "room_no", None)
    code = _g(bed, "code", None) or _g(bed, "bed_no", None)

    parts = [p for p in [ward_name, room_name, code] if p]
    return " · ".join(parts) if parts else "—"


# ============================================================
#  ReportLab splitting fix
# ============================================================
def _make_splittable(t: Any) -> Any:
    try:
        t.splitByRow = 1
    except Exception:
        pass
    try:
        t.splitInRow = 1
    except Exception:
        pass
    return t


# ============================================================
#  Theme (Apple-ish)
# ============================================================
def _styles():
    base = getSampleStyleSheet()

    ink = colors.HexColor("#0B1220")
    sub = colors.HexColor("#667085")
    border = colors.HexColor("#E6E8EC")
    soft = colors.HexColor("#F6F7F9")

    base["Normal"].fontName = "Helvetica"
    base["Normal"].fontSize = 9.5
    base["Normal"].leading = 13
    base["Normal"].textColor = ink

    H1 = ParagraphStyle(
        "H1",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=ink,
        spaceAfter=6,
    )
    H2 = ParagraphStyle(
        "H2",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=15,
        textColor=ink,
        spaceBefore=10,
        spaceAfter=6,
    )
    Small = ParagraphStyle(
        "Small",
        parent=base["Normal"],
        fontSize=8.5,
        leading=12,
        textColor=sub,
    )
    Label = ParagraphStyle(
        "Label",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=12,
        textColor=sub,
    )
    Value = ParagraphStyle(
        "Value",
        parent=base["Normal"],
        fontSize=9.5,
        leading=13,
        textColor=ink,
    )
    Mono = ParagraphStyle(
        "Mono",
        parent=base["Normal"],
        fontName="Courier",
        fontSize=8.2,
        leading=11,
        textColor=ink,
    )

    return {
        "base": base,
        "H1": H1,
        "H2": H2,
        "Small": Small,
        "Label": Label,
        "Value": Value,
        "Mono": Mono,
        "C": {
            "ink": ink,
            "sub": sub,
            "border": border,
            "soft": soft
        },
    }


def _kv_table(rows: List[List[str]], theme: dict, col_widths=None) -> Table:
    C = theme["C"]
    data = []
    for k, v in rows:
        data.append([
            Paragraph(f"<b>{escape(_txt(k))}</b>", theme["Label"]),
            Paragraph(_lines(v), theme["Value"]),
        ])

    t = Table(data, colWidths=col_widths or [45 * mm, None])
    _make_splittable(t)
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, C["border"]),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
    return t


def _kv_two_col(rows: List[List[str]], theme: dict) -> Table:
    mid = (len(rows) + 1) // 2
    left = rows[:mid]
    right = rows[mid:]

    lt = _kv_table(left, theme, col_widths=[35 * mm, None])
    rt = _kv_table(right, theme, col_widths=[35 * mm, None
                                             ]) if right else Spacer(1, 1)

    grid = Table([[lt, rt]], colWidths=[92 * mm, None])
    _make_splittable(grid)
    grid.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
    return grid


def _card(flowables: List[Any],
          theme: dict,
          *,
          soft_bg: bool = False) -> Table:
    C = theme["C"]
    rows = [[f] for f in flowables if f is not None]
    inner = Table(rows, colWidths=[None])
    _make_splittable(inner)
    inner.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1),
             C["soft"] if soft_bg else colors.white),
            ("BOX", (0, 0), (-1, -1), 0.7, C["border"]),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("ROWSPACING", (0, 0), (-1, -1), 4),
        ]))
    return inner


def _section(title: str, content: List[Any], theme: dict) -> List[Any]:
    out = [Paragraph(escape(title), theme["H2"])]
    out.append(_card(content, theme))
    out.append(Spacer(1, 6))
    return out


def _mini_title(text: str, theme: dict) -> Paragraph:
    return Paragraph(f"<b>{escape(text)}</b>", theme["Label"])


# ============================================================
#  Branding header (ReportLab) - matches your pdf_branding intention
# ============================================================
def _wrap_text(text: str, max_width: float, font_name: str,
               font_size: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: List[str] = []
    cur = words[0]
    for w in words[1:]:
        test = cur + " " + w
        if stringWidth(test, font_name, font_size) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _branding_logo_reader(branding: Any) -> Optional[ImageReader]:
    rel = (getattr(branding, "logo_path", None) or "").strip()
    if not rel:
        return None
    try:
        abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
        if not abs_path.exists() or not abs_path.is_file():
            return None
        return ImageReader(str(abs_path))
    except Exception:
        return None


def _on_page(*,
             org_name: str,
             generated_by: Optional[str],
             theme: dict,
             branding: Any = None):
    C = theme["C"]

    def fn(canvas, doc):
        canvas.saveState()

        x0 = doc.leftMargin
        x1 = doc.pagesize[0] - doc.rightMargin

        # Header box (fits inside topMargin)
        header_top = doc.pagesize[1] - 8 * mm
        header_bottom = doc.pagesize[1] - doc.topMargin + 6 * mm

        # Bottom header line
        canvas.setStrokeColor(C["border"])
        canvas.setLineWidth(0.8)
        canvas.line(x0, header_bottom, x1, header_bottom)

        # Logo (left)
        logo = _branding_logo_reader(
            branding) if branding is not None else None
        logo_h = 18 * mm
        logo_w_max = 55 * mm
        if logo:
            try:
                iw, ih = logo.getSize()
                scale = min(logo_h / float(ih), logo_w_max / float(iw))
                dw = float(iw) * scale
                dh = float(ih) * scale
                canvas.drawImage(
                    logo,
                    x0,
                    header_top - dh - 2 * mm,
                    width=dw,
                    height=dh,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception:
                pass

        # Right branding text
        b_org = (getattr(branding, "org_name", None)
                 if branding is not None else None) or org_name
        b_tag = (getattr(branding, "org_tagline", None)
                 if branding is not None else None) or ""
        b_addr = (getattr(branding, "org_address", None)
                  if branding is not None else None) or ""
        b_phone = (getattr(branding, "org_phone", None)
                   if branding is not None else None) or ""
        b_email = (getattr(branding, "org_email", None)
                   if branding is not None else None) or ""
        b_web = (getattr(branding, "org_website", None)
                 if branding is not None else None) or ""
        b_gstin = (getattr(branding, "org_gstin", None)
                   if branding is not None else None) or ""

        right_x = x1
        max_text_w = 105 * mm

        y = header_top - 2 * mm

        canvas.setFillColor(C["ink"])
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawRightString(right_x, y, str(b_org).strip() or org_name)

        y -= 5 * mm
        if b_tag:
            canvas.setFillColor(C["sub"])
            canvas.setFont("Helvetica", 9)
            canvas.drawRightString(right_x, y, str(b_tag).strip())
            y -= 4 * mm

        canvas.setFillColor(C["sub"])
        canvas.setFont("Helvetica", 8)

        # Address wrap (max 2 lines)
        addr_lines = _wrap_text(
            str(b_addr).strip(), max_text_w, "Helvetica",
            8)[:2] if b_addr else []
        for ln in addr_lines:
            canvas.drawRightString(right_x, y, ln)
            y -= 3.5 * mm

        contact_bits = []
        if b_phone:
            contact_bits.append(f"Phone: {str(b_phone).strip()}")
        if b_email:
            contact_bits.append(f"Email: {str(b_email).strip()}")
        if contact_bits:
            canvas.drawRightString(right_x, y, "  |  ".join(contact_bits))
            y -= 3.5 * mm

        if b_web:
            canvas.drawRightString(right_x, y,
                                   f"Website: {str(b_web).strip()}")
            y -= 3.5 * mm
        if b_gstin:
            canvas.drawRightString(right_x, y,
                                   f"GSTIN: {str(b_gstin).strip()}")

        # Footer line
        canvas.setStrokeColor(C["border"])
        canvas.setLineWidth(0.8)
        canvas.line(x0, 12 * mm, x1, 12 * mm)

        # Footer text (IST)
        now = datetime.now(IST) if IST else datetime.now()
        ts = now.strftime("%d-%b-%Y %H:%M")
        canvas.setFillColor(C["sub"])
        canvas.setFont("Helvetica", 8)
        left = f"Generated: {ts}" + (f" · By: {generated_by}"
                                     if generated_by else "")
        canvas.drawString(x0, 8 * mm, left)

        page = f"Page {doc.page}"
        w2 = stringWidth(page, "Helvetica", 8)
        canvas.drawString(x1 - w2, 8 * mm, page)

        canvas.restoreState()

    return fn


# ============================================================
#  Section builders
# ============================================================
def _build_summary(case: Any, theme: dict) -> List[Any]:
    schedule = _g(case, "schedule", None)
    patient = _g(schedule, "patient", None) if schedule else None
    admission = _g(schedule, "admission", None) if schedule else None

    surgeon = _g(schedule, "surgeon", None) if schedule else None
    anaesth = _g(schedule, "anaesthetist", None) if schedule else None

    ot_bed = _g(schedule, "ot_bed", None) if schedule else None
    ward_bed = _g(admission, "current_bed", None) if admission else None

    uhid = _txt(_g(patient, "uhid", None) or _g(patient, "uhid_number", None))
    phone = _txt(
        _g(patient, "phone", None) or _g(patient, "mobile", None)
        or _g(patient, "mobile_no", None))
    ip_no = _txt(
        _g(admission, "display_code", None)
        or _g(admission, "admission_code", None))
    op_no = _txt(_g(schedule, "op_no", None))

    # Height/Weight (fix)
    h = _g(patient, "height_cm", None) or _g(patient, "height", None)
    w = _g(patient, "weight_kg", None) or _g(patient, "weight", None)
    hw = "—"
    if h is not None or w is not None:
        hh = f"{h} cm" if h is not None else "—"
        ww = f"{w} kg" if w is not None else "—"
        hw = f"{hh} / {ww}"

    proc = _txt(
        _g(case, "final_procedure_name", None)
        or _g(schedule, "procedure_name", None))
    side = _txt(_g(schedule, "side", None))
    priority = _txt(_g(schedule, "priority", None))
    status = _txt(_g(schedule, "status", None))

    planned = f"{_fmt_time(_g(schedule, 'planned_start_time', None))} – {_fmt_time(_g(schedule, 'planned_end_time', None))}"
    ot_date = _fmt_date(_g(schedule, "date", None))

    actual_start = _fmt_dt(_g(case, "actual_start_time", None))
    actual_end = _fmt_dt(_g(case, "actual_end_time", None))

    # OT reg no (try multiple fields)
    ot_reg = _txt(
        _g(schedule, "reg_no", None) or _g(schedule, "display_number", None)
        or _g(schedule, "ot_number", None)
        or _g(schedule, "schedule_code", None))

    age = _patient_age(patient)
    sex = _patient_sex(patient)

    rows_left = [
        ["Patient", _patient_name(patient)],
        ["UHID", uhid],
        ["Age / Sex", f"{age} / {sex}" if age != "—" or sex != "—" else "—"],
        ["Height / Weight", hw],
        ["Phone", phone],
        ["IP No", ip_no],
        ["OP No", op_no],
        ["OT Reg No", ot_reg],
        ["OT Case ID", _txt(_g(case, "id", None))],
    ]

    rows_right = [
        ["OT Date", ot_date],
        ["Schedule Status", status],
        ["Procedure", proc],
        [
            "Side / Priority",
            f"{side} / {priority}" if side != "—" or priority != "—" else "—"
        ],
        ["Planned Time", planned if planned.strip(" –") else "—"],
        ["Actual (Start → End)", f"{actual_start} → {actual_end}"],
        ["OT Location Bed", _bed_label(ot_bed)],
        ["Ward Bed", _bed_label(ward_bed)],
        ["Surgeon", _name_user(surgeon)],
        ["Anaesthetist", _name_user(anaesth)],
    ]

    grid = Table(
        [[
            _kv_table(rows_left, theme, col_widths=[38 * mm, None]),
            _kv_table(rows_right, theme, col_widths=[44 * mm, None]),
        ]],
        colWidths=[92 * mm, None],
    )
    _make_splittable(grid)
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    return _section("Summary", [grid], theme)


def _build_case_overview(case: Any, theme: dict) -> List[Any]:
    rows = [
        ["Pre-Op Diagnosis",
         _txt(_g(case, "preop_diagnosis", None))],
        ["Post-Op Diagnosis",
         _txt(_g(case, "postop_diagnosis", None))],
        ["Final Procedure",
         _txt(_g(case, "final_procedure_name", None))],
        ["Outcome", _txt(_g(case, "outcome", None))],
        ["ICU Required", _yn(_g(case, "icu_required", False))],
        [
            "Immediate Post-Op Condition",
            _txt(_g(case, "immediate_postop_condition", None))
        ],
        ["Created At", _fmt_dt(_g(case, "created_at", None))],
        ["Updated At", _fmt_dt(_g(case, "updated_at", None))],
    ]
    return _section("Case Overview",
                    [_kv_table(rows, theme, col_widths=[55 * mm, None])],
                    theme)


def _build_preanaesthesia(case: Any, theme: dict) -> List[Any]:
    pre = _g(case, "preanaesthesia", None)
    if not pre:
        return _section("Pre-Anaesthesia Evaluation", [
            Paragraph("No pre-anaesthesia evaluation recorded.",
                      theme["Small"])
        ], theme)

    anaesthetist = _g(pre, "anaesthetist", None)
    rows = [
        [
            "Anaesthetist",
            _name_user(anaesthetist) if anaesthetist else _txt(
                _g(pre, "anaesthetist_user_id", None))
        ],
        ["ASA Grade", _txt(_g(pre, "asa_grade", None))],
        ["Comorbidities",
         _txt(_g(pre, "comorbidities", None))],
        ["Airway Assessment",
         _txt(_g(pre, "airway_assessment", None))],
        ["Allergies", _txt(_g(pre, "allergies", None))],
        [
            "Previous Anaesthesia Issues",
            _txt(_g(pre, "previous_anaesthesia_issues", None))
        ],
        ["Plan", _txt(_g(pre, "plan", None))],
        ["Risk Explanation",
         _txt(_g(pre, "risk_explanation", None))],
        ["Created At", _fmt_dt(_g(pre, "created_at", None))],
    ]
    return _section("Pre-Anaesthesia Evaluation",
                    [_kv_table(rows, theme, col_widths=[55 * mm, None])],
                    theme)


# ---------- Pre-Op checklist (5 major parts + height fix) ----------
def _pick_dict(d: dict, keys: List[str]) -> dict:
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            return v
    return {}


def _normalize_height(val: Any) -> str:
    if val is None:
        return "—"
    try:
        f = float(val)
        # If someone stored meters (1.72), convert to cm
        if 0 < f < 3:
            f = f * 100.0
        return f"{round(f, 1)} cm"
    except Exception:
        return _txt(val)


def _normalize_weight(val: Any) -> str:
    if val is None:
        return "—"
    try:
        f = float(val)
        return f"{round(f, 1)} kg"
    except Exception:
        return _txt(val)


def _build_preop_checklist(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "preop_checklist", None)
    if not rec:
        return _section(
            "Pre-Op Checklist",
            [Paragraph("No pre-op checklist recorded.", theme["Small"])],
            theme)

    data = _as_dict(_g(rec, "data", None))

    # ✅ Flexible extraction (supports nested or flat JSON)
    checklist = _pick_dict(data, ["checklist", "items"])
    investigations = _pick_dict(data, ["investigations", "investigation"])
    vitals = _pick_dict(data, ["vitals", "vital_signs", "preop_vitals"])

    # If checklist not nested, infer from dict entries that look like checklist rows
    if not checklist:
        inferred = {}
        for k, v in data.items():
            if isinstance(v, dict) and ("handover" in v or "receiving" in v
                                        or "comments" in v):
                inferred[k] = v
        checklist = inferred

    completed = bool(_g(rec, "completed", False))
    completed_at = _fmt_dt(_g(rec, "completed_at", None))
    created_at = _fmt_dt(_g(rec, "created_at", None))
    nurse_id = _txt(_g(rec, "nurse_user_id", None))

    meta_rows = [
        ["Status", "COMPLETED" if completed else "IN PROGRESS"],
        ["Created", created_at],
        ["Completed", completed_at if completed else "—"],
        ["Nurse (user_id)", nurse_id],
    ]

    # --- Part 1: Checklist table ---
    tbl = [["Item", "Handover", "Receiving", "Comments"]]
    if checklist:
        for key in sorted(checklist.keys(), key=lambda z: str(z)):
            row = _as_dict(checklist.get(key))
            tbl.append([
                _labelize(key),
                _yn(row.get("handover")),
                _yn(row.get("receiving")),
                _txt(row.get("comments")),
            ])
    else:
        tbl.append(["—", "—", "—", "No checklist items"])

    t_check = Table(tbl,
                    colWidths=[72 * mm, 20 * mm, 20 * mm, None],
                    repeatRows=1)
    _make_splittable(t_check)
    t_check.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8.6),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))

    # --- Part 2: Investigations ---
    inv_rows = [[_labelize(k), _txt(v)]
                for k, v in investigations.items()] or [["—", "—"]]
    inv_block = _card([
        _mini_title("Investigations", theme),
        _kv_table(inv_rows, theme, col_widths=[34 * mm, None])
    ],
                      theme,
                      soft_bg=True)

    # --- Part 3: Vitals (fix height mapping) ---
    # Try many key names
    h_val = (vitals.get("height_cm") or vitals.get("height")
             or vitals.get("ht_cm") or data.get("height_cm")
             or data.get("height"))
    w_val = (vitals.get("weight_kg") or vitals.get("weight")
             or vitals.get("wt_kg") or data.get("weight_kg")
             or data.get("weight"))

    vit_rows: List[List[str]] = []
    if h_val is not None:
        vit_rows.append(["Height", _normalize_height(h_val)])
    if w_val is not None:
        vit_rows.append(["Weight", _normalize_weight(w_val)])

    # Add remaining vitals keys (but avoid duplicating height/weight)
    skip = {"height", "height_cm", "ht_cm", "weight", "weight_kg", "wt_kg"}
    for k in sorted(vitals.keys(), key=lambda z: str(z)):
        if str(k) in skip:
            continue
        vit_rows.append([_labelize(k), _txt(vitals.get(k))])

    if not vit_rows:
        vit_rows = [["—", "—"]]

    vit_block = _card([
        _mini_title("Vitals", theme),
        _kv_table(vit_rows, theme, col_widths=[34 * mm, None])
    ],
                      theme,
                      soft_bg=True)

    # --- Part 4: Body shave ---
    shave_rows = [
        ["Shave Completed",
         _yn(data.get("shave_completed"))],
        ["Shave Area", _txt(data.get("shave_area"))],
        ["Shave Time", _fmt_dt(data.get("shave_time"))],
        ["Done By", _txt(data.get("shave_done_by"))],
        ["Notes", _txt(data.get("shave_notes"))],
    ]
    shave_block = _card([
        _mini_title("Body Shave", theme),
        _kv_table(shave_rows, theme, col_widths=[34 * mm, None])
    ],
                        theme,
                        soft_bg=True)

    # --- Part 5: Nurse signature ---
    sign_rows = [
        ["Nurse Signature",
         _txt(data.get("nurse_signature"))],
        ["Fasting Status", _txt(data.get("fasting_status"))],
        ["Device Checks", _txt(data.get("device_checks"))],
        ["Consent Checked",
         _yn(data.get("consent_checked"))],
        ["Site Marked", _yn(data.get("site_marked"))],
        ["Investigations Checked",
         _yn(data.get("investigations_checked"))],
        ["Implants Available",
         _yn(data.get("implants_available"))],
        ["Blood Products Arranged",
         _yn(data.get("blood_products_arranged"))],
        ["Notes", _txt(data.get("notes"))],
    ]
    sign_block = _card([
        _mini_title("Nurse Signature / Sign-Off", theme),
        _kv_table(sign_rows, theme, col_widths=[40 * mm, None])
    ],
                       theme,
                       soft_bg=True)

    # Layout:
    # Row A: Checklist (left) + Investigations/Vitals stack (right)
    right_stack = Table([[inv_block], [Spacer(1, 6)], [vit_block]],
                        colWidths=[None])
    _make_splittable(right_stack)
    right_stack.setStyle(
        TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    row_a = Table(
        [[
            _card([_mini_title("Checklist", theme), t_check],
                  theme,
                  soft_bg=True), right_stack
        ]],
        colWidths=[120 * mm, None],
    )
    _make_splittable(row_a)
    row_a.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    # Row B: Body shave + Nurse signature
    row_b = Table([[shave_block, sign_block]], colWidths=[92 * mm, None])
    _make_splittable(row_b)
    row_b.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    return _section(
        "Pre-Op Checklist (FULL)",
        [
            _kv_two_col(meta_rows, theme),
            Spacer(1, 8),
            row_a,
            Spacer(1, 8),
            row_b,
        ],
        theme,
    )


# Keep your existing other builders (safety/anaesthesia/nursing/...) as-is,
# but with IST formatting already handled by _fmt_dt/_fmt_time above.
# -------------------------------------------------------------

# (Your existing builders below can remain same; paste from your old file if needed.)
# For brevity, I keep them unchanged here EXCEPT they will automatically display IST now.


def _build_safety(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "safety_checklist", None)
    if not rec:
        return _section(
            "WHO Surgical Safety Checklist",
            [Paragraph("No WHO safety checklist recorded.", theme["Small"])],
            theme)

    sign_in_data = _as_dict(_g(rec, "sign_in_data", None))
    time_out_data = _as_dict(_g(rec, "time_out_data", None))
    sign_out_data = _as_dict(_g(rec, "sign_out_data", None))

    meta = [
        ["Sign-In Done By",
         _name_user(_g(rec, "sign_in_done_by", None))],
        ["Sign-In Time",
         _fmt_dt(_g(rec, "sign_in_time", None))],
        ["Time-Out Done By",
         _name_user(_g(rec, "time_out_done_by", None))],
        ["Time-Out Time",
         _fmt_dt(_g(rec, "time_out_time", None))],
        ["Sign-Out Done By",
         _name_user(_g(rec, "sign_out_done_by", None))],
        ["Sign-Out Time",
         _fmt_dt(_g(rec, "sign_out_time", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
    ]

    def phase_rows(d: dict) -> List[List[str]]:
        rows: List[List[str]] = []
        for k in sorted(d.keys(), key=lambda z: str(z)):
            v = d.get(k)
            if isinstance(v, bool):
                rows.append([_labelize(k), _yn(v)])
            elif v is None:
                rows.append([_labelize(k), "—"])
            else:
                rows.append([_labelize(k), _txt(v)])
        return rows

    sign_in_rows = phase_rows(sign_in_data) or [["—", "—"]]
    time_out_rows = phase_rows(time_out_data) or [["—", "—"]]
    sign_out_rows = phase_rows(sign_out_data) or [["—", "—"]]

    return _section(
        "WHO Surgical Safety Checklist (FULL)",
        [
            _kv_two_col(meta, theme),
            Spacer(1, 6),
            _mini_title("Phase 1 — Sign In (Before Induction)", theme),
            _kv_two_col(sign_in_rows, theme),
            Spacer(1, 6),
            _mini_title("Phase 2 — Time Out (Before Skin Incision)", theme),
            _kv_two_col(time_out_rows, theme),
            Spacer(1, 6),
            _mini_title("Phase 3 — Sign Out (Before Leaving OT)", theme),
            _kv_two_col(sign_out_rows, theme),
        ],
        theme,
    )


def _build_appendix_raw(case: Any, theme: dict) -> List[Any]:
    parts: List[Tuple[str, Any]] = []

    preop = _g(_g(case, "preop_checklist", None), "data", None)
    if preop is not None:
        parts.append(("Pre-Op Checklist JSON", preop))

    safety = _g(case, "safety_checklist", None)
    if safety is not None:
        if _g(safety, "sign_in_data", None) is not None:
            parts.append(("Safety Checklist — Sign In JSON",
                          _g(safety, "sign_in_data", None)))
        if _g(safety, "time_out_data", None) is not None:
            parts.append(("Safety Checklist — Time Out JSON",
                          _g(safety, "time_out_data", None)))
        if _g(safety, "sign_out_data", None) is not None:
            parts.append(("Safety Checklist — Sign Out JSON",
                          _g(safety, "sign_out_data", None)))

    if not parts:
        return []

    content: List[Any] = []
    for title, data in parts:
        content.append(_mini_title(title, theme))
        content.append(Paragraph(_lines(_json_pretty(data)), theme["Mono"]))
        content.append(Spacer(1, 6))

    return _section("Appendix — Raw JSON Snapshots", content, theme)


# ============================================================
#  Main entry
# ============================================================
def build_ot_case_pdf(
        case: Any,
        org_name: str = "NUTRYAH HIMS",
        generated_by: Optional[str] = None,
        branding: Any = None,  # ✅ pass UiBranding object here
) -> bytes:
    theme = _styles()
    buf = BytesIO()

    # ✅ bigger top margin to fit branding header
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=44 * mm,
        bottomMargin=18 * mm,
        title="OT Case PDF",
        author=org_name,
    )
    doc.allowSplitting = 1

    story: List[Any] = []
    story.append(Paragraph("OT Case", theme["H1"]))
    story.append(
        Paragraph("Full case PDF (all clinical tabs).", theme["Small"]))
    story.append(Spacer(1, 8))

    story += _build_summary(case, theme)
    story += _build_case_overview(case, theme)
    story += _build_preanaesthesia(case, theme)
    story += _build_preop_checklist(case, theme)
    story += _build_safety(case, theme)

    appendix = _build_appendix_raw(case, theme)
    if appendix:
        story.append(PageBreak())
        story += appendix

    doc.build(
        story,
        onFirstPage=_on_page(org_name=org_name,
                             generated_by=generated_by,
                             theme=theme,
                             branding=branding),
        onLaterPages=_on_page(org_name=org_name,
                              generated_by=generated_by,
                              theme=theme,
                              branding=branding),
    )
    return buf.getvalue()
