# FILE: app/models/dashboard.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DashboardLayout(Base):
    """
    Stores dashboard layout & selected widgets.

    If user_id is NULL and role is set -> role default layout.
    If user_id is set -> user-specific layout (overrides role default).
    layout_json is a JSON string with widget positions, sizes, etc.
    """
    __tablename__ = "dashboard_layouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role: Mapped[Optional[str]] = mapped_column(String(50),
                                                index=True,
                                                nullable=True)

    # e.g. 'admin', 'doctor', 'nurse', etc.
    layout_json: Mapped[str] = mapped_column(Text,
                                             nullable=False,
                                             default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime,
                                                 default=datetime.utcnow,
                                                 nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime,
                                                 default=datetime.utcnow,
                                                 onupdate=datetime.utcnow,
                                                 nullable=False)

    user = relationship("User", backref="dashboard_layouts")
