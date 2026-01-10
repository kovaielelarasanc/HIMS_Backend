# FILE: app/core/rbac.py
from __future__ import annotations

from fastapi import HTTPException


def _collect_codes(user) -> set[str]:
    codes: set[str] = set()
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            c = getattr(p, "code", None)
            if c:
                codes.add(str(c))
    return codes


def has_perm(user, code: str) -> bool:
    """
    Supports:
      - exact match: pharmacy.sales.view
      - wildcard: pharmacy.sales.*  (matches pharmacy.sales.view/create/update etc)
      - global wildcard: *
    """
    if getattr(user, "is_admin", False):
        return True

    want = (code or "").strip()
    if not want:
        return False

    have = _collect_codes(user)
    if "*" in have:
        return True
    if want in have:
        return True

    # wildcard check: "a.b.c" => also accept "a.b.*"
    parts = want.split(".")
    if len(parts) >= 2:
        wildcard = ".".join(parts[:-1] + ["*"])
        if wildcard in have:
            return True

    return False


def require_any(user, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    for c in (codes or []):
        if has_perm(user, c):
            return
    raise HTTPException(status_code=403, detail="Not permitted")
