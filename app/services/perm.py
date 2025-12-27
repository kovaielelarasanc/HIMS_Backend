# FILE: app/services/perm.py
from __future__ import annotations

from typing import Iterable
from fastapi import HTTPException

from app.models.user import User

def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []):
        for p in getattr(r, "permissions", []):
            if p.code == code:
                return True
    return False


def need_any(user: User, perms: Iterable[str]) -> None:
    if not any(has_perm(user, p) for p in perms):
        raise HTTPException(status_code=403, detail="Not permitted")


# Suggested permission codes (add to Permission master):
# ipd.dressing.view/create/update
# ipd.transfusion.view/create/update
# ipd.restraints.view/create/update/stop/monitor
# ipd.isolation.view/create/update/stop
# ipd.icu.view/create/update
# ipd.manage (super)
