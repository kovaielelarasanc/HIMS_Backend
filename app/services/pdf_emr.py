# FILE: app/services/pdf_emr.py
from __future__ import annotations

from io import BytesIO
from typing import Iterable, Optional, Dict, Any, List
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader


def _wrap_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    line_height: float = 13,
) -> float:
    """Simple left-aligned word wrapping."""
    if not text:
        return y
    words = (text or "").split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test) > max_width:
            c.drawString(x, y, line)
            y -= line_height
            line = w
        else:
            line = test
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y


def _kv(
    c: canvas.Canvas,
    x: float,
    y: float,
    label: str,
    value: Any,
    max_width: float,
) -> float:
    """Label: value helper (kept for compatibility, even if not used much)."""
    if value is None or value == "":
        return y
    return _wrap_text(c, f"{label}: {value}", x, y, max_width)


def _section_rule(c: canvas.Canvas, y: float, W: float) -> float:
    """Thin separator line (kept for compatibility)."""
    c.setStrokeColor(colors.HexColor("#E2E8F0"))
    c.line(20 * mm, y, W - 20 * mm, y)
    return y - 6 * mm


def generate_emr_pdf(
    patient: Dict[str, Any],
    items: Iterable[Dict[str, Any]],
    sections_selected: Optional[set[str]] = None,
    letterhead_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Generate a professional EMR summary PDF.

    Layout:
    - Optional hospital letterhead at the top (first page only)
    - Patient banner (Name, UHID, Gender, DOB, Phone, Email, Generated date)
    - Sections grouped by clinical area:
      OPD, Vitals, Prescriptions, Lab, Radiology, Pharmacy, IPD, OT,
      Billing, Attachments, Consents, Other
    - Each event shows:
      Date/time, title, status, doctor/department/location,
      followed by structured details.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # Margins / layout
    LEFT = 20 * mm
    RIGHT = W - 20 * mm
    TOP = H - 20 * mm
    BOTTOM = 18 * mm
    CONTENT_W = RIGHT - LEFT

    # ---- section mapping (aligned with EmrExport sections) ----
    def section_of(t: str) -> str:
        if t == "opd_visit":
            return "opd"
        if t == "opd_vitals":
            return "vitals"
        if t == "rx":
            return "prescriptions"
        if t == "lab":
            return "lab"
        if t == "radiology":
            return "radiology"
        if t == "pharmacy":
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

    SECTION_ORDER = [
        ("opd", "Outpatient (OPD) Encounters"),
        ("vitals", "Vitals"),
        ("prescriptions", "Prescriptions"),
        ("lab", "Laboratory Investigations"),
        ("radiology", "Radiology"),
        ("pharmacy", "Pharmacy Dispense"),
        ("ipd", "Inpatient (IPD)"),
        ("ot", "Operation Theatre"),
        ("billing", "Billing & Payments"),
        ("attachments", "Attachments"),
        ("consents", "Patient Consents"),
        ("other", "Other"),
    ]

    # ---- page helpers ----
    # Cache letterhead image once
    letterhead_img = None
    if letterhead_bytes:
        try:
            letterhead_img = ImageReader(BytesIO(letterhead_bytes))
        except Exception:
            letterhead_img = None

    def draw_page_header(first: bool = False) -> float:
        """Top-of-page: optional letterhead, then patient banner and rule."""
        y = TOP

        # Optional letterhead only on first page
        if first and letterhead_img is not None:
            iw, ih = letterhead_img.getSize()
            avail_w = RIGHT - LEFT
            scale = avail_w / float(iw)
            draw_w = avail_w
            draw_h = ih * scale
            c.drawImage(
                letterhead_img,
                LEFT,
                H - draw_h - 10 * mm,
                width=draw_w,
                height=draw_h,
                preserveAspectRatio=True,
                mask="auto",
            )
            y = H - draw_h - 16 * mm

        # Title
        c.setFont("Helvetica-Bold", 13)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.drawString(LEFT, y, "Patient EMR Summary")
        y -= 6 * mm

        # Patient banner
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
            f"Email: {email}        Generated at: "
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} (UTC)",
        )
        y -= 5 * mm

        # Separator
        c.setStrokeColor(colors.HexColor("#CBD5E1"))
        c.line(LEFT, y, RIGHT, y)
        y -= 8 * mm
        return y

    def draw_footer():
        """Footer with divider, brand and page number."""
        c.setStrokeColor(colors.HexColor("#E2E8F0"))
        c.line(LEFT, BOTTOM + 4 * mm, RIGHT, BOTTOM + 4 * mm)
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#94A3B8"))
        c.drawString(LEFT, BOTTOM, "Generated by Nutryah HIMS/EMR")
        c.drawRightString(RIGHT, BOTTOM, f"Page {c.getPageNumber()}")

    def page_break_if_needed(y: float) -> float:
        """Ensure we keep enough space before starting a new block."""
        if y < BOTTOM + 30 * mm:
            draw_footer()
            c.showPage()
            return draw_page_header(first=False)
        return y

    # ---- Prepare data: sort + group by section ----
    sorted_items = sorted(items or [],
                          key=lambda x: x.get("ts", ""),
                          reverse=True)

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
    y = draw_page_header(first=True)

    for sec_key, sec_label in SECTION_ORDER:
        rows = section_map.get(sec_key) or []
        if not rows:
            continue

        # Section heading
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

        # Each entry inside section
        for it in rows:
            y = page_break_if_needed(y)

            # Header line for this event
            raw_ts = it.get("ts")
            when_str = ""
            if isinstance(raw_ts, datetime):
                when_str = raw_ts.strftime("%Y-%m-%d %H:%M")
            elif isinstance(raw_ts, str):
                when_str = raw_ts.replace("T", " ")[:16]
            title = it.get("title") or "Event"

            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.HexColor("#111827"))
            header_line = f"{when_str}  |  {title}" if when_str else title
            c.drawString(LEFT, y, header_line)

            status = it.get("status")
            if status:
                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor("#6B7280"))
                c.drawRightString(RIGHT, y, status)
            y -= 5 * mm

            # Meta: doctor / department / location
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

            # Module specific block
            data = it.get("data") or {}
            typ = it.get("type")

            c.setFont("Helvetica", 9)
            c.setFillColor(colors.black)

            # ---------- OPD Visit ----------
            if typ == "opd_visit":
                y = _wrap_text(
                    c,
                    f"Chief Complaint: {data.get('chief_complaint') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Symptoms: {data.get('symptoms') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Subjective: {data.get('subjective') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Objective: {data.get('objective') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Assessment: {data.get('assessment') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Plan: {data.get('plan') or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                if data.get("episode_id"):
                    y = _wrap_text(
                        c,
                        f"Episode: {data.get('episode_id')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                ap = data.get("appointment") or {}
                if ap:
                    if ap.get("date"):
                        y = _wrap_text(
                            c,
                            f"Appointment Date: {ap.get('date')}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )
                    slot = ""
                    if ap.get("slot_start") or ap.get("slot_end"):
                        slot = (f"{ap.get('slot_start') or ''} – "
                                f"{ap.get('slot_end') or ''}")
                    if slot:
                        y = _wrap_text(
                            c,
                            f"Slot: {slot}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )

            # ---------- Vitals ----------
            elif typ == "opd_vitals":
                y = _wrap_text(
                    c,
                    f"Recorded at: {data.get('recorded_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Height (cm): {data.get('height_cm')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Weight (kg): {data.get('weight_kg')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"BMI: {data.get('bmi')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                bp = None
                if data.get("bp_systolic") and data.get("bp_diastolic"):
                    bp = (f"{data.get('bp_systolic')}/"
                          f"{data.get('bp_diastolic')} mmHg")
                y = _wrap_text(
                    c,
                    f"BP: {bp or '—'}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Pulse: {data.get('pulse')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"RR: {data.get('rr')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Temp (°C): {data.get('temp_c')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                # Avoid SpO₂ subscript – use SpO2
                y = _wrap_text(
                    c,
                    f"SpO2 (%): {data.get('spo2')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                if data.get("notes"):
                    y = _wrap_text(
                        c,
                        f"Notes: {data.get('notes')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )

            # ---------- Prescription ----------
            elif typ == "rx":
                if data.get("notes"):
                    y = _wrap_text(
                        c,
                        f"Notes: {data.get('notes')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                if data.get("signed_at"):
                    y = _wrap_text(
                        c,
                        f"Signed at: {data.get('signed_at')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                if data.get("signed_by"):
                    y = _wrap_text(
                        c,
                        f"Signed by: {data.get('signed_by')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                items_block = data.get("items") or []
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    for di in items_block:
                        y = page_break_if_needed(y)
                        line = (
                            f"- {di.get('drug_name') or ''} "
                            f"{di.get('strength') or ''}"
                            f" • {di.get('frequency') or ''}"
                            f" • {di.get('duration_days') or ''}d"
                            f" • Qty {di.get('quantity') or ''}"
                            f" • Rs {float(di.get('line_total') or 0):.2f}")
                        y = _wrap_text(
                            c,
                            line,
                            LEFT + 5 * mm,
                            y,
                            CONTENT_W - 5 * mm,
                        )

            # ---------- Lab ----------
            elif typ == "lab":
                item = data.get("item") or {}
                y = _wrap_text(
                    c,
                    f"Order ID: {data.get('order_id')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Priority: {data.get('priority')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Collected at: {data.get('collected_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Reported at: {data.get('reported_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Test: {item.get('test_name')} "
                    f"({item.get('test_code')})",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Unit: {item.get('unit')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Normal range: {item.get('normal_range')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Specimen: {item.get('specimen_type')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Status: {item.get('status')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Result: {item.get('result_value')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                if item.get("is_critical") is not None:
                    crit_text = "Yes" if item.get("is_critical") else "No"
                    y = _wrap_text(
                        c,
                        f"Critical: {crit_text}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                y = _wrap_text(
                    c,
                    f"Result at: {item.get('result_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )

            # ---------- Radiology ----------
            elif typ == "radiology":
                y = _wrap_text(
                    c,
                    f"Test: {data.get('test_name')} "
                    f"({data.get('test_code')})",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Modality: {data.get('modality')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Status: {data.get('status')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Scheduled at: {data.get('scheduled_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Scanned at: {data.get('scanned_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Reported at: {data.get('reported_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Approved at: {data.get('approved_at')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                if data.get("report_text"):
                    y = _wrap_text(
                        c,
                        f"Report: {data.get('report_text')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )

            # ---------- Pharmacy ----------
            elif typ == "pharmacy":
                y = _wrap_text(
                    c,
                    f"Sale ID: {data.get('sale_id')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Context: {data.get('context_type')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Payment: {data.get('payment_mode')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Total: Rs {data.get('total_amount')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                items_block = data.get("items") or []
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    for di in items_block:
                        y = page_break_if_needed(y)
                        line = (f"- {di.get('medicine_name')}"
                                "or di.get('medicine_id')}"
                                f" • Qty {di.get('qty') or ''}"
                                f" • Rs {float(di.get('amount') or 0):.2f}")
                        y = _wrap_text(
                            c,
                            line,
                            LEFT + 5 * mm,
                            y,
                            CONTENT_W - 5 * mm,
                        )

            # ---------- IPD Admission ----------
            elif typ == "ipd_admission":
                for label, key in [
                    ("Admission Code", "admission_code"),
                    ("Type", "admission_type"),
                    ("Admitted at", "admitted_at"),
                    ("Expected Discharge", "expected_discharge_at"),
                    ("Preliminary Diagnosis", "preliminary_diagnosis"),
                    ("History", "history"),
                    ("Care Plan", "care_plan"),
                    ("Current Bed", "current_bed_code"),
                    ("Payor", "payor_type"),
                    ("Insurer", "insurer_name"),
                    ("Policy", "policy_number"),
                    ("Status", "status"),
                ]:
                    val = data.get(key)
                    if val not in (None, ""):
                        y = _wrap_text(
                            c,
                            f"{label}: {val}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )

            # ---------- IPD Transfer ----------
            elif typ == "ipd_transfer":
                for label, key in [
                    ("Admission", "admission_id"),
                    ("From Bed", "from_bed_id"),
                    ("To Bed", "to_bed_id"),
                    ("Reason", "reason"),
                    ("Transferred at", "transferred_at"),
                ]:
                    val = data.get(key)
                    if val not in (None, ""):
                        y = _wrap_text(
                            c,
                            f"{label}: {val}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )

            # ---------- IPD Discharge ----------
            elif typ == "ipd_discharge":
                for label, key in [
                    ("Finalized", "finalized"),
                    ("Finalized at", "finalized_at"),
                    ("Demographics", "demographics"),
                    ("Medical History", "medical_history"),
                    ("Treatment Summary", "treatment_summary"),
                    ("Medications", "medications"),
                    ("Follow Up", "follow_up"),
                    ("ICD-10 Codes", "icd10_codes"),
                ]:
                    val = data.get(key)
                    if val not in (None, ""):
                        y = _wrap_text(
                            c,
                            f"{label}: {val}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )

            # ---------- OT ----------
            elif typ == "ot":
                for label, key in [
                    ("Surgery", "surgery_name"),
                    ("Code", "surgery_code"),
                    ("Est. Cost", "estimated_cost"),
                    ("Scheduled Start", "scheduled_start"),
                    ("Scheduled End", "scheduled_end"),
                    ("Actual Start", "actual_start"),
                    ("Actual End", "actual_end"),
                    ("Status", "status"),
                    ("Pre-op Notes", "preop_notes"),
                    ("Post-op Notes", "postop_notes"),
                ]:
                    val = data.get(key)
                    if val not in (None, ""):
                        y = _wrap_text(
                            c,
                            f"{label}: {val}",
                            LEFT,
                            y,
                            CONTENT_W,
                        )

            # ---------- Billing ----------
            elif typ == "billing":
                y = _wrap_text(
                    c,
                    f"Invoice ID: {data.get('invoice_id')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Status: {data.get('status')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Net Total: Rs {data.get('net_total')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Paid: Rs {data.get('amount_paid')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )
                y = _wrap_text(
                    c,
                    f"Balance: Rs {data.get('balance_due')}",
                    LEFT,
                    y,
                    CONTENT_W,
                )

                items_block = data.get("items") or []
                if items_block:
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(LEFT, y, "Items:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    for li in items_block:
                        y = page_break_if_needed(y)
                        line = (
                            f"- {li.get('service_type')}: "
                            f"{li.get('description') or ''}"
                            f" • Qty {li.get('quantity') or ''}"
                            f" • Rs {float(li.get('line_total') or 0):.2f}")
                        y = _wrap_text(
                            c,
                            line,
                            LEFT + 5 * mm,
                            y,
                            CONTENT_W - 5 * mm,
                        )

                pays = data.get("payments") or []
                if pays:
                    c.setFont("Helvetica-Bold", 9)
                    c.drawString(LEFT, y, "Payments:")
                    y -= 4 * mm
                    c.setFont("Helvetica", 9)
                    for pmt in pays:
                        y = page_break_if_needed(y)
                        line = (f"- {pmt.get('mode') or ''}"
                                f" • Rs {float(pmt.get('amount') or 0):.2f}"
                                f" • {pmt.get('paid_at') or ''}")
                        y = _wrap_text(
                            c,
                            line,
                            LEFT + 5 * mm,
                            y,
                            CONTENT_W - 5 * mm,
                        )

            # ---------- Consent ----------
            elif typ == "consent":
                if data.get("type"):
                    y = _wrap_text(
                        c,
                        f"Type: {data.get('type')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                if data.get("captured_at"):
                    y = _wrap_text(
                        c,
                        f"Captured at: {data.get('captured_at')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )
                if data.get("text"):
                    y = _wrap_text(
                        c,
                        f"Text: {data.get('text')}",
                        LEFT,
                        y,
                        CONTENT_W,
                    )

            # ---------- default / attachments-only ----------
            # Attachments (just list names; actual files are in EMR UI)
            atts = it.get("attachments") or []
            if atts:
                c.setFont("Helvetica-Bold", 9)
                c.drawString(LEFT, y, "Attachments:")
                y -= 4 * mm
                c.setFont("Helvetica", 9)
                for a in atts:
                    y = page_break_if_needed(y)
                    label = a.get("label") or "file"
                    y = _wrap_text(
                        c,
                        f"- {label}",
                        LEFT + 5 * mm,
                        y,
                        CONTENT_W - 5 * mm,
                    )

            # spacing between events
            y -= 3 * mm
            c.setStrokeColor(colors.HexColor("#E5E7EB"))
            c.line(LEFT, y, RIGHT, y)
            y -= 4 * mm

    # Final footer + save
    draw_footer()
    c.showPage()
    c.save()
    return buf.getvalue()
