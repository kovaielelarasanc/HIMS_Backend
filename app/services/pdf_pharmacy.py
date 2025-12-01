# FILE: app/services/pdf_pharmacy.py
from __future__ import annotations
from io import BytesIO
from datetime import datetime, date
from typing import Iterable, Sequence, Mapping, Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors


def _fmt_date(d: Any) -> str:
    if not d:
        return ""
    if isinstance(d, (datetime, date)):
        return d.strftime("%d-%m-%Y")
    return str(d)


def _fmt_dt(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%d-%m-%Y %H:%M")
    return str(dt)


def _new_canvas() -> tuple[canvas.Canvas, BytesIO]:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    return c, buf


def _draw_header(c: canvas.Canvas,
                 main_title: str,
                 sub_title: str = "") -> float:
    w, h = A4
    x = 18 * mm
    y = h - 20 * mm

    # Hospital / app name (you can later make this dynamic from branding)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, "Hospital Pharmacy")

    y -= 7 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, main_title)

    if sub_title:
        y -= 5 * mm
        c.setFont("Helvetica", 10)
        c.drawString(x, y, sub_title)

    y -= 4 * mm
    c.setStrokeColor(colors.grey)
    c.setLineWidth(0.6)
    c.line(x, y, w - x, y)
    y -= 6 * mm
    return y


def _table(
    c: canvas.Canvas,
    y: float,
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    col_widths_mm: Sequence[float],
) -> float:
    """Simple generic table (no auto page split beyond basic check)."""
    x0 = 18 * mm
    col_points = [w * mm for w in col_widths_mm]
    total_width = sum(col_points)

    # Header
    c.setFont("Helvetica-Bold", 9)
    for i, htxt in enumerate(headers):
        offset = sum(col_points[:i])
        c.drawString(x0 + offset, y, htxt)
    y -= 4 * mm
    c.setLineWidth(0.4)
    c.line(x0, y, x0 + total_width, y)
    y -= 5 * mm

    # Rows
    c.setFont("Helvetica", 9)
    for row in rows:
        if y < 25 * mm:
            c.showPage()
            y = _draw_header(c, "Continued", "")
            y -= 4 * mm
            c.setFont("Helvetica-Bold", 9)
            for i, htxt in enumerate(headers):
                offset = sum(col_points[:i])
                c.drawString(x0 + offset, y, htxt)
            y -= 4 * mm
            c.setLineWidth(0.4)
            c.line(x0, y, x0 + total_width, y)
            y -= 5 * mm
            c.setFont("Helvetica", 9)

        for i, cell in enumerate(row):
            offset = sum(col_points[:i])
            txt = (cell or "")[:60]
            c.drawString(x0 + offset, y, txt)
        y -= 4 * mm

    return y


# ===================== PO PDF =====================


def build_po_pdf(po, supplier, location, items) -> BytesIO:
    """
    po: PharmacyPO ORM
    supplier: PharmacySupplier or None
    location: PharmacyLocation or None
    items: iterable of objects with attrs: medicine_code, medicine_name, qty
    """
    c, buf = _new_canvas()
    y = _draw_header(c, "Purchase Order", f"PO #{po.id}")

    c.setFont("Helvetica", 9)
    x = 18 * mm
    c.drawString(
        x, y, f"Supplier : {getattr(supplier, 'name', '') or po.supplier_id}")
    y -= 4 * mm
    c.drawString(
        x, y, f"Location : {getattr(location, 'name', '') or po.location_id}")
    y -= 4 * mm
    c.drawString(x, y, f"Status   : {po.status}")
    y -= 4 * mm
    c.drawString(x, y, f"Created  : {_fmt_dt(po.created_at)}")
    if getattr(po, "approved_at", None):
        y -= 4 * mm
        c.drawString(x, y, f"Approved : {_fmt_dt(po.approved_at)}")
    y -= 8 * mm

    # Table
    headers = ["S.No", "Code", "Medicine", "Qty"]
    col_widths = [10, 25, 90, 20]  # mm
    rows = []
    for idx, it in enumerate(items, start=1):
        rows.append([
            str(idx),
            getattr(it, "medicine_code", ""),
            getattr(it, "medicine_name", ""),
            str(getattr(it, "qty", "")),
        ])
    _table(c, y, headers, rows, col_widths)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ===================== GRN PDF =====================


def build_grn_pdf(grn, supplier, location, po, items) -> BytesIO:
    """
    grn: PharmacyGRN ORM
    items: iterable with attrs: medicine_code, medicine_name, batch, expiry, qty, unit_cost
    """
    c, buf = _new_canvas()
    y = _draw_header(c, "Goods Receipt Note", f"GRN #{grn.id}")

    c.setFont("Helvetica", 9)
    x = 18 * mm
    c.drawString(
        x, y, f"Supplier : {getattr(supplier, 'name', '') or grn.supplier_id}")
    y -= 4 * mm
    c.drawString(
        x, y, f"Location : {getattr(location, 'name', '') or grn.location_id}")
    y -= 4 * mm
    if grn.po_id:
        c.drawString(x, y,
                     f"PO      : #{grn.po_id} ({getattr(po, 'status', '')})")
        y -= 4 * mm
    c.drawString(x, y, f"Received : {_fmt_dt(grn.received_at)}")
    y -= 8 * mm

    headers = [
        "S.No", "Code", "Medicine", "Batch", "Expiry", "Qty", "Unit Cost"
    ]
    col_widths = [8, 20, 60, 25, 25, 15, 25]
    rows = []
    for idx, it in enumerate(items, start=1):
        rows.append([
            str(idx),
            getattr(it, "medicine_code", ""),
            getattr(it, "medicine_name", ""),
            getattr(it, "batch", ""),
            _fmt_date(getattr(it, "expiry", None)),
            str(getattr(it, "qty", "")),
            str(getattr(it, "unit_cost", "")),
        ])
    _table(c, y, headers, rows, col_widths)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ===================== E-Prescription PDF =====================


def build_rx_pdf(rx, patient, prescriber, items) -> BytesIO:
    c, buf = _new_canvas()
    y = _draw_header(c, "E-Prescription", f"Rx #{rx.id}")

    x = 18 * mm
    c.setFont("Helvetica", 9)

    patient_name = (getattr(patient, "full_name", None)
                    or getattr(patient, "name", None)
                    or f"Patient #{getattr(patient, 'id', '')}")
    doctor_name = (getattr(prescriber, "full_name", None)
                   or getattr(prescriber, "name", None)
                   or getattr(prescriber, "username", "")
                   or f"User #{getattr(prescriber, 'id', '')}")

    c.drawString(
        x, y, f"Patient : {patient_name} (ID: {getattr(patient, 'id', '')})")
    y -= 4 * mm
    c.drawString(x, y, f"Doctor  : {doctor_name}")
    y -= 4 * mm
    c.drawString(
        x, y, f"Context : {rx.context_type.upper()} "
        f"{'(Visit #{})'.format(rx.visit_id) if rx.context_type=='opd' and rx.visit_id else ''}"
        f"{'(Admission #{})'.format(rx.admission_id) if rx.context_type=='ipd' and rx.admission_id else ''}"
    )
    y -= 4 * mm
    c.drawString(x, y, f"Created : {_fmt_dt(rx.created_at)}")
    y -= 4 * mm
    if getattr(rx, "updated_at", None):
        c.drawString(x, y, f"Updated : {_fmt_dt(rx.updated_at)}")
        y -= 4 * mm

    if getattr(rx, "notes", ""):
        y -= 4 * mm
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(x, y, f"Notes: {rx.notes[:150]}")
        y -= 6 * mm
    else:
        y -= 8 * mm

    headers = [
        "#", "Medicine", "Dose", "Freq", "Route", "Days", "Qty", "Instructions"
    ]
    col_widths = [6, 55, 15, 20, 15, 10, 10, 45]
    rows = []
    for idx, it in enumerate(items, start=1):
        freq_parts = []
        if getattr(it, "am", False): freq_parts.append("AM")
        if getattr(it, "af", False): freq_parts.append("AF")
        if getattr(it, "pm", False): freq_parts.append("PM")
        if getattr(it, "night", False): freq_parts.append("Night")
        freq = "+".join(freq_parts) or (getattr(it, "frequency", "") or "")

        rows.append([
            str(idx),
            getattr(
                it, "medicine_id",
                ""),  # FE can show real name using medicine master if needed
            getattr(it, "dose", ""),
            freq,
            getattr(it, "route", ""),
            str(getattr(it, "duration_days", "")),
            str(getattr(it, "quantity", "")),
            (getattr(it, "instructions", "") or "")[:60],
        ])

    _table(c, y, headers, rows, col_widths)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ===================== Pharmacy Bill PDF =====================


def build_sale_bill_pdf(sale, patient, location,
                        item_rows: Sequence[Mapping[str, Any]]) -> BytesIO:
    """
    item_rows: list of dicts with keys: code, name, qty, unit_price, amount
    """
    c, buf = _new_canvas()
    y = _draw_header(c, "Pharmacy Bill", f"Sale #{sale.id}")

    x = 18 * mm
    c.setFont("Helvetica", 9)

    patient_name = (getattr(patient, "full_name", None)
                    or getattr(patient, "name", None)
                    or f"Patient #{getattr(patient, 'id', '')}")
    loc_name = getattr(location, "name", "") if location else sale.location_id

    c.drawString(
        x, y, f"Patient : {patient_name} (ID: {getattr(patient, 'id', '')})")
    y -= 4 * mm
    c.drawString(x, y, f"Location: {loc_name}")
    y -= 4 * mm
    c.drawString(x, y, f"Context: {sale.context_type.upper()}")
    y -= 4 * mm
    c.drawString(x, y, f"Date   : {_fmt_dt(sale.created_at)}")
    y -= 8 * mm

    headers = ["#", "Code", "Medicine", "Qty", "Rate", "Amount"]
    col_widths = [6, 22, 70, 15, 20, 25]
    rows = []
    total = 0
    for idx, r in enumerate(item_rows, start=1):
        amt = r.get("amount") or 0
        total += float(amt)
        rows.append([
            str(idx),
            str(r.get("code", "")),
            str(r.get("name", "")),
            str(r.get("qty", "")),
            str(r.get("unit_price", "")),
            str(amt),
        ])

    y = _table(c, y, headers, rows, col_widths)
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(18 * mm + sum(w * mm for w in col_widths), y,
                      f"Total: {sale.total_amount}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
