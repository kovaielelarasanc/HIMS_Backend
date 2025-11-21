from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    email = Column(String(191), unique=True,
                   nullable=False)  # <= 191, no index=True
    password_hash = Column(String(255), nullable=False)

    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)

    # NEW FIELD
    is_doctor = Column(Boolean, default=False, nullable=False)

    department_id = Column(Integer,
                           ForeignKey("departments.id"),
                           nullable=True)
    department = relationship("Department", back_populates="users")

    roles = relationship("Role",
                         secondary="user_roles",
                         back_populates="users")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), primary_key=True)
