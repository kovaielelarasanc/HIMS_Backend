# app/models_master/tenant.py
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    JSON,
    Numeric,  # 👈 NEW
)

from app.db.base_master import MasterBase


class Tenant(MasterBase):
    """
    Central tenant registry.
    Each row = one hospital / clinic with its own DB.
    """
    __tablename__ = "tenants"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String(50), unique=True, nullable=False,
                  index=True)  # e.g. KGH001
    name = Column(String(255), nullable=False)  # Hospital / Clinic name

    contact_person = Column(String(255), nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(50), nullable=True)

    # Tenant DB info
    db_name = Column(String(191), unique=True, nullable=False)
    db_uri = Column(String(512), nullable=False)

    # Subscription / License (master data – controlled by NDH admin)
    subscription_plan = Column(String(100),
                               nullable=True)  # basic / standard / premium

    # exact registration timestamp (UTC) when tenant is provisioned
    license_start_date = Column(DateTime, nullable=True)

    # computed based on plan (basic: 30d, standard: 6m, premium: 1y)
    license_end_date = Column(DateTime, nullable=True)

    # commercial
    subscription_amount = Column(Numeric(12, 2),
                                 nullable=True)  # ₹, set by admin only
    amc_percent = Column(Integer, nullable=True)  # 30–40, set by admin only

    # next AMC due (license_end_date + 10 days)
    amc_next_due = Column(DateTime, nullable=True)

    # Status
    is_active = Column(Boolean, default=True)
    onboarding_status = Column(
        String(50),
        default="pending",
    )  # pending / provisioning / active / suspended / cancelled

    # Free-form config (API keys, branding, etc.)
    meta = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
