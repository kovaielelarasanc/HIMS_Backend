# FILE: app/schemas/dashboard.py
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


class DashboardFilter(BaseModel):
    """
    Common filter options for dashboards.
    """
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    unit_id: Optional[int] = None  # loosely: ward/location if needed
    department_id: Optional[int] = None


WidgetType = Literal["metric", "table", "chart", "alert", "custom"]


class DashboardWidget(BaseModel):
    code: str
    title: str
    widget_type: WidgetType
    description: Optional[str] = None
    data: Any
    config: Dict[str, Any] = {}


class DashboardWidgetInstance(BaseModel):
    """
    A widget + its runtime data returned to the frontend.
    """
    code: str
    title: str
    widget_type: WidgetType
    config: Dict[str, Any] = Field(default_factory=dict)
    data: Any


class DashboardLayoutBase(BaseModel):
    layout_json: Dict[str, Any] = Field(
        default_factory=dict,
        description="Layout config: positions, sizes, visible widgets, etc.",
    )


class DashboardLayoutCreate(DashboardLayoutBase):
    role: Optional[str] = None
    user_id: Optional[int] = None


class DashboardLayoutUpdate(DashboardLayoutBase):
    pass


class DashboardLayoutOut(DashboardLayoutBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    role: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DashboardDataResponse(BaseModel):
    role: str
    date_from: date
    date_to: date
    filters: Dict[str, Any] = {}
    widgets: List[DashboardWidget]