from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.ot_master import OtSurgeryMaster
from app.schemas.ot_master import OtSurgeryMasterIn, OtSurgeryMasterOut

router = APIRouter(prefix="/ot/masters", tags=["OT Masters"])


def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes:
                return
    raise HTTPException(403, "Not permitted")


@router.get("/surgeries", response_model=dict)
def list_surgeries(
        q: str = Query("", description="Search by name/code"),
        active: Optional[bool] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.masters.manage"])
    qry = db.query(OtSurgeryMaster)
    if q:
        like = f"%{q}%"
        qry = qry.filter(
            or_(OtSurgeryMaster.name.ilike(like),
                OtSurgeryMaster.code.ilike(like)))
    if active is not None:
        qry = qry.filter(OtSurgeryMaster.active.is_(active))

    total = qry.count()
    rows = (qry.order_by(OtSurgeryMaster.name.asc()).offset(
        (page - 1) * page_size).limit(page_size).all())

    return {
        "total":
        total,
        "page":
        page,
        "page_size":
        page_size,
        "items": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "default_cost": float(r.default_cost or 0),
                "hourly_cost": float(r.hourly_cost or 0),  # NEW
                "active": bool(r.active),
            } for r in rows
        ],
    }


@router.post("/surgeries", response_model=OtSurgeryMasterOut)
def create_surgery(
        payload: OtSurgeryMasterIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.masters.manage"])
    m = OtSurgeryMaster(
        code=payload.code.strip(),
        name=payload.name.strip(),
        default_cost=payload.default_cost or 0,
        hourly_cost=payload.hourly_cost or 0,  # NEW
        active=True if payload.active is None else bool(payload.active),
        created_by=user.id,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return OtSurgeryMasterOut(
        id=m.id,
        code=m.code,
        name=m.name,
        default_cost=float(m.default_cost or 0),
        hourly_cost=float(m.hourly_cost or 0),  # NEW
        active=bool(m.active),
    )


@router.put("/surgeries/{surg_id}", response_model=OtSurgeryMasterOut)
def update_surgery(
        surg_id: int,
        payload: OtSurgeryMasterIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.masters.manage"])
    m = db.query(OtSurgeryMaster).get(surg_id)
    if not m:
        raise HTTPException(404, "Surgery master not found")
    m.code = payload.code.strip()
    m.name = payload.name.strip()
    m.default_cost = payload.default_cost or 0
    m.hourly_cost = payload.hourly_cost or 0  # NEW
    if payload.active is not None:
        m.active = bool(payload.active)
    db.commit()
    db.refresh(m)
    return OtSurgeryMasterOut(
        id=m.id,
        code=m.code,
        name=m.name,
        default_cost=float(m.default_cost or 0),
        hourly_cost=float(m.hourly_cost or 0),  # NEW
        active=bool(m.active),
    )


@router.delete("/surgeries/{surg_id}")
def delete_surgery(
        surg_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.masters.manage"])
    m = db.query(OtSurgeryMaster).get(surg_id)
    if not m:
        raise HTTPException(404, "Surgery master not found")
    db.delete(m)
    db.commit()
    return {"message": "Deleted"}
