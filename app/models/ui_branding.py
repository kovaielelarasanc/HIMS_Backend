# app/models/ui_branding.py
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class UiBranding(Base):
    """
    Global UI + PDF customization for NABH HIMS.

    This is SINGLETON-ish: we always work with the first row.
    """

    __tablename__ = "ui_branding"

    id = Column(Integer, primary_key=True, index=True)

    # --- Organisation details (used in header / footer / topbar) ---
    org_name = Column(String(255), nullable=True)
    org_tagline = Column(String(255), nullable=True)
    org_address = Column(String(512), nullable=True)
    org_phone = Column(String(64), nullable=True)
    org_email = Column(String(255), nullable=True)
    org_website = Column(String(255), nullable=True)
    org_gstin = Column(String(32), nullable=True)

    # --- Logos / icons ---
    # main application logo (sidebar/topbar)
    logo_path = Column(String(255), nullable=True)
    # optional login-page variant (white background etc.)
    login_logo_path = Column(String(255), nullable=True)
    # favicon (for browser tab, future use)
    favicon_path = Column(String(255), nullable=True)

    # --- UI Colors (frontend uses subset of these) ---
    primary_color = Column(String(32), nullable=True)
    primary_color_dark = Column(String(32), nullable=True)

    sidebar_bg_color = Column(String(32), nullable=True)
    content_bg_color = Column(String(32), nullable=True)
    card_bg_color = Column(String(32), nullable=True)
    border_color = Column(String(32), nullable=True)

    text_color = Column(String(32), nullable=True)
    text_muted_color = Column(String(32), nullable=True)

    icon_color = Column(String(32), nullable=True)
    icon_bg_color = Column(String(32), nullable=True)

    # --- PDF: header/footer artwork + behaviour ---
    pdf_header_path = Column(String(255), nullable=True)
    pdf_footer_path = Column(String(255), nullable=True)

    # space reserved for header/footer (millimetres).
    # frontends never need this, only PDF code.
    pdf_header_height_mm = Column(Integer, nullable=True)
    pdf_footer_height_mm = Column(Integer, nullable=True)

    # show "Page X of Y" in footer
    pdf_show_page_number = Column(Boolean, default=True)

    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = relationship("User")
