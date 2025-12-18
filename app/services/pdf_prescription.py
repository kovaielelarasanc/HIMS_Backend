# FILE: app/services/pdf_prescription.py
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Optional, Tuple, List
from io import BytesIO
import html as _html

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics

from app.core.config import settings

# ✅ Use your branding header HTML/CSS (WeasyPrint path)
from app.services.pdf_branding import brand_header_css, render_brand_header_html


# -------------------------------
# Helpers
# -------------------------------
def _g(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe(v: Any) -> str:
    return "" if v is None else str(v)


def _esc(v: Any) -> str:
    """HTML escape for safe PDF HTML."""
    return _html.escape(_safe(v), quote=True)


def _to_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date()
    except Exception:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except Exception:
            continue
    return None


def _fmt_dt(v: Any) -> str:
    if not v:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %I:%M %p")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    s = str(v).strip()
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace("Z", ""))
        return dt.strftime("%d-%m-%Y %I:%M %p")
    except Exception:
        return s


def _fmt_date(v: Any) -> str:
    d = _to_date(v)
    return d.strftime("%d-%m-%Y") if d else ("—" if not v else str(v))


def _age_years_from_dob(dob: Any,
                        asof: Optional[date] = None) -> Optional[int]:
    d = _to_date(dob)
    if not d:
        return None
    asof = asof or date.today()
    years = asof.year - d.year - ((asof.month, asof.day) < (d.month, d.day))
    return max(0, int(years))


def _wrap(text: str, font: str, size: float, max_w: float) -> List[str]:
    s = (text or "").replace("\n", " ").strip()
    if not s:
        return [""]
    words = s.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(cand, font, size) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _doctor_department_name(doctor: Any) -> str:
    """
    Handles:
      - doctor.department.name (ORM relationship)
      - doctor["department"]["name"] (dict)
      - doctor.department_name / dept_name string
    """
    if not doctor:
        return "—"

    dept = _g(doctor, "department", None)
    if isinstance(dept, dict):
        n = (dept.get("name") or dept.get("title") or "").strip()
        if n:
            return n
    else:
        # ORM object
        n = _safe(_g(dept, "name", "")).strip()
        if n:
            return n

    for k in ("department_name", "dept_name", "department"):
        v = _safe(_g(doctor, k, "")).strip()
        # avoid "<Department object at ...>"
        if v and "object at" not in v:
            return v

    return "—"


def freq_to_slots(freq: Optional[str]) -> tuple[int, int, int, int]:
    if not freq:
        return (0, 0, 0, 0)
    f = str(freq).strip().upper()

    # 1-0-0-1 OR 1-0-1
    if "-" in f:
        parts = [p.strip() for p in f.split("-") if p.strip() != ""]
        nums: list[int] = []
        for p in parts:
            try:
                nums.append(int(float(p)))
            except Exception:
                nums.append(0)
        if len(nums) == 3:
            return (nums[0], nums[1], 0, nums[2])
        if len(nums) >= 4:
            return (nums[0], nums[1], nums[2], nums[3])

    mapping = {
        "OD": (1, 0, 0, 0),
        "QD": (1, 0, 0, 0),
        "BD": (1, 0, 0, 1),
        "BID": (1, 0, 0, 1),
        "TID": (1, 1, 0, 1),
        "TDS": (1, 1, 0, 1),
        "QID": (1, 1, 1, 1),
        "HS": (0, 0, 0, 1),
        "NIGHT": (0, 0, 0, 1),
    }
    return mapping.get(f, (0, 0, 0, 0))


# -------------------------------------------------------------------
# WeasyPrint HTML (uses your pdf_branding header CSS/HTML)
# -------------------------------------------------------------------
def _build_prescription_html(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> str:
    rx_no = _safe(_g(rx, "rx_number", _g(rx, "rx_no", "—"))) or "—"
    rx_dt = _fmt_dt(_g(rx, "rx_datetime") or _g(rx, "created_at"))
    op_uid = _safe(_g(rx, "op_uid", "—")) or "—"
    ip_uid = _safe(_g(rx, "ip_uid", "—")) or "—"

    p_name = " ".join([
        _safe(_g(patient, "prefix", "")).strip(),
        _safe(_g(patient, "first_name", "")).strip(),
        _safe(_g(patient, "last_name", "")).strip(),
    ]).strip() or (_safe(_g(patient, "full_name", "—")) or "—")

    p_uhid = _safe(_g(patient, "uhid", "—")) or "—"
    p_phone = _safe(_g(patient, "phone", "—")) or "—"

    p_dob_raw = _g(patient, "dob", _g(patient, "date_of_birth", None))
    p_dob = _fmt_date(p_dob_raw)
    age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{age_years} Y" if age_years is not None else "—"
    p_gender = _safe(_g(patient, "gender", _g(patient, "sex", "—"))) or "—"

    d_name = _safe(_g(doctor, "full_name", _g(doctor, "name", "—"))) or "—"
    d_reg = _safe(_g(doctor, "registration_no", "—")) or "—"
    d_dept = _doctor_department_name(doctor)

    notes = (_safe(_g(rx, "notes", "")) or "").strip()

    lines = _g(rx, "lines", []) or []
    tr_html = ""
    if not lines:
        tr_html = "<tr><td colspan='7' class='empty'>No medicines</td></tr>"
    else:
        for i, ln in enumerate(lines, start=1):
            drug = _safe(
                _g(ln, "item_name", _g(_g(ln, "item", None), "name",
                                       "—"))) or "—"
            dose = (_safe(_g(ln, "dose_text", "")) or "").strip()
            route = (_safe(_g(ln, "route", "")) or "").strip()
            timing = (_safe(_g(ln, "timing", "")) or "").strip()
            inst = (_safe(_g(ln, "instructions", "")) or "").strip()
            if not timing and inst:
                timing = inst

            days = _safe(_g(ln, "duration_days", _g(ln, "days", "—"))) or "—"
            qty = (_safe(_g(ln, "requested_qty", "")) or "").strip()

            freq = _g(ln, "frequency_code",
                      _g(ln, "frequency", _g(ln, "freq", None)))
            am, af, pm, night = freq_to_slots(freq)

            sub_parts = [p for p in [dose, route, timing] if p]
            sub = " • ".join(sub_parts)
            if qty:
                sub = (sub + f" • Qty: {qty}").strip(" •")

            tr_html += f"""
              <tr>
                <td class="c num">{i}</td>
                <td class="med">
                  <div class="drug">{_esc(drug)}</div>
                  {f"<div class='sub'>{_esc(sub)}</div>" if sub else ""}
                </td>
                <td class="c">{am}</td>
                <td class="c">{af}</td>
                <td class="c">{pm}</td>
                <td class="c">{night}</td>
                <td class="c">{_esc(days)}</td>
              </tr>
            """

    header_html = render_brand_header_html(branding_obj)

    css = f"""
    {brand_header_css()}

    @page {{
      size: A4;
      margin: 14mm 14mm 16mm 14mm;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      color: #0f172a;
      font-size: 11px;
      line-height: 1.25;
    }}

    .title-row {{
      display:flex;
      align-items:flex-end;
      justify-content: space-between;
      margin-top: 6px;
      margin-bottom: 10px;
    }}
    .title {{
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.2px;
    }}
    .meta-right {{
      text-align:right;
      color:#334155;
      font-size: 11px;
    }}
    .meta-right .small {{
      margin-top: 4px;
      font-size: 10px;
      color:#475569;
    }}

    .cards {{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .card {{
      border: 1px solid #e5e7eb;
      background: #f8fafc;
      border-radius: 10px;
      padding: 10px;
    }}
    .card h3 {{
      margin: 0 0 8px 0;
      font-size: 12px;
      font-weight: 800;
      color:#0f172a;
    }}
    .kv {{
      display:flex;
      gap: 10px;
      margin-top: 4px;
      font-size: 11px;
    }}
    .k {{
      width: 68px;
      color:#64748b;
    }}
    .v {{
      flex: 1;
      color:#0f172a;
      font-weight: 700;
    }}
    .muted {{
      color:#475569;
      font-weight: 600;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    thead th {{
      background: #0b1220;
      color: #ffffff;
      font-size: 10.5px;
      padding: 8px 8px;
      border-right: 1px solid #1f2937;
    }}
    thead th:last-child {{ border-right: none; }}

    tbody td {{
      border: 1px solid #e5e7eb;
      padding: 8px 8px;
      vertical-align: top;
    }}
    tbody tr:nth-child(even) td {{
      background: #f8fafc;
    }}

    .c {{ text-align:center; }}
    .num {{ width: 26px; }}
    .drug {{ font-weight: 800; color:#0f172a; }}
    .sub {{ margin-top: 3px; font-size: 10px; color:#475569; }}
    .empty {{
      text-align:left;
      color:#64748b;
      padding: 12px;
    }}

    .notes {{
      margin-top: 10px;
      border: 1px solid #e5e7eb;
      background: #f8fafc;
      border-radius: 10px;
      padding: 10px;
    }}
    .notes .label {{
      font-weight: 800;
      margin-bottom: 6px;
    }}
    .sig {{
      margin-top: 22px;
      display:flex;
      justify-content:flex-end;
    }}
    .sig .line {{
      width: 240px;
      border-top: 1px solid #111827;
      padding-top: 6px;
      text-align:right;
      font-weight: 800;
      font-size: 10px;
    }}

    tr {{ page-break-inside: avoid; }}
    .card, .notes {{ page-break-inside: avoid; }}
    """

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <style>{css}</style>
      </head>
      <body>
        {header_html}

        <div class="title-row">
          <div class="title">PRESCRIPTION</div>
          <div class="meta-right">
            <div>Rx No: <b>{_esc(rx_no)}</b> &nbsp; | &nbsp; Date: <b>{_esc(rx_dt)}</b></div>
            <div class="small">OP: <b>{_esc(op_uid)}</b> &nbsp; | &nbsp; IP: <b>{_esc(ip_uid)}</b></div>
          </div>
        </div>

        <div class="cards">
          <div class="card">
            <h3>Patient</h3>
            <div class="kv"><div class="k">Name</div><div class="v">{_esc(p_name)}</div></div>
            <div class="kv"><div class="k">UHID</div><div class="v">{_esc(p_uhid)}</div></div>
            <div class="kv"><div class="k">Phone</div><div class="v">{_esc(p_phone)}</div></div>
            <div class="kv"><div class="k">DOB</div><div class="v muted">{_esc(p_dob)} &nbsp; | &nbsp; Age/Sex: {_esc(p_age)} / {_esc(p_gender)}</div></div>
          </div>

          <div class="card">
            <h3>Doctor</h3>
            <div class="kv"><div class="k">Name</div><div class="v">{_esc(d_name)}</div></div>
            <div class="kv"><div class="k">Dept</div><div class="v muted">{_esc(d_dept)}</div></div>
            <div class="kv"><div class="k">Reg No</div><div class="v muted">{_esc(d_reg)}</div></div>
          </div>
        </div>

        <table>
          <thead>
            <tr>
              <th style="width:26px;">#</th>
              <th>Medicine / Instructions</th>
              <th style="width:38px;">AM</th>
              <th style="width:38px;">AF</th>
              <th style="width:38px;">PM</th>
              <th style="width:48px;">Night</th>
              <th style="width:44px;">Days</th>
            </tr>
          </thead>
          <tbody>
            {tr_html}
          </tbody>
        </table>

        {f"<div class='notes'><div class='label'>Notes</div><div>{_esc(notes)}</div></div>" if notes else ""}

        <div class="sig">
          <div class="line">Doctor Signature</div>
        </div>
      </body>
    </html>
    """.strip()

    return html


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------
def build_prescription_pdf(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> Tuple[bytes, str]:
    """
    ✅ Primary: WeasyPrint (to use brand_header_css + render_brand_header_html)
    ✅ Fallback: FULL ReportLab (no blank PDFs)
    Returns: (bytes, media_type)
    """
    # 1) WeasyPrint (best header)
    try:
        from weasyprint import HTML  # type: ignore

        html = _build_prescription_html(
            branding_obj=branding_obj,
            rx=rx,
            patient=patient,
            doctor=doctor,
        )
        pdf_bytes = HTML(string=html,
                         base_url=str(settings.STORAGE_DIR)).write_pdf()
        return pdf_bytes, "application/pdf"
    except Exception:
        # 2) ReportLab fallback (FULL content)
        return _build_prescription_pdf_reportlab(
            branding_obj=branding_obj,
            rx=rx,
            patient=patient,
            doctor=doctor,
        )


# -------------------------------------------------------------------
# ReportLab fallback (FULL – header + cards + table + notes + signature)
# -------------------------------------------------------------------
def _build_prescription_pdf_reportlab(
    *,
    branding_obj: Any,
    rx: Any,
    patient: Any | None,
    doctor: Any | None,
) -> Tuple[bytes, str]:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # Design tokens
    INK = colors.HexColor("#0f172a")
    MUTED = colors.HexColor("#475569")
    SOFT = colors.HexColor("#f8fafc")
    LINE = colors.HexColor("#e5e7eb")
    BAR = colors.HexColor("#0b1220")

    M = 14 * mm
    content_w = W - 2 * M
    gap = 6 * mm

    # Extract values
    org_name = (_safe(_g(branding_obj, "org_name", "NUTRYAH HIMS")).strip()
                or "NUTRYAH HIMS")
    org_tagline = _safe(_g(branding_obj, "org_tagline", "")).strip()
    org_addr = _safe(_g(branding_obj, "org_address", "")).strip()
    org_phone = _safe(_g(branding_obj, "org_phone", "")).strip()
    org_email = _safe(_g(branding_obj, "org_email", "")).strip()
    org_web = _safe(_g(branding_obj, "org_website", "")).strip()
    logo_path = _safe(_g(branding_obj, "logo_path", "")).strip()

    rx_no = _safe(_g(rx, "rx_number", _g(rx, "rx_no", "—"))) or "—"
    rx_dt = _fmt_dt(_g(rx, "rx_datetime") or _g(rx, "created_at"))
    op_uid = _safe(_g(rx, "op_uid", "—")) or "—"
    ip_uid = _safe(_g(rx, "ip_uid", "—")) or "—"

    p_name = " ".join([
        _safe(_g(patient, "prefix", "")).strip(),
        _safe(_g(patient, "first_name", "")).strip(),
        _safe(_g(patient, "last_name", "")).strip(),
    ]).strip() or (_safe(_g(patient, "full_name", "—")) or "—")

    p_uhid = _safe(_g(patient, "uhid", "—")) or "—"
    p_phone = _safe(_g(patient, "phone", "—")) or "—"
    p_dob_raw = _g(patient, "dob", _g(patient, "date_of_birth", None))
    p_dob = _fmt_date(p_dob_raw)
    p_age_years = _age_years_from_dob(p_dob_raw)
    p_age = f"{p_age_years} Y" if p_age_years is not None else "—"
    p_gender = _safe(_g(patient, "gender", _g(patient, "sex", "—"))) or "—"

    d_name = _safe(_g(doctor, "full_name", _g(doctor, "name", "—"))) or "—"
    d_reg = _safe(_g(doctor, "registration_no", "—")) or "—"
    d_dept = _doctor_department_name(doctor)

    # Layout
    header_h = 24 * mm
    card_h = 24 * mm
    card_w = (content_w - gap) / 2

    def draw_brand_header() -> float:
        y_top = H - M
        x = M

        # Logo
        logo_w = 64 * mm
        logo_h = 20 * mm
        if logo_path:
            try:
                from pathlib import Path
                abs_path = Path(settings.STORAGE_DIR).joinpath(logo_path)
                if abs_path.exists():
                    img = ImageReader(str(abs_path))
                    c.drawImage(
                        img,
                        x,
                        y_top - logo_h,
                        width=logo_w,
                        height=logo_h,
                        preserveAspectRatio=True,
                        mask="auto",
                    )
            except Exception:
                pass

        tx = x + logo_w + 6 * mm

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13.5)
        c.drawString(tx, y_top - 5.2 * mm, org_name.upper())

        meta_parts = [p for p in [org_tagline, org_addr] if p]
        contact_parts = [p for p in [org_phone, org_email, org_web] if p]
        meta_line = " | ".join(meta_parts)
        contact_line = " | ".join(contact_parts)

        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.3)
        maxw = W - M - tx

        if meta_line:
            ln = _wrap(meta_line, "Helvetica", 8.3, maxw)[0]
            c.drawString(tx, y_top - 10.1 * mm, ln)

        if contact_line:
            ln = _wrap(contact_line, "Helvetica", 8.3, maxw)[0]
            c.drawString(tx, y_top - 14.1 * mm, ln)

        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        div_y = y_top - header_h
        c.line(M, div_y, W - M, div_y)
        return div_y

    def draw_title_row(y_base: float) -> float:
        y = y_base - 9 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(M, y, "PRESCRIPTION")

        c.setFont("Helvetica", 9.2)
        c.setFillColor(MUTED)
        c.drawRightString(W - M, y, f"Rx No: {rx_no}   |   Date: {rx_dt}")

        c.setFont("Helvetica", 8.6)
        c.drawRightString(W - M, y - 4.9 * mm,
                          f"OP: {op_uid}   |   IP: {ip_uid}")
        return y - 8.5 * mm

    def draw_card(x: float, y_top: float, title: str) -> float:
        c.setStrokeColor(LINE)
        c.setFillColor(SOFT)
        c.setLineWidth(1)
        c.roundRect(x, y_top - card_h, card_w, card_h, 6, stroke=1, fill=1)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(x + 4 * mm, y_top - 5.5 * mm, title)
        return y_top - 9.5 * mm

    def draw_kv(x: float, y: float, label: str, value: str,
                max_value_w: float) -> None:
        c.setFont("Helvetica", 8.4)
        c.setFillColor(MUTED)
        c.drawString(x, y, label)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.8)

        v = _safe(value) or "—"
        while v and pdfmetrics.stringWidth(v, "Helvetica-Bold",
                                           8.8) > max_value_w:
            v = v[:-1]
        if v != (_safe(value) or "—"):
            v = (v[:-1] + "…") if len(v) > 1 else "—"
        c.drawString(x + 22 * mm, y, v)

    def draw_cards(y_base: float) -> float:
        y_top = y_base - 4 * mm
        max_value_w = card_w - 8 * mm - 22 * mm

        # Patient card
        inner_y = draw_card(M, y_top, "Patient")
        x1 = M + 4 * mm
        draw_kv(x1, inner_y, "Name", p_name, max_value_w)
        draw_kv(x1, inner_y - 5.0 * mm, "UHID", p_uhid, max_value_w)

        c.setFont("Helvetica", 8.4)
        c.setFillColor(MUTED)
        c.drawString(x1, inner_y - 10.0 * mm, "Phone")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.8)
        c.drawString(x1 + 22 * mm, inner_y - 10.0 * mm, (p_phone or "—")[:26])

        c.setFont("Helvetica", 8.2)
        c.setFillColor(MUTED)
        c.drawString(x1, inner_y - 15.0 * mm,
                     f"DOB: {p_dob}   |   Age/Sex: {p_age} / {p_gender}")

        # Doctor card
        inner_y2 = draw_card(M + card_w + gap, y_top, "Doctor")
        x2 = (M + card_w + gap) + 4 * mm
        draw_kv(x2, inner_y2, "Name", d_name, max_value_w)
        draw_kv(x2, inner_y2 - 5.0 * mm, "Dept", d_dept, max_value_w)
        draw_kv(x2, inner_y2 - 10.0 * mm, "Reg No", d_reg, max_value_w)

        return y_top - card_h - 7 * mm

    cols = [
        ("#", 9 * mm),
        ("Medicine / Instructions",
         content_w - (9 + 11 + 11 + 11 + 14 + 14) * mm),
        ("AM", 11 * mm),
        ("AF", 11 * mm),
        ("PM", 11 * mm),
        ("Night", 14 * mm),
        ("Days", 14 * mm),
    ]

    x_positions = [M]
    for _, wcol in cols:
        x_positions.append(x_positions[-1] + wcol)

    def draw_table_header(y_top: float) -> float:
        h = 8 * mm
        c.setFillColor(BAR)
        c.setStrokeColor(BAR)
        c.rect(M, y_top - h, content_w, h, stroke=1, fill=1)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8.8)

        xx = M
        for title, wcol in cols:
            c.drawCentredString(xx + wcol / 2, y_top - h + 2.4 * mm, title)
            xx += wcol

        c.setStrokeColor(colors.HexColor("#1f2937"))
        c.setLineWidth(1)
        xx = M
        for _, wcol in cols[:-1]:
            xx += wcol
            c.line(xx, y_top - h, xx, y_top)
        return y_top - h

    def new_page(*, include_table_header: bool) -> float:
        div_y = draw_brand_header()
        y_after = draw_title_row(div_y)
        y_after = draw_cards(y_after)
        if include_table_header:
            y_after = draw_table_header(y_after)
        return y_after

    def draw_row(
        y_top: float,
        idx: int,
        drug: str,
        sub: str,
        am: int,
        af: int,
        pm: int,
        night: int,
        days: str,
    ) -> float:
        med_w = cols[1][1] - 4 * mm
        drug_lines = _wrap(drug, "Helvetica-Bold", 9.2, med_w)[:2]
        sub_lines = _wrap(sub, "Helvetica", 8.0, med_w)[:2] if sub else []

        pad_t = 2.0 * mm
        pad_b = 2.0 * mm
        lh1 = 4.1 * mm
        lh2 = 3.6 * mm

        row_h = pad_t + len(drug_lines) * lh1 + (len(sub_lines) * lh2) + pad_b
        row_h = max(row_h, 9.5 * mm)

        if y_top - row_h < 34 * mm:
            c.showPage()
            y_top = new_page(include_table_header=True)

        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.rect(M, y_top - row_h, content_w, row_h, stroke=1, fill=1)

        for xp in x_positions[1:-1]:
            c.line(xp, y_top - row_h, xp, y_top)

        c.setFillColor(INK)
        c.setFont("Helvetica", 9)
        c.drawCentredString((x_positions[0] + x_positions[1]) / 2,
                            y_top - row_h / 2 - 1.5 * mm, str(idx))

        x_text = x_positions[1] + 2 * mm
        y_text = y_top - pad_t - lh1 + 0.6 * mm

        c.setFont("Helvetica-Bold", 9.2)
        c.setFillColor(INK)
        for ln in drug_lines:
            c.drawString(x_text, y_text, ln)
            y_text -= lh1

        if sub_lines:
            c.setFont("Helvetica", 8.0)
            c.setFillColor(MUTED)
            for ln in sub_lines:
                c.drawString(x_text, y_text + 0.2 * mm, ln)
                y_text -= lh2

        def draw_center(col_idx: int, val: Any) -> None:
            xc = (x_positions[col_idx] + x_positions[col_idx + 1]) / 2
            c.setFont("Helvetica-Bold", 9.0)
            c.setFillColor(INK)
            c.drawCentredString(xc, y_top - row_h / 2 - 1.5 * mm, str(val))

        draw_center(2, am)
        draw_center(3, af)
        draw_center(4, pm)
        draw_center(5, night)
        draw_center(6, days)

        return y_top - row_h

    # Render
    row_y = new_page(include_table_header=True)

    lines = _g(rx, "lines", []) or []
    if not lines:
        row_h = 10 * mm
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.rect(M, row_y - row_h, content_w, row_h, stroke=1, fill=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(M + 3 * mm, row_y - row_h / 2 - 1.5 * mm, "No medicines")
        row_y -= row_h
    else:
        for i, ln in enumerate(lines, start=1):
            drug = _safe(
                _g(ln, "item_name", _g(_g(ln, "item", None), "name",
                                       "—"))) or "—"
            dose = _safe(_g(ln, "dose_text", "")).strip()
            route = _safe(_g(ln, "route", "")).strip()
            timing = _safe(_g(ln, "timing", "")).strip()
            inst = _safe(_g(ln, "instructions", "")).strip()
            if not timing and inst:
                timing = inst

            qty = _safe(_g(ln, "requested_qty", "")).strip()
            days = _safe(_g(ln, "duration_days", _g(ln, "days", "—"))) or "—"
            freq = _g(ln, "frequency_code",
                      _g(ln, "frequency", _g(ln, "freq", None)))
            am, af, pm, night = freq_to_slots(freq)

            sub_parts = [p for p in [dose, route, timing] if p]
            sub = " • ".join(sub_parts)
            if qty and ("Qty:" not in sub):
                sub = (sub + f" • Qty: {qty}").strip(" •")

            row_y = draw_row(row_y, i, drug, sub, am, af, pm, night, days)

    # Notes
    notes = _safe(_g(rx, "notes", "")).strip()
    if notes:
        note_lines = _wrap(notes, "Helvetica", 9.0, content_w - 8 * mm)
        note_h = 6 * mm + min(len(note_lines), 6) * 4.2 * mm + 6 * mm

        if row_y - note_h < 34 * mm:
            c.showPage()
            row_y = new_page(include_table_header=False)

        y_top = row_y - 6 * mm
        c.setStrokeColor(LINE)
        c.setFillColor(SOFT)
        c.roundRect(M, y_top - note_h, content_w, note_h, 6, stroke=1, fill=1)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(M + 4 * mm, y_top - 6 * mm, "Notes")

        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)
        yy = y_top - 11 * mm
        for ln in note_lines[:6]:
            c.drawString(M + 4 * mm, yy, ln)
            yy -= 4.2 * mm

        row_y = y_top - note_h

    # Signature
    sig_y = 22 * mm
    c.setStrokeColor(colors.HexColor("#111827"))
    c.setLineWidth(1)
    c.line(W - 80 * mm, sig_y, W - M, sig_y)

    c.setFont("Helvetica-Bold", 8.8)
    c.setFillColor(INK)
    c.drawRightString(W - M, sig_y - 4.2 * mm, "Doctor Signature")

    c.showPage()
    c.save()
    return buf.getvalue(), "application/pdf"
