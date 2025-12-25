# backend/app/api/routes_masters.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.opd import Medicine, LabTest, RadiologyTest

router = APIRouter()


# ---------------- permissions ----------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) == code:
                return True
    return False


def must_admin(user: User):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")


def must_any_perm(user: User, codes: List[str]):
    if any(has_perm(user, c) for c in codes):
        return
    raise HTTPException(status_code=403,
                        detail=f"Missing permission: any of {codes}")


def must_perm(user: User, code: str):
    if not has_perm(user, code):
        raise HTTPException(status_code=403,
                            detail=f"Missing permission: {code}")


# ✅ Lab masters permission codes
LAB_MASTERS_VIEW = "lab.masters.view"
LAB_MASTERS_MANAGE = "lab.masters.manage"


# ----------- MEDICINES ----------- (unchanged)
@router.get("/medicines", response_model=List[dict])
def list_medicines(
        q: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    qry = db.query(Medicine)
    if q:
        like = f"%{q}%"
        qry = qry.filter(Medicine.name.ilike(like))
    meds = qry.order_by(Medicine.name.asc()).limit(200).all()
    return [{
        "id": m.id,
        "name": m.name,
        "form": m.form,
        "unit": m.unit,
        "price_per_unit": float(m.price_per_unit or 0),
    } for m in meds]


@router.post("/medicines", response_model=dict)
def create_medicine(
        payload: dict,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    must_admin(user)
    name = (payload.get("name") or "").strip()
    form = (payload.get("form") or "").strip()
    unit = (payload.get("unit") or "").strip()
    price = payload.get("price_per_unit") or 0
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if db.query(Medicine).filter(Medicine.name == name).first():
        raise HTTPException(status_code=400, detail="Medicine already exists")

    m = Medicine(name=name, form=form, unit=unit, price_per_unit=price)
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"id": m.id, "message": "Created"}


# ----------- LAB TESTS ----------- ✅ permission-based
@router.get("/lab-tests", response_model=List[dict])
def list_lab_tests(
        q: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ allow either view OR manage
    must_any_perm(user, [LAB_MASTERS_VIEW, LAB_MASTERS_MANAGE])

    qry = db.query(LabTest)
    if q:
        like = f"%{q}%"
        qry = qry.filter((LabTest.code.ilike(like))
                         | (LabTest.name.ilike(like)))
    tests = qry.order_by(LabTest.name.asc()).limit(200).all()
    return [{
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "price": float(t.price or 0),
    } for t in tests]


@router.post("/lab-tests", response_model=dict)
def create_lab_test(
        payload: dict,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ manage permission required
    must_perm(user, LAB_MASTERS_MANAGE)

    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    price = payload.get("price") or 0
    if not code or not name:
        raise HTTPException(status_code=400, detail="Code & Name required")
    if db.query(LabTest).filter(LabTest.code == code).first():
        raise HTTPException(status_code=400, detail="Test already exists")

    t = LabTest(code=code, name=name, price=price)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "message": "Created"}


@router.put("/lab-tests/{test_id}", response_model=dict)
def update_lab_test(
        test_id: int,
        payload: dict,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ manage permission required
    must_perm(user, LAB_MASTERS_MANAGE)

    t = db.query(LabTest).filter(LabTest.id == test_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Lab test not found")

    code = (payload.get("code") or t.code or "").strip()
    name = (payload.get("name") or t.name or "").strip()
    price = payload.get("price", t.price)

    if not code or not name:
        raise HTTPException(status_code=400, detail="Code & Name required")

    exists = (db.query(LabTest).filter(LabTest.code == code, LabTest.id
                                       != test_id).first())
    if exists:
        raise HTTPException(status_code=400, detail="Test code already exists")

    t.code = code
    t.name = name
    t.price = price

    db.commit()
    db.refresh(t)
    return {"id": t.id, "message": "Updated"}


@router.delete("/lab-tests/{test_id}", response_model=dict)
def delete_lab_test(
        test_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # ✅ manage permission required
    must_perm(user, LAB_MASTERS_MANAGE)

    t = db.query(LabTest).filter(LabTest.id == test_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Lab test not found")

    db.delete(t)
    db.commit()
    return {"id": test_id, "message": "Deleted"}


# ----------- RADIOLOGY TESTS ----------- (unchanged)
@router.get("/radiology-tests", response_model=List[dict])
def list_radiology_tests(
        q: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    qry = db.query(RadiologyTest)
    if q:
        like = f"%{q}%"
        qry = qry.filter((RadiologyTest.code.ilike(like))
                         | (RadiologyTest.name.ilike(like)))
    tests = qry.order_by(RadiologyTest.name.asc()).limit(200).all()
    return [{
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "price": float(t.price or 0),
    } for t in tests]


@router.post("/radiology-tests", response_model=dict)
def create_radiology_test(
        payload: dict,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    must_admin(user)
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    price = payload.get("price") or 0
    if not code or not name:
        raise HTTPException(status_code=400, detail="Code & Name required")
    if db.query(RadiologyTest).filter(RadiologyTest.code == code).first():
        raise HTTPException(status_code=400, detail="Test already exists")

    t = RadiologyTest(code=code, name=name, price=price)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "message": "Created"}
