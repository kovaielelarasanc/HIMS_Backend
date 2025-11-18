from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models.permission import Permission
from app.schemas.permission import PermissionCreate, PermissionOut


router = APIRouter()


@router.get("/", response_model=list[PermissionOut])
def list_permissions(db: Session = Depends(get_db)):
    return db.query(Permission).all()


@router.post("/", response_model=PermissionOut)
def create_permission(payload: PermissionCreate, db: Session = Depends(get_db)):
    if db.query(Permission).filter(Permission.code == payload.code).first():
        raise HTTPException(status_code=400, detail="Permission code exists")
    p = Permission(code=payload.code, label=payload.label, module=payload.module)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

@router.put("/{perm_id}", response_model=PermissionOut)
def update_permission(perm_id: int, payload: PermissionCreate, db: Session = Depends(get_db)):
    p = db.query(Permission).get(perm_id)
    if not p: raise HTTPException(status_code=404, detail="Not found")
    p.code, p.label, p.module = payload.code, payload.label, payload.module
    db.commit(); db.refresh(p)
    return p


@router.delete("/{perm_id}")
def delete_permission(perm_id: int, db: Session = Depends(get_db)):
    p = db.query(Permission).get(perm_id)
    if not p: raise HTTPException(status_code=404, detail="Not found")
    db.delete(p); db.commit()
    return {"message": "Deleted"}