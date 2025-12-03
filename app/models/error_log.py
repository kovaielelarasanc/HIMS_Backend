from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    JSON,
)

from app.db.base_master import MasterBase


class ErrorLog(MasterBase):
    """
    Centralized error / exception log in MASTER DB.
    Stores both backend + frontend error reports.
    """
    __tablename__ = "error_logs"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)

    # backend / frontend
    error_source = Column(String(50), nullable=False, default="backend")

    # quick summary
    description = Column(String(1000), nullable=True)

    # where it happened
    endpoint = Column(String(255), nullable=True)  # e.g. "POST /api/patients"
    module = Column(String(255), nullable=True)  # e.g. "routes_patients"
    function = Column(String(255), nullable=True)  # e.g. "create_patient"

    http_status = Column(Integer, nullable=True)

    # multi-tenant context
    tenant_code = Column(String(50), nullable=True)  # e.g. "KGH001"

    # raw payloads
    request_payload = Column(JSON, nullable=True)
    response_payload = Column(JSON, nullable=True)

    # exception / stack
    stack_trace = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
