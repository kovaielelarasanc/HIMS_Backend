# FILE: app/models/emr_template_library.py
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db.base import Base

CODE_32 = 32

MYSQL_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class EmrTemplateBlock(Base):
    """
    Reusable block library to be inserted into templates (e.g., VITALS, ALLERGIES).
    schema_json stores a JSON object (as TEXT) describing fields.
    tags_json stores an array (as TEXT) for quick filtering.
    """
    __tablename__ = "emr_template_blocks"
    __table_args__ = (
        UniqueConstraint("code", name="uq_emr_template_blocks_code"),
        Index("ix_emr_blocks_scope_active", "dept_code", "record_type_code", "is_active"),
        Index("ix_emr_blocks_category", "category"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(CODE_32), nullable=False, index=True)

    label = Column(String(140), nullable=False)
    description = Column(String(800), nullable=True)

    # scope (optional)
    dept_code = Column(String(CODE_32), nullable=True, index=True)
    record_type_code = Column(String(CODE_32), nullable=True, index=True)

    category = Column(String(50), nullable=True)
    tags_json = Column(Text, nullable=False, default="[]")

    schema_json = Column(Text, nullable=False, default="{}")
    preview_json = Column(Text, nullable=True)

    is_active = Column(Boolean, nullable=False, server_default="1")
    is_system = Column(Boolean, nullable=False, server_default="1")
    display_order = Column(Integer, nullable=False, server_default="1000")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
