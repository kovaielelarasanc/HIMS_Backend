# FILE: app/api/routes_ot_masters.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.ot import (
    OtSpeciality,
    OtEquipmentMaster,
    OtProcedure,
)
from app.schemas.ot import (
    # Speciality
    OtSpecialityCreate,
    OtSpecialityUpdate,
    OtSpecialityOut,
    # Equipment
    OtEquipmentMasterCreate,
    OtEquipmentMasterUpdate,
    OtEquipmentMasterOut,
    # Procedures
    OtProcedureCreate,
    OtProcedureOut,
    OtProcedureUpdate,
)
from app.models.user import User

router = APIRouter(prefix="/ot", tags=["OT - Masters"])

# ============================================================
#  RBAC Helper
# ============================================================


def _need_any(user: User, codes: list[str]) -> None:
    """
    Basic permission helper.
    Admin (is_admin=True) bypasses checks.
    """
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


# ============================================================
#  OT SPECIALITIES
# ============================================================


@router.get("/specialities", response_model=List[OtSpecialityOut])
def list_ot_specialities(
        active: Optional[bool] = Query(
            None, description="Filter by active/inactive"),
        search: Optional[str] = Query(None,
                                      description="Search by code or name"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.specialities.view"])

    q = db.query(OtSpeciality)

    if active is not None:
        q = q.filter(OtSpeciality.is_active == active)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtSpeciality.code.ilike(like))
                     | (OtSpeciality.name.ilike(like)))

    q = q.order_by(OtSpeciality.is_active.desc(), OtSpeciality.name.asc())
    return q.all()


@router.get("/specialities/{speciality_id}", response_model=OtSpecialityOut)
def get_ot_speciality(
        speciality_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.specialities.view"])

    speciality = db.query(OtSpeciality).get(speciality_id)
    if not speciality:
        raise HTTPException(status_code=404, detail="OT Speciality not found")
    return speciality


@router.post(
    "/specialities",
    response_model=OtSpecialityOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_speciality(
        payload: OtSpecialityCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.specialities.create"])

    existing = (db.query(OtSpeciality).filter(
        OtSpeciality.code == payload.code).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail="OT Speciality code already exists",
        )

    speciality = OtSpeciality(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(speciality)
    db.commit()
    db.refresh(speciality)
    return speciality


@router.put("/specialities/{speciality_id}", response_model=OtSpecialityOut)
def update_ot_speciality(
        speciality_id: int,
        payload: OtSpecialityUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.specialities.update"])

    speciality = db.query(OtSpeciality).get(speciality_id)
    if not speciality:
        raise HTTPException(status_code=404, detail="OT Speciality not found")

    data = payload.model_dump(exclude_unset=True)

    # if code is changing, check uniqueness
    new_code = data.get("code")
    if new_code and new_code != speciality.code:
        exists = (db.query(OtSpeciality).filter(
            OtSpeciality.code == new_code).first())
        if exists:
            raise HTTPException(
                status_code=400,
                detail="Another OT Speciality with this code already exists",
            )

    for field, value in data.items():
        setattr(speciality, field, value)

    db.add(speciality)
    db.commit()
    db.refresh(speciality)
    return speciality


@router.delete(
    "/specialities/{speciality_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ot_speciality(
        speciality_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Soft delete: mark speciality inactive.
    """
    _need_any(user, ["ot.masters.delete", "ot.specialities.delete"])

    speciality = db.query(OtSpeciality).get(speciality_id)
    if not speciality:
        raise HTTPException(status_code=404, detail="OT Speciality not found")

    speciality.is_active = False
    db.add(speciality)
    db.commit()
    return None


# ============================================================
#  OT PROCEDURES MASTER
# ============================================================


@router.get(
    "/procedures",
    response_model=List[OtProcedureOut],
)
def list_ot_procedures(
        db: Session = Depends(get_db),
        user=Depends(current_user),
        search: Optional[str] = Query(None),
        speciality_id: Optional[int] = Query(None),
        is_active: Optional[bool] = Query(None),
        limit: int = Query(100, ge=1, le=500),
):
    _need_any(user, ["ot.masters.view", "ot.procedures.view"])

    q = db.query(OtProcedure)

    if search:
        pattern = f"%{search.strip()}%"
        q = q.filter((OtProcedure.name.ilike(pattern))
                     | (OtProcedure.code.ilike(pattern)))

    if speciality_id:
        q = q.filter(OtProcedure.speciality_id == speciality_id)

    if is_active is not None:
        q = q.filter(OtProcedure.is_active == is_active)

    q = q.order_by(OtProcedure.name.asc()).limit(limit)
    return q.all()


@router.get(
    "/procedures/{procedure_id}",
    response_model=OtProcedureOut,
)
def get_ot_procedure(
        procedure_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.procedures.view"])
    proc = db.query(OtProcedure).get(procedure_id)
    if not proc:
        raise HTTPException(status_code=404, detail="OT procedure not found")
    return proc


@router.post(
    "/procedures",
    response_model=OtProcedureOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_procedure(
        payload: OtProcedureCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.manage", "ot.procedures.create"])

    # enforce unique code
    existing = (db.query(OtProcedure).filter(
        OtProcedure.code == payload.code).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Procedure code already exists",
        )

    proc = OtProcedure(
        code=payload.code,
        name=payload.name,
        speciality_id=payload.speciality_id,
        default_duration_min=payload.default_duration_min,
        rate_per_hour=payload.rate_per_hour,
        description=payload.description,
        is_active=payload.is_active,
    )

    db.add(proc)
    db.commit()
    db.refresh(proc)
    return proc


@router.put(
    "/procedures/{procedure_id}",
    response_model=OtProcedureOut,
)
def update_ot_procedure(
        procedure_id: int,
        payload: OtProcedureUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.manage", "ot.procedures.update"])

    proc = db.query(OtProcedure).get(procedure_id)
    if not proc:
        raise HTTPException(status_code=404, detail="OT procedure not found")

    data = payload.model_dump(exclude_unset=True)

    # if code changed, ensure uniqueness
    new_code = data.get("code")
    if new_code and new_code != proc.code:
        existing = (db.query(OtProcedure).filter(
            OtProcedure.code == new_code).first())
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Another procedure with this code already exists",
            )

    for field, value in data.items():
        setattr(proc, field, value)

    db.add(proc)
    db.commit()
    db.refresh(proc)
    return proc


@router.delete(
    "/procedures/{procedure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ot_procedure(
        procedure_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.manage", "ot.procedures.delete"])

    proc = db.query(OtProcedure).get(procedure_id)
    if not proc:
        raise HTTPException(status_code=404, detail="OT procedure not found")

    db.delete(proc)
    db.commit()
    return None


# ============================================================
#  OT EQUIPMENT MASTER
# ============================================================


@router.get("/equipment", response_model=List[OtEquipmentMasterOut])
def list_ot_equipment_master(
        active: Optional[bool] = Query(None),
        search: Optional[str] = Query(
            None, description="Search by code, name or category"),
        critical: Optional[bool] = Query(
            None, description="Filter by critical equipment"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.equipment.view"])

    q = db.query(OtEquipmentMaster)

    if active is not None:
        q = q.filter(OtEquipmentMaster.is_active == active)

    if critical is not None:
        q = q.filter(OtEquipmentMaster.is_critical == critical)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtEquipmentMaster.name.ilike(like))
                     | (OtEquipmentMaster.code.ilike(like))
                     | (OtEquipmentMaster.category.ilike(like)))

    q = q.order_by(
        OtEquipmentMaster.is_active.desc(),
        OtEquipmentMaster.is_critical.desc(),
        OtEquipmentMaster.name.asc(),
    )
    return q.all()


@router.get("/equipment/{equipment_id}", response_model=OtEquipmentMasterOut)
def get_ot_equipment(
        equipment_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.equipment.view"])

    equipment = db.query(OtEquipmentMaster).get(equipment_id)
    if not equipment:
        raise HTTPException(status_code=404, detail="OT Equipment not found")
    return equipment


@router.post(
    "/equipment",
    response_model=OtEquipmentMasterOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_equipment(
        payload: OtEquipmentMasterCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.equipment.create"])

    existing = (db.query(OtEquipmentMaster).filter(
        OtEquipmentMaster.code == payload.code).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail="OT Equipment code already exists",
        )

    equipment = OtEquipmentMaster(
        code=payload.code,
        name=payload.name,
        category=payload.category,
        description=payload.description,
        is_critical=payload.is_critical,
        is_active=payload.is_active,
    )
    db.add(equipment)
    db.commit()
    db.refresh(equipment)
    return equipment


@router.put("/equipment/{equipment_id}", response_model=OtEquipmentMasterOut)
def update_ot_equipment(
        equipment_id: int,
        payload: OtEquipmentMasterUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.equipment.update"])

    equipment = db.query(OtEquipmentMaster).get(equipment_id)
    if not equipment:
        raise HTTPException(status_code=404, detail="OT Equipment not found")

    data = payload.model_dump(exclude_unset=True)
    new_code = data.get("code")

    if new_code and new_code != equipment.code:
        existing = (db.query(OtEquipmentMaster).filter(
            OtEquipmentMaster.code == new_code).first())
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Another OT Equipment with this code already exists",
            )

    for field, value in data.items():
        setattr(equipment, field, value)

    db.add(equipment)
    db.commit()
    db.refresh(equipment)
    return equipment


@router.delete(
    "/equipment/{equipment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ot_equipment(
        equipment_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Soft delete – mark inactive.
    """
    _need_any(user, ["ot.masters.delete", "ot.equipment.delete"])

    equipment = db.query(OtEquipmentMaster).get(equipment_id)
    if not equipment:
        raise HTTPException(status_code=404, detail="OT Equipment not found")

    equipment.is_active = False
    db.add(equipment)
    db.commit()
    return None
