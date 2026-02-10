from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_db, current_user, require_perm
from app.models.role import Role, RolePermission
from app.models.permission import Permission
from app.schemas.role import RoleCreate, RoleOut
from app.models.user import User


router = APIRouter()


@router.get("/", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "roles.view")
    roles = db.query(Role).all()
    out = []
    for r in roles:
        out.append(RoleOut(id=r.id, name=r.name, description=r.description, permission_ids=[p.id for p in r.permissions]))
    return out

@router.post("/", response_model=RoleOut)
def create_role(payload: RoleCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "roles.create")
    if db.query(Role).filter(Role.name == payload.name).first():
        raise HTTPException(status_code=400, detail="Role exists")
    r = Role(name=payload.name, description=payload.description)
    db.add(r); db.commit(); db.refresh(r)
    # link permissions
    if payload.permission_ids:
        perms = db.query(Permission).filter(Permission.id.in_(payload.permission_ids)).all()
        r.permissions = perms
        db.commit(); db.refresh(r)
    return RoleOut(id=r.id, name=r.name, description=r.description, permission_ids=[p.id for p in r.permissions])


@router.put("/{role_id}", response_model=RoleOut)
def update_role(role_id: int, payload: RoleCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "roles.update")
    r = db.query(Role).get(role_id)
    if not r: raise HTTPException(status_code=404, detail="Not found")
    r.name = payload.name; r.description = payload.description
    r.permissions = db.query(Permission).filter(Permission.id.in_(payload.permission_ids)).all()
    db.commit(); db.refresh(r)
    return RoleOut(id=r.id, name=r.name, description=r.description,
    permission_ids=[p.id for p in r.permissions])


@router.delete("/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "roles.delete")
    r = db.query(Role).get(role_id)
    if not r: raise HTTPException(status_code=404, detail="Not found")
    db.delete(r); db.commit()
    return {"message": "Deleted"}
