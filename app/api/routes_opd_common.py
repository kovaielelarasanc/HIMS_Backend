# backend/app/api/routes_opd_common.py
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.api.deps import get_db
from app.api import deps as deps_mod
from app.models.department import Department
from app.models.user import User
from app.models.role import Role
from app.models.user import UserRole  # secondary table

router = APIRouter(tags=["OPD"])


def current_user(
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
) -> User:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    return deps_mod.get_current_user_from_token(token, db)


@router.get("/departments")
def list_departments(
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    rows = db.query(Department).order_by(Department.name.asc()).all()
    return [{"id": d.id, "name": d.name} for d in rows]


@router.get("/roles")
def list_roles_for_department(
        department_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    q = (db.query(
        Role.id,
        Role.name,
        func.count(User.id).label("members"),
    ).join(UserRole, UserRole.role_id == Role.id).join(
        User, User.id == UserRole.user_id).filter(
            User.department_id == department_id,
            User.is_active.is_(True),
        ).group_by(Role.id, Role.name).order_by(Role.name.asc()))
    rows = q.all()
    return [{
        "id": r.id,
        "name": r.name,
        "members": int(r.members)
    } for r in rows]


@router.get("/users")
def list_users_by_department_and_role(
        department_id: int = Query(...),
        role_id: Optional[int] = Query(None),
        is_doctor: Optional[bool] = Query(
            None,
            description=
            "If true, only doctors from this department (User.is_doctor=1)",
        ),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Department → users, optionally filtered by role and doctor flag.
    Used for OPD department-doctor selection.
    """
    base = db.query(User).filter(
        User.department_id == department_id,
        User.is_active.is_(True),
    )

    if role_id:
        base = base.join(
            UserRole,
            UserRole.user_id == User.id).filter(UserRole.role_id == role_id)

    if is_doctor is not None:
        base = base.filter(User.is_doctor.is_(is_doctor))

    users = base.order_by(User.name.asc()).all()

    out = []
    for u in users:
        role_names = [r.name for r in (u.roles or [])]
        out.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "department_id": u.department_id,
            "roles": role_names,
            "is_doctor": u.is_doctor,
        })
    return out
