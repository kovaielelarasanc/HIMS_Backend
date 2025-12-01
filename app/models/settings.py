from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.db.base import Base


class UiBranding(Base):
    __tablename__ = "ui_branding"

    id = Column(Integer, primary_key=True, index=True)

    # Branding (logo + colors)
    logo_path = Column(String(255), nullable=True)

    primary_color = Column(String(32),
                           nullable=True)  # main accent (buttons, highlights)
    sidebar_bg_color = Column(String(32), nullable=True)  # sidebar background
    content_bg_color = Column(String(32),
                              nullable=True)  # main content background
    text_color = Column(String(32), nullable=True)  # default text color
    icon_color = Column(String(32), nullable=True)  # icon stroke/fill
    icon_bg_color = Column(String(32), nullable=True)  # icon chip background

    # Global PDF header/footer used for *all* PDFs
    pdf_header_path = Column(String(255), nullable=True)
    pdf_footer_path = Column(String(255), nullable=True)

    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = relationship("User")
