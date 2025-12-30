# FILE: app/models/pdf_template.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


class PdfTemplate(Base):
    """
    Main Feature Checklist for PDFs (per module + document code).
    Stores enabled sections + ordering + settings.
    """
    __tablename__ = "pdf_templates"
    __table_args__ = (
        UniqueConstraint("module", "code", name="uq_pdf_templates_module_code"),
        Index("ix_pdf_templates_module_active", "module", "is_active"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True)

    # e.g. ipd/opd/ot/lab/radiology/pharmacy/billing
    module = Column(String(30), nullable=False, index=True)

    # e.g. case_sheet, discharge_summary, drug_chart, invoice
    code = Column(String(60), nullable=False, index=True)

    name = Column(String(120), nullable=False, default="Default Template")

    # sections: [{code, label, enabled, order, required}]
    sections = Column(JSON, nullable=False, default=list)

    # settings: {show_empty_sections, max_rows_per_section, watermark_text, ...}
    settings = Column(JSON, nullable=False, default=dict)

    is_active = Column(Boolean, default=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = relationship("User")
