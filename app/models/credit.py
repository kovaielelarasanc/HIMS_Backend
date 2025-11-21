# backend/app/models/credit.py
from sqlalchemy import Column, Integer, String, Boolean
from app.db.base import Base

COMMON_TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class CreditProvider(Base):
    """
    Master for Insurance / Corporate / Govt scheme providers.
    Example: Star Health, LIC, XYZ Corporate.
    """
    __tablename__ = "credit_providers"
    __table_args__ = COMMON_TABLE_ARGS

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    display_name = Column(String(191), nullable=True)
    code = Column(String(64), nullable=True, unique=True)
    type = Column(String(32),
                  nullable=True)  # insurance / corporate / govt / other
    is_active = Column(Boolean, default=True)


class Tpa(Base):
    """
    TPA master (Third Party Administrator).
    """
    __tablename__ = "tpas"
    __table_args__ = COMMON_TABLE_ARGS

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    display_name = Column(String(191), nullable=True)
    code = Column(String(64), nullable=True, unique=True)
    provider_name = Column(String(120), nullable=True)  # for info only
    is_active = Column(Boolean, default=True)


class CreditPlan(Base):
    """
    Specific plans: "Star Health Gold Plan", "XYZ Corporate Employee Plan", etc.
    """
    __tablename__ = "credit_plans"
    __table_args__ = COMMON_TABLE_ARGS

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    display_name = Column(String(191), nullable=True)
    code = Column(String(64), nullable=True, unique=True)

    provider_name = Column(String(120),
                           nullable=True)  # optional; plain text linkage
    tpa_name = Column(String(120), nullable=True)

    is_active = Column(Boolean, default=True)
