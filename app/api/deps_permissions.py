# FILE: app/api/deps_permissions.py
from fastapi import Depends, HTTPException, status
from app.api.deps import current_user
from app.models.user import User as UserModel


def has_perm(user: UserModel, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def require_permission(code: str):
    """
    Dependency factory:
    use as Depends(require_permission("lis.masters.departments.view"))
    """

    def _dep(user: UserModel = Depends(current_user)):
        if not has_perm(user, code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You do not have permission: {code}",
            )

    return _dep
