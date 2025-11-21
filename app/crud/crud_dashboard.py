# FILE: app/crud/crud_dashboard.py
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models.dashboard import DashboardLayout
from app.schemas.dashboard import (
    DashboardLayoutCreate,
    DashboardLayoutUpdate,
)


def get_layout_for_user_and_role(db: Session, *, user_id: int,
                                 role: str) -> Optional[DashboardLayout]:
    """
    1. Try user-specific layout.
    2. Fallback to role default layout.
    """
    stmt_user = (select(DashboardLayout).where(
        DashboardLayout.user_id == user_id).where(
            DashboardLayout.role == role))
    layout = db.scalar(stmt_user)
    if layout:
        return layout

    stmt_role = (select(DashboardLayout).where(
        DashboardLayout.user_id.is_(None)).where(DashboardLayout.role == role))
    return db.scalar(stmt_role)


def upsert_user_layout(db: Session, *, user_id: int, role: str,
                       obj_in: DashboardLayoutUpdate) -> DashboardLayout:
    """
    Create or update a user-specific layout.
    """
    stmt = (select(DashboardLayout).where(
        DashboardLayout.user_id == user_id).where(
            DashboardLayout.role == role))
    layout = db.scalar(stmt)
    if layout is None:
        layout = DashboardLayout(user_id=user_id,
                                 role=role,
                                 layout_json=obj_in.layout_json)
        db.add(layout)
    else:
        layout.layout_json = obj_in.layout_json
    db.commit()
    db.refresh(layout)
    return layout


def create_role_default_layout(
        db: Session, *, role: str,
        obj_in: DashboardLayoutCreate) -> DashboardLayout:
    """
    Create a role-level default layout (admin tool).
    """
    layout = DashboardLayout(user_id=None,
                             role=role,
                             layout_json=obj_in.layout_json)
    db.add(layout)
    db.commit()
    db.refresh(layout)
    return layout
