from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text,
    Enum, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base  # ✅ adjust if your Base path differs


class IntegrationDevice(Base):
    __tablename__ = "integration_devices"
    __table_args__ = (
        UniqueConstraint("protocol", "sending_facility_code", name="uq_intdev_protocol_facility"),
        Index("ix_intdev_enabled_protocol", "enabled", "protocol"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    # tenant hospital code (your multi-tenant key)
    tenant_code = Column(String(32), nullable=False, index=True)

    name = Column(String(120), nullable=False, default="")

    # HL7_MLLP | ASTM_HTTP | RAW_HTTP
    protocol = Column(String(20), nullable=False, index=True)

    # HL7: use MSH-4 (Sending Facility) to identify device/tenant
    sending_facility_code = Column(String(64), nullable=False, default="", index=True)

    enabled = Column(Boolean, nullable=False, default=True)

    # optional IP allow list: ["10.0.0.10", "10.0.0.11"]
    allowed_remote_ips = Column(JSON, nullable=True)

    # operational fields
    last_seen_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship("IntegrationMessage", back_populates="device")


class IntegrationMessage(Base):
    __tablename__ = "integration_messages"
    __table_args__ = (
        UniqueConstraint("device_id", "message_control_id", name="uq_intmsg_device_msgctl"),
        Index("ix_intmsg_status_received", "parse_status", "received_at"),
        Index("ix_intmsg_tenant_received", "tenant_code", "received_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    tenant_code = Column(String(32), nullable=False, index=True)

    device_id = Column(Integer, ForeignKey("integration_devices.id", ondelete="RESTRICT"), nullable=True, index=True)
    device = relationship("IntegrationDevice", back_populates="messages")

    protocol = Column(String(20), nullable=False, index=True)  # HL7_MLLP | ASTM_HTTP | RAW_HTTP
    direction = Column(String(6), nullable=False, default="IN")  # IN only for now

    received_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    remote_ip = Column(String(64), nullable=True)

    message_type = Column(String(32), nullable=True)          # HL7: MSH-9
    message_control_id = Column(String(64), nullable=True)    # HL7: MSH-10
    facility_code = Column(String(64), nullable=True)         # HL7: MSH-4 (or from device)

    parse_status = Column(String(16), nullable=False, default="RECEIVED")
    # RECEIVED | PARSED | PROCESSED | ERROR | DUPLICATE

    error_reason = Column(Text, nullable=True)

    raw_payload = Column(Text, nullable=False)
    parsed_json = Column(JSON, nullable=True)  # store parsed summary safely (no huge blobs)


class LabCodeMapping(Base):
    __tablename__ = "lab_code_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_code", "source_device_id", "external_code", name="uq_labmap_device_code"),
        Index("ix_labmap_tenant_code", "tenant_code", "external_code"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    tenant_code = Column(String(32), nullable=False, index=True)

    source_device_id = Column(Integer, ForeignKey("integration_devices.id", ondelete="CASCADE"), nullable=False, index=True)

    external_code = Column(String(80), nullable=False, index=True)  # OBX-3 or ASTM test code
    internal_test_id = Column(Integer, nullable=False, index=True)  # your HMIS test master id

    active = Column(Boolean, nullable=False, default=True)

    updated_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class LabInboundResult(Base):
    __tablename__ = "lab_inbound_results"
    __table_args__ = (
        Index("ix_inbound_tenant_patient", "tenant_code", "patient_identifier"),
        Index("ix_inbound_tenant_barcode", "tenant_code", "specimen_barcode"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    tenant_code = Column(String(32), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("integration_messages.id", ondelete="CASCADE"), nullable=False, index=True)

    patient_identifier = Column(String(80), nullable=True)     # UHID/MRN
    encounter_identifier = Column(String(80), nullable=True)   # encounter/visit id if present
    specimen_barcode = Column(String(80), nullable=True)       # sample id / barcode

    report_status = Column(String(20), nullable=False, default="RECEIVED")  # RECEIVED|FINAL|CORRECTED etc
    observed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class LabInboundResultItem(Base):
    __tablename__ = "lab_inbound_result_items"
    __table_args__ = (
        Index("ix_inbound_item_result", "result_id"),
        Index("ix_inbound_item_external", "external_code"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    result_id = Column(Integer, ForeignKey("lab_inbound_results.id", ondelete="CASCADE"), nullable=False, index=True)

    external_code = Column(String(80), nullable=True)
    internal_test_id = Column(Integer, nullable=True)

    value_text = Column(String(255), nullable=True)
    units = Column(String(40), nullable=True)
    ref_range = Column(String(80), nullable=True)
    abnormal_flag = Column(String(10), nullable=True)

    status = Column(String(10), nullable=True)  # F/P/C etc
    observed_at = Column(DateTime, nullable=True)
