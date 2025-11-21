# FILE: app/models/payer.py
from sqlalchemy import Column, Integer, String, Boolean, Text, ForeignKey
from sqlalchemy.orm import relationship

from app.db.base import Base


class Payer(Base):
    """
    Generic payer master:
    - Insurance company
    - Corporate / employer
    - Govt scheme
    """
    __tablename__ = "payers"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(191), unique=True, nullable=False)
    payer_type = Column(
        String(32), nullable=False)  # 'insurance', 'corporate', 'govt', etc.

    contact_person = Column(String(120), nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(191), nullable=True)
    address = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True)

    tpas = relationship("Tpa", back_populates="payer")
    credit_plans = relationship("CreditPlan", back_populates="payer")


class Tpa(Base):
    """
    TPA master – linked to a Payer (usually insurance).
    """
    __tablename__ = "tpas"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(191), unique=True, nullable=False)

    payer_id = Column(Integer,
                      ForeignKey("payers.id"),
                      nullable=True,
                      index=True)
    contact_person = Column(String(120), nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(191), nullable=True)

    is_active = Column(Boolean, default=True)

    payer = relationship("Payer", back_populates="tpas")
    credit_plans = relationship("CreditPlan", back_populates="tpa")


class CreditPlan(Base):
    """
    Credit / Insurance plan master.
    Examples:
      - "Star Health – Silver Plan"
      - "Corporate ABC – Employee Health Scheme"
    """
    __tablename__ = "credit_plans"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(191), unique=True, nullable=False)

    payer_id = Column(Integer,
                      ForeignKey("payers.id"),
                      nullable=True,
                      index=True)
    tpa_id = Column(Integer, ForeignKey("tpas.id"), nullable=True, index=True)

    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    payer = relationship("Payer", back_populates="credit_plans")
    tpa = relationship("Tpa", back_populates="credit_plans")


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
