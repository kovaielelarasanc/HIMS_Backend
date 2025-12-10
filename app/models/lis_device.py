# FILE: app/models/lis_device.py
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    Index,
    Enum as SAEnum,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


# ------------ Python Enums ------------

class DeviceConnectionType(str, PyEnum):
    RS232 = "rs232"
    TCP_IP = "tcp_ip"
    FILE_DROP = "file_drop"
    MANUAL = "manual"


class DeviceProtocolType(str, PyEnum):
    ASTM = "astm"
    HL7 = "hl7"
    CSV = "csv"
    JSON = "json"
    PROPRIETARY = "proprietary"


class DeviceResultStatus(str, PyEnum):
    STAGING = "staging"
    MAPPED = "mapped"
    POSTED = "posted"
    ERROR = "error"


# ------------ LabDevice ------------

class LabDevice(Base):
    __tablename__ = "lab_devices"
    __table_args__ = (
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)

    connection_type = Column(
        SAEnum(DeviceConnectionType),
        nullable=False,
    )
    protocol = Column(
        SAEnum(DeviceProtocolType),
        nullable=False,
    )

    # For securing connector -> backend
    api_key_hash = Column(String(255), nullable=False)

    # Optional reference fields
    location = Column(String(255), nullable=True)
    manufacturer = Column(String(255), nullable=True)
    model = Column(String(255), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    channels = relationship(
        "LabDeviceChannel",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    results = relationship(
        "LabDeviceResult",
        back_populates="device",
        cascade="all, delete-orphan",
    )
    message_logs = relationship(
        "LabDeviceMessageLog",
        back_populates="device",
        cascade="all, delete-orphan",
    )


# ------------ Channel Mapping ------------

class LabDeviceChannel(Base):
    """
    Per-test mapping: external test code on device -> LIS test (lab_tests.id).
    """
    __tablename__ = "lab_device_channels"
    __table_args__ = (
        Index("ix_lab_device_channel_device", "device_id"),
        Index("ix_lab_device_channel_code", "device_id", "external_test_code"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(
        Integer,
        ForeignKey("lab_devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    external_test_code = Column(String(64), nullable=False)
    external_test_name = Column(String(255), nullable=True)

    # LIS mapping -> lab_tests.id (used by LisOrderItem.test_id)
    lis_test_id = Column(Integer, ForeignKey("lab_tests.id"), nullable=True)

    # Defaults used if device does not send these, also for UI
    default_unit = Column(String(64), nullable=True)
    reference_range = Column(String(255), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    device = relationship("LabDevice", back_populates="channels")


# ------------ Staging Results ------------

class LabDeviceResult(Base):
    """
    Staging table for raw analyzer results.
    """
    __tablename__ = "lab_device_results"
    __table_args__ = (
        Index("ix_lab_dev_res_device_time", "device_id", "received_at"),
        Index("ix_lab_dev_res_sample", "sample_id"),
        Index("ix_lab_dev_res_status", "status"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    device_id = Column(
        Integer,
        ForeignKey("lab_devices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    sample_id = Column(String(64), nullable=False, index=True)  # tube barcode
    external_test_code = Column(String(64), nullable=False, index=True)
    external_test_name = Column(String(255), nullable=True)

    result_value = Column(String(255), nullable=False)
    unit = Column(String(64), nullable=True)
    flag = Column(String(16), nullable=True)
    reference_range = Column(String(255), nullable=True)

    measured_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Mapping links to LIS
    lis_order_id = Column(Integer, nullable=True, index=True)
    lis_test_id = Column(Integer, nullable=True, index=True)
    patient_id = Column(Integer, nullable=True, index=True)

    status = Column(
        SAEnum(DeviceResultStatus),
        nullable=False,
        default=DeviceResultStatus.STAGING,
        index=True,
    )

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    device = relationship("LabDevice", back_populates="results")


# ------------ Raw Message Logs ------------

class LabDeviceMessageLog(Base):
    """
    Raw ASTM/HL7 logs per device (for NABL traceability).
    """
    __tablename__ = "lab_device_message_logs"
    __table_args__ = (
        Index("ix_lab_dev_msg_device_time", "device_id", "created_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(
        Integer,
        ForeignKey("lab_devices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    direction = Column(String(8), nullable=False, default="in")  # in | out
    raw_payload = Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    device = relationship("LabDevice", back_populates="message_logs")
