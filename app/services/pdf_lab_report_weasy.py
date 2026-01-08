# FILE: app/services/pdf_lis_report.py
from __future__ import annotations

import logging
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, List

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics.barcode.qr import QrCodeWidget

from app.core.config import settings
from app.services.pdf_branding import brand_header_css

logger = logging.getLogger(__name__)

# ============================================================
# Common helpers
# ============================================================
def _norm_rel_path(p: str) -> str:
    # make "/uploads/a.png" -> "uploads/a.png"
    return (p or "").strip().lstrip("/\\")

def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

IST_TZ = ZoneInfo("Asia/Kolkata")  # IST

def _name_with_prefix(report: Any, fallback_name: str) -> str:
    """
    Build: 'Mr. Arun' / 'Dr. Arun' etc.
    Reads common fields: prefix, title, salutation.
    If already present in name, won't duplicate.
    """
    raw_name = (fallback_name or "").strip()
    if not raw_name:
        return "-"

    pref = str(_pick(report, "prefix", "salutation", "title", "name_prefix", default="") or "").strip()
    if not pref:
        return raw_name

    # Normalize prefix
    pref_clean = pref.replace(".", "").strip()
    if not pref_clean:
        return raw_name

    # Standard dot format
    pref_fmt = f"{pref_clean}."

    # Avoid duplicates if name already starts with prefix
    low_name = raw_name.lower()
    low_pref1 = pref_clean.lower() + " "
    low_pref2 = pref_fmt.lower() + " "
    if low_name.startswith(low_pref1) or low_name.startswith(low_pref2):
        return raw_name

    return f"{pref_fmt} {raw_name}"


def _to_ist(dt: datetime) -> datetime:
    """
    Convert datetime to IST.
    - If dt is naive, assume it's UTC (common for datetime.utcnow()).
    - If dt is aware, convert timezone properly.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST_TZ)

def _fmt_date_only(v: Any) -> str:
    if not v:
        return "-"
    if isinstance(v, datetime):
        return _to_ist(v).strftime("%d %b %Y")
    if isinstance(v, date):
        return v.strftime("%d %b %Y")
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return _to_ist(dt).strftime("%d %b %Y") if isinstance(dt, datetime) else str(v)
    except Exception:
        return str(v)


def _fmt_datetime(v: Any) -> str:
    if not v:
        return "-"
    if isinstance(v, datetime):
        return _to_ist(v).strftime("%d %b %Y, %I:%M %p")  # ✅ IST
    try:
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day).strftime("%d %b %Y")
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if isinstance(dt, datetime):
            return _to_ist(dt).strftime("%d %b %Y, %I:%M %p")  # ✅ IST
        return str(v)
    except Exception:
        return str(v)




def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        try:
            v = getattr(obj, n, None)
        except Exception:
            v = None
        if v is not None and str(v).strip() != "":
            return v
    return default


def _split_text_to_lines(txt: str, font_name: str, font_size: float, max_w: float) -> list[str]:
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


def _result_color(flag: str) -> colors.Color:
    f = (flag or "").strip().upper()
    if f in {"H", "HIGH"}:
        return colors.HexColor("#DC2626")
    if f in {"L", "LOW"}:
        return colors.HexColor("#2563EB")
    return colors.HexColor("#0F172A")


def _flag_label(flag: str) -> str:
    f = (flag or "").strip().upper()
    if f in {"H", "HIGH"}:
        return "High"
    if f in {"L", "LOW"}:
        return "Low"
    if f in {"N", "NORMAL"}:
        return "Normal"
    return ""


# ============================================================
# QR + Barcode helpers (WeasyPrint inline SVG)
# ============================================================


def _svg_clean(svg: str) -> str:
    s = svg or ""
    for token in ["<?xml", "<!DOCTYPE"]:
        i = s.find(token)
        if i != -1:
            j = s.find(">", i)
            if j != -1:
                s = s[:i] + s[j + 1 :]
    return s.strip()


def _qr_svg_from_text(data: str, size_mm: float = 22.0) -> str:
    data = (data or "").strip()
    if not data:
        return ""
    size = size_mm * mm
    qr = QrCodeWidget(data)
    x1, y1, x2, y2 = qr.getBounds()
    w = max(1.0, x2 - x1)
    h = max(1.0, y2 - y1)
    d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
    d.add(qr)
    return _svg_clean(renderSVG.drawToString(d))


def _code128_svg_from_text(data: str, width_mm: float = 55.0, height_mm: float = 10.0) -> str:
    data = (data or "").strip()
    if not data:
        return ""
    try:
        d = createBarcodeDrawing(
            "Code128",
            value=data,
            barHeight=height_mm * mm,
            humanReadable=False,
        )
        if d.width and d.width > 0:
            sx = (width_mm * mm) / d.width
            d.scale(sx, 1.0)
        return _svg_clean(renderSVG.drawToString(d))
    except Exception:
        return ""


def _best_qr_payload(report: Any, *, lab_no: str, uhid: str) -> str:
    """
    ✅ Always returns something so QR will never be empty.
    Priority:
    1) url fields
    2) explicit qr payload fields
    3) fallback to LAB_NO / UHID
    """
    url = str(
        _pick(
            report,
            "qr_url",
            "report_url",
            "portal_url",
            "public_url",
            "view_url",
            default="",
        )
        or ""
    ).strip()
    if url:
        return url

    payload = str(
        _pick(
            report,
            "qr_text",
            "qr_payload",
            "qr_data",
            "qr_value",
            "qr_code_value",
            default="",
        )
        or ""
    ).strip()
    if payload:
        return payload

    # fallback (still scannable)
    lab_no = (lab_no or "").strip()
    uhid = (uhid or "").strip()
    if lab_no and uhid:
        return f"UHID:{uhid} | LAB:{lab_no}"
    if lab_no:
        return f"LAB:{lab_no}"
    if uhid:
        return f"UHID:{uhid}"
    return ""


# ============================================================
# WeasyPrint (Optional) UI
# - barcode moved under "Sample Collected At"
# - QR always generated (payload fallback)
# - Lab No moved under "Sample Collected At"
# - Removed header bar line
# - Website moved below email
# ============================================================


def _css() -> str:
    return f"""
    {brand_header_css()}

    :root {{
      --ink:#0f172a;
      --muted:#475569;
      --line:#e2e8f0;
      --soft:#f8fafc;
      --soft2:#f1f5f9;

      --blue:#0B5CAD;
      --blue2:#0A4C92;

      --red:#dc2626;
      --low:#2563eb;

      --radius:10px;
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
      margin: 10mm 12mm 14mm 12mm;
      @top-center {{ content: element(pageHeader); }}
      @bottom-center {{ content: element(pageFooter); }}
    }}

    /* =========================================================
       HEADER
       Left (Logo only) 50%  |  Right (Org + Contacts) 50%
       Top-start aligned
       ========================================================= */
    .hdr {{
      width:100%;
      border-bottom:1px solid var(--line);
      padding: 3.5mm 0 3mm;
    }}

    .hdr-top {{
      display:flex;
      align-items:flex-start;     /* ✅ top */
      justify-content:flex-start;
      gap: 10px;
    }}

    .hdr-left {{
      flex: 0 0 50%;
      max-width: 50%;
      display:flex;
      align-items:flex-start;     /* ✅ top */
      justify-content:flex-start; /* ✅ start */
    }}

    .hdr-right {{
      flex: 0 0 50%;
      max-width: 50%;
      display:flex;
      flex-direction:column;
      align-items:flex-start;     /* ✅ top-start */
      justify-content:flex-start;
      text-align:left;            /* ✅ start */
      color: var(--muted);
      font-size: 9.2px;
      line-height: 1.35;
      white-space: normal;        /* allow address wrap */
      padding-top: 0.3mm;
    }}

    /* ✅ If no logo: hide left and expand right */
    .hdr-top.no-logo .hdr-left {{
      display:none;
    }}
    .hdr-top.no-logo .hdr-right {{
      flex: 1 1 100%;
      max-width: 100%;
    }}

    /* ✅ Bigger logo (left only) */
    .logo {{
      width: 92mm;        /* BIG */
      height: 34mm;       /* BIG */
      display:flex;
      align-items:flex-start;
      justify-content:flex-start;
      overflow:hidden;
      background: transparent;
      border-radius: 0;
    }}
    .logo img {{
      width:100%;
      height:100%;
      object-fit:contain;
      display:block;
    }}

    /* Right side org name */
    .org-name {{
      font-size: 16.5px;
      font-weight: 1000;
      letter-spacing: .2px;
      line-height: 1.05;
      color: var(--ink);
      margin-bottom: 1.1mm;
    }}
    .org-name .accent {{
      color: var(--blue);
    }}

    /* contact lines */
    .contact-line {{
      display:flex;
      justify-content:flex-start;
      gap:6px;
      align-items:center;
      white-space:nowrap;
      margin-top: 0.6mm;
    }}

    .addr-right {{
      margin-top: 1.4mm;
      font-size: 9px;
      color: var(--muted);
      font-weight: 650;
      line-height: 1.25;
      white-space: normal;
      max-width: 95mm;
    }}

    .dot {{
      width: 14px;
      height: 14px;
      border-radius: 999px;
      background: #e6f0ff;
      color: var(--blue2);
      display:inline-flex;
      align-items:center;
      justify-content:center;
      font-weight: 900;
      font-size: 9px;
      flex: 0 0 auto;
    }}

    /* =========================================================
       PATIENT STRIP
       ========================================================= */
    .patient-strip {{
      width:100%;
      display:grid;
      grid-template-columns: 1.05fr .55fr 1.1fr .9fr;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow:hidden;
      margin: 2mm 0 3mm;
    }}
    .ps {{
      padding: 7px 9px;
      background:#fff;
      min-height: 26mm;
    }}
    .ps + .ps {{
      border-left: 1px solid var(--line);
    }}
    .ps .label {{
      color: var(--muted);
      font-weight: 800;
      font-size: 9px;
    }}
    .ps .val {{
      color: var(--ink);
      font-weight: 900;
      font-size: 10px;
    }}
    .pname {{
      font-size: 14px;
      font-weight: 1000;
      margin-bottom: 2mm;
    }}
    .kv {{
      display:flex;
      gap: 8px;
      padding: 2px 0;
      font-size: 10px;
    }}
    .kv .k {{
      width: 18mm;
      color: var(--muted);
      font-weight: 800;
    }}
    .kv .v {{
      color: var(--ink);
      font-weight: 900;
    }}

    .barcode {{
      margin-top: 2mm;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 2mm 2mm;
    }}
    .barcode svg {{
      width: 100%;
      height: 11mm;
      display:block;
    }}
    .barcode .txt {{
      margin-top:1mm;
      font-size:9px;
      font-weight:900;
      color:var(--muted);
      text-align:center;
      letter-spacing:.2px;
    }}

    .qrbox {{
      display:flex;
      align-items:center;
      justify-content:center;
      background: var(--soft);
    }}
    .qrsvg {{
      width: 22mm;
      height: 22mm;
      background:#fff;
      padding: 2mm;
      border-radius: 6px;
      border: 1px solid var(--line);
    }}
    .qrsvg svg {{
      width: 100%;
      height: 100%;
      display:block;
    }}

    /* =========================================================
       TITLE + LOGO WATERMARK ONLY (NO TEXT)
       ========================================================= */
    .test-title {{
      text-align:center;
      font-size: 13px;
      font-weight: 1000;
      letter-spacing: .4px;
      margin: 2mm 0 1mm;
      text-transform: uppercase;
    }}
    .test-rule {{
      border-top: 1px solid var(--line);
      margin: 1mm 0 2mm;
    }}

    .sheet {{
      position:relative;
    }}

    /* ✅ WeasyPrint-friendly watermark repeated per page */
    .watermark {{
      position: fixed;
      left: 50%;
      top: 55%;
      transform: translate(-50%, -50%);
      pointer-events:none;
      user-select:none;
      z-index: 0;
      opacity: .07;
    }}
    .watermark img {{
      width: 120mm;
      max-width: 70vw;
      height: auto;
      object-fit: contain;
      display:block;
    }}

    /* keep content above watermark */
    table, .patient-strip, .test-title, .test-rule, .notes, .sigs {{
      position: relative;
      z-index: 1;
    }}

    /* =========================================================
       TABLE
       ========================================================= */
    table {{
      width:100%;
      border-collapse: collapse;
    }}
    thead th {{
      font-size: 10px;
      text-align:left;
      padding: 6px 6px;
      color: #0b1220;
      border-bottom: 1px solid var(--line);
      font-weight: 1000;
    }}
    tbody td {{
      padding: 6px 6px;
      vertical-align:top;
      border-bottom: 1px solid var(--soft2);
      font-size: 10px;
    }}
    tbody tr:last-child td {{
      border-bottom: 1px solid var(--line);
    }}

    .inv {{
      font-weight: 950;
    }}
    .inv .sub {{
      display:block;
      font-size: 8.8px;
      color: var(--muted);
      font-weight: 750;
      margin-top: 1px;
    }}

    .res {{
      font-weight: 1000;
    }}
    .flag {{
      display:block;
      font-size: 9px;
      font-weight: 1000;
      margin-top: 1px;
    }}
    .flag.high {{ color: var(--red); }}
    .flag.low {{ color: var(--low); }}

    /* =========================================================
       NOTES + SIGNATURES + FOOTER
       ========================================================= */
    .notes {{
      margin-top: 3mm;
      font-size: 9.6px;
      color: #111827;
    }}
    .notes .ntitle {{
      font-weight: 1000;
      margin-bottom: 2mm;
    }}
    .notes ol {{
      margin: 0 0 0 4.5mm;
      padding: 0;
      line-height: 1.35;
    }}

    .sigs {{
      margin-top: 7mm;
      display:grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 10mm;
      font-size: 9px;
      color: var(--muted);
      align-items:end;
    }}
    .sig {{
      text-align:center;
    }}
    .sig .line {{
      border-top: 1px solid var(--line);
      margin: 10mm 0 2mm;
    }}
    .sig .name {{
      color: var(--ink);
      font-weight: 900;
      font-size: 9.6px;
    }}

    footer .foot {{
      width:100%;
      border-top:1px solid var(--line);
      padding-top: 2mm;
      display:flex;
      justify-content:space-between;
      align-items:center;
      color: var(--muted);
      font-size: 9px;
    }}
    .pagenum:before {{ content: "Page " counter(page) " of " counter(pages); }}
    """


def _build_header_html(branding: Any) -> str:
    # ✅ Right side: org_name + phone/email/web + address
    org_name = (getattr(branding, "org_name", None) or "").strip()
    
    addr = (getattr(branding, "org_address", None) or "").strip()
    phone = (getattr(branding, "org_phone", None) or "").strip()
    email = (getattr(branding, "org_email", None) or "").strip()
    web = (getattr(branding, "org_website", None) or "").strip()

    # ✅ Logo: show ONLY if exists or url/data uri
    logo_raw = (getattr(branding, "logo_path", None) or "").strip()
    logo_src = ""
    logo_ok = False

    if logo_raw.startswith(("http://", "https://", "data:")):
        logo_ok = True
        logo_src = logo_raw
    else:
        logo_rel = _norm_rel_path(logo_raw)
        if logo_rel:
            abs_path = Path(settings.STORAGE_DIR).joinpath(logo_rel)
            if abs_path.exists() and abs_path.is_file():
                logo_ok = True
                logo_src = logo_rel  # relative works with base_url=STORAGE_DIR

    # split phone if multiple
    phone2 = ""
    if phone and ("/" in phone or "," in phone or "|" in phone):
        phone2 = phone
        phone = ""

    # org_name accent last word (optional)
    name_html = ""
    if org_name:
        parts = org_name.split()
        if len(parts) >= 2:
            p1 = " ".join(parts[:-1])
            p2 = parts[-1]
            name_html = f"{_h(p1)} <span class='accent'>{_h(p2)}</span>"
        else:
            name_html = f"<span class='accent'>{_h(org_name)}</span>"

    hdr_top_cls = "hdr-top" + ("" if logo_ok else " no-logo")

    # ✅ Left: logo only (big size handled in CSS)
    if logo_ok:
        left_html = f"""
        <div class="hdr-left">
          <div class="logo"><img src="{_h(logo_src)}" alt="logo"/></div>
        </div>
        """.strip()
    else:
        left_html = '<div class="hdr-left"></div>'

    # ✅ Right: org + contacts + address (top-start)
    right_html = f"""
    <div class="hdr-right">
      {"<div class='org-name'>" + name_html + "</div>" if name_html else ""}

      {"<div class='contact-line'><span class='dot'>☎</span><span>" + _h(org_name) + "</span></div>" if org_name else ""}
      {"<div class='contact-line'><span class='dot'>☎</span><span>" + _h(phone) + "</span></div>" if phone else ""}
      {"<div class='contact-line'><span class='dot'>☎</span><span>" + _h(phone2) + "</span></div>" if phone2 else ""}
      {"<div class='contact-line'><span class='dot'>@</span><span>" + _h(email) + "</span></div>" if email else ""}
      {"<div class='contact-line'><span class='dot'>🌐</span><span>" + _h(web) + "</span></div>" if web else ""}

      {"<div class='addr-right'>" + _h(addr) + "</div>" if addr else ""}
    </div>
    """.strip()

    return f"""
    <div class="hdr">
      <div class="{hdr_top_cls}">
        {left_html}
        {right_html}
      </div>
    </div>
    """.strip()



def _build_lab_report_html(
    *,
    branding: Any,
    report: Any,
    patient: Any,
    lab_no: str,
    order_date: Any,
    collected_by_name: Optional[str],
) -> str:
    raw_pname = str(_pick(report, "patient_name", "name", default="-") or "-")
    pname = _h(_name_with_prefix(report, raw_pname))

    age_text = _pick(report, "patient_age_text", "age_text", default="-")
    gender = _pick(report, "patient_gender", "gender", default="-")
    pid = _pick(report, "patient_uhid", "patient_id", "uhid", default="-")
    pid_str = str(pid or "-").strip() or "-"

    # ✅ QR payload (always)
    qr_payload = _best_qr_payload(
        report, lab_no=str(lab_no or ""), uhid=pid_str if pid_str != "-" else ""
    )
    qr_svg = _qr_svg_from_text(qr_payload) if qr_payload else ""

    # ✅ Barcode (UHID)
    barcode_svg = (
        _code128_svg_from_text(pid_str, width_mm=55.0, height_mm=10.0)
        if pid_str and pid_str != "-"
        else ""
    )

    sample_collected_at = _pick(
        report, "sample_collected_at", "collection_site", "collection_location", default=""
    )
    sample_address = _pick(
        report, "sample_collected_address", "collection_address", "patient_address", default=""
    )
    ref_by = _pick(report, "ref_by", "referred_by", "ref_doctor", "doctor_name", default="")

    registered_on = _pick(report, "registered_on", "created_at", default=order_date)
    collected_on = _pick(report, "collected_on", "received_on", "sample_received_on", default=None)
    reported_on = _pick(report, "reported_on", "resulted_on", default=None)

    registered_dt = _fmt_datetime(registered_on)
    collected_dt = _fmt_datetime(collected_on)
    reported_dt = _fmt_datetime(reported_on)

    sections = getattr(report, "sections", None) or []
    if len(sections) == 1:
        sec0 = sections[0]
        sec_title = (_pick(sec0, "panel_name", "department_name", default="Laboratory Report") or "").strip()
        test_title = _h(sec_title.upper()) if sec_title else "LABORATORY REPORT"
    else:
        test_title = "LABORATORY REPORT"

    rows_html = ""
    for sec in sections:
        for r in (getattr(sec, "rows", None) or []):
            inv = _h(_pick(r, "service_name", "test_name", default="-"))
            result_val = _h(_pick(r, "result_value", "value", default="-"))
            unit = _h(_pick(r, "unit", default="-"))
            ref = _h(_pick(r, "normal_range", "reference_range", default="-"))
            flag = str(_pick(r, "flag", "abnormal_flag", default="") or "")
            ftxt = _flag_label(flag)
            fclass = (
                "high"
                if (flag or "").strip().upper() in {"H", "HIGH"}
                else ("low" if (flag or "").strip().upper() in {"L", "LOW"} else "")
            )
            comments = (str(_pick(r, "comments", "comment", default="") or "")).strip()

            res_style = ""
            if fclass == "high":
                res_style = "style='color:var(--red);'"
            elif fclass == "low":
                res_style = "style='color:var(--low);'"

            sub_html = f"<span class='sub'>{_h(comments)}</span>" if comments else ""
            flag_html = f"<span class='flag {fclass}'>{_h(ftxt)}</span>" if ftxt else ""

            rows_html += f"""
            <tr>
              <td><div class="inv">{inv}{sub_html}</div></td>
              <td><div class="res" {res_style}>{result_val}{flag_html}</div></td>
              <td>{ref}</td>
              <td>{unit}</td>
            </tr>
            """.strip()

    if not rows_html:
        rows_html = "<tr><td colspan='4' style='padding:10px;color:#64748b;'>No results.</td></tr>"

    notes_text = str(_pick(report, "notes", "note", "remarks", "interpretation", default="") or "").strip()
    notes_html = ""
    if notes_text:
        lines = [ln.strip() for ln in notes_text.replace("\r\n", "\n").split("\n") if ln.strip()]
        li = "".join(f"<li>{_h(ln)}</li>" for ln in lines)
        notes_html = f"""
        <div class="notes">
          <div class="ntitle">Note :</div>
          <ol>{li}</ol>
        </div>
        """.strip()

    header_html = _build_header_html(branding)
    gen_on = _fmt_datetime(datetime.utcnow())

    # QR cell
    qr_cell = f"<div class='qrsvg'>{qr_svg}</div>" if qr_svg else "<div class='qrsvg'></div>"

    # ✅ Barcode under Sample Collected
    barcode_html = ""
    if barcode_svg:
        barcode_html = f"""
        <div class="barcode">
          {barcode_svg}
          <div class="txt">UHID: {_h(pid_str)}</div>
        </div>
        """.strip()

    # ✅ Lab No under Sample Collected
    labno_html = f"""
      <div style="margin-top:6px;font-size:10px;">
        <span class="label">Lab No:</span> <span class="val">{_h(lab_no)}</span>
      </div>
    """.strip()

        # =========================================================
    # ✅ Watermark = LOGO only (center). If no logo → hidden
    # =========================================================
    logo_raw = (getattr(branding, "logo_path", None) or "").strip()
    logo_src = ""
    logo_ok = False

    if logo_raw.startswith(("http://", "https://", "data:")):
        logo_ok = True
        logo_src = logo_raw
    else:
        logo_rel = _norm_rel_path(logo_raw)
        if logo_rel:
            abs_path = Path(settings.STORAGE_DIR).joinpath(logo_rel)
            if abs_path.exists() and abs_path.is_file():
                logo_ok = True
                logo_src = logo_rel

    watermark_html = ""
    if logo_ok:
        watermark_html = f"""
        <div class="watermark">
          <img src="{_h(logo_src)}" alt="watermark"/>
        </div>
        """.strip()


    return f"""
    <html>
      <head><meta charset="utf-8"/></head>
      <body>
        <header>{header_html}</header>

        <footer>
          <div class="foot">
            <div>Generated on: {_h(gen_on)}</div>
            <div class="pagenum"></div>
          </div>
        </footer>

        <div class="sheet">
          <div class="patient-strip">
            <div class="ps">
              <div class="pname">{pname}</div>
              <div class="kv"><div class="k">Age</div><div class="v">{_h(age_text)}</div></div>
              <div class="kv"><div class="k">Sex</div><div class="v">{_h(gender)}</div></div>
              <div class="kv"><div class="k">UHID</div><div class="v">{_h(pid_str)}</div></div>
            </div>

            <div class="ps qrbox">
              {qr_cell}
            </div>

            <div class="ps">
              <div class="val" style="font-size:10.2px;font-weight:1000;">Sample Collected At:</div>
              {"<div style='margin-top:2px;font-size:10px;font-weight:900;'>" + _h(sample_collected_at) + "</div>" if sample_collected_at else ""}
              {barcode_html}
              {labno_html}
              {"<div style='margin-top:6px;font-size:9.4px;color:var(--muted);font-weight:750;line-height:1.25;'>" + _h(sample_address) + "</div>" if sample_address else ""}
              {"<div style='margin-top:6px;font-size:10px;'><span class='label'>Ref. By:</span> <span class='val'>" + _h(ref_by) + "</span></div>" if ref_by else ""}
              {"<div style='margin-top:2px;font-size:10px;'><span class='label'>Collected By:</span> <span class='val'>" + _h(collected_by_name) + "</span></div>" if collected_by_name else ""}
            </div>

            <div class="ps">
              <div class="kv"><div class="k">Registered</div><div class="v">{_h(registered_dt)}</div></div>
              <div class="kv"><div class="k">Collected</div><div class="v">{_h(collected_dt)}</div></div>
              <div class="kv"><div class="k">Reported</div><div class="v">{_h(reported_dt)}</div></div>
            </div>
          </div>

          <div class="test-title">{test_title}</div>
          <div class="test-rule"></div>

          {watermark_html}

          <table>
            <thead>
              <tr>
                <th style="width:46%;">Investigation</th>
                <th style="width:16%;">Result</th>
                <th style="width:28%;">Reference Value</th>
                <th style="width:10%;">Unit</th>
              </tr>
            </thead>
            <tbody>
              {rows_html}
            </tbody>
          </table>

          {notes_html}

          <div class="sigs">
            <div class="sig">
              <div class="line"></div>
              <div class="name">Medical Lab Technician</div>
              <div>(DMLT, BMLT)</div>
            </div>
            <div class="sig">
              <div class="line"></div>
              <div class="name">Dr. / Pathologist</div>
              <div>(Verified)</div>
            </div>
            <div class="sig">
              <div class="line"></div>
              <div class="name">Authorized Signatory</div>
              <div>(MD)</div>
            </div>
          </div>
        </div>
      </body>
    </html>
    """.strip()


# ============================================================
# ReportLab fallback (Always)
# - barcode moved under "Sample Collected At"
# - QR always generated (payload fallback)
# - Lab No moved under "Sample Collected At"
# ============================================================


def _draw_letterhead_background(c: canvas.Canvas, branding: Any, page_num: int = 1) -> None:
    if not branding or not getattr(branding, "letterhead_path", None):
        return

    position = getattr(branding, "letterhead_position", "background") or "background"
    if position == "none":
        return
    if position == "first_page_only" and page_num != 1:
        return
    if getattr(branding, "letterhead_type", None) not in {"image", None}:
        return

    full_path = Path(settings.STORAGE_DIR).joinpath(getattr(branding, "letterhead_path"))
    if not full_path.exists():
        return

    try:
        img = ImageReader(str(full_path))
        w, h = A4
        c.drawImage(img, 0, 0, width=w, height=h, preserveAspectRatio=True, mask="auto")
    except Exception:
        logger.exception("Failed to draw letterhead background")


def _logo_reader(branding: Any) -> Optional[ImageReader]:
    rel_raw = (getattr(branding, "logo_path", None) or "").strip()
    if not rel_raw:
        return None

    # ReportLab can't load http/data URIs safely here
    if rel_raw.startswith(("http://", "https://", "data:")):
        return None

    rel = _norm_rel_path(rel_raw)
    if not rel:
        return None

    try:
        abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
        if abs_path.exists() and abs_path.is_file():
            return ImageReader(str(abs_path))
    except Exception:
        return None
    return None



def _draw_brand_header_reportlab(
    c: canvas.Canvas,
    branding: Any,
    *,
    page_w: float,
    page_h: float,
    left: float,
    right: float,
    top: float,
) -> float:
    MUTED = colors.HexColor("#475569")
    LINE = colors.HexColor("#E2E8F0")

    addr = (getattr(branding, "org_address", None) or "").strip()
    phone = (getattr(branding, "org_phone", None) or "").strip()
    email = (getattr(branding, "org_email", None) or "").strip()
    web = (getattr(branding, "org_website", None) or "").strip()

    y_top = page_h - top

    # ✅ Big logo box (70mm width)
    LOGO_W = 70 * mm
    LOGO_H = 28 * mm

    # --- LEFT: logo only (if available) ---
    lr = _logo_reader(branding)
    logo_bottom_y = y_top  # if no logo, doesn't affect header height

    if lr:
        try:
            # drawImage uses bottom-left origin
            c.drawImage(
                lr,
                left,
                y_top - LOGO_H,
                width=LOGO_W,
                height=LOGO_H,
                preserveAspectRatio=True,
                mask="auto",
            )
            logo_bottom_y = y_top - LOGO_H
        except Exception:
            lr = None

    # --- RIGHT: phone/email/web/address ---
    xr = page_w - right
    yy = y_top - 2.5 * mm

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.4)

    def rline(text: str, gap: float = 3.8):
        nonlocal yy
        if text:
            c.drawRightString(xr, yy, text)
            yy -= gap * mm

    rline(phone)
    rline(email)
    rline(web)

    # address can wrap
    if addr:
        c.setFont("Helvetica", 7.8)
        addr_lines = _wrap_simple(addr, "Helvetica", 7.8, 95 * mm)[:2]
        for ln in addr_lines:
            c.drawRightString(xr, yy, ln)
            yy -= 3.4 * mm

    right_bottom_y = yy

    # header bottom = lower of (logo bottom) and (right content bottom)
    y_line = min(logo_bottom_y, right_bottom_y) - 2.5 * mm

    c.setStrokeColor(LINE)
    c.setLineWidth(0.9)
    c.line(left, y_line, page_w - right, y_line)

    return y_line - 6.0 * mm



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

    LEFT = 12 * mm
    RIGHT = 12 * mm
    TOP = 8 * mm
    BOTTOM = 14 * mm

    INK = colors.HexColor("#0F172A")
    MUTED = colors.HexColor("#475569")
    LINE = colors.HexColor("#E2E8F0")
    SOFT = colors.HexColor("#F8FAFC")
    SOFT2 = colors.HexColor("#F1F5F9")

    TABLE_W = page_w - LEFT - RIGHT
    W_INV = 86 * mm
    W_RES = 28 * mm
    W_REF = 50 * mm
    W_UNIT = TABLE_W - (W_INV + W_RES + W_REF)

    X_INV = LEFT
    X_RES = X_INV + W_INV
    X_REF = X_RES + W_RES
    X_UNIT = X_REF + W_REF

    PAD = 1.8 * mm

    pname = str(_pick(report, "patient_name", default="-") or "-")
    age_text = str(_pick(report, "patient_age_text", "age_text", default="-") or "-")
    gender = str(_pick(report, "patient_gender", "gender", default="-") or "-")
    pid = str(_pick(report, "patient_uhid", "patient_id", "uhid", default="-") or "-").strip() or "-"

    qr_payload = _best_qr_payload(report, lab_no=str(lab_no or ""), uhid=pid if pid != "-" else "")

    sample_collected_at = str(_pick(report, "sample_collected_at", "collection_site", "collection_location", default="") or "")
    sample_address = str(_pick(report, "sample_collected_address", "collection_address", "patient_address", default="") or "")
    ref_by = str(_pick(report, "ref_by", "referred_by", "ref_doctor", "doctor_name", default="") or "")

    registered_on = _pick(report, "registered_on", "created_at", default=order_date)
    collected_on = _pick(report, "collected_on", "received_on", "sample_received_on", default=None)
    reported_on = _pick(report, "reported_on", "resulted_on", default=None)

    registered_dt = _fmt_datetime(registered_on)
    collected_dt = _fmt_datetime(collected_on)
    reported_dt = _fmt_datetime(reported_on)

    sections = getattr(report, "sections", None) or []
    if len(sections) == 1:
        sec0 = sections[0]
        title_text = (str(_pick(sec0, "panel_name", "department_name", default="Laboratory Report") or "")).strip()
        title_text = title_text.upper() if title_text else "LABORATORY REPORT"
    else:
        title_text = "LABORATORY REPORT"

    def draw_footer(page_no: int) -> None:
        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.line(LEFT, BOTTOM - 5 * mm, page_w - RIGHT, BOTTOM - 5 * mm)
        c.setFont("Helvetica", 7.6)
        c.setFillColor(MUTED)
        c.drawString(LEFT, BOTTOM - 9 * mm, f"Generated on: {_fmt_datetime(datetime.utcnow())}")
        c.drawRightString(page_w - RIGHT, BOTTOM - 9 * mm, f"Page {page_no}")

    def draw_watermark(y_mid: float) -> None:
        # ✅ watermark = logo only (no text). If no logo → skip.
        lr = _logo_reader(branding)
        if not lr:
            return

        try:
            c.saveState()

            # try transparency (not always available depending on reportlab build)
            try:
                c.setFillAlpha(0.08)
                c.setStrokeAlpha(0.08)
            except Exception:
                pass

            # draw centered
            wm_w = 120 * mm
            wm_h = 45 * mm
            x = (page_w - wm_w) / 2
            y = y_mid - (wm_h / 2)

            c.drawImage(
                lr,
                x,
                y,
                width=wm_w,
                height=wm_h,
                preserveAspectRatio=True,
                mask="auto",
            )
            c.restoreState()
        except Exception:
            try:
                c.restoreState()
            except Exception:
                pass


    def draw_patient_strip(y_top: float) -> float:
        strip_h = 28 * mm
        x0 = LEFT
        y0 = y_top - strip_h

        col1 = 64 * mm
        col2 = 26 * mm
        col3 = 58 * mm
        col4 = TABLE_W - (col1 + col2 + col3)

        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.9)
        c.roundRect(x0, y0, TABLE_W, strip_h, 8, stroke=1, fill=1)

        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.line(x0 + col1, y0, x0 + col1, y_top)
        c.line(x0 + col1 + col2, y0, x0 + col1 + col2, y_top)
        c.line(x0 + col1 + col2 + col3, y0, x0 + col1 + col2 + col3, y_top)

        # Patient block (NO barcode here now)
        px = x0 + 3.5 * mm
        py = y_top - 5.2 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12.5)
        c.drawString(px, py, pname)

        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9.0)
        py -= 6.0 * mm
        c.drawString(px, py, f"Age : {age_text}")
        py -= 4.6 * mm
        c.drawString(px, py, f"Sex : {gender}")
        py -= 4.6 * mm
        c.drawString(px, py, f"UHID : {pid}")

        # QR block (✅ always try to generate from payload)
        qx0 = x0 + col1
        qx = qx0 + (col2 / 2)
        qy = y0 + strip_h / 2
        drawn = False

        if qr_payload:
            try:
                size = 20 * mm
                qr = QrCodeWidget(qr_payload)
                x1, y1, x2, y2 = qr.getBounds()
                w = max(1.0, x2 - x1)
                h = max(1.0, y2 - y1)
                d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
                d.add(qr)
                renderPDF.draw(d, c, qx - size / 2, qy - size / 2)
                drawn = True
            except Exception:
                drawn = False

        if not drawn:
            c.setStrokeColor(colors.HexColor("#94A3B8"))
            c.setDash(2, 2)
            s = 20 * mm
            c.roundRect(qx - s / 2, qy - s / 2, s, s, 6, stroke=1, fill=0)
            c.setDash()
            c.setFillColor(colors.HexColor("#64748B"))
            c.setFont("Helvetica-Bold", 8.8)
            c.drawCentredString(qx, qy - 3, "QR")

        # Sample collected block (✅ barcode + lab no moved here)
        sx = x0 + col1 + col2 + 3.5 * mm
        sy = y_top - 6.0 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(sx, sy, "Sample Collected At:")

        sy -= 4.8 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.2)
        if sample_collected_at:
            c.drawString(sx, sy, sample_collected_at[:40])
            sy -= 4.2 * mm
        else:
            sy -= 1.2 * mm

        # ✅ Barcode under Sample Collected At
        if pid and pid != "-":
            try:
                bar_h = 6.2 * mm
                b = createBarcodeDrawing("Code128", value=pid, barHeight=bar_h, humanReadable=False)
                target_w = col3 - 7.0 * mm
                if b.width and b.width > 0:
                    b.scale(target_w / b.width, 1.0)
                y_bar = sy - bar_h - 0.8 * mm
                renderPDF.draw(b, c, sx, y_bar)
                sy = y_bar - 3.2 * mm
            except Exception:
                pass

        # ✅ Lab No under Sample Collected At (after barcode)
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.2)
        c.drawString(sx, sy, "Lab No:")
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.6)
        c.drawString(sx + 12 * mm, sy, str(lab_no)[:20])
        sy -= 4.0 * mm

        if sample_address:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.2)
            addr_lines = _wrap_simple(sample_address, "Helvetica", 8.2, col3 - 7 * mm)[:2]
            for ln in addr_lines:
                c.drawString(sx, sy, ln)
                sy -= 3.6 * mm

        if ref_by:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Bold", 8.2)
            c.drawString(sx, sy, "Ref. By:")
            c.setFillColor(INK)
            c.setFont("Helvetica", 8.6)
            c.drawString(sx + 13 * mm, sy, ref_by[:28])
            sy -= 3.8 * mm

        if collected_by_name:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Bold", 8.2)
            c.drawString(sx, sy, "Collected By:")
            c.setFillColor(INK)
            c.setFont("Helvetica", 8.6)
            c.drawString(sx + 23 * mm, sy, str(collected_by_name)[:24])

        # Times block (✅ only times now)
        tx = x0 + col1 + col2 + col3 + 3.5 * mm
        ty = y_top - 6.0 * mm
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.2)
        c.drawString(tx, ty, "Registered on:")
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.4)
        c.drawString(tx, ty - 3.7 * mm, registered_dt)

        ty -= 8.0 * mm
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.2)
        c.drawString(tx, ty, "Collected on:")
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.4)
        c.drawString(tx, ty - 3.7 * mm, collected_dt)

        ty -= 8.0 * mm
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.2)
        c.drawString(tx, ty, "Reported on:")
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.4)
        c.drawString(tx, ty - 3.7 * mm, reported_dt)

        return y0 - 5.0 * mm

    def draw_table_header(y_top: float) -> float:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.2)
        c.drawString(X_INV + PAD, y_top, "Investigation")
        c.drawString(X_RES + PAD, y_top, "Result")
        c.drawString(X_REF + PAD, y_top, "Reference Value")
        c.drawString(X_UNIT + PAD, y_top, "Unit")

        c.setStrokeColor(LINE)
        c.setLineWidth(0.9)
        c.line(LEFT, y_top - 3.0 * mm, page_w - RIGHT, y_top - 3.0 * mm)
        return y_top - 8.0 * mm

    def draw_result_cell(x: float, y_top: float, value: str, flag: str) -> None:
        col = _result_color(flag)
        lbl = _flag_label(flag)
        c.setFillColor(col)
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(x, y_top, value)
        if lbl:
            c.setFont("Helvetica-Bold", 8.0)
            c.drawString(x, y_top - 4.2 * mm, lbl)

    page_no = 1
    current_y = page_h

    def start_page(page_no: int) -> None:
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
        )

        y = draw_patient_strip(y)

        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12.4)
        c.drawCentredString(page_w / 2, y, title_text)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.8)
        c.line(LEFT, y - 2.8 * mm, page_w - RIGHT, y - 2.8 * mm)
        y -= 9.0 * mm

        draw_watermark(y - 45 * mm)
        current_y = draw_table_header(y)

    def ensure_space(need_h: float) -> None:
        nonlocal page_no, current_y
        if current_y - need_h < (BOTTOM + 10 * mm):
            draw_footer(page_no)
            c.showPage()
            page_no += 1
            start_page(page_no)

    start_page(page_no)

    zebra = 0
    for sec in sections:
        for row in (getattr(sec, "rows", None) or []):
            inv = (str(_pick(row, "service_name", "test_name", default="-") or "-")).strip()
            result = (str(_pick(row, "result_value", "value", default="-") or "-")).strip()
            unit = (str(_pick(row, "unit", default="-") or "-")).strip()
            ref = (str(_pick(row, "normal_range", "reference_range", default="-") or "-")).strip()
            flag = (str(_pick(row, "flag", "abnormal_flag", default="") or "")).strip()
            comments = (str(_pick(row, "comments", "comment", default="") or "")).strip()

            inv_lines = _split_text_to_lines(inv, "Helvetica-Bold", 8.8, W_INV - 2 * PAD)
            cmt_lines = (
                _split_text_to_lines(comments, "Helvetica-Oblique", 8.0, W_INV - 2 * PAD)[:2]
                if comments
                else []
            )
            ref_lines = _split_text_to_lines(ref, "Helvetica", 8.2, W_REF - 2 * PAD)[:5]

            base_lines = len(inv_lines) + (len(cmt_lines) if cmt_lines else 0)
            res_lines = 2 if _flag_label(flag) else 1
            lines_n = max(base_lines, len(ref_lines), res_lines, 1)

            row_h = (lines_n * 4.2 + 4.0) * mm
            ensure_space(row_h + 2 * mm)

            top_y = current_y
            bot_y = top_y - row_h

            if zebra % 2 == 1:
                c.setFillColor(SOFT)
                c.rect(LEFT, bot_y, TABLE_W, row_h, stroke=0, fill=1)

            y_line = top_y - 4.0 * mm

            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 8.8)
            for i, ln in enumerate(inv_lines):
                c.drawString(X_INV + PAD, y_line - (i * 4.2 * mm), ln)

            if cmt_lines:
                cy = y_line - (len(inv_lines) * 4.2 * mm) - 0.3 * mm
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Oblique", 8.0)
                for i, ln in enumerate(cmt_lines):
                    c.drawString(X_INV + PAD, cy - (i * 4.0 * mm), ln)

            draw_result_cell(X_RES + PAD, y_line, result, flag)

            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.2)
            for i, ln in enumerate(ref_lines):
                c.drawString(X_REF + PAD, y_line - (i * 4.2 * mm), ln)

            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8.2)
            c.drawString(X_UNIT + PAD, y_line, unit)

            c.setStrokeColor(SOFT2)
            c.setLineWidth(0.6)
            c.line(LEFT, bot_y, page_w - RIGHT, bot_y)

            current_y = bot_y
            zebra += 1

    notes_text = str(_pick(report, "notes", "note", "remarks", "interpretation", default="") or "").strip()
    if notes_text:
        ensure_space(32 * mm)
        current_y -= 3.0 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.4)
        c.drawString(LEFT, current_y, "Note :")
        current_y -= 5.0 * mm

        c.setFillColor(INK)
        c.setFont("Helvetica", 8.8)
        lines = [ln.strip() for ln in notes_text.replace("\r\n", "\n").split("\n") if ln.strip()]
        for i, ln in enumerate(lines[:8], start=1):
            wrapped = _split_text_to_lines(f"{i}. {ln}", "Helvetica", 8.8, TABLE_W - 4 * mm)
            for wln in wrapped:
                ensure_space(6 * mm)
                c.drawString(LEFT + 2 * mm, current_y, wln)
                current_y -= 4.2 * mm
        current_y -= 2 * mm

    ensure_space(34 * mm)
    current_y -= 4 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)

    sig_y = current_y - 10 * mm
    c.line(LEFT, sig_y, LEFT + 60 * mm, sig_y)
    c.line(LEFT + 65 * mm, sig_y, LEFT + 125 * mm, sig_y)
    c.line(page_w - RIGHT - 60 * mm, sig_y, page_w - RIGHT, sig_y)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.8)
    c.drawString(LEFT, sig_y - 5 * mm, "Medical Lab Technician")
    c.drawString(LEFT + 65 * mm, sig_y - 5 * mm, "Dr. / Pathologist")
    c.drawRightString(page_w - RIGHT, sig_y - 5 * mm, "Authorized Signatory")

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.8)
    c.drawString(LEFT, sig_y - 9 * mm, "(DMLT, BMLT)")
    c.drawString(LEFT + 65 * mm, sig_y - 9 * mm, "(Verified)")
    c.drawRightString(page_w - RIGHT, sig_y - 9 * mm, "(MD)")

    draw_footer(page_no)
    c.save()
    return buf.getvalue()


# ============================================================
# Public API
# ============================================================


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
    ✅ WeasyPrint first (if available)
    ✅ ReportLab fallback always

    Fixes requested:
    - Barcode moved under Sample Collected At
    - QR now always generates (fallback payload)
    - Lab No moved under Sample Collected At
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
        return HTML(string=html, base_url=str(settings.STORAGE_DIR)).write_pdf(
            stylesheets=[CSS(string=_css())]
        )
    except Exception:
        return _build_lab_report_pdf_reportlab(
            branding=branding,
            report=report,
            patient=patient,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
        )
