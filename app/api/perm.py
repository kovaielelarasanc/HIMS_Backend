from __future__ import annotations

from app.models.user import User
from app.core.rbac import has_perm as rbac_has_perm


def has_perm(user: User, code: str) -> bool:
    return rbac_has_perm(user, code)
