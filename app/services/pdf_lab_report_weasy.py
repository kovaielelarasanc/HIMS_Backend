# FILE: app/services/pdf_lis_report.py
from __future__ import annotations

import logging
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, List

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from app.core.config import settings
from app.services.pdf_branding import render_brand_header_html, brand_header_css

logger = logging.getLogger(__name__)


# ---------------------------
# Common helpers
# ---------------------------
def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _present(v: Any) -> bool:
    s = ("" if v is None else str(v)).strip()
    return bool(s) and s not in ("—", "-", "None", "null", "NULL")


def _fmt_date_only(v: Any) -> str:
    if not v:
        return "-"
    if isinstance(v, datetime):
        return v.strftime("%d-%b-%Y")
    if isinstance(v, date):
        return v.strftime("%d-%b-%Y")
    try:
        return datetime.fromisoformat(str(v).replace(
            "Z", "+00:00")).strftime("%d-%b-%Y")
    except Exception:
        return str(v)


def _split_text_to_lines(txt: str, font_name: str, font_size: float,
                         max_w: float) -> list[str]:
    """
    ReportLab wrap with newline support.
    """
    t = (txt or "-").replace("\r\n", "\n").strip()
    if not t:
        return ["-"]

    out: list[str] = []
    for para in t.split("\n"):
        para = para.strip()
        if not para:
            continue
        words = para.split(" ")
        line = ""
        for w in words:
            trial = (line + " " + w).strip()
            if stringWidth(trial, font_name, font_size) <= max_w:
                line = trial
                continue
            if line:
                out.append(line)
                line = ""
            # hard-wrap long token
            if stringWidth(w, font_name, font_size) <= max_w:
                line = w
            else:
                chunk = ""
                for ch in w:
                    trial2 = chunk + ch
                    if stringWidth(trial2, font_name, font_size) <= max_w:
                        chunk = trial2
                    else:
                        if chunk:
                            out.append(chunk)
                        chunk = ch
                line = chunk
        if line:
            out.append(line)

    return out if out else ["-"]


# ---------------------------
# WeasyPrint (Optional) UI
# ---------------------------
def _flag_badge(flag: str) -> str:
    f = (flag or "").strip().upper()
    if f in {"H", "HIGH"}:
        return "<span class='pill pill-red'>HIGH</span>"
    if f in {"L", "LOW"}:
        return "<span class='pill pill-amber'>LOW</span>"
    if f in {"N", "NORMAL"}:
        return "<span class='pill pill-green'>NORMAL</span>"
    if f:
        return f"<span class='pill'>{_h(f)}</span>"
    return "<span class='pill'>—</span>"


def _css() -> str:
    return f"""
    {brand_header_css()}

    :root {{
      --ink:#0f172a;
      --muted:#64748b;
      --line:#e2e8f0;
      --soft:#f8fafc;
      --soft2:#f1f5f9;
      --dark:#0b1220;
      --radius:12px;
    }}

    *{{box-sizing:border-box}}
    body{{
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      color:var(--ink);
      font-size:11px;
      margin:0;
      padding:0;
    }}

    header{{ position: running(pageHeader); }}
    footer{{ position: running(pageFooter); }}

    @page {{
      size: A4;
      margin: 14mm 14mm 16mm 14mm;
      @top-center {{ content: element(pageHeader); }}
      @bottom-center {{ content: element(pageFooter); }}
    }}

    .wrap{{ width:100%; }}

    .title-row{{
      display:flex;
      justify-content:space-between;
      align-items:flex-end;
      margin: 2px 0 10px;
      gap: 12px;
    }}
    .title{{
      font-size:15px;
      font-weight:900;
      margin:0;
      letter-spacing:-0.2px;
    }}
    .right-meta{{
      text-align:right;
      color:var(--muted);
      font-size:10px;
      line-height:1.35;
      white-space:nowrap;
    }}

    .hr{{ border-top:1px solid var(--line); margin: 10px 0; }}

    .grid{{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:10px;
      margin: 10px 0 12px;
    }}

    .card{{
      border:1px solid var(--line);
      border-radius:var(--radius);
      overflow:hidden;
      background:#fff;
    }}
    .card-h{{
      background:var(--soft);
      padding:7px 10px;
      font-weight:900;
      font-size:10px;
      letter-spacing:.25px;
      text-transform:uppercase;
    }}
    .card-b{{
      padding:8px 10px 10px;
    }}
    .row{{
      display:flex;
      justify-content:space-between;
      gap:10px;
      padding:4px 0;
      border-bottom:1px solid var(--soft2);
    }}
    .row:last-child{{ border-bottom:none; }}
    .k{{ color:var(--muted); font-weight:700; min-width:92px; }}
    .v{{ color:var(--ink); font-weight:800; text-align:right; }}

    .section-title{{
      font-weight:900;
      font-size:10px;
      letter-spacing:.25px;
      text-transform:uppercase;
      margin: 12px 0 6px;
    }}

    table{{
      width:100%;
      border-collapse:separate;
      border-spacing:0;
      border:1px solid var(--line);
      border-radius:var(--radius);
      overflow:hidden;
      page-break-inside: auto;
    }}
    thead th{{
      background:var(--dark);
      color:#fff;
      font-size:9.5px;
      letter-spacing:.35px;
      text-transform:uppercase;
      padding:7px 8px;
      text-align:left;
    }}
    tbody td{{
      padding:7px 8px;
      border-bottom:1px solid var(--line);
      vertical-align:top;
      background:#fff;
    }}
    tbody tr:nth-child(even) td{{ background:var(--soft); }}
    tbody tr:last-child td{{ border-bottom:none; }}
    tr{{ page-break-inside: avoid; }}

    .tname{{ font-weight:900; }}
    .tcomment{{ margin-top:2px; color:var(--muted); font-size:9.5px; }}

    .pill{{
      display:inline-block;
      padding:2px 8px;
      border-radius:999px;
      font-size:9px;
      font-weight:900;
      border:1px solid var(--line);
      background:var(--soft);
      color:var(--ink);
      letter-spacing:.2px;
      white-space:nowrap;
    }}
    .pill-red{{ border-color:#fecaca; background:#fef2f2; color:#991b1b; }}
    .pill-amber{{ border-color:#fde68a; background:#fffbeb; color:#92400e; }}
    .pill-green{{ border-color:#bbf7d0; background:#ecfdf5; color:#065f46; }}

    .sigs{{
      margin-top:16px;
      display:grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap:10px;
    }}
    .sigbox{{
      border-top:1px solid var(--line);
      padding-top:6px;
      text-align:center;
      color:var(--muted);
      font-size:9.5px;
    }}

    .foot{{
      width:100%;
      border-top:1px solid var(--line);
      padding-top:6px;
      display:flex;
      justify-content:space-between;
      align-items:center;
      color:var(--muted);
      font-size:9.5px;
    }}
    .pagenum:before {{ content: "Page " counter(page) " / " counter(pages); }}
    """


def _build_lab_report_html(
    *,
    branding: Any,
    report: Any,
    patient: Any,
    lab_no: str,
    order_date: Any,
    collected_by_name: Optional[str],
) -> str:
    header_html = render_brand_header_html(branding)

    pname = _h(getattr(report, "patient_name", None) or "-")
    uhid = _h(getattr(report, "patient_uhid", None) or "-")
    age_text = getattr(report, "patient_age_text", None)
    gender = getattr(report, "patient_gender", None)
    age_sex = _h(" / ".join([x for x in [age_text, gender] if x]) or "-")
    dob = _fmt_date_only(getattr(report, "patient_dob", None))
    mobile = getattr(patient, "mobile", None) or getattr(
        patient, "phone", None) or "-"
    mobile = _h(mobile)

    patient_type = _h(getattr(report, "patient_type", None) or "-")

    order_dt = _fmt_date_only(order_date)
    collected_dt = _fmt_date_only(getattr(report, "received_on", None))
    reported_dt = _fmt_date_only(getattr(report, "reported_on", None))

    sec_html = ""
    for sec in (getattr(report, "sections", None) or []):
        title = (getattr(sec, "department_name", "") or "Department").strip()
        sub = (getattr(sec, "sub_department_name", None) or "").strip()
        if sub:
            title = f"{title} / {sub}"

        rows = getattr(sec, "rows", None) or []
        rows_html = ""
        for r in rows:
            tname = _h(getattr(r, "service_name", None) or "-")
            result = _h(getattr(r, "result_value", None) or "-")
            unit = _h(getattr(r, "unit", None) or "-")
            ref = _h(getattr(r, "normal_range", None) or "-")
            flag = _flag_badge(getattr(r, "flag", None) or "")
            comments = (getattr(r, "comments", None) or "").strip()
            cmt_html = f"<div class='tcomment'>{_h(comments)}</div>" if comments else ""

            rows_html += f"""
              <tr>
                <td>
                  <div class="tname">{tname}</div>
                  {cmt_html}
                </td>
                <td><b>{result}</b></td>
                <td>{unit}</td>
                <td>{flag}</td>
                <td style="white-space:pre-line">{ref}</td>
              </tr>
            """

        if not rows_html:
            rows_html = "<tr><td colspan='5' style='color:#64748b;padding:10px;'>No results.</td></tr>"

        sec_html += f"""
          <div class="section-title">{_h(title)}</div>
          <table>
            <thead>
              <tr>
                <th style="width:42%;">Test</th>
                <th style="width:12%;">Result</th>
                <th style="width:10%;">Unit</th>
                <th style="width:12%;">Flag</th>
                <th style="width:24%;">Reference Range</th>
              </tr>
            </thead>
            <tbody>
              {rows_html}
            </tbody>
          </table>
        """

    return f"""
    <html>
      <head><meta charset="utf-8"/></head>
      <body>
        <header>{header_html}</header>

        <footer>
          <div class="foot">
            <div>Generated on: {_fmt_date_only(datetime.utcnow())}</div>
            <div class="pagenum"></div>
          </div>
        </footer>

        <div class="wrap">
          <div class="title-row">
            <div>
              <h1 class="title">Laboratory Report</h1>
            </div>
            <div class="right-meta">
              <div><b>Order Date:</b> {_h(order_dt)}</div>
            </div>
          </div>

          <div class="grid">
            <div class="card">
              <div class="card-h">Patient Details</div>
              <div class="card-b">
                <div class="row"><div class="k">Name</div><div class="v">{pname}</div></div>
                <div class="row"><div class="k">UHID</div><div class="v">{uhid}</div></div>
                <div class="row"><div class="k">Age/Sex</div><div class="v">{age_sex}</div></div>
                <div class="row"><div class="k">DOB</div><div class="v">{_h(dob)}</div></div>
                <div class="row"><div class="k">Mobile</div><div class="v">{mobile}</div></div>
              </div>
            </div>

            <div class="card">
              <div class="card-h">Order Details</div>
              <div class="card-b">
                <div class="row"><div class="k">Lab No</div><div class="v">{_h(lab_no)}</div></div>
                <div class="row"><div class="k">Order Date</div><div class="v">{_h(order_dt)}</div></div>
                <div class="row"><div class="k">Collected</div><div class="v">{_h(collected_dt)}</div></div>
                <div class="row"><div class="k">Reported</div><div class="v">{_h(reported_dt)}</div></div>
                <div class="row"><div class="k">Type</div><div class="v">{patient_type}</div></div>
                <div class="row"><div class="k">Collected By</div><div class="v">{_h(collected_by_name or "-")}</div></div>
              </div>
            </div>
          </div>

          <div class="hr"></div>

          {sec_html}

          <div class="sigs">
            <div class="sigbox">Lab Technician</div>
            <div class="sigbox">Verified By</div>
            <div class="sigbox">Authorized Signatory</div>
          </div>
        </div>
      </body>
    </html>
    """.strip()


# ---------------------------
# ReportLab fallback (Always)
# ---------------------------
def _draw_letterhead_background(c: canvas.Canvas,
                                branding: Any,
                                page_num: int = 1) -> None:
    if not branding or not getattr(branding, "letterhead_path", None):
        return

    position = getattr(branding, "letterhead_position",
                       "background") or "background"
    if position == "none":
        return
    if position == "first_page_only" and page_num != 1:
        return
    if getattr(branding, "letterhead_type", None) not in {"image", None}:
        return

    full_path = Path(settings.STORAGE_DIR).joinpath(
        getattr(branding, "letterhead_path"))
    if not full_path.exists():
        return

    try:
        img = ImageReader(str(full_path))
        w, h = A4
        c.drawImage(img,
                    0,
                    0,
                    width=w,
                    height=h,
                    preserveAspectRatio=True,
                    mask="auto")
    except Exception:
        logger.exception("Failed to draw letterhead background")


def _logo_reader(branding: Any) -> Optional[ImageReader]:
    rel = (getattr(branding, "logo_path", None) or "").strip()
    if not rel:
        return None
    try:
        abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
        if abs_path.exists() and abs_path.is_file():
            return ImageReader(str(abs_path))
    except Exception:
        return None
    return None


def _wrap_simple(text: str, font: str, size: float, max_w: float) -> List[str]:
    s = (text or "").replace("\n", " ").strip()
    if not s:
        return []
    words = s.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if stringWidth(cand, font, size) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_brand_header_reportlab(
    c: canvas.Canvas,
    branding: Any,
    *,
    page_w: float,
    page_h: float,
    left: float,
    right: float,
    top: float,
    show_rule: bool = True,
) -> float:
    """
    Matches your pdf_branding style:
    Left: Logo
    Right: Org details (right-aligned)
    Returns Y position to continue drawing.
    """
    INK = colors.HexColor("#0f172a")
    MUTED = colors.HexColor("#64748b")
    LINE = colors.HexColor("#e5e7eb")

    org_name = (getattr(branding, "org_name", None) or "").strip() or "NUTRYAH"
    org_tagline = (getattr(branding, "org_tagline", None) or "").strip()
    org_addr = (getattr(branding, "org_address", None) or "").strip()
    org_phone = (getattr(branding, "org_phone", None) or "").strip()
    org_email = (getattr(branding, "org_email", None) or "").strip()
    org_web = (getattr(branding, "org_website", None) or "").strip()

    y_top = page_h - top

    # Logo box
    lr = _logo_reader(branding)
    logo_w = 64 * mm
    logo_h = 18 * mm
    if lr:
        try:
            c.drawImage(
                lr,
                left,
                y_top - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            pass

    # Right block
    xr = page_w - right
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 13.5)
    c.drawRightString(xr, y_top - 4.8 * mm, org_name)

    yy = y_top - 9.8 * mm
    if org_tagline:
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.6)
        c.drawRightString(xr, yy, org_tagline)
        yy -= 3.9 * mm

    if org_addr:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.4)
        addr_lines = _wrap_simple(org_addr, "Helvetica", 8.4, 92 * mm)[:2]
        for ln in addr_lines:
            c.drawRightString(xr, yy, ln)
            yy -= 3.7 * mm

    contact_parts = [p for p in [org_email, org_phone] if p]
    if contact_parts:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.4)
        c.drawRightString(xr, yy, " | ".join(contact_parts))
        yy -= 3.7 * mm

    if org_web:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.4)
        c.drawRightString(xr, yy, org_web)

    # bottom rule
    y_after = y_top - 24 * mm
    if show_rule:
        c.setStrokeColor(LINE)
        c.setLineWidth(0.9)
        c.line(left, y_after, page_w - right, y_after)

    return y_after - 6 * mm


def _build_lab_report_pdf_reportlab(
    *,
    branding: Any,
    report: Any,
    patient: Any,
    lab_no: str,
    order_date: Any,
    collected_by_name: Optional[str],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    LEFT = 14 * mm
    RIGHT = 14 * mm
    TOP = 10 * mm
    BOTTOM = 14 * mm

    TABLE_W = page_w - LEFT - RIGHT

    # columns
    W_TEST = 68 * mm
    W_RESULT = 22 * mm
    W_UNIT = 18 * mm
    W_FLAG = 18 * mm
    W_REF = TABLE_W - (W_TEST + W_RESULT + W_UNIT + W_FLAG)

    X_TEST = LEFT
    X_RESULT = X_TEST + W_TEST
    X_UNIT = X_RESULT + W_RESULT
    X_FLAG = X_UNIT + W_UNIT
    X_REF = X_FLAG + W_FLAG

    PAD = 1.8 * mm

    INK = colors.HexColor("#0F172A")
    MUTED = colors.HexColor("#475569")
    LINE = colors.HexColor("#E2E8F0")
    SOFT = colors.HexColor("#F8FAFC")
    SOFT2 = colors.HexColor("#F1F5F9")
    DARK = colors.HexColor("#0B1220")
    WHITE = colors.white

    # patient fields
    pname = getattr(report, "patient_name", None) or "-"
    uhid = getattr(report, "patient_uhid", None) or "-"
    age_text = getattr(report, "patient_age_text", None)
    gender = getattr(report, "patient_gender", None)
    age_sex = " / ".join([x for x in [age_text, gender] if x]) or "-"
    dob = _fmt_date_only(getattr(report, "patient_dob", None))
    mobile = getattr(patient, "mobile", None) or getattr(
        patient, "phone", None) or "-"
    ptype = getattr(report, "patient_type", None) or "-"

    order_dt = _fmt_date_only(order_date)
    collected_dt = _fmt_date_only(getattr(report, "received_on", None))
    reported_dt = _fmt_date_only(getattr(report, "reported_on", None))

    def draw_footer(page_no: int):
        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.line(LEFT, BOTTOM - 5 * mm, page_w - RIGHT, BOTTOM - 5 * mm)
        c.setFont("Helvetica", 7.2)
        c.setFillColor(MUTED)
        c.drawString(LEFT, BOTTOM - 9 * mm,
                     f"Generated on: {_fmt_date_only(datetime.utcnow())}")
        c.drawRightString(page_w - RIGHT, BOTTOM - 9 * mm, f"Page {page_no}")

    def pill(x: float, y: float, text: str, fill, stroke, tcol):
        h = 5.6 * mm
        tw = stringWidth(text, "Helvetica-Bold", 7.8)
        w = max(18 * mm, tw + 10 * mm)
        r = h / 2
        c.setFillColor(fill)
        c.setStrokeColor(stroke)
        c.setLineWidth(0.7)
        c.roundRect(x, y - h + 1.3 * mm, w, h, r, stroke=1, fill=1)
        c.setFont("Helvetica-Bold", 7.8)
        c.setFillColor(tcol)
        c.drawCentredString(x + (w / 2), y - 3.7 * mm, text)
        return w

    def flag_pill(x: float, y: float, flag: str):
        f = (flag or "").strip().upper()
        if not f:
            c.setFont("Helvetica", 8.2)
            c.setFillColor(MUTED)
            c.drawString(x + PAD, y, "—")
            return
        if f in {"H", "HIGH"}:
            pill(x, y + 2.2 * mm, "HIGH", colors.HexColor("#FEF2F2"),
                 colors.HexColor("#FECACA"), colors.HexColor("#991B1B"))
        elif f in {"L", "LOW"}:
            pill(x, y + 2.2 * mm, "LOW", colors.HexColor("#FFFBEB"),
                 colors.HexColor("#FDE68A"), colors.HexColor("#92400E"))
        elif f in {"N", "NORMAL"}:
            pill(x, y + 2.2 * mm, "NORMAL", colors.HexColor("#ECFDF5"),
                 colors.HexColor("#BBF7D0"), colors.HexColor("#065F46"))
        else:
            pill(x, y + 2.2 * mm, f, SOFT, LINE, INK)

    def card(x: float, y_top: float, w: float, title: str,
             pairs: list[tuple[str, str]]):
        pairs = [(k, v) for (k, v) in pairs if _present(v)]

        row_h = 5.4 * mm
        head_h = 8.2 * mm
        min_rows = max(len(pairs), 3)
        h = head_h + (min_rows * row_h) + 6 * mm

        y_bot = y_top - h

        c.setFillColor(WHITE)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.9)
        c.roundRect(x, y_bot, w, h, 10, stroke=1, fill=1)

        c.setFillColor(SOFT)
        c.setStrokeColor(SOFT)
        c.roundRect(x, y_top - head_h, w, head_h, 10, stroke=0, fill=1)

        c.setFont("Helvetica-Bold", 9.2)
        c.setFillColor(INK)
        c.drawString(x + 4 * mm, y_top - 5.8 * mm, title)

        start_y = y_top - head_h - 4.2 * mm
        label_w = 22 * mm

        for i in range(min_rows):
            ry = start_y - (i * row_h)
            if i % 2 == 1:
                c.setFillColor(SOFT2)
                c.rect(x + 2 * mm,
                       ry - 4.2 * mm,
                       w - 4 * mm,
                       4.9 * mm,
                       stroke=0,
                       fill=1)

            if i < len(pairs):
                k, v = pairs[i]
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Bold", 8.0)
                c.drawString(x + 4 * mm, ry - 3.3 * mm, f"{k}:")
                c.setFillColor(INK)
                c.setFont("Helvetica", 8.6)

                max_w = w - (4 * mm + label_w + 6 * mm)
                lines = _split_text_to_lines(str(v), "Helvetica", 8.6, max_w)
                c.drawString(x + 4 * mm + label_w, ry - 3.3 * mm, lines[0])

        return y_bot - 6 * mm

    page_no = 1
    current_y = page_h

    def start_page(page_no: int, compact_meta: bool):
        nonlocal current_y
        _draw_letterhead_background(c, branding, page_no)

        y = _draw_brand_header_reportlab(
            c,
            branding,
            page_w=page_w,
            page_h=page_h,
            left=LEFT,
            right=RIGHT,
            top=TOP,
            show_rule=True,
        )

        # Title row
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(INK)
        c.drawString(LEFT, y, "Laboratory Report")
        c.setFont("Helvetica", 9.2)
        c.setFillColor(MUTED)
        c.drawRightString(page_w - RIGHT, y + 1.0 * mm,
                          f"Order Date: {order_dt}")
        y -= 7 * mm

        # divider
        c.setStrokeColor(LINE)
        c.setLineWidth(0.8)
        c.line(LEFT, y, page_w - RIGHT, y)
        y -= 7 * mm

        gap = 8 * mm
        card_w = (TABLE_W - gap) / 2

        if compact_meta:
            patient_pairs = [
                ("Name", str(pname)),
                ("UHID", str(uhid)),
                ("Age/Sex", str(age_sex)),
            ]
            order_pairs = [
                ("Lab No", str(lab_no)),
                ("Reported", str(reported_dt)),
                ("Type", str(ptype)),
            ]
        else:
            patient_pairs = [
                ("Name", str(pname)),
                ("UHID", str(uhid)),
                ("Age/Sex", str(age_sex)),
                ("DOB", str(dob)),
                ("Mobile", str(mobile)),
            ]
            order_pairs = [
                ("Lab No", str(lab_no)),
                ("Order Date", str(order_dt)),
                ("Collected", str(collected_dt)),
                ("Reported", str(reported_dt)),
                ("Collected By", str(collected_by_name or "-")),
                ("Type", str(ptype)),
            ]

        y_cards_top = y
        y_after_patient = card(LEFT, y_cards_top, card_w, "Patient Details",
                               patient_pairs)
        y_after_order = card(LEFT + card_w + gap, y_cards_top, card_w,
                             "Order Details", order_pairs)
        y = min(y_after_patient, y_after_order)

        c.setStrokeColor(LINE)
        c.setLineWidth(0.8)
        c.line(LEFT, y, page_w - RIGHT, y)
        y -= 8 * mm

        current_y = y

    def ensure_space(need_h: float, section: Optional[str] = None):
        nonlocal page_no, current_y
        if current_y - need_h < (BOTTOM + 10 * mm):
            draw_footer(page_no)
            c.showPage()
            page_no += 1
            start_page(page_no, compact_meta=True)
            if section:
                draw_section_header(section)

    def draw_table_header():
        nonlocal current_y
        c.setFillColor(DARK)
        c.rect(LEFT, current_y - 8 * mm, TABLE_W, 8 * mm, stroke=0, fill=1)

        c.setFont("Helvetica-Bold", 8.8)
        c.setFillColor(WHITE)
        c.drawString(X_TEST + PAD, current_y - 5.6 * mm, "TEST")
        c.drawString(X_RESULT + PAD, current_y - 5.6 * mm, "RESULT")
        c.drawString(X_UNIT + PAD, current_y - 5.6 * mm, "UNIT")
        c.drawString(X_FLAG + PAD, current_y - 5.6 * mm, "FLAG")
        c.drawString(X_REF + PAD, current_y - 5.6 * mm, "REFERENCE RANGE")

        current_y -= 10 * mm

    def draw_section_header(section_title: str):
        nonlocal current_y
        ensure_space(18 * mm)

        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(INK)
        c.drawString(LEFT, current_y, section_title.upper())

        c.setStrokeColor(LINE)
        c.setLineWidth(0.8)
        c.line(LEFT, current_y - 2.5 * mm, page_w - RIGHT,
               current_y - 2.5 * mm)

        current_y -= 7 * mm
        draw_table_header()

    # start
    start_page(page_no, compact_meta=False)

    zebra = 0
    for sec in (getattr(report, "sections", None) or []):
        title = (getattr(sec, "department_name", None) or "Department").strip()
        sub = (getattr(sec, "sub_department_name", None) or "").strip()
        if sub:
            title = f"{title} / {sub}"

        draw_section_header(title)

        for row in (getattr(sec, "rows", None) or []):
            test = (getattr(row, "service_name", None) or "-").strip()
            result = (getattr(row, "result_value", None) or "-").strip()
            unit = (getattr(row, "unit", None) or "-").strip()
            flag = (getattr(row, "flag", None) or "").strip()
            ref = (getattr(row, "normal_range", None) or "-").strip()
            comments = (getattr(row, "comments", None) or "").strip()

            test_lines = _split_text_to_lines(test, "Helvetica-Bold", 8.8,
                                              W_TEST - 2 * PAD)
            cmt_lines = _split_text_to_lines(comments, "Helvetica-Oblique",
                                             8.0, W_TEST -
                                             2 * PAD) if comments else []
            ref_lines = _split_text_to_lines(ref, "Helvetica", 8.2,
                                             W_REF - 2 * PAD)

            left_lines = len(test_lines) + (len(cmt_lines) if cmt_lines else 0)
            lines_n = max(left_lines, len(ref_lines), 1)

            row_h = (lines_n * 4.4 + 4.0) * mm
            ensure_space(row_h, section=title)

            top_y = current_y
            row_bottom = top_y - row_h

            if zebra % 2 == 1:
                c.setFillColor(SOFT)
                c.rect(LEFT, row_bottom, TABLE_W, row_h, stroke=0, fill=1)

            y_line = top_y - 4.2 * mm

            # Test
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 8.8)
            for i, ln in enumerate(test_lines):
                c.drawString(X_TEST + PAD, y_line - (i * 4.4 * mm), ln)

            # Comments
            if cmt_lines:
                cy = y_line - (len(test_lines) * 4.4 * mm) - 0.3 * mm
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Oblique", 8.0)
                for i, ln in enumerate(cmt_lines[:3]):
                    c.drawString(X_TEST + PAD, cy - (i * 4.2 * mm), ln)

            # Result
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 10.2)
            c.drawString(X_RESULT + PAD, y_line, result)

            # Unit
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.6)
            c.drawString(X_UNIT + PAD, y_line, unit)

            # Flag
            flag_pill(X_FLAG + 1.5 * mm, y_line + 1.2 * mm, flag)

            # Ref range
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.2)
            for i, ln in enumerate(ref_lines[:6]):
                c.drawString(X_REF + PAD, y_line - (i * 4.4 * mm), ln)

            # Row separator
            c.setStrokeColor(LINE)
            c.setLineWidth(0.6)
            c.line(LEFT, row_bottom, page_w - RIGHT, row_bottom)

            current_y = row_bottom
            zebra += 1

        current_y -= 6 * mm

    # Signatures
    ensure_space(34 * mm)
    c.setFont("Helvetica-Bold", 9.2)
    c.setFillColor(INK)
    c.drawString(LEFT, current_y, "Signatures")
    current_y -= 6 * mm

    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)

    sig_y = current_y - 10 * mm
    c.line(LEFT, sig_y, LEFT + 58 * mm, sig_y)
    c.line(LEFT + 70 * mm, sig_y, LEFT + 128 * mm, sig_y)
    c.line(page_w - RIGHT - 58 * mm, sig_y, page_w - RIGHT, sig_y)

    c.setFont("Helvetica", 8.5)
    c.setFillColor(MUTED)
    c.drawString(LEFT, sig_y - 5 * mm, "Lab Technician")
    c.drawString(LEFT + 70 * mm, sig_y - 5 * mm, "Verified By")
    c.drawRightString(page_w - RIGHT, sig_y - 5 * mm, "Authorized Signatory")

    draw_footer(page_no)
    c.save()
    return buf.getvalue()


# ---------------------------
# Public API (WeasyPrint + fallback)
# ---------------------------
def build_lab_report_pdf_bytes(
    *,
    branding: Any,
    report: Any,
    patient: Any,
    lab_no: str,
    order_date: Any,
    collected_by_name: Optional[str] = None,
) -> bytes:
    """
    ✅ Tries WeasyPrint first (if available).
    ✅ Always falls back to ReportLab (works in all environments).
    """
    try:
        from weasyprint import HTML, CSS  # type: ignore

        html = _build_lab_report_html(
            branding=branding,
            report=report,
            patient=patient,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
        )
        return HTML(string=html, base_url=str(
            settings.STORAGE_DIR)).write_pdf(stylesheets=[CSS(string=_css())])
    except Exception:
        return _build_lab_report_pdf_reportlab(
            branding=branding,
            report=report,
            patient=patient,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
        )
