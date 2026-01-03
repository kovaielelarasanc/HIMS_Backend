# FILE: app/schemas/common.py
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class ApiError(BaseModel):
    msg: str
    code: Optional[str] = None
    details: Optional[Any] = None


class ApiResponse(BaseModel, Generic[T]):
    status: bool = True
    data: Optional[T] = None
    error: Optional[ApiError] = None
