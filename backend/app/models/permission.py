from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import relationship
from app.db.base import Base

class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True)
    code = Column(String(120), unique=True, nullable=False)   # e.g. "users.view"
    label = Column(String(255), nullable=False)               # UI label
    module = Column(String(120), nullable=False)              # e.g. "users"

    roles = relationship("Role", secondary="role_permissions", back_populates="permissions")
