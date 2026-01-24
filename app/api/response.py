# FILE: app/api/response.py
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def ok(
    data: Any = None,
    *,
    meta: Optional[Dict[str, Any]] = None,
    status_code: int = 200,
) -> JSONResponse:
    """
    Standard success wrapper:
    {
      "ok": true,
      "data": ...,
      "meta": {...} (optional)
    }
    """
    payload: Dict[str, Any] = {"ok": True, "data": data}
    if meta is not None:
        payload["meta"] = meta

    # ✅ jsonable_encoder converts datetime/date/Decimal/Enum etc. to JSON-safe types
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def err(
    msg: str = "Something went wrong",
    *,
    status_code: int = 400,
    code: Optional[str] = None,
    details: Any = None,
) -> JSONResponse:
    """
    Standard error wrapper:
    {
      "ok": false,
      "error": {
        "msg": "...",
        "code": "...",
        "details": ...
      }
    }
    """
    payload: Dict[str, Any] = {
        "ok": False,
        "error": {
            "msg": msg,
            "code": code,
            "details": details,
        },
    }
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))
