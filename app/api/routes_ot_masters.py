# FILE: app/api/routes_ot_masters.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.ot import (
    OtSpeciality,
    OtTheatre,
    OtEquipmentMaster,
    OtEnvironmentSetting,
)
from app.schemas.ot import (
    # Speciality
    OtSpecialityCreate,
    OtSpecialityUpdate,
    OtSpecialityOut,
    # Theatre
    OtTheatreCreate,
    OtTheatreUpdate,
    OtTheatreOut,
    # Equipment
    OtEquipmentMasterCreate,
    OtEquipmentMasterUpdate,
    OtEquipmentMasterOut,
    # Environment
    OtEnvironmentSettingCreate,
    OtEnvironmentSettingUpdate,
    OtEnvironmentSettingOut,
)
from app.models.user import User


router = APIRouter(prefix="/ot", tags=["OT - Masters"])

# ============================================================
#  OT SPECIALITIES
# ============================================================


# ---------------- RBAC ----------------
def _need_any(user: User, codes: list[str]) -> None:
    if getattr(user, "is_admin", False):
        return
    have = {p.code for r in (user.roles or []) for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(403, "Not permitted")


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

    # if code is changing, check uniqueness
    data = payload.model_dump(exclude_unset=True)
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


@router.delete("/specialities/{speciality_id}",
               status_code=status.HTTP_204_NO_CONTENT)
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
#  OT THEATRES
# ============================================================


@router.get("/theatres", response_model=List[OtTheatreOut])
def list_ot_theatres(
        active: Optional[bool] = Query(None),
        search: Optional[str] = Query(
            None, description="Search by code, name or location"),
        speciality_id: Optional[int] = Query(
            None, description="Filter by speciality_id"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.theatres.view"])

    q = db.query(OtTheatre)

    if active is not None:
        q = q.filter(OtTheatre.is_active == active)

    if speciality_id is not None:
        q = q.filter(OtTheatre.speciality_id == speciality_id)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtTheatre.name.ilike(like))
                     | (OtTheatre.code.ilike(like))
                     | (OtTheatre.location.ilike(like)))

    q = q.order_by(OtTheatre.is_active.desc(), OtTheatre.name.asc())
    return q.all()


@router.get("/theatres/{theatre_id}", response_model=OtTheatreOut)
def get_ot_theatre(
        theatre_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.theatres.view"])

    theatre = db.query(OtTheatre).get(theatre_id)
    if not theatre:
        raise HTTPException(status_code=404, detail="OT Theatre not found")
    return theatre


@router.post(
    "/theatres",
    response_model=OtTheatreOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_theatre(
        payload: OtTheatreCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.theatres.create"])

    existing = (db.query(OtTheatre).filter(
        OtTheatre.code == payload.code).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail="OT Theatre code already exists",
        )

    theatre = OtTheatre(
        code=payload.code,
        name=payload.name,
        location=payload.location,
        speciality_id=payload.speciality_id,
        is_active=payload.is_active,
    )
    db.add(theatre)
    db.commit()
    db.refresh(theatre)
    return theatre


@router.put("/theatres/{theatre_id}", response_model=OtTheatreOut)
def update_ot_theatre(
        theatre_id: int,
        payload: OtTheatreUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.theatres.update"])

    theatre = db.query(OtTheatre).get(theatre_id)
    if not theatre:
        raise HTTPException(status_code=404, detail="OT Theatre not found")

    data = payload.model_dump(exclude_unset=True)
    new_code = data.get("code")

    if new_code and new_code != theatre.code:
        existing = (db.query(OtTheatre).filter(
            OtTheatre.code == new_code).first())
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Another OT Theatre with this code already exists",
            )

    for field, value in data.items():
        setattr(theatre, field, value)

    db.add(theatre)
    db.commit()
    db.refresh(theatre)
    return theatre


@router.delete("/theatres/{theatre_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_theatre(
        theatre_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    Soft delete – mark inactive instead of hard delete.
    """
    _need_any(user, ["ot.masters.delete", "ot.theatres.delete"])

    theatre = db.query(OtTheatre).get(theatre_id)
    if not theatre:
        raise HTTPException(status_code=404, detail="OT Theatre not found")

    theatre.is_active = False
    db.add(theatre)
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


@router.delete("/equipment/{equipment_id}",
               status_code=status.HTTP_204_NO_CONTENT)
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


# ============================================================
#  OT ENVIRONMENT SETTINGS
# ============================================================


@router.get(
    "/environment-settings",
    response_model=List[OtEnvironmentSettingOut],
)
def list_ot_environment_settings(
        theatre_id: Optional[int] = Query(None,
                                          description="Filter by theatre_id"),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.environment.view"])

    q = db.query(OtEnvironmentSetting)

    if theatre_id is not None:
        q = q.filter(OtEnvironmentSetting.theatre_id == theatre_id)

    q = q.order_by(OtEnvironmentSetting.theatre_id.asc(),
                   OtEnvironmentSetting.id.asc())
    return q.all()


@router.get(
    "/environment-settings/{setting_id}",
    response_model=OtEnvironmentSettingOut,
)
def get_ot_environment_setting(
        setting_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.environment.view"])

    setting = db.query(OtEnvironmentSetting).get(setting_id)
    if not setting:
        raise HTTPException(status_code=404,
                            detail="OT Environment setting not found")
    return setting


@router.post(
    "/environment-settings",
    response_model=OtEnvironmentSettingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ot_environment_setting(
        payload: OtEnvironmentSettingCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    """
    One environment setting per theatre is recommended.
    """
    _need_any(user, ["ot.masters.create", "ot.environment.create"])

    existing = (db.query(OtEnvironmentSetting).filter(
        OtEnvironmentSetting.theatre_id == payload.theatre_id).first())
    if existing:
        raise HTTPException(
            status_code=400,
            detail=
            "Environment setting already exists for this theatre. Use update.",
        )

    setting = OtEnvironmentSetting(
        theatre_id=payload.theatre_id,
        min_temperature_c=payload.min_temperature_c,
        max_temperature_c=payload.max_temperature_c,
        min_humidity_percent=payload.min_humidity_percent,
        max_humidity_percent=payload.max_humidity_percent,
        min_pressure_diff_pa=payload.min_pressure_diff_pa,
        max_pressure_diff_pa=payload.max_pressure_diff_pa,
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


@router.put(
    "/environment-settings/{setting_id}",
    response_model=OtEnvironmentSettingOut,
)
def update_ot_environment_setting(
        setting_id: int,
        payload: OtEnvironmentSettingUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.environment.update"])

    setting = db.query(OtEnvironmentSetting).get(setting_id)
    if not setting:
        raise HTTPException(status_code=404,
                            detail="OT Environment setting not found")

    data = payload.model_dump(exclude_unset=True)

    # If theatre_id is changing, check there is not another setting for that theatre
    new_theatre_id = data.get("theatre_id")
    if new_theatre_id and new_theatre_id != setting.theatre_id:
        existing = (db.query(OtEnvironmentSetting).filter(
            OtEnvironmentSetting.theatre_id == new_theatre_id).first())
        if existing:
            raise HTTPException(
                status_code=400,
                detail=
                "Another environment setting already exists for this theatre",
            )

    for field, value in data.items():
        setattr(setting, field, value)

    db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


@router.delete(
    "/environment-settings/{setting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ot_environment_setting(
        setting_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.delete", "ot.environment.delete"])

    setting = db.query(OtEnvironmentSetting).get(setting_id)
    if not setting:
        raise HTTPException(status_code=404,
                            detail="OT Environment setting not found")

    db.delete(setting)
    db.commit()
    return None
