from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Numeric,
    Index,
    UniqueConstraint,
    Enum as SAEnum,
)
from app.db.base import Base
from app.models.billing import ServiceGroup
MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}
class ChargeItemCategory(str, Enum):
    ADM = "ADM"
    DIET = "DIET"
    MISC = "MISC"
    BLOOD = "BLOOD"


class ChargeItemMaster(Base):
    __tablename__ = "charge_item_masters"
    __table_args__ = (
        UniqueConstraint("category",
                         "code",
                         name="uq_charge_item_category_code"),
        Index("ix_charge_item_category_active", "category", "is_active"),
        Index("ix_charge_item_misc_headers", "module_header",
              "service_header"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    # ADM | DIET | MISC | BLOOD
    category = Column(String(20), nullable=False, index=True)

    code = Column(String(40), nullable=False)
    name = Column(String(255), nullable=False)

    # Only meaningful when category = MISC
    # Suggested values like: OPD / IPD / OT / LAB / RIS / PHARM / ROOM / ER / MISC
    module_header = Column(String(16), nullable=True, index=True)

    # Suggested values align with Billing.ServiceGroup:
    # CONSULT / LAB / RAD / PHARM / OT / PROC / ROOM / NURSING / MISC
    service_header = Column(String(16), nullable=True, index=True)

    price = Column(Numeric(12, 2), nullable=False, default=0)
    gst_rate = Column(Numeric(5, 2), nullable=False, default=0)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)


class ChargeItemModuleHeader(Base):
    __tablename__ = "charge_item_module_headers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_charge_item_module_header_code"),
        Index("ix_charge_item_module_header_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(16), nullable=False,
                  index=True)  # OPD / IPD / OT / ...
    name = Column(String(64), nullable=True)  # Display label
    is_active = Column(Boolean, default=True, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)


class ChargeItemServiceHeader(Base):
    __tablename__ = "charge_item_service_headers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_charge_item_service_header_code"),
        Index("ix_charge_item_service_header_active", "is_active"),
        MYSQL_ARGS,
    )

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(16), nullable=False,
                  index=True)  # CONSULT / DENTAL / IMPLANT / ...
    name = Column(String(64), nullable=True)  # Display label

    # ✅ IMPORTANT: custom service headers must map to existing billing grouping
    service_group = Column(SAEnum(ServiceGroup),
                           nullable=False,
                           default=ServiceGroup.MISC)

    is_active = Column(Boolean, default=True, nullable=False)
    is_system = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)
