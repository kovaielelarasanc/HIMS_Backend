# backend/app/api/routes_dashboard.py
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends

from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.schemas.dashboard import DashboardDataResponse
from app.services.dashboard_service import build_dashboard_for_user

router = APIRouter()


@router.get("/data", response_model=DashboardDataResponse)
def get_dashboard_data(
    date_from: date,
    date_to: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
) -> DashboardDataResponse:
    """
    Professional live dashboard data endpoint.
    - Aware of OPD, IPD, Pharmacy, Lab, Radiology, OT & Billing.
    - Returns metrics, tables and charts based on your DB.
    """
    # Guard: swap if user sends wrong order
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    return build_dashboard_for_user(
        db=db,
        user=current_user,
        date_from=date_from,
        date_to=date_to,
    )
