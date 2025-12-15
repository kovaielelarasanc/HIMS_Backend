from __future__ import annotations

import json
from io import BytesIO
from datetime import datetime, date, time
from typing import Any, List, Optional, Tuple

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
from xml.sax.saxutils import escape

# ============================================================
#  Helpers (safe getters + formatters)
# ============================================================


def _g(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _as_list(x: Any) -> list:
    return x if isinstance(x, list) else []


def _fmt_date(x: Any) -> str:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x.strftime("%d-%b-%Y")
    if isinstance(x, datetime):
        return x.strftime("%d-%b-%Y")
    return "—"


def _fmt_time(x: Any) -> str:
    if isinstance(x, time):
        return x.strftime("%H:%M")
    if isinstance(x, str) and len(x) >= 5 and x[2] == ":":
        return x[:5]
    if isinstance(x, datetime):
        return x.strftime("%H:%M")
    return "—"


def _fmt_dt(x: Any) -> str:
    if isinstance(x, datetime):
        return x.strftime("%d-%b-%Y %H:%M")
    return "—"


def _yn(x: Any) -> str:
    return "✓" if bool(x) else "—"


def _txt(x: Any) -> str:
    if x is None:
        return "—"
    s = str(x).strip()
    return s if s else "—"


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


def _patient_name(p: Any) -> str:
    if not p:
        return "—"
    fn = (_g(p, "first_name", "") or "").strip()
    ln = (_g(p, "last_name", "") or "").strip()
    nm = (fn + " " + ln).strip()
    if nm:
        return nm
    return _txt(_g(p, "name", None))


def _patient_sex(p: Any) -> str:
    if not p:
        return "—"
    for k in ("sex", "gender"):
        v = _g(p, k, None)
        if v:
            return _txt(v)
    return "—"


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


def _json_pretty(x: Any) -> str:
    if x is None:
        return ""
    try:
        return json.dumps(x, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(x)


def _flatten_kv(data: Any, prefix: str = "") -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []

    if isinstance(data, dict):
        for k in sorted(data.keys(), key=lambda z: str(z)):
            v = data.get(k)
            kk = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.extend(_flatten_kv(v, kk))
            else:
                out.append((kk, _txt(v)))
        return out

    if isinstance(data, list):
        for i, v in enumerate(data):
            kk = f"{prefix}[{i}]" if prefix else f"[{i}]"
            if isinstance(v, (dict, list)):
                out.extend(_flatten_kv(v, kk))
            else:
                out.append((kk, _txt(v)))
        return out

    if prefix:
        out.append((prefix, _txt(data)))
    return out


# ============================================================
#  CRITICAL FIX: make tables splittable (including 1-row wrappers)
# ============================================================


def _make_splittable(t: Any) -> Any:
    """
    Fixes ReportLab LayoutError:
      "N rows in cell(0,0) in (2 x 1) table ... too large on page"
    by allowing a table to split INSIDE a single row (splitInRow=1).
    """
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
#  Apple-ish PDF theme
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
            ("ROWSPACING", (0, 0), (-1, -1), 2),
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
    _make_splittable(grid)  # ✅ fixes (2x1) wrapper splits
    grid.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
    return grid


def _card(flowables: List[Any], theme: dict) -> Table:
    C = theme["C"]
    rows = [[f] for f in flowables if f is not None]
    inner = Table(rows, colWidths=[None])
    _make_splittable(inner)  # ✅ MOST IMPORTANT for cards
    inner.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
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
#  Section builders (aligned to YOUR models)
# ============================================================


def _build_summary(case: Any, theme: dict) -> List[Any]:
    schedule = _g(case, "schedule", None)
    patient = _g(schedule, "patient", None) if schedule else None
    admission = _g(schedule, "admission", None) if schedule else None

    surgeon = _g(schedule, "surgeon", None) if schedule else None
    anaesth = _g(schedule, "anaesthetist", None) if schedule else None

    ot_bed = _g(schedule, "ot_bed", None) if schedule else None
    ward_bed = _g(admission, "current_bed", None) if admission else None
    bed = ot_bed or ward_bed

    speciality = _g(case, "speciality", None)
    speciality_name = _txt(_g(speciality, "name", None))

    uhid = _txt(_g(patient, "uhid", None) or _g(patient, "uhid_number", None))
    ip_no = _txt(
        _g(admission, "display_code", None)
        or _g(admission, "admission_code", None))
    op_no = _txt(_g(schedule, "op_no", None))

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

    bed_label = "—"
    if bed:
        bed_label = " · ".join([
            x for x in [
                _g(bed, "ward_name", None),
                _g(bed, "room_name", None),
                _g(bed, "code", None)
            ] if x
        ])

    age = _txt(_g(case, "age", None))
    sex = _txt(_g(case, "sex", None))
    if age == "—":
        age = _patient_age(patient)
    if sex == "—":
        sex = _patient_sex(patient)

    rows_left = [
        ["Patient", _patient_name(patient)],
        ["UHID", uhid],
        ["Age / Sex", f"{age} / {sex}" if age != "—" or sex != "—" else "—"],
        ["IP No", ip_no],
        ["OP No", op_no],
    ]

    rows_right = [
        ["OT Date", ot_date],
        ["Schedule Status", status],
        ["Speciality", speciality_name],
        ["Procedure", proc],
        [
            "Side / Priority",
            f"{side} / {priority}" if side != "—" or priority != "—" else "—"
        ],
        ["Planned Time", planned if planned.strip(" –") else "—"],
        ["Actual (Start → End)", f"{actual_start} → {actual_end}"],
        ["OT / Ward Bed", bed_label],
        ["Surgeon", _name_user(surgeon)],
        ["Anaesthetist", _name_user(anaesth)],
    ]

    grid = Table(
        [[
            _kv_table(rows_left, theme, col_widths=[35 * mm, None]),
            _kv_table(rows_right, theme, col_widths=[40 * mm, None]),
        ]],
        colWidths=[90 * mm, None],
    )
    _make_splittable(grid)  # ✅ fixes summary wrapper too
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


def _build_preop_checklist(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "preop_checklist", None)
    if not rec:
        return _section(
            "Pre-Op Checklist",
            [Paragraph("No pre-op checklist recorded.", theme["Small"])],
            theme)

    data = _as_dict(_g(rec, "data", None))
    checklist = _as_dict(data.get("checklist"))
    investigations = _as_dict(data.get("investigations"))
    vitals = _as_dict(data.get("vitals"))

    completed = bool(_g(rec, "completed", False))
    completed_at = _fmt_dt(_g(rec, "completed_at", None))
    created_at = _fmt_dt(_g(rec, "created_at", None))
    nurse_id = _txt(_g(rec, "nurse_user_id", None))

    meta_rows = [
        ["Status", "COMPLETED" if completed else "IN PROGRESS"],
        ["Created", created_at],
        ["Completed", completed_at if completed else "—"],
        ["Nurse (user_id)", nurse_id],
        ["Shave Completed",
         _txt(data.get("shave_completed"))],
        ["Nurse Signature",
         _txt(data.get("nurse_signature"))],
        ["Fasting Status", _txt(data.get("fasting_status"))],
        ["Device Checks", _txt(data.get("device_checks"))],
        ["Notes", _txt(data.get("notes"))],
        [
            "Patient Identity Confirmed",
            _yn(data.get("patient_identity_confirmed"))
        ],
        ["Consent Checked",
         _yn(data.get("consent_checked"))],
        ["Site Marked", _yn(data.get("site_marked"))],
        ["Investigations Checked",
         _yn(data.get("investigations_checked"))],
        ["Implants Available",
         _yn(data.get("implants_available"))],
        ["Blood Products Arranged",
         _yn(data.get("blood_products_arranged"))],
    ]

    tbl = [["Item", "Handover", "Receiving", "Comments"]]
    for key in sorted(checklist.keys(), key=lambda z: str(z)):
        row = _as_dict(checklist.get(key))
        tbl.append([
            _labelize(key),
            _yn(row.get("handover")),
            _yn(row.get("receiving")),
            _txt(row.get("comments")),
        ])

    t = Table(tbl, colWidths=[72 * mm, 20 * mm, 20 * mm, None], repeatRows=1)
    _make_splittable(t)
    t.setStyle(
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

    inv_rows = [[_labelize(k), _txt(v)]
                for k, v in investigations.items()] or [["—", "—"]]
    vit_rows = [[_labelize(k), _txt(v)]
                for k, v in vitals.items()] or [["—", "—"]]

    right = Table(
        [
            [_mini_title("Investigations", theme)],
            [_kv_table(inv_rows, theme, col_widths=[32 * mm, None])],
            [Spacer(1, 4)],
            [_mini_title("Vitals", theme)],
            [_kv_table(vit_rows, theme, col_widths=[32 * mm, None])],
        ],
        colWidths=[None],
    )
    _make_splittable(right)
    right.setStyle(
        TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

    layout = Table([[t, right]], colWidths=[120 * mm, None])
    _make_splittable(layout)  # ✅ FIXES YOUR (2 x 1) LAYOUT ERROR
    layout.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    return _section(
        "Pre-Op Checklist (FULL)",
        [
            _kv_two_col(meta_rows, theme),
            Spacer(1, 6),
            layout,
        ],
        theme,
    )


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


def _build_anaesthesia(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "anaesthesia_record", None)
    if not rec:
        return _section(
            "Anaesthesia Record",
            [Paragraph("No anaesthesia record recorded.", theme["Small"])],
            theme)

    header_rows = [
        [
            "Anaesthetist",
            _name_user(_g(rec, "anaesthetist", None))
            or _txt(_g(rec, "anaesthetist_user_id", None))
        ],
        ["Plan", _txt(_g(rec, "plan", None))],
        ["Airway Plan", _txt(_g(rec, "airway_plan", None))],
        ["Intra-Op Summary",
         _txt(_g(rec, "intraop_summary", None))],
        ["Complications",
         _txt(_g(rec, "complications", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
    ]

    preop_vitals = _g(rec, "preop_vitals", None)
    preop_rows: List[List[str]] = []
    if isinstance(preop_vitals, dict):
        for k, v in _flatten_kv(preop_vitals):
            preop_rows.append([_labelize(k), _txt(v)])
    elif preop_vitals is not None:
        preop_rows.append(["Pre-Op Vitals", _txt(preop_vitals)])

    vitals = list(_g(rec, "vitals", None) or [])
    vt = [[
        "Time", "Pulse", "BP", "SpO₂", "RR", "Temp", "EtCO₂", "UO", "Blood",
        "Vent", "Ppeak", "ST", "Comments"
    ]]

    def _bp(v: Any) -> str:
        sys = _g(v, "bp_systolic", None)
        dia = _g(v, "bp_diastolic", None)
        if sys is None and dia is None:
            return "—"
        if sys is None:
            return f"—/{dia}"
        if dia is None:
            return f"{sys}/—"
        return f"{sys}/{dia}"

    for v in vitals:
        tt = _g(v, "time", None)
        vt.append([
            _fmt_time(tt) if isinstance(tt, (datetime, time)) else _txt(tt),
            _txt(_g(v, "pulse", None)),
            _bp(v),
            _txt(_g(v, "spo2", None)),
            _txt(_g(v, "rr", None)),
            _txt(_g(v, "temperature", None)),
            _txt(_g(v, "etco2", None)),
            _txt(_g(v, "urine_output_ml", None)),
            _txt(_g(v, "blood_loss_ml", None)),
            _txt(_g(v, "ventilation_mode", None)),
            _txt(_g(v, "peak_airway_pressure", None)),
            _txt(_g(v, "st_segment", None)),
            _txt(_g(v, "comments", None)),
        ])

    vit_table = Table(
        vt,
        colWidths=[
            14 * mm, 12 * mm, 16 * mm, 11 * mm, 9 * mm, 11 * mm, 11 * mm,
            10 * mm, 11 * mm, 12 * mm, 12 * mm, 10 * mm, None
        ],
        repeatRows=1,
    )
    _make_splittable(vit_table)
    vit_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.0),
            ("FONTSIZE", (0, 1), (-1, -1), 7.8),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))

    drugs = list(_g(rec, "drugs", None) or [])
    dt = [["Time", "Drug", "Dose", "Route", "Remarks"]]
    for d in drugs:
        tt = _g(d, "time", None)
        dt.append([
            _fmt_time(tt) if isinstance(tt, (datetime, time)) else _txt(tt),
            _txt(_g(d, "drug_name", None)),
            _txt(_g(d, "dose", None)),
            _txt(_g(d, "route", None)),
            _txt(_g(d, "remarks", None)),
        ])

    drug_table = Table(dt,
                       colWidths=[16 * mm, 60 * mm, 20 * mm, 18 * mm, None],
                       repeatRows=1)
    _make_splittable(drug_table)
    drug_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.3),
            ("FONTSIZE", (0, 1), (-1, -1), 8.0),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))

    content: List[Any] = [
        _kv_table(header_rows, theme, col_widths=[45 * mm, None]),
    ]

    if preop_rows:
        content += [
            Spacer(1, 6),
            _mini_title("Pre-Op Snapshot (JSON)", theme),
            _kv_two_col(preop_rows, theme),
        ]

    content += [
        Spacer(1, 6),
        _mini_title("Vitals (Intra-op)", theme),
        vit_table,
        Spacer(1, 6),
        _mini_title("Drugs", theme),
        drug_table,
    ]

    return _section("Anaesthesia Record (FULL)", content, theme)


def _build_nursing(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "nursing_record", None)
    if not rec:
        return _section(
            "Nursing Record",
            [Paragraph("No nursing record recorded.", theme["Small"])], theme)

    rows = [
        ["Primary Nurse",
         _name_user(_g(rec, "primary_nurse", None))],
        ["Scrub Nurse", _txt(_g(rec, "scrub_nurse_name", None))],
        ["Circulating Nurse",
         _txt(_g(rec, "circulating_nurse_name", None))],
        ["Positioning", _txt(_g(rec, "positioning", None))],
        ["Skin Prep", _txt(_g(rec, "skin_prep", None))],
        ["Catheterisation",
         _txt(_g(rec, "catheterisation", None))],
        ["Diathermy Plate Site",
         _txt(_g(rec, "diathermy_plate_site", None))],
        ["Counts Initial Done",
         _yn(_g(rec, "counts_initial_done", None))],
        ["Counts Closure Done",
         _yn(_g(rec, "counts_closure_done", None))],
        ["Antibiotics Time",
         _fmt_time(_g(rec, "antibiotics_time", None))],
        ["Warming Measures",
         _txt(_g(rec, "warming_measures", None))],
        ["Notes", _txt(_g(rec, "notes", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
        ["Updated At", _fmt_dt(_g(rec, "updated_at", None))],
    ]
    return _section("Nursing Record",
                    [_kv_table(rows, theme, col_widths=[55 * mm, None])],
                    theme)


def _build_counts(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "counts_record", None)
    if not rec:
        return _section(
            "Instrument / Sponge Counts",
            [Paragraph("No counts record recorded.", theme["Small"])], theme)

    initial = _g(rec, "initial_count_data", None)
    final = _g(rec, "final_count_data", None)

    rows = [
        ["Discrepancy", _yn(_g(rec, "discrepancy", None))],
        ["Discrepancy Notes",
         _txt(_g(rec, "discrepancy_notes", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
        ["Updated At", _fmt_dt(_g(rec, "updated_at", None))],
    ]

    content: List[Any] = [_kv_table(rows, theme, col_widths=[55 * mm, None])]

    if isinstance(initial, dict):
        content += [
            Spacer(1, 6),
            _mini_title("Initial Count (JSON)", theme),
            _kv_two_col([[_labelize(k), _txt(v)]
                         for k, v in _flatten_kv(initial)], theme),
        ]
    elif initial is not None:
        content += [
            Spacer(1, 6),
            _mini_title("Initial Count (Raw)", theme),
            Paragraph(_lines(initial), theme["Value"])
        ]

    if isinstance(final, dict):
        content += [
            Spacer(1, 6),
            _mini_title("Final Count (JSON)", theme),
            _kv_two_col([[_labelize(k), _txt(v)]
                         for k, v in _flatten_kv(final)], theme),
        ]
    elif final is not None:
        content += [
            Spacer(1, 6),
            _mini_title("Final Count (Raw)", theme),
            Paragraph(_lines(final), theme["Value"])
        ]

    return _section("Instrument / Sponge Counts", content, theme)


def _build_implants(case: Any, theme: dict) -> List[Any]:
    items = list(_g(case, "implant_records", None) or [])
    if not items:
        return _section("Implants / Prosthesis",
                        [Paragraph("No implant records.", theme["Small"])],
                        theme)

    tbl = [["Implant", "Size", "Batch", "Lot", "Manufacturer", "Expiry"]]
    for it in items:
        tbl.append([
            _txt(_g(it, "implant_name", None)),
            _txt(_g(it, "size", None)),
            _txt(_g(it, "batch_no", None)),
            _txt(_g(it, "lot_no", None)),
            _txt(_g(it, "manufacturer", None)),
            _fmt_date(_g(it, "expiry_date", None)),
        ])

    t = Table(tbl,
              colWidths=[45 * mm, 14 * mm, 18 * mm, 18 * mm, None, 18 * mm],
              repeatRows=1)
    _make_splittable(t)
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("FONTSIZE", (0, 1), (-1, -1), 8.2),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
    return _section("Implants / Prosthesis", [t], theme)


def _build_blood(case: Any, theme: dict) -> List[Any]:
    items = list(_g(case, "blood_records", None) or [])
    if not items:
        return _section(
            "Blood & Fluids",
            [Paragraph("No blood transfusion records.", theme["Small"])],
            theme)

    tbl = [["Component", "Units", "Start", "End", "Reaction", "Notes"]]
    for it in items:
        tbl.append([
            _txt(_g(it, "component", None)),
            _txt(_g(it, "units", None)),
            _fmt_dt(_g(it, "start_time", None)),
            _fmt_dt(_g(it, "end_time", None)),
            _txt(_g(it, "reaction", None)),
            _txt(_g(it, "notes", None)),
        ])

    t = Table(tbl,
              colWidths=[24 * mm, 12 * mm, 28 * mm, 28 * mm, 22 * mm, None],
              repeatRows=1)
    _make_splittable(t)
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.5),
            ("FONTSIZE", (0, 1), (-1, -1), 8.2),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
    return _section("Blood & Fluids", [t], theme)


def _build_operation_note(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "operation_note", None)
    if not rec:
        return _section(
            "Operation Note",
            [Paragraph("No operation note recorded.", theme["Small"])], theme)

    surgeon = _g(rec, "surgeon", None)
    rows = [
        [
            "Surgeon",
            _name_user(surgeon) if surgeon else _txt(
                _g(rec, "surgeon_user_id", None))
        ],
        ["Pre-Op Diagnosis",
         _txt(_g(rec, "preop_diagnosis", None))],
        ["Post-Op Diagnosis",
         _txt(_g(rec, "postop_diagnosis", None))],
        ["Indication", _txt(_g(rec, "indication", None))],
        ["Findings", _txt(_g(rec, "findings", None))],
        ["Procedure Steps",
         _txt(_g(rec, "procedure_steps", None))],
        ["Blood Loss (ml)",
         _txt(_g(rec, "blood_loss_ml", None))],
        ["Complications",
         _txt(_g(rec, "complications", None))],
        ["Drains", _txt(_g(rec, "drains_details", None))],
        ["Post-Op Instructions",
         _txt(_g(rec, "postop_instructions", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
        ["Updated At", _fmt_dt(_g(rec, "updated_at", None))],
    ]
    return _section("Operation Note",
                    [_kv_table(rows, theme, col_widths=[55 * mm, None])],
                    theme)


def _build_pacu(case: Any, theme: dict) -> List[Any]:
    rec = _g(case, "pacu_record", None)
    if not rec:
        return _section(
            "PACU / Recovery",
            [Paragraph("No PACU record recorded.", theme["Small"])], theme)

    rows = [
        ["Nurse (user_id)",
         _txt(_g(rec, "nurse_user_id", None))],
        ["Admission Time",
         _fmt_dt(_g(rec, "admission_time", None))],
        ["Discharge Time",
         _fmt_dt(_g(rec, "discharge_time", None))],
        ["Disposition", _txt(_g(rec, "disposition", None))],
        ["Complications",
         _txt(_g(rec, "complications", None))],
        ["Created At", _fmt_dt(_g(rec, "created_at", None))],
    ]

    content: List[Any] = [_kv_table(rows, theme, col_widths=[55 * mm, None])]

    pain_scores = _g(rec, "pain_scores", None)
    if isinstance(pain_scores, dict) and pain_scores:
        ps = [[
            _labelize(k), _txt(v)
        ] for k, v in sorted(pain_scores.items(), key=lambda kv: str(kv[0]))]
        content += [
            Spacer(1, 6),
            _mini_title("Pain Scores", theme),
            _kv_two_col(ps, theme)
        ]
    elif pain_scores is not None:
        content += [
            Spacer(1, 6),
            _mini_title("Pain Scores (Raw)", theme),
            Paragraph(_lines(pain_scores), theme["Value"])
        ]

    vit = _g(rec, "vitals", None)
    if isinstance(vit, dict) and vit:
        vr = [[_labelize(k), _txt(v)]
              for k, v in sorted(vit.items(), key=lambda kv: str(kv[0]))]
        content += [
            Spacer(1, 6),
            _mini_title("Vitals (PACU)", theme),
            _kv_two_col(vr, theme)
        ]
    elif isinstance(vit, list) and vit:
        content += [Spacer(1, 6), _mini_title("Vitals (PACU)", theme)]
        for i, item in enumerate(vit[:50], start=1):
            content += [
                Paragraph(f"<b>Snapshot {i}</b>", theme["Label"]),
                Paragraph(_lines(_json_pretty(item)), theme["Mono"]),
                Spacer(1, 4),
            ]
    elif vit is not None:
        content += [
            Spacer(1, 6),
            _mini_title("Vitals (Raw)", theme),
            Paragraph(_lines(vit), theme["Value"])
        ]

    return _section("PACU / Recovery", content, theme)


def _build_cleaning_logs(case: Any, theme: dict) -> List[Any]:
    logs = list(_g(case, "cleaning_logs", None) or [])
    if not logs:
        return _section("Cleaning / Sterility Logs", [
            Paragraph("No cleaning logs recorded for this case.",
                      theme["Small"])
        ], theme)

    tbl = [["Date", "Session", "Method", "Done By", "Remarks"]]
    for it in logs:
        done_by = _g(it, "done_by", None)
        tbl.append([
            _fmt_date(_g(it, "date", None)),
            _txt(_g(it, "session", None)),
            _txt(_g(it, "method", None)),
            _name_user(done_by) if done_by else _txt(
                _g(it, "done_by_user_id", None)),
            _txt(_g(it, "remarks", None)),
        ])

    t = Table(tbl,
              colWidths=[18 * mm, 22 * mm, 45 * mm, 35 * mm, None],
              repeatRows=1)
    _make_splittable(t)
    t.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), theme["C"]["soft"]),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8.6),
            ("FONTSIZE", (0, 1), (-1, -1), 8.2),
            ("GRID", (0, 0), (-1, -1), 0.25, theme["C"]["border"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))

    return _section("Cleaning / Sterility Logs", [t], theme)


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

    an = _g(case, "anaesthesia_record", None)
    if an is not None and _g(an, "preop_vitals", None) is not None:
        parts.append(
            ("Anaesthesia — Pre-Op Snapshot JSON", _g(an, "preop_vitals",
                                                      None)))

    counts = _g(case, "counts_record", None)
    if counts is not None:
        if _g(counts, "initial_count_data", None) is not None:
            parts.append(
                ("Counts — Initial JSON", _g(counts, "initial_count_data",
                                             None)))
        if _g(counts, "final_count_data", None) is not None:
            parts.append(
                ("Counts — Final JSON", _g(counts, "final_count_data", None)))

    if not parts:
        return []

    content: List[Any] = []
    for title, data in parts:
        content.append(_mini_title(title, theme))
        content.append(Paragraph(_lines(_json_pretty(data)), theme["Mono"]))
        content.append(Spacer(1, 6))

    return _section("Appendix — Raw JSON Snapshots", content, theme)


# ============================================================
#  Header / Footer
# ============================================================


def _on_page(org_name: str, generated_by: Optional[str], theme: dict):
    C = theme["C"]

    def fn(canvas, doc):
        canvas.saveState()

        x0 = doc.leftMargin
        x1 = doc.pagesize[0] - doc.rightMargin
        y = doc.pagesize[1] - 12 * mm

        canvas.setStrokeColor(C["border"])
        canvas.setLineWidth(0.7)
        canvas.line(x0, y, x1, y)

        canvas.setFillColor(C["ink"])
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(x0, y + 4 * mm, org_name)

        canvas.setFillColor(C["sub"])
        canvas.setFont("Helvetica", 9)
        title = "OT CASE PDF"
        w = stringWidth(title, "Helvetica", 9)
        canvas.drawString(x1 - w, y + 4 * mm, title)

        canvas.setStrokeColor(C["border"])
        canvas.setLineWidth(0.7)
        canvas.line(x0, 12 * mm, x1, 12 * mm)

        canvas.setFillColor(C["sub"])
        canvas.setFont("Helvetica", 8)
        ts = datetime.now().strftime("%d-%b-%Y %H:%M")
        left = f"Generated: {ts}" + (f" · By: {generated_by}"
                                     if generated_by else "")
        canvas.drawString(x0, 8 * mm, left)

        page = f"Page {doc.page}"
        w2 = stringWidth(page, "Helvetica", 8)
        canvas.drawString(x1 - w2, 8 * mm, page)

        canvas.restoreState()

    return fn


# ============================================================
#  Main entry
# ============================================================


def build_ot_case_pdf(
    case: Any,
    org_name: str = "NUTRYAH HIMS",
    generated_by: Optional[str] = None,
) -> bytes:
    theme = _styles()
    buf = BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="OT Case PDF",
        author=org_name,
    )
    doc.allowSplitting = 1  # ✅ important

    story: List[Any] = []
    story.append(Paragraph("OT Case", theme["H1"]))
    story.append(
        Paragraph(
            "Full case PDF (all clinical tabs rendered into a single document).",
            theme["Small"]))
    story.append(Spacer(1, 8))

    story += _build_summary(case, theme)
    story += _build_case_overview(case, theme)
    story += _build_preanaesthesia(case, theme)
    story += _build_preop_checklist(case, theme)
    story += _build_safety(case, theme)
    story += _build_anaesthesia(case, theme)
    story += _build_nursing(case, theme)
    story += _build_counts(case, theme)
    story += _build_implants(case, theme)
    story += _build_blood(case, theme)
    story += _build_operation_note(case, theme)
    story += _build_pacu(case, theme)
    story += _build_cleaning_logs(case, theme)

    appendix = _build_appendix_raw(case, theme)
    if appendix:
        story.append(PageBreak())
        story += appendix

    doc.build(
        story,
        onFirstPage=_on_page(org_name, generated_by, theme),
        onLaterPages=_on_page(org_name, generated_by, theme),
    )
    return buf.getvalue()
