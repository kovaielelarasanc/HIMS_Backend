from __future__ import annotations
from io import BytesIO
from typing import Iterable, Optional, Dict, Any, List
from datetime import datetime

# pip install reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors


def _wrap_text(c: canvas.Canvas,
               text: str,
               x: float,
               y: float,
               max_width: float,
               line_height: float = 13) -> float:
    if not text: return y
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


def _kv(c: canvas.Canvas, x: float, y: float, label: str, value: Any,
        max_width: float) -> float:
    if value is None or value == "": return y
    return _wrap_text(c, f"{label}: {value}", x, y, max_width)


def _section_rule(c: canvas.Canvas, y: float, W: float) -> float:
    c.setStrokeColor(colors.HexColor("#E2E8F0"))
    c.line(20 * mm, y, W - 20 * mm, y)
    return y - 6 * mm


def _page_break_if_needed(c: canvas.Canvas, y: float, H: float) -> float:
    if y < 25 * mm:
        c.showPage()
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.HexColor("#0F172A"))
        return H - 20 * mm
    return y


def generate_emr_pdf(
    patient: Dict[str, Any],
    items: Iterable[Dict[str, Any]],
    sections_selected: Optional[set[str]] = None,
    letterhead_bytes: Optional[bytes] = None,
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # Letterhead (optional)
    y = H - 20 * mm
    if letterhead_bytes:
        try:
            img = ImageReader(BytesIO(letterhead_bytes))
            iw, ih = img.getSize()
            scale = (W - 20 * mm) / iw
            draw_w = (W - 20 * mm)
            draw_h = ih * scale
            c.drawImage(img,
                        10 * mm,
                        H - draw_h - 10 * mm,
                        width=draw_w,
                        height=draw_h,
                        preserveAspectRatio=True,
                        mask='auto')
            y = H - draw_h - 16 * mm
        except Exception:
            pass

    # Header
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor("#0F172A"))
    c.drawString(20 * mm, y, "Patient EMR Summary")
    y -= 8 * mm

    c.setFont("Helvetica", 10)
    c.setFillColor(colors.black)
    head = [
        f"Name: {patient.get('name') or '—'}",
        f"UHID: {patient.get('uhid') or '—'}",
        f"Gender: {patient.get('gender') or '—'}",
        f"DOB: {patient.get('dob') or '—'}",
        f"Phone: {patient.get('phone') or '—'}  Email: {patient.get('email') or '—'}",
        f"Generated at: {datetime.utcnow().isoformat()} (UTC)",
    ]
    for line in head:
        c.drawString(20 * mm, y, line)
        y -= 6 * mm
    y = _section_rule(c, y, W)
    y -= 2 * mm

    # helper for section filtering
    def section_of(t: str) -> str:
        if t == "opd_visit": return "opd"
        if t == "opd_vitals": return "vitals"
        if t == "rx": return "prescriptions"
        if t == "lab": return "lab"
        if t == "radiology": return "radiology"
        if t == "pharmacy": return "pharmacy"
        if t.startswith("ipd_"): return "ipd"
        if t == "ot": return "ot"
        if t == "billing": return "billing"
        if t == "attachment": return "attachments"
        if t == "consent": return "consents"
        return "other"

    # Body
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.HexColor("#0F172A"))

    sorted_items = sorted(items or [],
                          key=lambda x: x.get("ts", ""),
                          reverse=True)
    for it in sorted_items:
        sec = section_of(it.get("type", ""))
        if sections_selected and sec not in sections_selected:
            continue

        y = _page_break_if_needed(c, y, H)

        # Row header
        title = it.get("title") or "Event"
        when = it.get("ts")
        if isinstance(when, str): when = when.replace("T", " ")[:16]
        header = f"{title}  •  {when or ''}"
        status = it.get("status")
        if status: header += f"  •  {status}"
        c.drawString(20 * mm, y, header)
        y -= 6 * mm

        # Meta line
        c.setFont("Helvetica", 10)
        meta_bits: List[str] = []
        if it.get("doctor_name"):
            meta_bits.append(f"Doctor: {it['doctor_name']}")
        if it.get("department_name"):
            meta_bits.append(f"Dept: {it['department_name']}")
        if it.get("location_name"):
            meta_bits.append(f"Loc: {it['location_name']}")
        if meta_bits:
            y = _wrap_text(c, "  •  ".join(meta_bits), 20 * mm, y, W - 40 * mm)

        # Module-specific details
        data = it.get("data") or {}
        typ = it.get("type")

        if typ == "opd_visit":
            y = _kv(c, 20 * mm, y, "Chief Complaint",
                    data.get("chief_complaint"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Symptoms", data.get("symptoms"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Subjective", data.get("subjective"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Objective", data.get("objective"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Assessment", data.get("assessment"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Plan", data.get("plan"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Episode", data.get("episode_id"),
                    W - 40 * mm)
            ap = data.get("appointment") or {}
            if ap:
                y = _kv(c, 20 * mm, y, "Appointment Date", ap.get("date"),
                        W - 40 * mm)
                y = _kv(c, 20 * mm, y, "Slot",
                        f"{ap.get('slot_start')} – {ap.get('slot_end')}",
                        W - 40 * mm)

        elif typ == "opd_vitals":
            y = _kv(c, 20 * mm, y, "Recorded at", data.get("recorded_at"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Height (cm)", data.get("height_cm"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Weight (kg)", data.get("weight_kg"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "BMI", data.get("bmi"), W - 40 * mm)
            y = _kv(
                c, 20 * mm, y, "BP",
                f"{data.get('bp_systolic')}/{data.get('bp_diastolic')} mmHg",
                W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Pulse", data.get("pulse"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "RR", data.get("rr"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Temp (°C)", data.get("temp_c"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "SpO₂ (%)", data.get("spo2"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Notes", data.get("notes"), W - 40 * mm)

        elif typ == "rx":
            y = _kv(c, 20 * mm, y, "Notes", data.get("notes"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Signed at", data.get("signed_at"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Signed by", data.get("signed_by"),
                    W - 40 * mm)
            items = data.get("items") or []
            if items:
                c.setFont("Helvetica-Bold", 10)
                c.drawString(20 * mm, y, "Items:")
                y -= 6 * mm
                c.setFont("Helvetica", 10)
                for di in items:
                    y = _page_break_if_needed(c, y, H)
                    line = f"- {di.get('drug_name')} {di.get('strength') or ''} • {di.get('frequency') or ''} • {di.get('duration_days')}d • Qty {di.get('quantity')} • ₹{di.get('line_total') or 0:.2f}"
                    y = _wrap_text(c, line, 25 * mm, y, W - 45 * mm)

        elif typ == "lab":
            item = (data.get("item") or {})
            y = _kv(c, 20 * mm, y, "Order ID", data.get("order_id"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Priority", data.get("priority"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Collected at", data.get("collected_at"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Reported at", data.get("reported_at"),
                    W - 40 * mm)
            for k, label in [
                ("test_name", "Test"),
                ("test_code", "Code"),
                ("unit", "Unit"),
                ("normal_range", "Normal Range"),
                ("specimen_type", "Specimen"),
                ("status", "Status"),
                ("result_value", "Result"),
                ("is_critical", "Critical"),
                ("result_at", "Result at"),
            ]:
                y = _kv(c, 20 * mm, y, label, item.get(k), W - 40 * mm)

        elif typ == "radiology":
            for k, label in [
                ("test_name", "Test"),
                ("test_code", "Code"),
                ("modality", "Modality"),
                ("status", "Status"),
                ("scheduled_at", "Scheduled"),
                ("scanned_at", "Scanned"),
                ("reported_at", "Reported"),
                ("approved_at", "Approved"),
            ]:
                y = _kv(c, 20 * mm, y, label, data.get(k), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Report Text", data.get("report_text"),
                    W - 40 * mm)

        elif typ == "pharmacy":
            y = _kv(c, 20 * mm, y, "Sale ID", data.get("sale_id"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Context", data.get("context_type"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Payment", data.get("payment_mode"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Total", data.get("total_amount"),
                    W - 40 * mm)
            items = data.get("items") or []
            if items:
                c.setFont("Helvetica-Bold", 10)
                c.drawString(20 * mm, y, "Items:")
                y -= 6 * mm
                c.setFont("Helvetica", 10)
                for di in items:
                    y = _page_break_if_needed(c, y, H)
                    line = f"- {di.get('medicine_name') or di.get('medicine_id')} • Qty {di.get('qty')} • ₹{di.get('amount') or 0:.2f}"
                    y = _wrap_text(c, line, 25 * mm, y, W - 45 * mm)

        elif typ == "ipd_admission":
            for k, label in [
                ("admission_code", "Admission Code"),
                ("admission_type", "Type"),
                ("admitted_at", "Admitted at"),
                ("expected_discharge_at", "Expected Discharge"),
                ("preliminary_diagnosis", "Preliminary Diagnosis"),
                ("history", "History"),
                ("care_plan", "Care Plan"),
                ("current_bed_code", "Current Bed"),
                ("payor_type", "Payor"),
                ("insurer_name", "Insurer"),
                ("policy_number", "Policy"),
                ("status", "Status"),
            ]:
                y = _kv(c, 20 * mm, y, label, data.get(k), W - 40 * mm)

        elif typ == "ipd_transfer":
            for k, label in [
                ("admission_id", "Admission"),
                ("from_bed_id", "From Bed"),
                ("to_bed_id", "To Bed"),
                ("reason", "Reason"),
                ("transferred_at", "When"),
            ]:
                y = _kv(c, 20 * mm, y, label, data.get(k), W - 40 * mm)

        elif typ == "ipd_discharge":
            for k, label in [
                ("finalized", "Finalized"),
                ("finalized_at", "Finalized at"),
                ("demographics", "Demographics"),
                ("medical_history", "Medical History"),
                ("treatment_summary", "Treatment Summary"),
                ("medications", "Medications"),
                ("follow_up", "Follow Up"),
                ("icd10_codes", "ICD-10 Codes"),
            ]:
                y = _kv(c, 20 * mm, y, label, data.get(k), W - 40 * mm)

        elif typ == "ot":
            for k, label in [
                ("surgery_name", "Surgery"),
                ("surgery_code", "Code"),
                ("estimated_cost", "Est. Cost"),
                ("scheduled_start", "Scheduled Start"),
                ("scheduled_end", "Scheduled End"),
                ("actual_start", "Actual Start"),
                ("actual_end", "Actual End"),
                ("status", "Status"),
                ("preop_notes", "Pre-op Notes"),
                ("postop_notes", "Post-op Notes"),
            ]:
                y = _kv(c, 20 * mm, y, label, data.get(k), W - 40 * mm)

        elif typ == "billing":
            y = _kv(c, 20 * mm, y, "Invoice ID", data.get("invoice_id"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Status", data.get("status"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Net Total", data.get("net_total"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Paid", data.get("amount_paid"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Balance", data.get("balance_due"),
                    W - 40 * mm)
            items = data.get("items") or []
            if items:
                c.setFont("Helvetica-Bold", 10)
                c.drawString(20 * mm, y, "Items:")
                y -= 6 * mm
                c.setFont("Helvetica", 10)
                for li in items:
                    y = _page_break_if_needed(c, y, H)
                    line = f"- {li.get('service_type')}: {li.get('description')} • Qty {li.get('quantity')} • ₹{li.get('line_total') or 0:.2f}"
                    y = _wrap_text(c, line, 25 * mm, y, W - 45 * mm)
            pays = data.get("payments") or []
            if pays:
                c.setFont("Helvetica-Bold", 10)
                c.drawString(20 * mm, y, "Payments:")
                y -= 6 * mm
                c.setFont("Helvetica", 10)
                for p in pays:
                    y = _page_break_if_needed(c, y, H)
                    line = f"- {p.get('mode')} • ₹{p.get('amount') or 0:.2f} • {p.get('paid_at')}"
                    y = _wrap_text(c, line, 25 * mm, y, W - 45 * mm)

        elif typ == "attachment":
            pass  # already lists under attachments

        elif typ == "consent":
            y = _kv(c, 20 * mm, y, "Type", data.get("type"), W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Captured at", data.get("captured_at"),
                    W - 40 * mm)
            y = _kv(c, 20 * mm, y, "Text", data.get("text"), W - 40 * mm)

        # attachments list (if any)
        atts = it.get("attachments") or []
        for a in atts:
            y = _wrap_text(
                c,
                f"Attachment: {(a.get('label') or 'file')} — {a.get('url') or ''}",
                20 * mm, y, W - 40 * mm)

        # row divider
        y -= 3 * mm
        y = _section_rule(c, y, W)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(colors.HexColor("#0F172A"))

    c.showPage()
    c.save()
    return buf.getvalue()
