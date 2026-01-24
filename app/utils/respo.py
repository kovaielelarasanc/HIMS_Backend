# FILE: app/utils/resp.py
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def ok(data: Any = None, status: int = 200, *, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """Standard success envelope (frontend unwrap supports ok/status)."""
    payload = {"ok": True, "status": True, "data": data}
    return JSONResponse(status_code=int(status), content=jsonable_encoder(payload), headers=headers)


def err(
    message: str,
    status: int = 400,
    *,
    code: Optional[str] = None,
    details: Any = None,
    data: Any = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """Standard error envelope."""
    error: Dict[str, Any] = {"msg": str(message)}
    if code is not None:
        error["code"] = str(code)
    if details is not None:
        error["details"] = details

    payload = {"ok": False, "status": False, "data": data, "error": error, "message": str(message)}
    return JSONResponse(status_code=int(status), content=jsonable_encoder(payload), headers=headers)
