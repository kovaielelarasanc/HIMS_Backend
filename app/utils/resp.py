# FILE: app/api/utils/resp.py
from __future__ import annotations

from typing import Any
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from app.schemas.common import ApiResponse, ApiError


def ok(data: Any = None, status_code: int = 200) -> JSONResponse:
    payload = ApiResponse(status=True, data=data)
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def err(msg: str, status_code: int = 400) -> JSONResponse:
    payload = ApiResponse(status=False, error=ApiError(msg=msg))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))
