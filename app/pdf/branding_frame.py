# app/pdf/branding_frame.py
from pathlib import Path
from typing import Dict

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.ui_branding import get_ui_branding


def draw_branding_header_footer(c: canvas.Canvas,
                                db: Session) -> Dict[str, float]:
    """
    Draw global header/footer (mandatory for NABH).
    Returns a dict with top/bottom Y coordinates you should respect
    when writing your own content.
    """
    branding = get_ui_branding(db)
    width, height = A4

    header_h = footer_h = 0

    if branding:
        header_h_mm = branding.pdf_header_height_mm or 25
        footer_h_mm = branding.pdf_footer_height_mm or 20
        header_h = header_h_mm * mm
        footer_h = footer_h_mm * mm

        # HEADER IMAGE
        if branding.pdf_header_path:
            header_path = Path(settings.STORAGE_DIR).joinpath(
                branding.pdf_header_path)
            if header_path.exists():
                img = ImageReader(str(header_path))
                c.drawImage(
                    img,
                    x=15 * mm,
                    y=height - header_h - 10 * mm,
                    width=width - 30 * mm,
                    height=header_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )

        # FOOTER IMAGE
        if branding.pdf_footer_path:
            footer_path = Path(settings.STORAGE_DIR).joinpath(
                branding.pdf_footer_path)
            if footer_path.exists():
                img = ImageReader(str(footer_path))
                c.drawImage(
                    img,
                    x=15 * mm,
                    y=10 * mm,
                    width=width - 30 * mm,
                    height=footer_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )

        # Optional page number: you can use this pattern in your own code:
        # if branding.pdf_show_page_number:
        #   c.setFont("Helvetica", 8)
        #   text = f"Page {page_no} of {total_pages}"
        #   c.drawRightString(width - 20 * mm, 15 * mm, text)

    content_top = height - (header_h + 25 * mm)
    content_bottom = footer_h + 25 * mm

    return {
        "content_top_y": content_top,
        "content_bottom_y": content_bottom,
    }
