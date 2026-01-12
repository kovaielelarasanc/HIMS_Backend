from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class UiBranding(Base):
    """
    Global UI + PDF customization for NABH HIMS.
    Singleton-ish: always use first row.
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
    logo_path = Column(String(255), nullable=True)
    login_logo_path = Column(String(255), nullable=True)
    favicon_path = Column(String(255), nullable=True)

    # --- UI Colors ---
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

    letterhead_path = Column(String(255), nullable=True)
    letterhead_type = Column(String(50), nullable=True)  # pdf / image / doc / docx
    letterhead_position = Column(String(50), default="background")  # background / none

    pdf_header_height_mm = Column(Integer, nullable=True)
    pdf_footer_height_mm = Column(Integer, nullable=True)
    pdf_show_page_number = Column(Boolean, default=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = relationship("User")


class UiBrandingContext(Base):
    """
    Context-specific overrides (e.g., pharmacy).
    Only filled fields override the global UiBranding in /ui-branding/public?context=...
    """

    __tablename__ = "ui_branding_contexts"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ui_branding_contexts_code"),
        Index("ix_ui_branding_contexts_code", "code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), nullable=False)  # e.g. "pharmacy"

    # Optional org override fields
    org_name = Column(String(255), nullable=True)
    org_tagline = Column(String(255), nullable=True)
    org_address = Column(String(512), nullable=True)
    org_phone = Column(String(64), nullable=True)
    org_email = Column(String(255), nullable=True)
    org_website = Column(String(255), nullable=True)
    org_gstin = Column(String(32), nullable=True)

    # Pharmacy legal extras (optional)
    license_no = Column(String(64), nullable=True)
    license_no2 = Column(String(64), nullable=True)
    pharmacist_name = Column(String(255), nullable=True)
    pharmacist_reg_no = Column(String(64), nullable=True)

    # Assets (optional overrides)
    logo_path = Column(String(255), nullable=True)
    pdf_header_path = Column(String(255), nullable=True)
    pdf_footer_path = Column(String(255), nullable=True)

    letterhead_path = Column(String(255), nullable=True)
    letterhead_type = Column(String(50), nullable=True)
    letterhead_position = Column(String(50), default="background")

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = relationship("User")
