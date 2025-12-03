from datetime import datetime
from typing import Optional, Any

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    JSON,
)

from app.db.base import Base


class AuditLog(Base):
    """
    Per-tenant audit log.
    Every CREATE / UPDATE / DELETE should write here.
    """
    __tablename__ = "audit_logs"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, nullable=True)  # system jobs may be null
    action = Column(String(20), nullable=False)  # CREATE / UPDATE / DELETE

    table_name = Column(String(255), nullable=False)
    record_id = Column(String(100),
                       nullable=False)  # generic pk, stored as string

    old_values = Column(JSON, nullable=True)
    new_values = Column(JSON, nullable=True)

    ip_address = Column(String(45), nullable=True)  # IPv4/IPv6
    user_agent = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
