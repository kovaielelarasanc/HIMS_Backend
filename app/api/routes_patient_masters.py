# FILE: app/api/routes_patient_masters.py
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.department import Department  # noqa: F401  (for relationship)
from app.models.role import Role  # noqa: F401  (kept for backward compatibility)
from app.models.payer import Payer, Tpa, CreditPlan
from app.schemas.patient_masters import (
    PayerCreate,
    PayerUpdate,
    PayerOut,
    TpaCreate,
    TpaUpdate,
    TpaOut,
    CreditPlanCreate,
    CreditPlanUpdate,
    CreditPlanOut,
    DoctorRefOut,
    ReferenceSourceOut,
)

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


# ---------- Reference sources ----------


@router.get("/reference-sources", response_model=List[ReferenceSourceOut])
def get_reference_sources(user: User = Depends(auth_current_user)):
    # any logged-in user
    sources = [
        {
            "code": "doctor",
            "label": "Doctor"
        },
        {
            "code": "google",
            "label": "Google"
        },
        {
            "code": "social_media",
            "label": "Social Media"
        },
        {
            "code": "ads",
            "label": "Advertisements"
        },
        {
            "code": "other",
            "label": "Other"
        },
    ]
    return [ReferenceSourceOut(**s) for s in sources]


# ---------- Doctors list (for referring doctor) ----------


@router.get("/doctors", response_model=List[DoctorRefOut])
def list_doctors(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    List doctors that can be used as:
      - Referring doctors for Patient.ref_doctor_id
      - Consulting doctors in other modules.

    We now rely on User.is_doctor flag + department.
    """
    if not has_perm(user, "patients.masters.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = (
        db.query(User).outerjoin(User.department).filter(
            User.is_active.is_(True),
            User.is_doctor.is_(True),  # <-- new boolean flag on User
        ).order_by(User.name.asc()))

    results: List[DoctorRefOut] = []
    for u in q.all():
        dept_name = u.department.name if u.department else None
        results.append(
            DoctorRefOut(id=u.id, name=u.name, department_name=dept_name))
    return results


# ---------- Payer CRUD ----------


@router.get("/payers", response_model=List[PayerOut])
def list_payers(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return (db.query(Payer).filter(Payer.is_active.is_(True)).order_by(
        Payer.name.asc()).all())


@router.post("/payers", response_model=PayerOut)
def create_payer(
        payload: PayerCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    exists = db.query(Payer).filter(Payer.code == payload.code).first()
    if exists:
        raise HTTPException(status_code=400,
                            detail="Payer code already exists")

    p = Payer(**payload.dict())
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.put("/payers/{payer_id}", response_model=PayerOut)
def update_payer(
        payer_id: int,
        payload: PayerUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Payer).get(payer_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")

    body = payload.dict(exclude_unset=True)
    if "code" in body:
        new_code = body["code"]
        if new_code and new_code != p.code:
            exists = db.query(Payer).filter(Payer.code == new_code).first()
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Payer code already exists")

    for k, v in body.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/payers/{payer_id}")
def delete_payer(
        payer_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    p = db.query(Payer).get(payer_id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    p.is_active = False
    db.commit()
    return {"message": "Deactivated"}


# ---------- TPA CRUD ----------


@router.get("/tpas", response_model=List[TpaOut])
def list_tpas(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return (db.query(Tpa).filter(Tpa.is_active.is_(True)).order_by(
        Tpa.name.asc()).all())


@router.post("/tpas", response_model=TpaOut)
def create_tpa(
        payload: TpaCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    exists = db.query(Tpa).filter(Tpa.code == payload.code).first()
    if exists:
        raise HTTPException(status_code=400, detail="TPA code already exists")

    t = Tpa(**payload.dict())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@router.put("/tpas/{tpa_id}", response_model=TpaOut)
def update_tpa(
        tpa_id: int,
        payload: TpaUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    t = db.query(Tpa).get(tpa_id)
    if not t:
        raise HTTPException(status_code=404, detail="Not found")

    body = payload.dict(exclude_unset=True)
    if "code" in body:
        new_code = body["code"]
        if new_code and new_code != t.code:
            exists = db.query(Tpa).filter(Tpa.code == new_code).first()
            if exists:
                raise HTTPException(status_code=400,
                                    detail="TPA code already exists")

    for k, v in body.items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return t


@router.delete("/tpas/{tpa_id}")
def delete_tpa(
        tpa_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    t = db.query(Tpa).get(tpa_id)
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    t.is_active = False
    db.commit()
    return {"message": "Deactivated"}


# ---------- Credit Plan CRUD ----------


@router.get("/credit-plans", response_model=List[CreditPlanOut])
def list_credit_plans(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return (db.query(CreditPlan).filter(
        CreditPlan.is_active.is_(True)).order_by(CreditPlan.name.asc()).all())


@router.post("/credit-plans", response_model=CreditPlanOut)
def create_credit_plan(
        payload: CreditPlanCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    exists = db.query(CreditPlan).filter(
        CreditPlan.code == payload.code).first()
    if exists:
        raise HTTPException(status_code=400,
                            detail="Credit plan code already exists")

    cp = CreditPlan(**payload.dict())
    db.add(cp)
    db.commit()
    db.refresh(cp)
    return cp


@router.put("/credit-plans/{plan_id}", response_model=CreditPlanOut)
def update_credit_plan(
        plan_id: int,
        payload: CreditPlanUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    cp = db.query(CreditPlan).get(plan_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Not found")

    body = payload.dict(exclude_unset=True)
    if "code" in body:
        new_code = body["code"]
        if new_code and new_code != cp.code:
            exists = db.query(CreditPlan).filter(
                CreditPlan.code == new_code).first()
            if exists:
                raise HTTPException(status_code=400,
                                    detail="Credit plan code already exists")

    for k, v in body.items():
        setattr(cp, k, v)
    db.commit()
    db.refresh(cp)
    return cp


@router.delete("/credit-plans/{plan_id}")
def delete_credit_plan(
        plan_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.masters.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    cp = db.query(CreditPlan).get(plan_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Not found")
    cp.is_active = False
    db.commit()
    return {"message": "Deactivated"}


# ---------- Aggregated masters for patient registration ----------


@router.get("/all")
def all_patient_masters(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Convenience endpoint to load all patient-related masters in one call.

    Returns:
      {
        "reference_sources": [...],
        "doctors": [...],
        "payers": [...],
        "tpas": [...],
        "credit_plans": [...]
      }
    """
    if not has_perm(user, "patients.masters.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    reference_sources = get_reference_sources(user)
    doctors = list_doctors(db=db, user=user)
    payers = list_payers(db=db, user=user)
    tpas = list_tpas(db=db, user=user)
    credit_plans = list_credit_plans(db=db, user=user)

    return {
        "reference_sources": reference_sources,
        "doctors": doctors,
        "payers": payers,
        "tpas": tpas,
        "credit_plans": credit_plans,
    }
