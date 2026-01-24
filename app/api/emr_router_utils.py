# FILE: app/api/emr_router_utils.py
from __future__ import annotations

import json
from typing import Any, Iterable, List, Optional

from fastapi import HTTPException

from app.models.user import User


def need_any(user: User, codes: Iterable[str]) -> None:
    """
    Shared permission gate (refactor target).
    Replace internals if you have a different permission system.
    """
    if bool(getattr(user, "is_admin", False)):
        return

    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
                return

    raise HTTPException(status_code=403, detail="Not permitted")


def norm_code(v: Any) -> str:
    return (str(v or "").strip().upper().replace(" ", "_"))


def code_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.upper() in {"ALL", "*", "ANY"}:
        return None
    return norm_code(s)


def as_list(v: Any) -> List[Any]:
    """
    Accept: list, JSON-string list, CSV string, scalar -> list.
    """
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return [x.strip() for x in s.split(",") if x.strip()]
    return [v]


def as_json_obj(v: Any, default: Any = None) -> Any:
    """
    Accept dict/list directly; accept JSON string; else return default.
    """
    if default is None:
        default = {}

    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            raise HTTPException(status_code=422, detail="schema_json must be valid JSON")

    return default
