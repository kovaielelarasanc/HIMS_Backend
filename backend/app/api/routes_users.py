from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.models.role import Role
from app.schemas.user import UserCreate, UserOut


router = APIRouter()


@router.get("/", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    users = db.query(User).all()
    out = []
    for u in users:
        out.append(UserOut(id=u.id, name=u.name, email=u.email, is_active=u.is_active,is_admin=u.is_admin, department_id=u.department_id, role_ids=[r.id for r in u.roles]))
    return out


@router.post("/", response_model=UserOut)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email exists")
    u = User(name=payload.name, email=payload.email, password_hash=hash_password(payload.password), department_id=payload.department_id)
    db.add(u); db.commit(); db.refresh(u)
    if payload.role_ids:
        roles = db.query(Role).filter(Role.id.in_(payload.role_ids)).all()
        u.roles = roles; db.commit(); db.refresh(u)
    return UserOut(id=u.id, name=u.name, email=u.email, is_active=u.is_active,
                    is_admin=u.is_admin, department_id=u.department_id,
                    role_ids=[r.id for r in u.roles])


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserCreate, db: Session = Depends(get_db)):
    u = db.query(User).get(user_id)
    if not u: raise HTTPException(status_code=404, detail="Not found")
    u.name = payload.name; u.email = payload.email; u.department_id = payload.department_id
    if payload.password:
        u.password_hash = hash_password(payload.password)
    u.roles = db.query(Role).filter(Role.id.in_(payload.role_ids)).all()
    db.commit(); db.refresh(u)
    return UserOut(id=u.id, name=u.name, email=u.email, is_active=u.is_active,
                    is_admin=u.is_admin, department_id=u.department_id,
                    role_ids=[r.id for r in u.roles])
    
@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    u = db.query(User).get(user_id)
    if not u: raise HTTPException(status_code=404, detail="Not found")
    if u.is_admin:
        raise HTTPException(status_code=400, detail="Cannot delete the Admin user")
    db.delete(u); db.commit()
    return {"message": "Deleted"}