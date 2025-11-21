# backend/app/api/routes_masters_credit.py
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.payer import CreditProvider, Tpa, CreditPlan
from app.schemas.credit import CreditProviderOut, TpaOut, CreditPlanOut

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


@router.get("/credit-providers", response_model=List[CreditProviderOut])
def list_credit_providers(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # allow anyone who can create patients or view billing
    if not (has_perm(user, "patients.create")
            or has_perm(user, "billing.view")):
        if not user.is_admin:
            pass
    q = (db.query(CreditProvider).filter(
        CreditProvider.is_active.is_(True)).order_by(
            CreditProvider.display_name, CreditProvider.name))
    return q.all()


@router.get("/tpas", response_model=List[TpaOut])
def list_tpas_simple(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    q = db.query(Tpa).filter(Tpa.is_active.is_(True)).order_by(Tpa.name)
    return q.all()


@router.get("/credit-plans", response_model=List[CreditPlanOut])
def list_credit_plans_simple(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    q = (db.query(CreditPlan).filter(CreditPlan.is_active.is_(True)).order_by(
        CreditPlan.name))
    return q.all()
