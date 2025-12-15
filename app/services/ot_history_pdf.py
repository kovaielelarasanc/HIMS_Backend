# app/services/ot_history_pdf.py
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


def _get(obj, name: str, default=None):
    return getattr(obj, name, default) if obj is not None else default


def _patient_name(p) -> str:
    if not p:
        return "—"
    prefix = _get(p, "prefix") or _get(p, "title")
    first = _get(p, "first_name") or _get(p, "given_name")
    last = _get(p, "last_name") or _get(p, "family_name")
    full = " ".join([x for x in [prefix, first, last] if x])
    return full or _get(p, "full_name") or _get(p, "display_name") or "—"


def _fmt_date(d) -> str:
    if not d:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    try:
        return d.strftime("%d-%b-%Y")
    except Exception:
        return str(d)


def _fmt_time(t) -> str:
    if not t:
        return "—"
    if isinstance(t, str):
        return t[:5]
    try:
        return t.strftime("%H:%M")
    except Exception:
        return str(t)[:5]


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%d-%b-%Y %H:%M")
    except Exception:
        return str(dt)


def build_patient_ot_history_pdf(
    *,
    patient,
    schedules: List,
    org_name: Optional[str] = None,
    generated_by: Optional[str] = None,
) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Patient OT History",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1",
                        parent=styles["Heading1"],
                        fontSize=14,
                        spaceAfter=6)
    h2 = ParagraphStyle("h2",
                        parent=styles["Heading2"],
                        fontSize=10,
                        spaceAfter=4)
    small = ParagraphStyle("small",
                           parent=styles["Normal"],
                           fontSize=8,
                           textColor=colors.grey)
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontSize=9)

    story = []

    title = org_name or "OT Department"
    story.append(Paragraph(title, h2))
    story.append(Paragraph("Patient OT History", h1))

    uhid = _get(patient, "uhid") or _get(patient, "uhid_number") or "—"
    sex = _get(patient, "sex") or _get(patient, "gender") or "—"
    age = _get(patient, "age_display") or _get(patient, "age") or "—"
    dob = _get(patient, "dob")

    story.append(
        Paragraph(
            f"<b>Patient:</b> {_patient_name(patient)} &nbsp;&nbsp; "
            f"<b>UHID:</b> {uhid} &nbsp;&nbsp; "
            f"<b>Age/Sex:</b> {age}/{sex} &nbsp;&nbsp; "
            f"<b>DOB:</b> {_fmt_date(dob)}",
            normal,
        ))
    story.append(Spacer(1, 6))

    data = [[
        "Date",
        "OT Reg No",
        "Procedure",
        "Surgeon",
        "Anaesthetist",
        "OT Location",
        "Planned",
        "Actual",
        "Status/Outcome",
    ]]

    if not schedules:
        data.append(
            ["—", "—", "No OT records found", "—", "—", "—", "—", "—", "—"])
    else:
        for s in schedules:
            case = _get(s, "case")
            bed = _get(s, "ot_bed") or _get(s, "bed")

            ward = _get(bed, "ward_name") or ""
            room = _get(bed, "room_name") or ""
            bcode = _get(bed, "code") or ""
            ot_loc = " · ".join([x for x in [ward, room, bcode] if x]) or "—"

            ot_reg = (_get(s, "reg_no") or _get(s, "display_number")
                      or _get(s, "ot_number") or _get(s, "schedule_code")
                      or "—")

            proc = _get(case, "final_procedure_name") or _get(
                s, "procedure_name") or "—"

            surgeon = _get(_get(s, "surgeon"), "full_name") or _get(
                s, "surgeon_name")
            if not surgeon and _get(s, "surgeon_user_id"):
                surgeon = f"Doctor #{_get(s,'surgeon_user_id')}"
            anaes = _get(_get(s, "anaesthetist"), "full_name") or _get(
                s, "anaesthetist_name")
            if not anaes and _get(s, "anaesthetist_user_id"):
                anaes = f"Doctor #{_get(s,'anaesthetist_user_id')}"

            planned = f"{_fmt_time(_get(s,'planned_start_time'))}–{_fmt_time(_get(s,'planned_end_time'))}"
            actual = f"{_fmt_dt(_get(case,'actual_start_time'))} → {_fmt_dt(_get(case,'actual_end_time'))}"

            status = _get(
                s, "status") or ("closed" if _get(case, "outcome") else "open")
            outcome = _get(case, "outcome")
            status_out = f"{status}" + (f" / {outcome}" if outcome else "")

            data.append([
                _fmt_date(_get(s, "date")),
                ot_reg,
                proc,
                surgeon or "—",
                anaes or "—",
                ot_loc,
                planned,
                actual,
                status_out,
            ])

    table = Table(
        data,
        colWidths=[
            18 * mm, 20 * mm, 34 * mm, 24 * mm, 26 * mm, 26 * mm, 22 * mm,
            28 * mm, 22 * mm
        ],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("FONTSIZE", (0, 1), (-1, -1), 7.6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f8fafc")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
    story.append(table)
    story.append(Spacer(1, 8))

    now = datetime.now().strftime("%d-%b-%Y %H:%M")
    footer = f"Generated: {now}"
    if generated_by:
        footer += f" | By: {generated_by}"
    story.append(Paragraph(footer, small))

    doc.build(story)
    return buf.getvalue()
