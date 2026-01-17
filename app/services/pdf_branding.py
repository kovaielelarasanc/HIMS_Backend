#app/service/pdf_branding.py
from __future__ import annotations

import base64
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Optional, Any

from app.core.config import settings
from app.models.ui_branding import UiBranding

# --- ADD BELOW AT END OF: app/services/pdf_branding.py ---

from io import BytesIO
from pathlib import Path
from typing import Optional, Any, Tuple

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _logo_data_uri(branding: UiBranding,
                   *,
                   max_px: int = 320) -> Optional[str]:
    """
    Read branding.logo_path and return data-uri.
    ✅ Downscale to max_px x max_px (no upscaling) if Pillow is available.
    """
    rel = (branding.logo_path or "").strip()
    if not rel:
        return None

    abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
    if not abs_path.exists() or not abs_path.is_file():
        return None

    mime, _ = mimetypes.guess_type(str(abs_path))
    if not mime:
        mime = "image/png"

    try:
        raw = abs_path.read_bytes()
    except Exception:
        return None

    # Optional: downscale to reduce PDF weight (and improve consistent quality)
    try:
        from PIL import Image  # type: ignore

        im = Image.open(BytesIO(raw))
        im.load()

        # Don't upscale (thumbnail only shrinks)
        im.thumbnail((max_px, max_px))

        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
        mime = "image/png"
    except Exception:
        pass

    enc = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{enc}"


def brand_header_css() -> str:
    """
    ✅ Bigger, header-fitted logo.
    - Logo renders at fixed height (looks “medium” and consistent)
    - Still safe for PDF layout (table cells)
    """
    return """
    .brand-header{
      --logo-col: 270px;   /* left column width */
      --logo-w: 240px;     /* max logo width */
      --logo-h: 72px;      /* logo display height (increase this if you want bigger) */

      width:100%;
      padding-bottom: 16px;
      margin-bottom: 10px;
      border-bottom: 1px solid #e5e7eb;
    }

    .brand-row{
      display: table;
      width: 100%;
      table-layout: fixed;
      padding-bottom: 10px;
    }
    .brand-left{
      display: table-cell;
      width: var(--logo-col);
      vertical-align: middle;
    }
    .brand-right{
      display: table-cell;
      vertical-align: top;
      text-align: right;
      padding-left: 12px;
    }

    .brand-logo-wrap{
      width: var(--logo-w);
      height: calc(var(--logo-h) + 8px);
      display: flex;
      align-items: center;        /* ✅ center vertically */
      justify-content: flex-start;
      overflow: hidden;
    }
    .brand-logo{
      height: var(--logo-h);      /* ✅ this makes it visibly bigger */
      width: auto;
      max-width: var(--logo-w);
      object-fit: contain;
      display: block;
    }

    .brand-logo-placeholder{
      font-size: 10px;
      color: #94a3b8;
      letter-spacing: 0.6px;
      border: 1px dashed #cbd5e1;
      padding: 6px 10px;
      border-radius: 999px;
      display: inline-block;
    }

    .brand-box{
      display: inline-block;
      text-align: left;
      max-width: 420px;
    }

    .brand-name{
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.2px;
      margin: 0;
      color: #0f172a;
      line-height: 1.1;
    }
    .brand-tagline{
      margin-top: 3px;
      font-size: 11px;
      color: #64748b;
      line-height: 1.25;
    }

    .brand-meta{
      margin-top: 8px;
      font-size: 10.5px;
      color: #0f172a;
      line-height: 1.35;
    }
    .brand-muted{ color: #64748b; }
    .brand-meta-line{ margin-top: 2px; }
    """


def render_brand_header_html(branding: UiBranding) -> str:
    """
    Left: logo_path
    Right: org_name, tagline, address, phone, email, website, GSTIN (only if present)
    """
    # ✅ embed a higher-res logo so it looks crisp at bigger header size
    logo_src = _logo_data_uri(branding, max_px=320)

    org_name = _h(branding.org_name or "")
    org_tagline = _h(branding.org_tagline or "")

    addr = _h(branding.org_address or "")
    phone = _h(branding.org_phone or "")
    email = _h(branding.org_email or "")
    website = _h(branding.org_website or "")
    gstin = _h(branding.org_gstin or "")

    meta_lines: list[str] = []

    if addr:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>Address:</span> {addr}</div>"
        )

    contact_bits: list[str] = []
    if phone:
        contact_bits.append(
            f"<span><span class='brand-muted'>Phone:</span> {phone}</span>")
    if email:
        contact_bits.append(
            f"<span><span class='brand-muted'>Email:</span> {email}</span>")

    if contact_bits:
        meta_lines.append("<div class='brand-meta-line'>" +
                          " &nbsp; | &nbsp; ".join(contact_bits) + "</div>")
    if website:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>Website:</span> {website}</div>"
        )

    if gstin:
        meta_lines.append(
            f"<div class='brand-meta-line'><span class='brand-muted'>GSTIN:</span> {gstin}</div>"
        )

    meta_html = f"<div class='brand-meta'>{''.join(meta_lines)}</div>" if meta_lines else ""

    if logo_src:
        logo_html = f"<img class='brand-logo' src='{logo_src}' alt='Logo' />"
    else:
        logo_html = "<div class='brand-logo-placeholder'>LOGO</div>"

    return f"""
    <div class="brand-header">
      <div class="brand-row">
        <div class="brand-left">
          <div class="brand-logo-wrap">{logo_html}</div>
        </div>
        <div class="brand-right">
          <div class="brand-box">
            <p class="brand-name">{org_name}</p>
            {f"<div class='brand-tagline'>{org_tagline}</div>" if org_tagline else ""}
            {meta_html}
          </div>
        </div>
      </div>
    </div>
    """.strip()


def resolve_asset_path(path_str: Optional[str]) -> Optional[Path]:
    """
    ✅ resolves both absolute and relative paths.
    ✅ tries STORAGE_DIR first (your usual saved logo paths).
    """
    if not path_str:
        return None
    p = Path(str(path_str))

    if p.is_absolute() and p.exists():
        return p

    bases = []
    storage = getattr(settings, "STORAGE_DIR", None)
    if storage:
        bases.append(Path(str(storage)))

    for attr in ("MEDIA_ROOT", "UPLOAD_DIR", "STATIC_DIR", "BASE_DIR"):
        base = getattr(settings, attr, None)
        if base:
            bases.append(Path(str(base)))

    bases.append(Path("."))

    for b in bases:
        pp = (b / p).resolve()
        if pp.exists():
            return pp
    return None


def _logo_reader_reportlab(
        branding: Any,
        *,
        max_px: int = 320) -> Optional[Tuple[ImageReader, int, int]]:
    """
    ✅ same idea as _logo_data_uri (but for ReportLab):
    - read logo bytes
    - optionally downscale with Pillow (if available)
    - return ImageReader(BytesIO(png_bytes))
    """
    rel = (getattr(branding, "logo_path", None) or "").strip()
    if not rel:
        return None

    p = resolve_asset_path(rel)
    if not p or not p.exists() or not p.is_file():
        return None

    try:
        raw = p.read_bytes()
    except Exception:
        return None

    # Optional downscale (no upscaling) – improves crispness and reduces file size
    try:
        from PIL import Image  # type: ignore
        im = Image.open(BytesIO(raw))
        im.load()
        im.thumbnail((max_px, max_px))
        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
    except Exception:
        pass

    try:
        ir = ImageReader(BytesIO(raw))
        iw, ih = ir.getSize()
        return ir, int(iw or 0), int(ih or 0)
    except Exception:
        return None


def _resolve_brand_asset(path_str: Optional[str]) -> Optional[Path]:
    """
    Resolve asset paths for branding:
    - absolute paths
    - relative to STORAGE_DIR (your normal saved logo paths)
    - other common dirs
    """
    if not path_str:
        return None
    p = Path(str(path_str))

    if p.is_absolute() and p.exists():
        return p

    bases: list[Path] = []
    storage = getattr(settings, "STORAGE_DIR", None)
    if storage:
        bases.append(Path(str(storage)))

    for attr in ("MEDIA_ROOT", "UPLOAD_DIR", "STATIC_DIR", "BASE_DIR"):
        base = getattr(settings, attr, None)
        if base:
            bases.append(Path(str(base)))

    bases.append(Path("."))

    for b in bases:
        pp = (b / p).resolve()
        if pp.exists():
            return pp
    return None


def _load_logo_image_reader(
    branding: UiBranding,
    *,
    max_px: int = 320,
) -> Optional[Tuple[ImageReader, int, int]]:
    """
    Like your HTML helper:
    - read logo file
    - optionally downscale with Pillow (thumbnail) => crisp + lighter PDF
    """
    rel = (branding.logo_path or "").strip()
    if not rel:
        return None

    abs_path = _resolve_brand_asset(rel)
    if not abs_path or not abs_path.exists() or not abs_path.is_file():
        return None

    try:
        raw = abs_path.read_bytes()
    except Exception:
        return None

    try:
        from PIL import Image  # type: ignore

        im = Image.open(BytesIO(raw))
        im.load()
        im.thumbnail((max_px, max_px))
        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
    except Exception:
        pass

    try:
        ir = ImageReader(BytesIO(raw))
        iw, ih = ir.getSize()
        return ir, int(iw or 0), int(ih or 0)
    except Exception:
        return None





def _resolve_storage_path(rel_or_abs: str) -> Optional[Path]:
    if not rel_or_abs:
        return None
    p = Path(str(rel_or_abs))
    if p.is_absolute() and p.exists():
        return p

    # ✅ your common setup (logo stored inside STORAGE_DIR)
    base = Path(str(getattr(settings, "STORAGE_DIR", ".")))
    pp = (base / p).resolve()
    if pp.exists():
        return pp

    # fallback
    if p.exists():
        return p.resolve()
    return None


def _read_logo_imagereader(
        branding: UiBranding,
        *,
        max_px: int = 640) -> Optional[tuple[ImageReader, int, int]]:
    rel = (branding.logo_path or "").strip()
    if not rel:
        return None

    fp = _resolve_storage_path(rel)
    if not fp or not fp.exists() or not fp.is_file():
        return None

    try:
        raw = fp.read_bytes()
    except Exception:
        return None

    # ✅ optional downscale like your HTML logic (crisp + smaller PDF)
    try:
        from PIL import Image  # type: ignore
        im = Image.open(BytesIO(raw))
        im.load()
        im.thumbnail((max_px, max_px))
        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
    except Exception:
        pass

    try:
        ir = ImageReader(BytesIO(raw))
        iw, ih = ir.getSize()
        return ir, int(iw or 0), int(ih or 0)
    except Exception:
        return None


def draw_brand_header_reportlab(
        c,
        branding: Optional[UiBranding],
        x: float,
        top_y: float,
        w: float,
        *,
        logo_h_mm: float = 26.0,  # ✅ bigger logo
        logo_max_w_mm: float = 72.0,  # ✅ allow wide logo like your sample
        logo_col_mm: float = 76.0,  # left column width
) -> float:
    """
    Premium header like your HTML:
    - Left logo (bigger)
    - Right org block aligned to right edge (drawRightString)
    - Contact combines phone+email
    - Soft divider line
    Returns: bottom-y of header
    """
    # colors like your CSS
    C_TEXT = colors.HexColor("#0f172a")
    C_MUTED = colors.HexColor("#64748b")
    C_DIV = colors.HexColor("#e5e7eb")

    # header height (auto, but allow override)
    logo_h = logo_h_mm * mm
    logo_max_w = logo_max_w_mm * mm
    logo_col_w = logo_col_mm * mm
    gap = 6 * mm

    header_h = max(36 * mm, logo_h + 10 * mm)
    if branding and getattr(branding, "pdf_header_height_mm", None):
        try:
            header_h = max(header_h, float(branding.pdf_header_height_mm) * mm)
        except Exception:
            pass

    y0 = top_y
    y1 = top_y - header_h

    # optional background header image (keep support)
    if branding and getattr(branding, "pdf_header_path", None):
        try:
            fp = _resolve_storage_path(str(branding.pdf_header_path))
            if fp:
                img = ImageReader(str(fp))
                c.drawImage(img,
                            x,
                            y1,
                            width=w,
                            height=header_h,
                            preserveAspectRatio=True,
                            anchor="sw",
                            mask="auto")
        except Exception:
            pass

    # ----------------
    # LEFT: Logo (start)
    # ----------------
    center_y = y0 - header_h / 2.0
    logo_y = center_y - (logo_h / 2.0)
    logo_x = x

    drawn_logo_w = 0.0
    if branding:
        lr = _read_logo_imagereader(branding, max_px=640)
        if lr:
            ir, iw, ih = lr
            if iw > 0 and ih > 0:
                ratio = iw / float(ih)
                draw_h = logo_h
                draw_w = min(logo_max_w, draw_h * ratio)
            else:
                draw_h = logo_h
                draw_w = min(logo_max_w, 60 * mm)

            try:
                c.drawImage(ir,
                            logo_x,
                            logo_y,
                            width=draw_w,
                            height=draw_h,
                            preserveAspectRatio=True,
                            mask="auto")
                drawn_logo_w = draw_w
            except Exception:
                drawn_logo_w = 0.0

    # placeholder if logo missing
    if drawn_logo_w <= 0.0:
        c.setStrokeColor(C_MUTED)
        c.setDash(3, 3)
        c.roundRect(logo_x, logo_y + 3, 36 * mm, 12 * mm, 10, stroke=1, fill=0)
        c.setDash()
        c.setFillColor(C_MUTED)
        c.setFont("Helvetica", 8)
        c.drawString(logo_x + 10, logo_y + 7, "LOGO")

    # ----------------
    # RIGHT: Org details (RIGHT aligned to end)
    # ----------------
    xr = x + w  # right edge
    text_left_limit = x + max(logo_col_w, drawn_logo_w) + gap
    text_w = max(40 * mm, xr - text_left_limit)
    box_w = min(140 * mm, text_w)  # similar to HTML max-width 420px-ish

    org_name = (getattr(branding, "org_name", "") if branding else "") or ""
    org_tagline = (getattr(branding, "org_tagline", "")
                   if branding else "") or ""
    org_addr = (getattr(branding, "org_address", "") if branding else "") or ""
    org_phone = (getattr(branding, "org_phone", "") if branding else "") or ""
    org_email = (getattr(branding, "org_email", "") if branding else "") or ""
    org_web = (getattr(branding, "org_website", "") if branding else "") or ""
    org_gstin = (getattr(branding, "org_gstin", "") if branding else "") or ""

    def _wrap_lines(txt: str, font: str, size: float, max_w: float,
                    max_lines: int) -> list[str]:
        txt = (txt or "").strip()
        if not txt:
            return []
        return (simpleSplit(txt, font, size, max_w) or [])[:max_lines]

    y = y0 - 5 * mm

    # org name
    if org_name.strip():
        c.setFillColor(C_TEXT)
        c.setFont("Helvetica-Bold", 13)  # slightly bigger
        for ln in _wrap_lines(org_name, "Helvetica-Bold", 13, box_w, 2):
            c.drawRightString(xr, y, ln)
            y -= 14

    # tagline
    if org_tagline.strip():
        c.setFillColor(C_MUTED)
        c.setFont("Helvetica", 9)
        for ln in _wrap_lines(org_tagline, "Helvetica", 9, box_w, 2):
            c.drawRightString(xr, y, ln)
            y -= 11

    # meta lines (Address + Contact combined)
    meta_lines: list[str] = []
    if org_addr.strip():
        meta_lines.append(f"Address: {org_addr.strip()}")

    contact_bits: list[str] = []
    if org_phone.strip():
        contact_bits.append(f"Ph: {org_phone.strip()}")
    if org_email.strip():
        contact_bits.append(f"Email: {org_email.strip()}")
    if contact_bits:
        meta_lines.append("Contact: " + " | ".join(contact_bits))

    if org_web.strip():
        meta_lines.append(f"Website: {org_web.strip()}")
    if org_gstin.strip():
        meta_lines.append(f"GSTIN: {org_gstin.strip()}")

    if meta_lines:
        y -= 2
        c.setFont("Helvetica", 8.6)
        for ml in meta_lines:
            c.setFillColor(C_MUTED)
            for ln in _wrap_lines(ml, "Helvetica", 8.6, box_w, 2):
                c.drawRightString(xr, y, ln)
                y -= 10

    # divider (soft)
    c.setStrokeColor(C_DIV)
    c.setLineWidth(0.9)
    c.line(x, y1 + 1.2 * mm, x + w, y1 + 1.2 * mm)

    return y1
