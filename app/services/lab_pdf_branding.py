# FILE: app/services/pdf_branding.py
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception:
            v = None
        if v not in (None, "", " "):
            return v
    return default


def _resolve_storage_path(rel: str | None) -> Optional[Path]:
    rel = (rel or "").strip()
    if not rel:
        return None
    p = Path(settings.STORAGE_DIR).joinpath(rel)
    if p.exists() and p.is_file():
        return p
    return None


def _ellipsize(text: str, font: str, size: float, max_w: float) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if stringWidth(s, font, size) <= max_w:
        return s
    # add … until fits
    base = s
    while base and stringWidth(base + "…", font, size) > max_w:
        base = base[:-1]
    return (base + "…") if base else s[:1] + "…"


def draw_clean_brand_header(
    c,
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
    Draw clean header like your screenshot:
      logo (optional) + brand name (blue) + address lines.
    Returns y (content start) after the header.
    """
    primary = colors.HexColor(
        _get(branding, "primary_color", "brand_primary", default="#2563EB")
    )

    brand_name = _get(
        branding,
        "hospital_name",
        "org_name",
        "brand_name",
        "name",
        default="LAB",
    )
    addr1 = _get(branding, "address_line1", "address1", "address", default=None)
    addr2 = _get(branding, "address_line2", "address2", default=None)
    phone = _get(branding, "phone", "phone_no", "mobile", default=None)
    email = _get(branding, "email", default=None)

    logo_path = _resolve_storage_path(_get(branding, "logo_path", "pdf_logo_path", default=""))

    y = page_h - top

    # logo block
    logo_w = 18 * mm
    logo_h = 18 * mm
    text_x = left

    if logo_path:
        try:
            img = ImageReader(str(logo_path))
            c.drawImage(
                img,
                left,
                y - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )
            text_x = left + logo_w + 6 * mm
        except Exception:
            logger.exception("PDF header logo draw failed")

    max_text_w = (page_w - right) - text_x

    # Brand name
    c.setFillColor(primary)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(text_x, y - 4 * mm, _ellipsize(str(brand_name), "Helvetica-Bold", 12, max_text_w))

    # Address + contacts
    c.setFillColor(colors.HexColor("#334155"))
    c.setFont("Helvetica", 8.6)

    lines: list[str] = []
    if addr1:
        lines.append(str(addr1))
    if addr2:
        lines.append(str(addr2))

    contact_bits: list[str] = []
    if phone:
        contact_bits.append(str(phone))
    if email:
        contact_bits.append(str(email))
    if contact_bits:
        lines.append(" | ".join(contact_bits))

    ty = y - 9 * mm
    for ln in lines[:3]:
        c.drawString(text_x, ty, _ellipsize(ln, "Helvetica", 8.6, max_text_w))
        ty -= 3.8 * mm

    # compute bottom of header block
    header_bottom = min(y - logo_h, ty + 3.8 * mm) - 6 * mm

    if show_rule:
        c.setStrokeColor(colors.HexColor("#E2E8F0"))
        c.setLineWidth(0.7)
        c.line(left, header_bottom, page_w - right, header_bottom)
        header_bottom -= 7 * mm

    return header_bottom
