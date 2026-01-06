# app/models/otp.py
from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import relationship

from app.db.base import Base


class OtpToken(Base):
    __tablename__ = "otp_tokens"
    __table_args__ = (
        Index("ix_otp_user_purpose_used", "user_id", "purpose", "used"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)

    otp_code = Column(String(6), nullable=False)

    # login | email_verify
    purpose = Column(String(20), nullable=False, default="login")

    # store the email used at the time (helps audits)
    email = Column(String(191), nullable=True)

    used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    user = relationship("User")

    @staticmethod
    def expiry(minutes: int = 10) -> datetime:
        return datetime.utcnow() + timedelta(minutes=minutes)
