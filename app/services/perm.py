# FILE: app/services/perm.py
from __future__ import annotations

from typing import Iterable
from fastapi import HTTPException

from app.models.user import User
from app.core.rbac import has_perm as rbac_has_perm

def has_perm(user: User, code: str) -> bool:
    return rbac_has_perm(user, code)


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
