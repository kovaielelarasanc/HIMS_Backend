# FILE: app/schemas/common.py
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, ConfigDict


class ApiError(BaseModel):
    msg: str


class ApiResponse(BaseModel):
    status: bool
    data: Optional[Any] = None
    error: Optional[ApiError] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)
