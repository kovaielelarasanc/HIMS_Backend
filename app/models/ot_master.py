# app/models/ot_master.py
from __future__ import annotations
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Numeric,
    Boolean,
    DateTime,
    UniqueConstraint,
    Index,
    ForeignKey,
)

from app.db.base import Base

MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class OtSurgeryMaster(Base):
    """
    OT Master — standardized surgeries mapped to codes & default pricing.
    (Legacy/Optional: can be used as Surgery package master)
    """
    __tablename__ = "ot_surgery_masters"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ot_surgery_code"),
        UniqueConstraint("name", name="uq_ot_surgery_name"),
        Index("ix_ot_surgery_active", "active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)

    # Existing package / base cost
    default_cost = Column(Numeric(12, 2), nullable=False, default=0)

    # NEW: OT hourly cost (₹ / hour) – for billing purpose
    hourly_cost = Column(Numeric(12, 2), nullable=False, default=0)

    description = Column(String(500), default="")
    active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)


# ------------------------------------------------------------
# NEW: OT THEATER MASTER (hour based)
# ------------------------------------------------------------
class OtTheaterMaster(Base):
    __tablename__ = "ot_theater_masters"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ot_theater_code"),
        UniqueConstraint("name", name="uq_ot_theater_name"),
        Index("ix_ot_theater_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)
    cost_per_hour = Column(Numeric(12, 2), nullable=False, default=0)
    description = Column(String(500), default="")
    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)


# ------------------------------------------------------------
# NEW: OT INSTRUMENT MASTER (tracking)
# ------------------------------------------------------------
class OtInstrumentMaster(Base):
    __tablename__ = "ot_instrument_masters"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ot_instrument_code"),
        UniqueConstraint("name", name="uq_ot_instrument_name"),
        Index("ix_ot_instrument_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)

    available_qty = Column(Integer, nullable=False, default=0)
    cost_per_qty = Column(Numeric(12, 2), nullable=False, default=0)
    uom = Column(String(30), nullable=False, default="Nos")  # Nos/Sets/etc.

    description = Column(String(500), default="")
    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)


# ------------------------------------------------------------
# NEW: AIRWAY + MONITOR DEVICES MASTER (single table)
# ------------------------------------------------------------
class OtDeviceMaster(Base):
    """
    category: AIRWAY / MONITOR
    """
    __tablename__ = "ot_device_masters"
    __table_args__ = (
        UniqueConstraint("category", "code",
                         name="uq_ot_device_category_code"),
        UniqueConstraint("category", "name",
                         name="uq_ot_device_category_name"),
        Index("ix_ot_device_active", "is_active"),
        Index("ix_ot_device_category", "category"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(20), nullable=False)  # AIRWAY | MONITOR
    code = Column(String(40), nullable=False)
    name = Column(String(200), nullable=False)
    cost = Column(Numeric(12, 2), nullable=False, default=0)
    description = Column(String(500), default="")
    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)
