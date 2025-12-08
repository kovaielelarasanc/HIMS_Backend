# backend/app/models/tenant.py
from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import relationship

from app.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(191), nullable=False)
    code = Column(String(64), nullable=False, unique=True, index=True)
    contact_email = Column(String(191), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    users = relationship("User", back_populates="tenant")
    roles = relationship("Role",
                         back_populates="tenant",
                         cascade="all, delete-orphan")
    departments = relationship("Department",
                               back_populates="tenant",
                               cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} code={self.code} name={self.name}>"
