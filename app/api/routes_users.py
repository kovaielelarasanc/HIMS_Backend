# app/api/routes_user.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user, require_perm
from app.core.security import hash_password
from app.models.user import User
from app.models.role import Role
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services.rbac_helpers import (
    ensure_admin_has_all_permissions,
    ensure_user_has_atleast_one_role,
)

router = APIRouter()


@router.get("/", response_model=list[UserOut])
def list_users(
        db: Session = Depends(get_db),
        me: User = Depends(current_user),
):
    require_perm(me, "users.view")

    users = (db.query(User).options(joinedload(User.roles)).all())

    out: list[UserOut] = []
    for u in users:
        out.append(
            UserOut(
                id=u.id,
                name=u.name,
                email=u.email,
                is_active=u.is_active,
                is_admin=u.is_admin,
                is_doctor=u.is_doctor,
                department_id=u.department_id,
                role_ids=[r.id for r in (u.roles or [])],
            ))
    return out


@router.get("/doctors", response_model=list[UserOut])
def list_doctors(
        db: Session = Depends(get_db),
        me: User = Depends(current_user),
):
    require_perm(me, "users.view")

    users = (db.query(User).options(joinedload(User.roles)).filter(
        User.is_doctor.is_(True)).all())

    out: list[UserOut] = []
    for u in users:
        out.append(
            UserOut(
                id=u.id,
                name=u.name,
                email=u.email,
                is_active=u.is_active,
                is_admin=u.is_admin,
                is_doctor=u.is_doctor,
                department_id=u.department_id,
                role_ids=[r.id for r in (u.roles or [])],
            ))
    return out


@router.post("/", response_model=UserOut)
def create_user(
        payload: UserCreate,
        db: Session = Depends(get_db),
        me: User = Depends(current_user),
):
    require_perm(me, "users.create")

    # Email uniqueness within tenant DB
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email exists")

    u = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        department_id=payload.department_id,
        is_active=payload.is_active,
        is_doctor=payload.is_doctor,
    )
    db.add(u)
    db.flush()  # get u.id

    # Roles: validate if provided
    if payload.role_ids is not None and len(payload.role_ids) > 0:
        roles = db.query(Role).filter(Role.id.in_(payload.role_ids)).all()
        if len(roles) != len(set(payload.role_ids)):
            raise HTTPException(status_code=400, detail="Invalid role_ids")
        u.roles = roles

    # Ensure at least one role (fixes old-style creates too)
    ensure_user_has_atleast_one_role(db, u)

    # Admin role always has all permissions
    ensure_admin_has_all_permissions(db)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Create failed")
    db.refresh(u)

    return UserOut(
        id=u.id,
        name=u.name,
        email=u.email,
        is_active=u.is_active,
        is_admin=u.is_admin,
        is_doctor=u.is_doctor,
        department_id=u.department_id,
        role_ids=[r.id for r in (u.roles or [])],
    )


@router.put("/{user_id}", response_model=UserOut)
def update_user(
        user_id: int,
        payload: UserUpdate,
        db: Session = Depends(get_db),
        me: User = Depends(current_user),
):
    require_perm(me, "users.update")

    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Not found")

    # Email uniqueness
    if payload.email != u.email:
        exists = db.query(User).filter(User.email == payload.email).first()
        if exists:
            raise HTTPException(status_code=409, detail="Email already exists")

    # Update fields
    u.name = payload.name
    u.email = payload.email
    u.department_id = payload.department_id
    u.is_active = payload.is_active
    u.is_doctor = payload.is_doctor

    if payload.password:
        u.password_hash = hash_password(payload.password)

    # ✅ Role-safe logic (prevents old users losing roles)
    # None  => keep existing roles
    # []    => clear + assign default role
    # [..]  => set these roles
    if payload.role_ids is None:
        pass
    elif len(payload.role_ids) == 0:
        u.roles = []
        ensure_user_has_atleast_one_role(db, u)
    else:
        roles = db.query(Role).filter(Role.id.in_(payload.role_ids)).all()
        if len(roles) != len(set(payload.role_ids)):
            raise HTTPException(status_code=400, detail="Invalid role_ids")
        u.roles = roles

    # If user has no roles (old data), repair it
    ensure_user_has_atleast_one_role(db, u)

    # Ensure admin role always gets new permissions
    ensure_admin_has_all_permissions(db)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Update failed")
    db.refresh(u)

    return UserOut(
        id=u.id,
        name=u.name,
        email=u.email,
        is_active=u.is_active,
        is_admin=u.is_admin,
        is_doctor=u.is_doctor,
        department_id=u.department_id,
        role_ids=[r.id for r in (u.roles or [])],
    )


@router.delete("/{user_id}")
def delete_user(
        user_id: int,
        db: Session = Depends(get_db),
        me: User = Depends(current_user),
):
    # In HMIS: never hard delete users (FK audit trails). Deactivate instead.
    require_perm(me, "users.delete")

    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="Not found")

    if u.is_admin:
        raise HTTPException(status_code=400,
                            detail="Cannot deactivate Admin user")

    u.is_active = False

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Deactivate failed")

    return {"message": "Deactivated"}
