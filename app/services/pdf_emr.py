# FILE: app/services/pdf_emr.py
from __future__ import annotations

from io import BytesIO
from typing import Iterable, Optional, Dict, Any, List, Tuple
from datetime import datetime
from pathlib import Path
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader

from app.core.config import settings

# -------------------------------
# Formatting helpers
# -------------------------------


def _fmt(v: Any, dash: str = "—") -> str:
    if v is None:
        return dash
    if isinstance(v, str):
        s = v.strip()
        return s if s else dash
    return str(v)


def _fmt_money(v: Any, dash: str = "—") -> str:
    if v is None or v == "":
        return dash
    try:
        if isinstance(v, Decimal):
            return f"{v:.2f}"
        return f"{Decimal(str(v)):.2f}"
    except Exception:
        return _fmt(v, dash=dash)


def _fmt_dt(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, str):
        s = v.replace("T", " ").replace("Z", "").strip()
        return s[:16] if len(s) >= 16 else s
    return str(v)


def _wrap_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    line_height: float = 13,
) -> float:
    if not text:
        return y

    font_name = getattr(c, "_fontname", "Helvetica")
    font_size = getattr(c, "_fontsize", 9)

    lines = str(text).splitlines() or [""]
    for para in lines:
        words = para.split()
        if not words:
            y -= line_height
            continue

        line = ""
        for w in words:
            test = (line + " " + w).strip()
            if line and c.stringWidth(test, font_name, font_size) > max_width:
                c.drawString(x, y, line)
                y -= line_height
                line = w
            else:
                line = test

        if line:
            c.drawString(x, y, line)
            y -= line_height

    return y


def _load_branding_images(
        branding: Any | None) -> tuple[ImageReader | None, ImageReader | None]:
    header_img = footer_img = None
    if not branding:
        return None, None

    try:
        header_path = getattr(branding, "pdf_header_path", None)
        if header_path:
            hp = Path(settings.STORAGE_DIR).joinpath(header_path)
            if hp.exists():
                header_img = ImageReader(str(hp))
    except Exception:
        header_img = None

    try:
        footer_path = getattr(branding, "pdf_footer_path", None)
        if footer_path:
            fp = Path(settings.STORAGE_DIR).joinpath(footer_path)
            if fp.exists():
                footer_img = ImageReader(str(fp))
    except Exception:
        footer_img = None

    return header_img, footer_img


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _draw_kv(c: canvas.Canvas, y: float, left: float, width: float, label: str,
             value: Any) -> float:
    if value in (None, ""):
        return y
    return _wrap_text(c, f"{label}: {_fmt(value)}", left, y, width)


def _draw_dict_block(
    c: canvas.Canvas,
    y: float,
    left: float,
    width: float,
    title: str,
    d: Any,
    *,
    indent_mm: float = 5,
    max_items: int = 200,
) -> float:
    dd = _as_dict(d)
    if not dd:
        return y

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#111827"))
    c.drawString(left, y, f"{title}:")
    y -= 4 * mm

    c.setFont("Helvetica", 9)
    c.setFillColor(colors.black)

    count = 0
    for k, v in dd.items():
        count += 1
        if count > max_items:
            y = _wrap_text(c, "…", left + indent_mm * mm, y,
                           width - indent_mm * mm)
            break
        if isinstance(v, dict):
            y = _wrap_text(c, f"- {k}:", left + indent_mm * mm, y,
                           width - indent_mm * mm)
            for kk, vv in v.items():
                y = _wrap_text(c, f"  • {kk}: {_fmt(vv)}",
                               left + indent_mm * mm, y,
                               width - indent_mm * mm)
        elif isinstance(v, list):
            y = _wrap_text(c, f"- {k}:", left + indent_mm * mm, y,
                           width - indent_mm * mm)
            for it in v[:50]:
                if isinstance(it, dict):
                    bits = ", ".join(
                        [f"{a}={_fmt(b)}" for a, b in list(it.items())[:8]])
                    y = _wrap_text(c, f"  • {bits}", left + indent_mm * mm, y,
                                   width - indent_mm * mm)
                else:
                    y = _wrap_text(c, f"  • {_fmt(it)}", left + indent_mm * mm,
                                   y, width - indent_mm * mm)
        else:
            y = _wrap_text(c, f"- {k}: {_fmt(v)}", left + indent_mm * mm, y,
                           width - indent_mm * mm)

    return y


def generate_emr_pdf(
    patient: Dict[str, Any],
    items: Iterable[Dict[str, Any]],
    sections_selected: Optional[set[str]] = None,
    letterhead_bytes: Optional[bytes] = None,
    branding: Any | None = None,
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    LEFT = 20 * mm
    RIGHT = W - 20 * mm
    TOP = H - 20 * mm
    BOTTOM = 18 * mm
    CONTENT_W = RIGHT - LEFT

    header_img, footer_img = _load_branding_images(branding)
    if header_img is None and letterhead_bytes:
        try:
            header_img = ImageReader(BytesIO(letterhead_bytes))
        except Exception:
            header_img = None

    def section_of(t: str) -> str:
        if t in ("opd_appointment", "opd_visit"):
            return "opd"
        if t in ("opd_vitals", ):
            return "vitals"
        if t in ("rx", ):
            return "prescriptions"
        if t in ("opd_lab_order", "lab"):
            return "lab"
        if t in ("opd_radiology_order", "radiology"):
            return "radiology"
        if t in ("pharmacy_rx", "pharmacy"):
            return "pharmacy"
        if t.startswith("ipd_"):
            return "ipd"
        if t == "ot":
            return "ot"
        if t == "billing":
            return "billing"
        if t == "attachment":
            return "attachments"
        if t == "consent":
            return "consents"
        return "other"

    SECTION_ORDER: List[Tuple[str, str]] = [
        ("opd", "Outpatient (OPD)"),
        ("vitals", "Vitals"),
        ("prescriptions", "Prescriptions"),
        ("lab", "Laboratory"),
        ("radiology", "Radiology"),
        ("pharmacy", "Pharmacy"),
        ("ipd", "Inpatient (IPD)"),
        ("ot", "Operation Theatre"),
        ("billing", "Billing & Payments"),
        ("attachments", "Attachments"),
        ("consents", "Patient Consents"),
        ("other", "Other"),
    ]

    def draw_page_header() -> float:
        y = TOP

        if header_img is not None:
            try:
                iw, ih = header_img.getSize()
                avail_w = RIGHT - LEFT
                scale = avail_w / float(iw)
                draw_w = avail_w
                draw_h = ih * scale
                c.drawImage(
                    header_img,
                    LEFT,
                    H - draw_h - 1 * mm,
                    width=draw_w,
                    height=draw_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                y = H - draw_h - 14 * mm
            except Exception:
                y = TOP

        org_name = getattr(branding, "org_name", None) if branding else None
        org_tagline = getattr(branding, "org_tagline",
                              None) if branding else None
        if org_name:
            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.HexColor("#0F172A"))
            c.drawRightString(RIGHT, y + 2 * mm, org_name)
            if org_tagline:
                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor("#6B7280"))
                c.drawRightString(RIGHT, y - 2 * mm, org_tagline)
        y -= 8 * mm

        c.setFont("Helvetica-Bold", 13)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.drawString(LEFT, y, "Patient EMR Summary")
        y -= 6 * mm

        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#111827"))
        name = patient.get("name") or "—"
        uhid = patient.get("uhid") or "—"
        gender = patient.get("gender") or "—"
        dob = patient.get("dob") or "—"
        phone = patient.get("phone") or "—"
        email = patient.get("email") or "—"

        c.drawString(LEFT, y, f"Name: {name}        UHID: {uhid}")
        y -= 4.5 * mm
        c.drawString(LEFT, y, f"Gender: {gender}        DOB: {dob}")
        y -= 4.5 * mm
        c.drawString(LEFT, y, f"Phone: {phone}")
        y -= 4.5 * mm
        c.setFillColor(colors.HexColor("#4B5563"))
        c.drawString(
            LEFT,
            y,
            f"Email: {email}        Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} (UTC)",
        )
        y -= 5 * mm

        c.setStrokeColor(colors.HexColor("#CBD5E1"))
        c.line(LEFT, y, RIGHT, y)
        y -= 8 * mm
        return y

    def draw_footer():
        FOOTER_TEXT_H = 5 * mm
        y_line = BOTTOM + FOOTER_TEXT_H + 2 * mm

        if footer_img is not None:
            try:
                iw, ih = footer_img.getSize()
                avail_w = RIGHT - LEFT
                scale = avail_w / float(iw)
                draw_w = avail_w
                draw_h = ih * scale
                c.drawImage(
                    footer_img,
                    LEFT,
                    BOTTOM + FOOTER_TEXT_H,
                    width=draw_w,
                    height=draw_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                y_line = BOTTOM + FOOTER_TEXT_H + draw_h + 2 * mm
            except Exception:
                y_line = BOTTOM + FOOTER_TEXT_H + 2 * mm

        c.setStrokeColor(colors.HexColor("#E2E8F0"))
        c.line(LEFT, y_line, RIGHT, y_line)

        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#94A3B8"))

        org_name = getattr(branding, "org_name", None) if branding else None
        brand_text = "Generated by Nutryah HIMS/EMR"
        if org_name:
            brand_text = f"Generated by {org_name} via Nutryah HIMS/EMR"

        c.drawString(LEFT, BOTTOM, brand_text)
        c.drawRightString(RIGHT, BOTTOM, f"Page {c.getPageNumber()}")

    def page_break_if_needed(y: float, min_space: float = 30 * mm) -> float:
        if y < BOTTOM + min_space:
            draw_footer()
            c.showPage()
            return draw_page_header()
        return y

    # ---- Prepare data: sort + group by section ----
    def _ts_sort_key(it: Dict[str, Any]) -> str:
        return _fmt_dt(it.get("ts"))

    sorted_items = sorted(list(items or []), key=_ts_sort_key, reverse=True)

    section_map: Dict[str, List[Dict[str, Any]]] = {
        k: []
        for k, _ in SECTION_ORDER
    }
    for it in sorted_items:
        sec = section_of(it.get("type", "") or "")
        if sections_selected and sec not in sections_selected:
            continue
        if sec not in section_map:
            sec = "other"
        section_map.setdefault(sec, []).append(it)

    # ---- Render ----
    y = draw_page_header()

    for sec_key, sec_label in SECTION_ORDER:
        rows = section_map.get(sec_key) or []
        if not rows:
            continue

        y = page_break_if_needed(y)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.drawString(LEFT, y, sec_label)
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#6B7280"))
        c.drawRightString(RIGHT, y, f"{len(rows)} record(s)")
        y -= 3 * mm
        c.setStrokeColor(colors.HexColor("#E5E7EB"))
        c.line(LEFT, y, RIGHT, y)
        y -= 6 * mm

        for it in rows:
            y = page_break_if_needed(y)

            raw_ts = it.get("ts")
            when_str = _fmt_dt(raw_ts)
            title = it.get("title") or "Event"
            typ = it.get("type") or ""
            data = _as_dict(it.get("data") or {})

            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.HexColor("#111827"))
            header_line = f"{when_str}  |  {title}" if when_str else title
            c.drawString(LEFT, y, header_line)

            status = it.get("status")
            if status:
                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor("#6B7280"))
                c.drawRightString(RIGHT, y, str(status))
            y -= 5 * mm

            meta_bits: List[str] = []
            if it.get("doctor_name"):
                meta_bits.append(f"Doctor: {it['doctor_name']}")
            if it.get("department_name"):
                meta_bits.append(f"Dept: {it['department_name']}")
            if it.get("location_name"):
                meta_bits.append(f"Location: {it['location_name']}")
            if meta_bits:
                c.setFont("Helvetica", 9)
                c.setFillColor(colors.HexColor("#4B5563"))
                y = _wrap_text(c, "  •  ".join(meta_bits), LEFT, y, CONTENT_W)
                y -= 1 * mm

            c.setFont("Helvetica", 9)
            c.setFillColor(colors.black)

            # ---------------- OPD Appointment ----------------
            if typ == "opd_appointment":
                ap = _as_dict(data.get("appointment"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Purpose",
                             ap.get("purpose"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Date", ap.get("date"))
                y = _draw_kv(
                    c, y, LEFT, CONTENT_W, "Slot",
                    f"{_fmt(ap.get('slot_start'))} - {_fmt(ap.get('slot_end'))}"
                )
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", ap.get("status"))

            # ---------------- OPD Visit ----------------
            elif typ == "opd_visit":
                v = _as_dict(data.get("visit"))
                y = _wrap_text(
                    c, f"Chief Complaint: {_fmt(v.get('chief_complaint'))}",
                    LEFT, y, CONTENT_W)
                y = _wrap_text(c, f"Symptoms: {_fmt(v.get('symptoms'))}", LEFT,
                               y, CONTENT_W)
                y = _wrap_text(c, f"Subjective:\n{_fmt(v.get('subjective'))}",
                               LEFT, y, CONTENT_W)
                y = _wrap_text(c, f"Objective:\n{_fmt(v.get('objective'))}",
                               LEFT, y, CONTENT_W)
                y = _wrap_text(c, f"Assessment:\n{_fmt(v.get('assessment'))}",
                               LEFT, y, CONTENT_W)
                y = _wrap_text(c, f"Plan:\n{_fmt(v.get('plan'))}", LEFT, y,
                               CONTENT_W)

            # ---------------- OPD Vitals ----------------
            elif typ == "opd_vitals":
                vt = _as_dict(data.get("vitals"))
                bmi = data.get("bmi")
                bp = "—"
                if vt.get("bp_systolic") and vt.get("bp_diastolic"):
                    bp = f"{vt.get('bp_systolic')}/{vt.get('bp_diastolic')} mmHg"
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Recorded at",
                             vt.get("created_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Height (cm)",
                             vt.get("height_cm"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Weight (kg)",
                             vt.get("weight_kg"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "BMI", bmi)
                y = _draw_kv(c, y, LEFT, CONTENT_W, "BP", bp)
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Pulse", vt.get("pulse"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "RR", vt.get("rr"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Temp (°C)",
                             vt.get("temp_c"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "SpO₂ (%)", vt.get("spo2"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Notes", vt.get("notes"))

            # ---------------- Prescription ----------------
            elif typ == "rx":
                rx = _as_dict(data.get("prescription"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Notes", rx.get("notes"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Signed at",
                             rx.get("signed_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Signed by",
                             data.get("signed_by_name"))

                items_block = _as_list(data.get("items"))
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for di in items_block:
                        di = _as_dict(di)
                        y = page_break_if_needed(y)
                        line = (
                            f"- {_fmt(di.get('drug_name'), '')} {_fmt(di.get('strength'), '')}"
                            f" • {_fmt(di.get('frequency'), '')}"
                            f" • {_fmt(di.get('duration_days'), '')}d"
                            f" • Qty {_fmt(di.get('quantity'), '')}").strip()
                        y = _wrap_text(c, line, LEFT + 5 * mm, y,
                                       CONTENT_W - 5 * mm)

            # ---------------- OPD Lab Order ----------------
            elif typ == "opd_lab_order":
                lo = _as_dict(data.get("opd_lab_order"))
                test = _as_dict(data.get("test"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Test",
                             test.get("name") or lo.get("test_name"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", lo.get("status"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Ordered at",
                             lo.get("ordered_at"))

            # ---------------- OPD Radiology Order ----------------
            elif typ == "opd_radiology_order":
                ro = _as_dict(data.get("opd_radiology_order"))
                test = _as_dict(data.get("test"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Test",
                             test.get("name") or ro.get("test_name"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", ro.get("status"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Ordered at",
                             ro.get("ordered_at"))

            # ---------------- LIS Result ----------------
            elif typ == "lab":
                o = _as_dict(data.get("lis_order"))
                itx = _as_dict(data.get("lis_item"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Order ID", o.get("id"))
                y = _draw_kv(
                    c, y, LEFT, CONTENT_W, "Test",
                    f"{_fmt(itx.get('test_name'))} ({_fmt(itx.get('test_code'))})"
                )
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Result",
                             itx.get("result_value"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Unit", itx.get("unit"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Normal range",
                             itx.get("normal_range"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status",
                             itx.get("status"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Result at",
                             itx.get("result_at"))

                lines = _as_list(data.get("result_lines"))
                if lines:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Result Lines:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for ln in lines[:80]:
                        ln = _as_dict(ln)
                        y = page_break_if_needed(y)
                        y = _wrap_text(
                            c,
                            f"- {_fmt(ln.get('name'))}: {_fmt(ln.get('value'))} {_fmt(ln.get('unit'), '')}",
                            LEFT + 5 * mm, y, CONTENT_W - 5 * mm)

            # ---------------- RIS ----------------
            elif typ == "radiology":
                ro = _as_dict(data.get("ris_order"))
                y = _draw_kv(
                    c, y, LEFT, CONTENT_W, "Test",
                    f"{_fmt(ro.get('test_name'))} ({_fmt(ro.get('test_code'))})"
                )
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Modality",
                             ro.get("modality"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", ro.get("status"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Reported at",
                             ro.get("reported_at"))
                if ro.get("report_text"):
                    y = _wrap_text(c,
                                   f"Report:\n{_fmt(ro.get('report_text'))}",
                                   LEFT, y, CONTENT_W)

            # ---------------- Pharmacy RX ----------------
            elif typ == "pharmacy_rx":
                pr = _as_dict(data.get("pharmacy_prescription"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Type", pr.get("type"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", pr.get("status"))
                lines = _as_list(data.get("lines"))
                if lines:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Lines:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for di in lines[:120]:
                        di = _as_dict(di)
                        y = page_break_if_needed(y)
                        y = _wrap_text(
                            c,
                            f"- {_fmt(di.get('medicine_name') or di.get('medicine_id'))} • Qty {_fmt(di.get('qty'))}",
                            LEFT + 5 * mm, y, CONTENT_W - 5 * mm)

            # ---------------- Pharmacy Sale ----------------
            elif typ == "pharmacy":
                s = _as_dict(data.get("pharmacy_sale"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Sale ID", s.get("id"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Payment",
                             s.get("payment_mode"))
                y = _draw_kv(
                    c, y, LEFT, CONTENT_W, "Net",
                    _fmt_money(s.get("net_amount") or s.get("total_amount"),
                               dash="0.00"))
                items_block = _as_list(data.get("items"))
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for di in items_block[:150]:
                        di = _as_dict(di)
                        y = page_break_if_needed(y)
                        y = _wrap_text(
                            c,
                            f"- {_fmt(di.get('medicine_name') or di.get('medicine_id'))} • Qty {_fmt(di.get('qty'))}",
                            LEFT + 5 * mm, y, CONTENT_W - 5 * mm)

            # ---------------- IPD Admission ----------------
            elif typ == "ipd_admission":
                a = _as_dict(data.get("admission"))
                bed = _as_dict(data.get("current_bed"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Admission Code",
                             a.get("display_code") or a.get("admission_code"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Admitted at",
                             a.get("admitted_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status", a.get("status"))
                if bed:
                    y = _draw_kv(
                        c, y, LEFT, CONTENT_W, "Current Bed",
                        bed.get("code") or bed.get("bed_code")
                        or bed.get("name"))

            # ---------------- IPD Transfer ----------------
            elif typ == "ipd_transfer":
                t = _as_dict(data.get("transfer"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Transferred at",
                             t.get("transferred_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Reason", t.get("reason"))

            # ---------------- IPD Discharge ----------------
            elif typ == "ipd_discharge":
                ds = _as_dict(data.get("discharge_summary"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Finalized",
                             ds.get("finalized"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Finalized at",
                             ds.get("finalized_at"))
                y = _wrap_text(
                    c,
                    f"Final Diagnosis:\n{_fmt(ds.get('final_diagnosis_primary'))}",
                    LEFT, y, CONTENT_W)
                y = _wrap_text(
                    c, f"Hospital Course:\n{_fmt(ds.get('hospital_course'))}",
                    LEFT, y, CONTENT_W)

            # ---------------- IPD Vitals ----------------
            elif typ == "ipd_vitals":
                v = _as_dict(data.get("ipd_vitals"))
                bp = "—"
                if v.get("bp_systolic") and v.get("bp_diastolic"):
                    bp = f"{v.get('bp_systolic')}/{v.get('bp_diastolic')} mmHg"
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Recorded at",
                             v.get("recorded_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "BP", bp)
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Pulse", v.get("pulse"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "RR", v.get("rr"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Temp (°C)",
                             v.get("temp_c"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "SpO₂ (%)", v.get("spo2"))

            # ---------------- ✅ IPD Nursing Note ----------------
            elif typ == "ipd_nursing_note":
                nn = _as_dict(data.get("nursing_note"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Note Type",
                             nn.get("note_type"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Shift", nn.get("shift"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Entry Time",
                             nn.get("entry_time"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Nurse",
                             data.get("nurse_name"))

                y = _wrap_text(
                    c,
                    f"Patient Condition:\n{_fmt(nn.get('patient_condition'))}",
                    LEFT, y, CONTENT_W)
                y = _wrap_text(
                    c,
                    f"Significant Events:\n{_fmt(nn.get('significant_events'))}",
                    LEFT, y, CONTENT_W)
                y = _wrap_text(
                    c,
                    f"Nursing Interventions:\n{_fmt(nn.get('nursing_interventions'))}",
                    LEFT, y, CONTENT_W)
                y = _wrap_text(
                    c,
                    f"Response / Progress:\n{_fmt(nn.get('response_progress'))}",
                    LEFT, y, CONTENT_W)
                if nn.get("handover_note"):
                    y = _wrap_text(
                        c, f"Handover:\n{_fmt(nn.get('handover_note'))}", LEFT,
                        y, CONTENT_W)

                vs = _as_dict(data.get("linked_vitals"))
                if vs:
                    bp2 = "—"
                    if vs.get("bp_systolic") and vs.get("bp_diastolic"):
                        bp2 = f"{vs.get('bp_systolic')}/{vs.get('bp_diastolic')} mmHg"
                    y = _wrap_text(
                        c, "Linked Vitals:\n"
                        f"BP {bp2} • Pulse {_fmt(vs.get('pulse'))} • RR {_fmt(vs.get('rr'))} • "
                        f"SpO₂ {_fmt(vs.get('spo2'))} • Temp {_fmt(vs.get('temp_c'))} • At {_fmt(vs.get('recorded_at'))}",
                        LEFT, y, CONTENT_W)

            # ---------------- IPD Intake/Output ----------------
            elif typ == "ipd_intake_output":
                io = _as_dict(data.get("intake_output"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Recorded at",
                             io.get("recorded_at"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Intake (ml)",
                             io.get("intake_ml"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Urine (ml)",
                             io.get("urine_ml"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Drains (ml)",
                             io.get("drains_ml"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Remarks",
                             io.get("remarks"))

            # ---------------- IPD Round ----------------
            elif typ == "ipd_round":
                rd = _as_dict(data.get("round"))
                y = _wrap_text(c, f"Notes:\n{_fmt(rd.get('notes'))}", LEFT, y,
                               CONTENT_W)

            # ---------------- IPD Progress ----------------
            elif typ == "ipd_progress":
                pn = _as_dict(data.get("progress_note"))
                y = _wrap_text(c,
                               f"Observation:\n{_fmt(pn.get('observation'))}",
                               LEFT, y, CONTENT_W)
                y = _wrap_text(c, f"Plan:\n{_fmt(pn.get('plan'))}", LEFT, y,
                               CONTENT_W)

            # ---------------- OT (Schedule bundle + OtOrder) ----------------
            elif typ == "ot":
                source = data.get("source")

                if source == "ot_schedule":
                    oc = _as_dict(data.get("ot_case"))
                    sc = _as_dict(oc.get("schedule"))
                    case = _as_dict(oc.get("case"))
                    ot_bed = _as_dict(oc.get("ot_bed"))
                    surgeon = _as_dict(oc.get("surgeon"))
                    anaes = _as_dict(oc.get("anaesthetist"))

                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Scheduled Date",
                                 sc.get("date"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Planned Start",
                                 sc.get("planned_start_time"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Planned End",
                                 sc.get("planned_end_time"))
                    if ot_bed:
                        y = _draw_kv(
                            c, y, LEFT, CONTENT_W, "OT Bed",
                            ot_bed.get("name") or ot_bed.get("code")
                            or ot_bed.get("bed_code"))
                    if surgeon:
                        y = _draw_kv(
                            c, y, LEFT, CONTENT_W, "Surgeon",
                            surgeon.get("full_name") or surgeon.get("name"))
                    if anaes:
                        y = _draw_kv(
                            c, y, LEFT, CONTENT_W, "Anaesthetist",
                            anaes.get("full_name") or anaes.get("name"))

                    # Procedures
                    procs = _as_list(oc.get("schedule_procedures"))
                    if procs:
                        names = []
                        for p in procs:
                            p = _as_dict(p)
                            pr = _as_dict(p.get("procedure"))
                            nm = pr.get("name") or pr.get("code")
                            if nm:
                                names.append(nm)
                        if names:
                            y = _wrap_text(
                                c, "Procedures:\n" +
                                "\n".join([f"- {x}" for x in names]), LEFT, y,
                                CONTENT_W)

                    # Case actual times
                    if case:
                        y = _draw_kv(c, y, LEFT, CONTENT_W, "Actual Start",
                                     case.get("actual_start_time"))
                        y = _draw_kv(c, y, LEFT, CONTENT_W, "Actual End",
                                     case.get("actual_end_time"))

                    # Pre-op checklist
                    preop = _as_dict(oc.get("preop_checklist"))
                    if preop:
                        preop_json = preop.get("data")
                        y = _draw_dict_block(c, y, LEFT, CONTENT_W,
                                             "Pre-op Checklist", preop_json)

                    # Anaesthesia (header + vitals + drugs)
                    ana = _as_dict(oc.get("anaesthesia_record"))
                    if ana:
                        hdr = _as_dict(ana.get("header"))
                        y = _draw_dict_block(c, y, LEFT, CONTENT_W,
                                             "Anaesthesia Header", hdr)

                        vitals = _as_list(ana.get("vitals"))
                        if vitals:
                            c.setFont("Helvetica-Bold", 9)
                            c.setFillColor(colors.HexColor("#111827"))
                            c.drawString(LEFT, y, "Anaesthesia Vitals:")
                            y -= 4 * mm
                            c.setFont("Helvetica", 9)
                            c.setFillColor(colors.black)
                            for v in vitals[:200]:
                                v = _as_dict(v)
                                y = page_break_if_needed(y)
                                line = (
                                    f"- {_fmt(v.get('time'))} | "
                                    f"BP {_fmt(v.get('bp_systolic'))}/{_fmt(v.get('bp_diastolic'))} "
                                    f"| Pulse {_fmt(v.get('pulse'))} "
                                    f"| SpO₂ {_fmt(v.get('spo2'))} "
                                    f"| RR {_fmt(v.get('rr'))} "
                                    f"| Temp {_fmt(v.get('temp_c'))}")
                                y = _wrap_text(c, line, LEFT + 5 * mm, y,
                                               CONTENT_W - 5 * mm)

                        drugs = _as_list(ana.get("drugs"))
                        if drugs:
                            c.setFont("Helvetica-Bold", 9)
                            c.setFillColor(colors.HexColor("#111827"))
                            c.drawString(LEFT, y, "Anaesthesia Drugs:")
                            y -= 4 * mm
                            c.setFont("Helvetica", 9)
                            c.setFillColor(colors.black)
                            for d in drugs[:200]:
                                d = _as_dict(d)
                                y = page_break_if_needed(y)
                                line = (
                                    f"- {_fmt(d.get('time'))} | "
                                    f"{_fmt(d.get('drug_name') or d.get('drug'))} "
                                    f"{_fmt(d.get('dose'), '')} {_fmt(d.get('unit'), '')} "
                                    f"{_fmt(d.get('route'), '')}").strip()
                                y = _wrap_text(c, line, LEFT + 5 * mm, y,
                                               CONTENT_W - 5 * mm)

                else:
                    # OtOrder style
                    o = _as_dict(data.get("ot_order"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Surgery",
                                 o.get("surgery_name"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Scheduled Start",
                                 o.get("scheduled_start"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Scheduled End",
                                 o.get("scheduled_end"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Actual Start",
                                 o.get("actual_start"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Actual End",
                                 o.get("actual_end"))
                    y = _draw_kv(c, y, LEFT, CONTENT_W, "Status",
                                 o.get("status"))
                    y = _wrap_text(
                        c, f"Pre-op Notes:\n{_fmt(o.get('preop_notes'))}",
                        LEFT, y, CONTENT_W)
                    y = _wrap_text(
                        c, f"Post-op Notes:\n{_fmt(o.get('postop_notes'))}",
                        LEFT, y, CONTENT_W)

            # ---------------- Billing ----------------
            elif typ == "billing":
                inv = _as_dict(data.get("invoice"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Invoice ID",
                             inv.get("id"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Status",
                             inv.get("status"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Net Total",
                             _fmt_money(inv.get("net_total"), dash="0.00"))

                items_block = _as_list(data.get("items"))
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for li in items_block[:200]:
                        li = _as_dict(li)
                        y = page_break_if_needed(y)
                        lt = _fmt_money(li.get("line_total"), dash="0.00")
                        line = (
                            f"- {_fmt(li.get('service_type'), '')}: {_fmt(li.get('description'), '')}"
                            f" • Qty {_fmt(li.get('quantity'), '')} • Rs {lt}"
                        ).strip()
                        y = _wrap_text(c, line, LEFT + 5 * mm, y,
                                       CONTENT_W - 5 * mm)

                pays = _as_list(data.get("payments"))
                if pays:
                    c.setFont("Helvetica-Bold", 9)
                    c.setFillColor(colors.HexColor("#111827"))
                    c.drawString(LEFT, y, "Payments:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    c.setFillColor(colors.black)
                    for pmt in pays[:200]:
                        pmt = _as_dict(pmt)
                        y = page_break_if_needed(y)
                        line = f"- {_fmt(pmt.get('mode'), '')} • Rs {_fmt_money(pmt.get('amount'), dash='0.00')} • {_fmt(pmt.get('paid_at'), '')}"
                        y = _wrap_text(c, line, LEFT + 5 * mm, y,
                                       CONTENT_W - 5 * mm)

            # ---------------- Consent ----------------
            elif typ == "consent":
                cc = _as_dict(data.get("consent"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Type", cc.get("type"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Captured at",
                             cc.get("captured_at"))
                if cc.get("text"):
                    y = _wrap_text(c, f"Text:\n{_fmt(cc.get('text'))}", LEFT,
                                   y, CONTENT_W)

            # ---------------- Attachment ----------------
            elif typ == "attachment":
                ff = _as_dict(data.get("file"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "File", ff.get("filename"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Note", ff.get("note"))
                y = _draw_kv(c, y, LEFT, CONTENT_W, "Uploaded at",
                             ff.get("uploaded_at"))

            # Attachments list if present in timeline item root
            atts = _as_list(it.get("attachments") or [])
            if atts:
                c.setFont("Helvetica-Bold", 9)
                c.setFillColor(colors.HexColor("#111827"))
                c.drawString(LEFT, y, "Attachments:")
                y -= 4 * mm
                c.setFont("Helvetica", 9)
                c.setFillColor(colors.black)
                for a in atts[:50]:
                    a = _as_dict(a)
                    y = page_break_if_needed(y)
                    label = a.get("label") or "file"
                    y = _wrap_text(c, f"- {label}", LEFT + 5 * mm, y,
                                   CONTENT_W - 5 * mm)

            y -= 3 * mm
            c.setStrokeColor(colors.HexColor("#E5E7EB"))
            c.line(LEFT, y, RIGHT, y)
            y -= 4 * mm

    draw_footer()
    c.save()
    return buf.getvalue()
