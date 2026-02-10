from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.api.deps import get_db, current_user, require_perm
from app.models.department import Department
from app.schemas.department import DepartmentCreate, DepartmentOut
from app.models.user import User


router = APIRouter()


@router.get("/", response_model=list[DepartmentOut])
def list_departments(db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "departments.view")
    return db.query(Department).all()


@router.post("/", response_model=DepartmentOut)
def create_department(payload: DepartmentCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "departments.create")
    if db.query(Department).filter(Department.name == payload.name).first():
        raise HTTPException(status_code=400, detail="Department exists")
    d = Department(name=payload.name, description=payload.description)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d

@router.put("/{dept_id}", response_model=DepartmentOut)
def update_department(dept_id: int, payload: DepartmentCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "departments.update")
    d = db.query(Department).get(dept_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    d.name = payload.name
    d.description = payload.description
    db.commit()
    db.refresh(d)
    return d


@router.delete("/{dept_id}")
def delete_department(dept_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "departments.delete")
    d = db.query(Department).get(dept_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(d)
    db.commit()
    return {"message": "Deleted"}
