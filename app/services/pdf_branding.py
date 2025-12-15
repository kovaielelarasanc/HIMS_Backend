from __future__ import annotations

import base64
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Optional, Any

from app.core.config import settings
from app.models.ui_branding import UiBranding


def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _logo_data_uri(branding: UiBranding,
                   *,
                   max_px: int = 100) -> Optional[str]:
    """
    Read branding.logo_path and return data-uri.
    ✅ Tries to downscale to max_px x max_px (no upscaling) if Pillow is available.
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

    raw = None
    try:
        raw = abs_path.read_bytes()
    except Exception:
        return None

    # Optional: downscale to reduce PDF weight (and guarantee max size)
    try:
        from PIL import Image  # type: ignore

        im = Image.open(BytesIO(raw))
        im.load()
        # Don't upscale:
        im.thumbnail((max_px, max_px))

        out = BytesIO()
        # Save as PNG for predictable rendering
        im.save(out, format="PNG", optimize=True)
        raw = out.getvalue()
        mime = "image/png"
    except Exception:
        # Pillow missing or failed -> use original bytes (CSS will still constrain)
        pass

    enc = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{enc}"


def brand_header_css() -> str:
    """
    ✅ Two-column header.
    - Left: Logo (max 100x100, never upscales)
    - Right: Right-anchored info box (so it ends nicely at the right margin)
    """
    return """
    .brand-header {
      width: 100%;
      padding-bottom: 10px;
      margin-bottom: 14px;
      border-bottom: 1px solid #e5e7eb;
    }

    /* Stable PDF layout */
    .brand-row {
      display: table;
      width: 100%;
      table-layout: fixed;
    }
    .brand-left {
      display: table-cell;
      width: 112px;        /* 100px logo + breathing room */
      vertical-align: top;
    }
    .brand-right {
      display: table-cell;
      vertical-align: top;
      text-align: right;   /* ✅ anchor the box to the right edge */
      padding-left: 12px;
    }

    .brand-logo-wrap {
      width: 100px;
      height: 100px;
      display: flex;
      align-items: flex-start;
      justify-content: flex-start;
      overflow: hidden;
    }
    .brand-logo {
      max-width: 100px;
      max-height: 100px;
      width: auto;     /* ✅ no upscaling */
      height: auto;    /* ✅ no upscaling */
      object-fit: contain;
      display: block;
    }
    .brand-logo-placeholder {
      font-size: 10px;
      color: #94a3b8;
      letter-spacing: 0.6px;
      border: 1px dashed #cbd5e1;
      padding: 6px 10px;
      border-radius: 999px;
      display: inline-block;
    }

    /* This box sits at the right edge but keeps text readable */
    .brand-box {
      display: inline-block;     /* ✅ lets text-align:right anchor it */
      text-align: left;          /* ✅ text inside stays left-aligned */
      max-width: 420px;          /* prevent super-wide address lines */
    }

    .brand-name {
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.2px;
      margin: 0;
      color: #0f172a;
      line-height: 1.1;
    }
    .brand-tagline {
      margin-top: 3px;
      font-size: 11px;
      color: #64748b;
      line-height: 1.25;
    }

    .brand-meta {
      margin-top: 8px;
      font-size: 10.5px;
      color: #0f172a;
      line-height: 1.35;
    }
    .brand-muted { color: #64748b; }
    .brand-meta-line { margin-top: 2px; }
    """


def render_brand_header_html(branding: UiBranding) -> str:
    """
    Left: logo_path
    Right: org_name, tagline, address, phone, email, website, GSTIN (only if present)
    """
    logo_src = _logo_data_uri(branding, max_px=100)

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
    if website:
        contact_bits.append(
            f"<span><span class='brand-muted'>Website:</span> {website}</span>"
        )

    if contact_bits:
        meta_lines.append("<div class='brand-meta-line'>" +
                          " &nbsp; • &nbsp; ".join(contact_bits) + "</div>")

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
