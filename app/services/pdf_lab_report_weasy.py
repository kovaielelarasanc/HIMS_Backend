# FILE: app/services/pdf_lis_report.py
from __future__ import annotations

import os
import logging
import re
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, List, Tuple

from fastapi import Request
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
IST_TZ = ZoneInfo("Asia/Kolkata")  # IST


# ============================================================
# Common helpers
# ============================================================
def _norm_rel_path(p: str) -> str:
    return (p or "").strip().lstrip("/\\")


def _clean_inline(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip())


def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _pick(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        try:
            v = getattr(obj, n, None)
        except Exception:
            v = None
        if v is not None and str(v).strip() != "":
            return v
    return default


def _name_with_prefix(report: Any, fallback_name: str) -> str:
    raw_name = _clean_inline(fallback_name)
    if not raw_name:
        return "-"

    pref = _clean_inline(_pick(report, "prefix", "salutation", "title", "name_prefix", default="") or "")
    if not pref:
        return raw_name

    pref_clean = pref.replace(".", "").strip()
    if not pref_clean:
        return raw_name

    pref_fmt = f"{pref_clean}."
    low_name = raw_name.lower()
    low_pref1 = pref_clean.lower() + " "
    low_pref2 = pref_fmt.lower() + " "
    if low_name.startswith(low_pref1) or low_name.startswith(low_pref2):
        return raw_name

    return f"{pref_fmt} {raw_name}"


def _to_ist(dt: datetime) -> datetime:
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
        return _to_ist(v).strftime("%d %b %Y, %I:%M %p")
    try:
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day).strftime("%d %b %Y")
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if isinstance(dt, datetime):
            return _to_ist(dt).strftime("%d %b %Y, %I:%M %p")
        return str(v)
    except Exception:
        return str(v)


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


def _extract_order_id(report: Any) -> Tuple[Optional[int], str]:
    order_id_val = _pick(report, "order_id", "lis_order_id", "id", default="")
    order_id_int: Optional[int] = None
    order_id_str = ""
    try:
        order_id_int = int(order_id_val)
        order_id_str = str(order_id_int)
        return order_id_int, order_id_str
    except Exception:
        order_id_str = _clean_inline(order_id_val) or ""
        if order_id_str.isdigit():
            try:
                order_id_int = int(order_id_str)
            except Exception:
                order_id_int = None
        return order_id_int, order_id_str


# ============================================================
# QR + Barcode helpers (inline SVG for WeasyPrint)
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


def _to_str_maybe(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", "ignore")
        except Exception:
            return ""
    return str(v)


def _qr_svg_from_text(data: str, size_mm: float = 26.0) -> str:
    """
    FIX: translate QR bounds to (0,0) before scaling.
    Without translation, QR often shifts/crops and looks "left misaligned".
    """
    data = (data or "").strip()
    if not data:
        return ""
    size = size_mm * mm
    qr = QrCodeWidget(data)
    x1, y1, x2, y2 = qr.getBounds()
    w = max(1.0, x2 - x1)
    h = max(1.0, y2 - y1)
    sx = size / w
    sy = size / h
    tx = -x1 * sx
    ty = -y1 * sy
    d = Drawing(size, size, transform=[sx, 0, 0, sy, tx, ty])
    d.add(qr)
    raw = renderSVG.drawToString(d)
    return _svg_clean(_to_str_maybe(raw))


def _code128_svg_from_text(data: str, width_mm: float = 55.0, height_mm: float = 10.0) -> str:
    data = (data or "").strip()
    if not data:
        return ""
    try:
        d = createBarcodeDrawing("Code128", value=data, barHeight=height_mm * mm, humanReadable=False)
        if d.width and d.width > 0:
            sx = (width_mm * mm) / d.width
            d.scale(sx, 1.0)
        raw = renderSVG.drawToString(d)
        return _svg_clean(_to_str_maybe(raw))
    except Exception:
        return ""


def _public_base_url(request: Optional[Request] = None) -> str:
    """
    Returns absolute base URL with scheme.
    Priority:
      1) settings.PUBLIC_BASE_URL
      2) env PUBLIC_BASE_URL
      3) request.base_url (if request given)
    """
    base = ""
    try:
        base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip()
    except Exception:
        base = ""

    if not base:
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip()

    if not base and request is not None:
        try:
            base = str(request.base_url).strip()
        except Exception:
            base = ""

    base = base.rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base.lstrip("/")
    return base


def _lab_report_pdf_url(request: Optional[Request], order_id: int, download: bool = True) -> str:
    base = _public_base_url(request)
    if not base:
        return ""
    url = f"{base}/api/lab/orders/{int(order_id)}/report-pdf"
    if download:
        url = url + "?download=1"
    return url


def _best_qr_payload(
    report: Any,
    *,
    lab_no: str,
    uhid: str,
    order_id: Optional[int] = None,
    pdf_url: Optional[str] = None,
    base_url: str = "",
) -> str:
    """
    QR should ideally be an HTTP(s) URL. blob: URLs are browser-only and won't work when scanned.
    This prefers:
      1) pdf_url (if http(s) or relative -> made absolute using base_url)
      2) report-provided URL fields (also normalized)
      3) report-provided qr_text payload
      4) fallback labels
    """
    def _fix_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        if u.startswith(("http://", "https://")):
            return u
        if u.startswith("/") and base_url:
            return base_url.rstrip("/") + u
        # ignore blob:, file:, data:, etc for QR scanning use-cases
        return ""

    if pdf_url:
        u = _fix_url(str(pdf_url))
        if u:
            return u

    url = str(
        _pick(
            report,
            "download_pdf_url",
            "report_pdf_url",
            "qr_url",
            "report_url",
            "portal_url",
            "public_url",
            "view_url",
            default="",
        )
        or ""
    )
    url = _fix_url(url)
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

    lab_no = (lab_no or "").strip()
    uhid = (uhid or "").strip()
    if uhid and lab_no:
        return f"UHID:{uhid} | LAB:{lab_no}"
    if lab_no:
        return f"LAB:{lab_no}"
    if uhid:
        return f"UHID:{uhid}"
    if order_id is not None:
        return f"ORDER:{int(order_id)}"
    return ""


# ============================================================
# WeasyPrint HTML/CSS (tight header + logo top-left)
# ============================================================
def _css() -> str:
    return f"""
    {brand_header_css()}

    :root {{
      --ink:#0f172a;
      --muted:#475569;
      --line:#000000;
      --soft:#f8fafc;
      --soft2:#f1f5f9;
      --radius:10px;
      --red:#dc2626;
      --low:#2563eb;
    }}

    *{{box-sizing:border-box}}
    body{{
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      color:var(--ink);
      font-size:11px;
      margin:0;
      padding:0;
    }}

    header{{ position: running(pageHeader); margin:0; padding:0; }}
    footer{{ position: running(pageFooter); margin:0; padding:0; }}

    @page {{
      size: A4;
      margin: 6mm 12mm 14mm 12mm;
      @top-center {{
        content: element(pageHeader);
        vertical-align: top;
        padding-top: 0;
      }}
      @bottom-center {{
        content: element(pageFooter);
        vertical-align: bottom;
        padding-bottom: 0;
      }}
    }}

    .hdr, .hdr * {{ margin:0 !important; padding:0 !important; }}
    .hdr {{
      width:100%;
      border-bottom: 1px solid var(--line);
      padding: 0 0 2mm 0 !important;
    }}
    .hdr-top {{
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap: 6mm;
      width:100%;
    }}
    .hdr-left {{
      flex: 0 0 auto;
      width: 70mm;
      display:flex;
      align-items:flex-start;
      justify-content:flex-start;
    }}
    .logo {{
      width: 70mm;
      height: 22mm;
      display:flex;
      align-items:flex-start;
      justify-content:flex-start;
      overflow:hidden;
    }}
    .logo img {{
      width:100%;
      height:100%;
      object-fit: contain;
      object-position: left top;
      display:block;
    }}
    .hdr-right {{
      flex: 1 1 auto;
      text-align:right;
      display:flex;
      flex-direction:column;
      align-items:flex-end;
      justify-content:flex-start;
      line-height: 1.18;
      color: #000;
      padding-top: 0;
    }}
    .org-name {{ font-size: 12.5px; font-weight: 900; line-height: 1.05; }}
    .org-tag {{ font-size: 9px; margin-top: 1mm !important; line-height: 1.15; }}
    .org-meta {{
      font-size: 8px;
      margin-top: 1mm !important;
      line-height: 1.2;
      max-width: 115mm;
      word-break: break-word;
    }}
    .org-meta .addr {{ margin-top: .8mm !important; }}

    .hdr-top.no-logo .hdr-left {{ display:none; }}

    .sheet {{ padding: 0; }}
    .patient-strip {{
      width:100%;
      display:grid;
      grid-template-columns: 1.05fr .55fr 1.1fr .9fr;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      overflow:hidden;
      margin: 2mm 0 3mm;
    }}
    .ps {{
      padding: 7px 9px;
      background:#fff;
      min-height: 26mm;
    }}
    .ps + .ps {{ border-left: 1px solid #e2e8f0; }}

    .ps .label {{ color: #475569; font-weight: 800; font-size: 9px; }}
    .ps .val {{ color: #0f172a; font-weight: 900; font-size: 10px; }}

    .pname {{ font-size: 14px; font-weight: 1000; margin-bottom: 2mm; }}

    .kv {{ display:flex; gap: 8px; padding: 2px 0; font-size: 10px; }}
    .kv .k {{ width: 18mm; color: #475569; font-weight: 800; }}
    .kv .v {{ color: #0f172a; font-weight: 900; }}

    .barcode {{
      margin-top: 2mm;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 2mm 2mm;
    }}
    .barcode svg {{ width: 100%; height: 11mm; display:block; }}
    .barcode .txt {{
      margin-top:1mm;
      font-size:9px;
      font-weight:900;
      color:#475569;
      text-align:center;
      letter-spacing:.2px;
    }}

    .qrbox {{
      display:flex;
      flex-direction:column;
      align-items:center;
      justify-content:center;
      background: #f8fafc;
    }}
    .qrsvg {{
      width: 22mm;
      height: 22mm;
      background:#fff;
      padding: 2mm;
      border-radius: 6px;
      border: 1px solid #e2e8f0;
      display:flex;
      align-items:center;
      justify-content:center;
    }}
    .qrsvg svg {{ width: 100%; height: 100%; display:block; }}

    .inv {{ font-weight: 900; }}
    .sub {{ display:block; font-size: 9px; color: var(--muted); font-style: italic; margin-top: 1px; }}
    .res {{ font-weight: 1000; }}

    .flag {{
      margin-left: 6px;
      font-size: 8px;
      font-weight: 900;
      padding: 1px 6px;
      border-radius: 999px;
      border: 1px solid #e2e8f0;
      background: #fff;
      color: #0f172a;
    }}
    .flag.high {{ color: var(--red); border-color: #fecaca; background: #fff1f2; }}
    .flag.low  {{ color: var(--low); border-color: #bfdbfe; background: #eff6ff; }}

    .test-title {{
      text-align:center;
      font-size: 13px;
      font-weight: 1000;
      letter-spacing: .4px;
      margin: 2mm 0 1mm;
      text-transform: uppercase;
    }}
    .test-rule {{ border-top: 1px solid #e2e8f0; margin: 1mm 0 2mm; }}

    table {{ width:100%; border-collapse: collapse; }}
    thead th {{
      font-size: 10px;
      text-align:left;
      padding: 6px 6px;
      color: #0b1220;
      border-bottom: 1px solid #e2e8f0;
      font-weight: 1000;
    }}
    tbody td {{
      padding: 6px 6px;
      vertical-align:top;
      border-bottom: 1px solid #f1f5f9;
      font-size: 10px;
    }}
    tbody tr:last-child td {{ border-bottom: 1px solid #e2e8f0; }}

    footer .foot {{
      width:100%;
      border-top:1px solid #e2e8f0;
      padding-top: 2mm;
      display:flex;
      justify-content:space-between;
      align-items:center;
      color:#475569;
      font-size: 9px;
    }}
    .pagenum:before {{ content: "Page " counter(page) " of " counter(pages); }}
    """


def _build_header_html(branding: Any) -> str:
    org_name = (getattr(branding, "org_name", None) or "").strip()
    org_tag = (getattr(branding, "org_tagline", None) or "").strip()
    addr = (getattr(branding, "org_address", None) or "").strip()
    phone = (getattr(branding, "org_phone", None) or "").strip()
    email = (getattr(branding, "org_email", None) or "").strip()
    web = (getattr(branding, "org_website", None) or "").strip()

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

    hdr_top_cls = "hdr-top" + ("" if logo_ok else " no-logo")

    left_html = ""
    if logo_ok:
        left_html = f"""
        <div class="hdr-left">
          <div class="logo">
            <img src="{_h(logo_src)}" alt="logo"/>
          </div>
        </div>
        """.strip()

    meta_line = " | ".join([x for x in [
        (f"Ph: {phone}" if phone else ""),
        (email if email else ""),
        (web if web else ""),
    ] if x]).strip()

    right_html = f"""
    <div class="hdr-right">
      {"<div class='org-name'>" + _h(org_name) + "</div>" if org_name else ""}
      {"<div class='org-tag'>" + _h(org_tag) + "</div>" if org_tag else ""}
      <div class="org-meta">
        {"<div>" + _h(meta_line) + "</div>" if meta_line else ""}
        {"<div class='addr'>" + _h(addr) + "</div>" if addr else ""}
      </div>
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
    pdf_url: Optional[str] = None,
    base_url: str = "",
) -> str:
    raw_pname = _clean_inline(_pick(report, "patient_name", "name", default="-") or "-")
    pname = _h(_name_with_prefix(report, raw_pname))

    age_text = _clean_inline(_pick(report, "patient_age_text", "age_text", default="-") or "-")
    gender = _clean_inline(_pick(report, "patient_gender", "gender", default="-") or "-")
    pid = _clean_inline(_pick(report, "patient_uhid", "patient_id", "uhid", default="-") or "-")
    pid_str = pid or "-"

    order_id_int, order_id_str = _extract_order_id(report)

    # Ensure we have a real http(s) pdf_url for QR (NOT blob:)
    if not pdf_url and order_id_int is not None and base_url:
        pdf_url = _lab_report_pdf_url(None, order_id_int, download=True) or ""
        if pdf_url.startswith("/") and base_url:
            pdf_url = base_url.rstrip("/") + pdf_url

    qr_payload = _best_qr_payload(
        report,
        order_id=order_id_int,
        lab_no=_clean_inline(lab_no),
        uhid=pid_str if pid_str != "-" else "",
        pdf_url=pdf_url,
        base_url=base_url,
    )
    qr_svg = _qr_svg_from_text(qr_payload, size_mm=26.0) if qr_payload else ""
    barcode_svg = _code128_svg_from_text(order_id_str, width_mm=55.0, height_mm=10.0) if order_id_str else ""

    sample_collected_at = _clean_inline(
        _pick(report, "sample_collected_at", "collection_site", "collection_location", default="") or ""
    )
    sample_address = (str(_pick(report, "sample_collected_address", "collection_address", "patient_address", default="") or "")).strip()
    ref_by = _clean_inline(_pick(report, "ref_by", "referred_by", "ref_doctor", "doctor_name", default="") or "")

    registered_on = _pick(report, "registered_on", "created_at", default=order_date)
    collected_on = _pick(report, "collected_on", "received_on", "sample_received_on", default=None)
    reported_on = _pick(report, "reported_on", "resulted_on", default=None)

    registered_dt = _fmt_datetime(registered_on)
    collected_dt = _fmt_datetime(collected_on)
    reported_dt = _fmt_datetime(reported_on)

    sections = getattr(report, "sections", None) or []
    if len(sections) == 1:
        sec0 = sections[0]
        sec_title = _clean_inline(_pick(sec0, "panel_name", "department_name", default="Laboratory Report") or "")
        test_title = _h(sec_title.upper()) if sec_title else "LABORATORY REPORT"
    else:
        test_title = "LABORATORY REPORT"

    rows_html = ""
    for sec in sections:
        for r in (getattr(sec, "rows", None) or []):
            inv = _h(_clean_inline(_pick(r, "service_name", "test_name", default="-") or "-"))
            result_val = _h(_clean_inline(_pick(r, "result_value", "value", default="-") or "-"))
            unit = _h(_clean_inline(_pick(r, "unit", default="-") or "-"))
            ref = _h(_clean_inline(_pick(r, "normal_range", "reference_range", default="-") or "-"))
            flag = str(_pick(r, "flag", "abnormal_flag", default="") or "")
            ftxt = _flag_label(flag)
            fclass = "high" if (flag or "").strip().upper() in {"H", "HIGH"} else ("low" if (flag or "").strip().upper() in {"L", "LOW"} else "")
            comments = _clean_inline(_pick(r, "comments", "comment", default="") or "")

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

    qr_cell = (
        f"<div class='qrsvg'>{qr_svg}</div>"
        f"<div style='margin-top:3px;font-size:8px;color:#475569;font-weight:800;text-align:center;'>Scan to download</div>"
        if qr_svg else "<div class='qrsvg'></div>"
    )

    barcode_html = ""
    if barcode_svg:
        barcode_html = f"""
        <div class="barcode">
          {barcode_svg}
          <div class="txt">ORDER ID: {_h(order_id_str)}</div>
        </div>
        """.strip()

    labno_html = f"""
      <div style="margin-top:6px;font-size:10px;">
        <span class="label">Lab No:</span> <span class="val">{_h(_clean_inline(lab_no))}</span>
      </div>
    """.strip()

    sample_address_html = ""
    if sample_address.strip():
        sample_address_html = (
            "<div style='margin-top:6px;font-size:9.4px;color:var(--muted);font-weight:750;line-height:1.25;'>"
            + _h(sample_address.strip())
            + "</div>"
        )

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
              {sample_address_html}
              {"<div style='margin-top:6px;font-size:10px;'><span class='label'>Ref. By:</span> <span class='val'>" + _h(ref_by) + "</span></div>" if ref_by else ""}
              {"<div style='margin-top:2px;font-size:10px;'><span class='label'>Collected By:</span> <span class='val'>" + _h(_clean_inline(collected_by_name)) + "</span></div>" if collected_by_name else ""}
            </div>

            <div class="ps">
              <div class="kv"><div class="k">Registered</div><div class="v">{_h(registered_dt)}</div></div>
              <div class="kv"><div class="k">Collected</div><div class="v">{_h(collected_dt)}</div></div>
              <div class="kv"><div class="k">Reported</div><div class="v">{_h(reported_dt)}</div></div>
            </div>
          </div>

          <div class="test-title">{test_title}</div>
          <div class="test-rule"></div>

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
        </div>
      </body>
    </html>
    """.strip()


# ============================================================
# ReportLab fallback (Always)
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
    rel_raw = _clean_inline(getattr(branding, "logo_path", None) or "")
    if not rel_raw:
        return None
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
    LINE = colors.black
    TEXT = colors.black

    org_name = (getattr(branding, "org_name", None) or "").strip()
    org_tag = (getattr(branding, "org_tagline", None) or "").strip()
    addr = (getattr(branding, "org_address", None) or "").strip()
    phone = (getattr(branding, "org_phone", None) or "").strip()
    email = (getattr(branding, "org_email", None) or "").strip()
    web = (getattr(branding, "org_website", None) or "").strip()

    LOGO_W = 70 * mm
    pad_top = 2 * mm
    pad_bottom = 1 * mm

    y_top = page_h - top
    header_box_h = 28 * mm
    header_bottom = y_top - header_box_h

    c.saveState()
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.line(left, header_bottom, page_w - right, header_bottom)
    c.restoreState()

    lr = _logo_reader(branding)
    if lr:
        try:
            logo_box_h = max(1, header_box_h - (pad_top + pad_bottom))
            c.drawImage(
                lr,
                left,
                y_top - pad_top - logo_box_h,
                width=LOGO_W,
                height=logo_box_h,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            logger.exception("drawImage failed for branding logo")

    gap = 6 * mm
    text_x = left + LOGO_W + gap
    text_w = max(0, (page_w - right) - text_x)

    y = y_top - pad_top
    c.setFillColor(TEXT)

    if org_name:
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(page_w - right, y, org_name)
        y -= 5.2 * mm

    if org_tag:
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - right, y, org_tag)
        y -= 4.6 * mm

    meta = " | ".join([x for x in [
        (f"Ph: {phone}" if phone else ""),
        (email if email else ""),
        (web if web else ""),
    ] if x])

    c.setFont("Helvetica", 8)
    if meta:
        c.drawRightString(page_w - right, y, meta)
        y -= 3.8 * mm

    if addr:
        c.setFont("Helvetica", 8)
        addr_lines = _wrap_simple(addr, "Helvetica", 8, text_w)[:2]
        for ln in addr_lines:
            c.drawRightString(page_w - right, y, ln)
            y -= 3.6 * mm

    return header_bottom - 6 * mm


def _build_lab_report_pdf_reportlab(
    *,
    branding: Any,
    report: Any,
    patient: Any,
    lab_no: str,
    order_date: Any,
    collected_by_name: Optional[str],
    pdf_url: Optional[str] = None,
    request: Optional[Request] = None,
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    # FIX: Match WeasyPrint margins (left was 4mm -> caused "left alignment wrong")
    LEFT = 12 * mm
    RIGHT = 12 * mm
    TOP = 6 * mm
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

    pname = _clean_inline(_pick(report, "patient_name", default="-") or "-")
    age_text = _clean_inline(_pick(report, "patient_age_text", "age_text", default="-") or "-")
    gender = _clean_inline(_pick(report, "patient_gender", "gender", default="-") or "-")
    pid = _clean_inline(_pick(report, "patient_uhid", "patient_id", "uhid", default="-") or "-") or "-"

    order_id_int, order_id_str = _extract_order_id(report)

    base_url = _public_base_url(request)
    if not pdf_url and order_id_int is not None and base_url:
        pdf_url = _lab_report_pdf_url(request, order_id_int, download=True) or ""

    qr_payload = _best_qr_payload(
        report,
        order_id=order_id_int,
        lab_no=_clean_inline(lab_no),
        uhid=pid if pid != "-" else "",
        pdf_url=pdf_url,
        base_url=base_url,
    )

    sample_collected_at = _clean_inline(_pick(report, "sample_collected_at", "collection_site", "collection_location", default="") or "")
    sample_address = str(_pick(report, "sample_collected_address", "collection_address", "patient_address", default="") or "").strip()
    ref_by = _clean_inline(_pick(report, "ref_by", "referred_by", "ref_doctor", "doctor_name", default="") or "")

    registered_on = _pick(report, "registered_on", "created_at", default=order_date)
    collected_on = _pick(report, "collected_on", "received_on", "sample_received_on", default=None)
    reported_on = _pick(report, "reported_on", "resulted_on", default=None)

    registered_dt = _fmt_datetime(registered_on)
    collected_dt = _fmt_datetime(collected_on)
    reported_dt = _fmt_datetime(reported_on)

    sections = getattr(report, "sections", None) or []
    if len(sections) == 1:
        sec0 = sections[0]
        title_text = _clean_inline(_pick(sec0, "panel_name", "department_name", default="Laboratory Report") or "")
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

        px = x0 + 3.5 * mm
        py = y_top - 5.2 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 12.5)
        c.drawString(px, py, pname or "-")

        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9.0)
        py -= 6.0 * mm
        c.drawString(px, py, f"Age : {age_text}")
        py -= 4.6 * mm
        c.drawString(px, py, f"Sex : {gender}")
        py -= 4.6 * mm
        c.drawString(px, py, f"UHID : {pid}")

        # QR (FIX: apply bounds translation + scale)
        qx0 = x0 + col1
        qx = qx0 + (col2 / 2)
        qy = y0 + strip_h / 2
        drawn = False

        if qr_payload:
            try:
                size = 22 * mm
                qr = QrCodeWidget(qr_payload)
                x1, y1, x2, y2 = qr.getBounds()
                w = max(1.0, x2 - x1)
                h = max(1.0, y2 - y1)
                sx = size / w
                sy = size / h
                tx = -x1 * sx
                ty = -y1 * sy
                d = Drawing(size, size, transform=[sx, 0, 0, sy, tx, ty])
                d.add(qr)
                renderPDF.draw(d, c, qx - size / 2, qy - size / 2)
                drawn = True
            except Exception:
                drawn = False

        if not drawn:
            c.setStrokeColor(colors.HexColor("#94A3B8"))
            c.setDash(2, 2)
            s = 22 * mm
            c.roundRect(qx - s / 2, qy - s / 2, s, s, 6, stroke=1, fill=0)
            c.setDash()
            c.setFillColor(colors.HexColor("#64748B"))
            c.setFont("Helvetica-Bold", 8.8)
            c.drawCentredString(qx, qy - 3, "QR")

        # Sample block
        sx0 = x0 + col1 + col2
        sx = sx0 + 3.5 * mm
        sy0 = y_top - 6.0 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.6)
        c.drawString(sx, sy0, "Sample Collected At:")

        sy = sy0 - 4.8 * mm
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.2)
        if sample_collected_at:
            c.drawString(sx, sy, sample_collected_at[:40])
            sy -= 4.2 * mm
        else:
            sy -= 1.2 * mm

        # Barcode order id
        if order_id_str:
            try:
                bar_h = 6.2 * mm
                b = createBarcodeDrawing("Code128", value=order_id_str, barHeight=bar_h, humanReadable=False)
                target_w = col3 - 7.0 * mm
                if b.width and b.width > 0:
                    b.scale(target_w / b.width, 1.0)
                y_bar = sy - bar_h - 0.8 * mm
                renderPDF.draw(b, c, sx, y_bar)
                sy = y_bar - 3.2 * mm
            except Exception:
                pass

        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8.2)
        c.drawString(sx, sy, "Lab No:")
        c.setFillColor(INK)
        c.setFont("Helvetica", 8.6)
        c.drawString(sx + 12 * mm, sy, _clean_inline(lab_no)[:20])
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
            c.drawString(sx + 23 * mm, sy, _clean_inline(collected_by_name)[:24])

        # Times block
        tx0 = x0 + col1 + col2 + col3
        tx = tx0 + 3.5 * mm
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
            inv = _clean_inline(_pick(row, "service_name", "test_name", default="-") or "-")
            result = _clean_inline(_pick(row, "result_value", "value", default="-") or "-")
            unit = _clean_inline(_pick(row, "unit", default="-") or "-")
            ref = _clean_inline(_pick(row, "normal_range", "reference_range", default="-") or "-")
            flag = _clean_inline(_pick(row, "flag", "abnormal_flag", default="") or "")
            comments = _clean_inline(_pick(row, "comments", "comment", default="") or "")

            inv_lines = _split_text_to_lines(inv, "Helvetica-Bold", 8.8, W_INV - 2 * PAD)
            cmt_lines = _split_text_to_lines(comments, "Helvetica-Oblique", 8.0, W_INV - 2 * PAD)[:2] if comments else []
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

    # signatures
    ensure_space(34 * mm)
    current_y -= 4 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)

    sig_y = current_y - 10 * mm
    c.line(LEFT, sig_y, LEFT + 60 * mm, sig_y)
    c.line(page_w - RIGHT - 60 * mm, sig_y, page_w - RIGHT, sig_y)

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.8)
    c.drawString(LEFT, sig_y - 5 * mm, "Medical Lab Technician")
    c.drawRightString(page_w - RIGHT, sig_y - 5 * mm, "Authorized Signatory")

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.8)
    c.drawString(LEFT, sig_y - 9 * mm, "(DMLT, BMLT)")
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
    pdf_url: Optional[str] = None,
    request: Optional[Request] = None,
) -> bytes:
    """
    ✅ WeasyPrint first (if installed + deps)
    ✅ ReportLab fallback always
    ✅ QR uses a real http(s) pdf_url (NOT blob:)
    - If pdf_url not provided, and request/base_url is available, it auto-builds:
        {PUBLIC_BASE_URL}/api/lab/orders/{id}/report-pdf?download=1
    """
    base_url = _public_base_url(request)
    order_id_int, _ = _extract_order_id(report)

    if not pdf_url and order_id_int is not None and base_url:
        pdf_url = _lab_report_pdf_url(request, order_id_int, download=True) or None

    try:
        from weasyprint import HTML, CSS  # type: ignore

        html = _build_lab_report_html(
            branding=branding,
            report=report,
            patient=patient,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
            pdf_url=pdf_url,
            base_url=base_url,
        )
        return HTML(string=html, base_url=str(settings.STORAGE_DIR)).write_pdf(
            stylesheets=[CSS(string=_css())]
        )
    except Exception as e:
        logger.warning("WeasyPrint unavailable, using ReportLab fallback. Reason: %s", e)
        return _build_lab_report_pdf_reportlab(
            branding=branding,
            report=report,
            patient=patient,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
            pdf_url=pdf_url,
            request=request,
        )
