# FILE: app/models/emr_all.py
from __future__ import annotations

from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

# ---------------------------------------------------------
# MySQL-safe sizes (avoid "Specified key was too long" error)
# ---------------------------------------------------------
CODE_32 = 32
NAME_120 = 120

MYSQL_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


# =========================
#  Enums
# =========================
class EmrTemplateStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class EmrRecordStatus(str, Enum):
    DRAFT = "DRAFT"
    SIGNED = "SIGNED"
    VOID = "VOID"


class EmrDraftStage(str, Enum):
    INCOMPLETE = "INCOMPLETE"
    READY = "READY"


class EmrRecordAuditAction(str, Enum):
    CREATE_DRAFT = "CREATE_DRAFT"
    UPDATE_DRAFT = "UPDATE_DRAFT"
    SIGN = "SIGN"
    VOID = "VOID"
    VIEW = "VIEW"


class EmrInboxSource(str, Enum):
    LAB = "LAB"
    RIS = "RIS"
    OTHER = "OTHER"


class EmrInboxStatus(str, Enum):
    NEW = "NEW"
    ACK = "ACK"


class EmrExportStatus(str, Enum):
    DRAFT = "DRAFT"
    GENERATED = "GENERATED"
    RELEASED = "RELEASED"


class EmrExportAuditAction(str, Enum):
    CREATE_BUNDLE = "CREATE_BUNDLE"
    UPDATE_BUNDLE = "UPDATE_BUNDLE"
    GENERATE_PDF = "GENERATE_PDF"
    CREATE_SHARE = "CREATE_SHARE"
    REVOKE_SHARE = "REVOKE_SHARE"


# =========================
#  Master data
# =========================
class EmrDepartment(Base):
    __tablename__ = "emr_departments"
    __table_args__ = (
        UniqueConstraint("code", name="uq_emr_departments_code"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(CODE_32), nullable=False, index=True)
    name = Column(String(NAME_120), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="1")
    display_order = Column(Integer, nullable=False, server_default="1000")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class EmrRecordType(Base):
    __tablename__ = "emr_record_types"
    __table_args__ = (
        UniqueConstraint("code", name="uq_emr_record_types_code"),
        Index("ix_emr_record_types_category", "category"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(CODE_32), nullable=False, index=True)
    label = Column(String(NAME_120), nullable=False)
    category = Column(String(60), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="1")
    display_order = Column(Integer, nullable=False, server_default="1000")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# =========================
#  Section Library
# =========================
# class EmrSectionLibrary(Base):
#     __tablename__ = "emr_section_library"
#     __table_args__ = (
#         UniqueConstraint("code", "dept_code", "record_type_code", name="uq_emr_section_scope"),
#         Index("ix_emr_section_scope_active", "dept_code", "record_type_code", "is_active"),
#         MYSQL_KW,
#     )

#     id = Column(Integer, primary_key=True)
#     code = Column(String(CODE_32), nullable=False, index=True)
#     label = Column(String(140), nullable=False)

#     # scope (optional)
#     dept_code = Column(String(CODE_32), nullable=True, index=True)
#     record_type_code = Column(String(CODE_32), nullable=True, index=True)

#     group = Column(String(50), nullable=True)

#     is_active = Column(Boolean, nullable=False, server_default="1")
#     display_order = Column(Integer, nullable=False, server_default="1000")

#     created_at = Column(DateTime, nullable=False, server_default=func.now())
#     updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# =========================
#  Templates & Versions
# =========================
class EmrTemplate(Base):
    __tablename__ = "emr_templates"
    __table_args__ = (
        UniqueConstraint("dept_code", "record_type_code", "name", name="uq_emr_tpl_scope_name"),
        Index("ix_emr_templates_scope", "dept_code", "record_type_code"),
        Index("ix_emr_templates_status", "status"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)

    dept_code = Column(String(CODE_32), nullable=False, index=True)
    record_type_code = Column(String(CODE_32), nullable=False, index=True)

    name = Column(String(NAME_120), nullable=False)
    description = Column(String(800), nullable=True)

    restricted = Column(Boolean, nullable=False, server_default="0")
    premium = Column(Boolean, nullable=False, server_default="0")
    is_default = Column(Boolean, nullable=False, server_default="0")

    status = Column(SAEnum(EmrTemplateStatus), nullable=False, server_default=EmrTemplateStatus.DRAFT.value)

    active_version_id = Column(Integer, ForeignKey("emr_template_versions.id", ondelete="SET NULL"), nullable=True)
    published_version_id = Column(Integer, ForeignKey("emr_template_versions.id", ondelete="SET NULL"), nullable=True)

    created_by_user_id = Column(Integer, nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    versions = relationship("EmrTemplateVersion", back_populates="template", foreign_keys="EmrTemplateVersion.template_id")


class EmrTemplateVersion(Base):
    __tablename__ = "emr_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version_no", name="uq_emr_tpl_version_no"),
        Index("ix_emr_tpl_versions_tpl", "template_id", "version_no"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("emr_templates.id", ondelete="CASCADE"), nullable=False, index=True)

    version_no = Column(Integer, nullable=False, default=1)
    changelog = Column(String(255), nullable=True)

    # Keep section codes separately for fast lists
    sections_json = Column(Text, nullable=False, default="[]")
    # Full normalized schema
    schema_json = Column(Text, nullable=False, default="{}")

    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    template = relationship("EmrTemplate", back_populates="versions", foreign_keys=[template_id])


# =========================
#  Records
# =========================
class EmrRecord(Base):
    __tablename__ = "emr_records"
    __table_args__ = (
        Index("ix_emr_records_patient", "patient_id", "created_at"),
        Index("ix_emr_records_status_stage", "status", "draft_stage"),
        Index("ix_emr_records_scope", "dept_code", "record_type_code"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)

    patient_id = Column(Integer, nullable=False, index=True)

    encounter_type = Column(String(8), nullable=False)  # OP/IP/ER/OT
    encounter_id = Column(String(64), nullable=False, default="")

    dept_code = Column(String(CODE_32), nullable=False, index=True)
    record_type_code = Column(String(CODE_32), nullable=False, index=True)

    template_id = Column(Integer, ForeignKey("emr_templates.id", ondelete="SET NULL"), nullable=True)
    template_version_id = Column(Integer, ForeignKey("emr_template_versions.id", ondelete="SET NULL"), nullable=True)

    title = Column(String(255), nullable=False)
    note = Column(String(1200), nullable=True)

    confidential = Column(Boolean, nullable=False, server_default="0")

    content_json = Column(Text, nullable=False, default="{}")

    status = Column(SAEnum(EmrRecordStatus), nullable=False, server_default=EmrRecordStatus.DRAFT.value)
    draft_stage = Column(SAEnum(EmrDraftStage), nullable=False, server_default=EmrDraftStage.INCOMPLETE.value)

    # signature / void
    signed_by_user_id = Column(Integer, nullable=True)
    signed_at = Column(DateTime, nullable=True)

    voided_by_user_id = Column(Integer, nullable=True)
    voided_at = Column(DateTime, nullable=True)
    void_reason = Column(String(500), nullable=True)

    created_by_user_id = Column(Integer, nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class EmrRecordAuditLog(Base):
    __tablename__ = "emr_record_audit_logs"
    __table_args__ = (
        Index("ix_emr_audit_record", "record_id", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer, ForeignKey("emr_records.id", ondelete="CASCADE"), nullable=False, index=True)

    action = Column(SAEnum(EmrRecordAuditAction), nullable=False)
    user_id = Column(Integer, nullable=True)

    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)
    meta_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# =========================
#  Recent & Pinned
# =========================
class EmrPinnedPatient(Base):
    __tablename__ = "emr_pinned_patients"
    __table_args__ = (
        UniqueConstraint("user_id", "patient_id", name="uq_emr_pinned_patient"),
        Index("ix_emr_pinned_patient_user", "user_id", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    patient_id = Column(Integer, nullable=False, index=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmrPinnedRecord(Base):
    __tablename__ = "emr_pinned_records"
    __table_args__ = (
        UniqueConstraint("user_id", "record_id", name="uq_emr_pinned_record"),
        Index("ix_emr_pinned_record_user", "user_id", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    record_id = Column(Integer, ForeignKey("emr_records.id", ondelete="CASCADE"), nullable=False, index=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmrRecentView(Base):
    __tablename__ = "emr_recent_views"
    __table_args__ = (
        UniqueConstraint("user_id", "patient_id", "record_id", name="uq_emr_recent_view_key"),
        Index("ix_emr_recent_user", "user_id", "last_seen_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    patient_id = Column(Integer, nullable=False, index=True)
    record_id = Column(Integer, nullable=False, server_default="0", index=True)

    last_seen_at = Column(DateTime, nullable=False, server_default=func.now())


# =========================
#  Inbox
# =========================
class EmrInboxItem(Base):
    __tablename__ = "emr_inbox_items"
    __table_args__ = (
        Index("ix_emr_inbox_bucket", "source", "status", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)

    source = Column(SAEnum(EmrInboxSource), nullable=False)
    status = Column(SAEnum(EmrInboxStatus), nullable=False, server_default=EmrInboxStatus.NEW.value)

    patient_id = Column(Integer, nullable=False, index=True)

    encounter_type = Column(String(8), nullable=True)
    encounter_id = Column(String(64), nullable=True)

    title = Column(String(255), nullable=False)

    source_ref_type = Column(String(64), nullable=True)
    source_ref_id = Column(String(64), nullable=True)

    payload_json = Column(Text, nullable=True)

    acknowledged_by_user_id = Column(Integer, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# =========================
#  Exports / Sharing
# =========================
class EmrExportBundle(Base):
    __tablename__ = "emr_export_bundles"
    __table_args__ = (
        Index("ix_emr_export_patient", "patient_id", "created_at"),
        Index("ix_emr_export_status", "status"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)

    patient_id = Column(Integer, nullable=False, index=True)

    encounter_type = Column(String(8), nullable=True)
    encounter_id = Column(String(64), nullable=True)

    title = Column(String(255), nullable=False)
    filters_json = Column(Text, nullable=False, default="{}")

    watermark_text = Column(String(255), nullable=True)

    status = Column(SAEnum(EmrExportStatus), nullable=False, server_default=EmrExportStatus.DRAFT.value)

    pdf_file_key = Column(String(512), nullable=True)
    generated_at = Column(DateTime, nullable=True)

    created_by_user_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class EmrShareLink(Base):
    __tablename__ = "emr_share_links"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_emr_share_token_hash"),
        Index("ix_emr_share_bundle", "bundle_id", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    bundle_id = Column(Integer, ForeignKey("emr_export_bundles.id", ondelete="CASCADE"), nullable=False, index=True)

    token_hash = Column(String(64), nullable=False, index=True)

    expires_at = Column(DateTime, nullable=True)
    max_downloads = Column(Integer, nullable=True)
    download_count = Column(Integer, nullable=False, server_default="0")

    revoked_at = Column(DateTime, nullable=True)

    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmrExportAuditLog(Base):
    __tablename__ = "emr_export_audit_logs"
    __table_args__ = (
        Index("ix_emr_export_audit_bundle", "bundle_id", "created_at"),
        MYSQL_KW,
    )

    id = Column(Integer, primary_key=True)
    bundle_id = Column(Integer, ForeignKey("emr_export_bundles.id", ondelete="CASCADE"), nullable=False, index=True)

    action = Column(SAEnum(EmrExportAuditAction), nullable=False)
    user_id = Column(Integer, nullable=True)

    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)
    meta_json = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
