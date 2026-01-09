# FILE: app/models/charge_item_master.py
from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, Index, UniqueConstraint
from app.db.base import Base


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
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    category = Column(String(20), nullable=False,
                      index=True)  # ADM | DIET | MISC | BLOOD
    code = Column(String(40), nullable=False)
    name = Column(String(255), nullable=False)

    price = Column(Numeric(12, 2), nullable=False, default=0)
    gst_rate = Column(Numeric(5, 2), nullable=False, default=0)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow,
                        nullable=False)
