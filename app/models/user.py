# app/models/user.py
from __future__ import annotations

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

    # ✅ System-generated unique 6-digit login id (per tenant DB)
    login_id = Column(String(6), unique=True, index=True, nullable=False, default="")

    name = Column(String(120), nullable=False)

    # ✅ optional unless 2FA enabled
    email = Column(String(191), unique=True, nullable=True)
    email_verified = Column(Boolean, default=False, nullable=False)

    password_hash = Column(String(255), nullable=False)

    # ✅ settings toggles
    two_fa_enabled = Column(Boolean, default=False, nullable=False)
    multi_login_enabled = Column(Boolean, default=True, nullable=False)

    # ✅ increments on password change -> invalidates all tokens immediately
    token_version = Column(Integer, default=1, nullable=False)

    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)

    # Doctor mapping
    is_doctor = Column(Boolean, default=False, nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    department = relationship("Department", back_populates="users")

    # ✅ NEW: Doctor optional fields (only meaningful when is_doctor=true)
    doctor_qualification = Column(String(255), nullable=True)
    doctor_registration_no = Column(String(64), nullable=True, index=True)

    roles = relationship("Role", secondary="user_roles", back_populates="users")

    sessions = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def full_name(self) -> str:
        return self.name


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), primary_key=True)


class UserLoginSeq(Base):
    """
    Helper table: insert a row -> id auto increments
    login_id = f"{id:06d}"
    """
    __tablename__ = "user_login_seq"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, autoincrement=True)


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)

    session_id = Column(String(36), unique=True, index=True, nullable=False)  # uuid4
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)

    created_at = Column(String(32), nullable=False)  # keep as string? no -> store as datetime in real
    # NOTE: if you prefer DateTime columns, change these to DateTime in model + migrate.
    last_seen_at = Column(String(32), nullable=True)
    expires_at = Column(String(32), nullable=False)
    revoked_at = Column(String(32), nullable=True)
    revoke_reason = Column(String(50), nullable=True)

    user = relationship("User", back_populates="sessions")
