from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional, Set

from fastapi import HTTPException, status


def _code(x: Any) -> str:
    """
    Normalize permission code safely.
    Supports:
      - Enum -> enum.value
      - str  -> str
      - object with .code -> str/Enum
      - dict {"code": ...}
    """
    if x is None:
        return ""

    if isinstance(x, Enum):
        return str(x.value)

    if isinstance(x, str):
        return x

    if isinstance(x, dict) and "code" in x:
        return _code(x["code"])

    if hasattr(x, "code"):
        c = getattr(x, "code")
        if isinstance(c, Enum):
            return str(c.value)
        return str(c)

    return str(x)


def is_admin_user(user: Any) -> bool:
    """
    Admin bypass: change checks to match your User model.
    """
    if not user:
        return False
    if bool(getattr(user, "is_admin", False)):
        return True
    if bool(getattr(user, "is_superuser", False)):
        return True

    # optional string-based admin types
    for attr in ("role", "role_code", "user_type", "type"):
        v = getattr(user, attr, None)
        if isinstance(v, str) and v.upper() in {"ADMIN", "SUPER_ADMIN", "ROOT", "SUPERUSER"}:
            return True

    return False


def iter_user_perm_codes(user: Any) -> Set[str]:
    """
    Collect permission codes from:
      user.permissions (optional)
      user.roles[*].permissions
    Works even if some attributes are missing.
    """
    out: Set[str] = set()
    if not user:
        return out

    # direct user perms
    up = getattr(user, "permissions", None)
    if up:
        try:
            for p in up:
                c = _code(p).strip()
                if c:
                    out.add(c)
        except TypeError:
            c = _code(up).strip()
            if c:
                out.add(c)

    # role perms
    roles = getattr(user, "roles", None)
    if roles:
        for r in roles:
            perms = getattr(r, "permissions", None)
            if not perms:
                continue
            for p in perms:
                c = _code(p).strip()
                if c:
                    out.add(c)

    return out


def has_perm(user: Any, code: str) -> bool:
    """
    Simple, safe permission check.
    """
    if is_admin_user(user):
        return True

    want = _code(code).strip()
    if not want:
        return False

    return want in iter_user_perm_codes(user)


def require_any(user: Any, required: Iterable[Any], *, message: Optional[str] = None) -> None:
    """
    Raise 403 if user doesn't have at least one permission from 'required'.
    """
    if is_admin_user(user):
        return

    required_set = {_code(x).strip() for x in required if _code(x).strip()}
    if not required_set:
        return

    user_codes = iter_user_perm_codes(user)

    if user_codes.intersection(required_set):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=message or "You do not have permission to perform this action.",
    )
