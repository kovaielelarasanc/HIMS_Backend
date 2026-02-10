# FILE: app/api/routes_permissions.py
from __future__ import annotations

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from app.api.deps import get_db, current_user, require_perm
from app.models.user import User
from app.models.permission import Permission
from app.schemas.permission import PermissionCreate, PermissionOut, ModuleCountOut

router = APIRouter()

# -------------------------
# Routes
# -------------------------

@router.get("/modules", response_model=list[ModuleCountOut])
def permission_modules(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_perm(user, "permissions.view")

    rows = (
        db.query(Permission.module, func.count(Permission.id))
        .group_by(Permission.module)
        .order_by(Permission.module.asc())
        .all()
    )

    return [{"module": m or "unknown", "count": int(c or 0)} for (m, c) in rows]


@router.get("/", response_model=List[PermissionOut])
def list_permissions(
    q: Optional[str] = Query(default=None, description="Search in code/label/module"),
    module: Optional[str] = Query(default=None, description="Filter by module"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_perm(user, "permissions.view")

    qry = db.query(Permission)

    if module:
        qry = qry.filter(Permission.module == module)

    if q:
        s = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                Permission.code.ilike(s),
                Permission.label.ilike(s),
                Permission.module.ilike(s),
            )
        )

    return qry.order_by(Permission.module.asc(), Permission.code.asc()).all()


@router.post("/", response_model=PermissionOut, status_code=status.HTTP_201_CREATED)
def create_permission(
    payload: PermissionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_perm(user, "permissions.create")

    code = (payload.code or "").strip()
    label = (payload.label or "").strip()
    module = (payload.module or "").strip()

    if not code or not label or not module:
        raise HTTPException(status_code=400, detail="code, label, module are required")

    exists = db.query(Permission).filter(Permission.code == code).first()
    if exists:
        raise HTTPException(status_code=400, detail="Permission code exists")

    p = Permission(code=code, label=label, module=module)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.put("/{perm_id}", response_model=PermissionOut)
def update_permission(
    perm_id: int,
    payload: PermissionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_perm(user, "permissions.update")

    p = db.get(Permission, perm_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    new_code = (payload.code or "").strip()
    new_label = (payload.label or "").strip()
    new_module = (payload.module or "").strip()

    if not new_code or not new_label or not new_module:
        raise HTTPException(status_code=400, detail="code, label, module are required")

    if new_code != p.code:
        exists = db.query(Permission).filter(Permission.code == new_code).first()
        if exists:
            raise HTTPException(status_code=400, detail="Permission code exists")

    p.code = new_code
    p.label = new_label
    p.module = new_module

    db.commit()
    db.refresh(p)
    return p


@router.delete("/{perm_id}", status_code=status.HTTP_200_OK)
def delete_permission(
    perm_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    require_perm(user, "permissions.delete")

    p = db.get(Permission, perm_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    db.delete(p)
    db.commit()
    return {"message": "Deleted"}
