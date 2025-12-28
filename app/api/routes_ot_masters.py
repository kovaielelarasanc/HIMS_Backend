# FILE: app/api/routes_ot_masters.py
from __future__ import annotations

from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.api.deps import get_db, current_user
from app.models.user import User

from app.models.ot import (
    OtSpeciality,
    OtEquipmentMaster,
    OtProcedure,
)

from app.models.ot_master import (
    OtSurgeryMaster,
    OtTheaterMaster,
    OtInstrumentMaster,
    OtDeviceMaster,
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

from app.schemas.ot_master import (
    OtSurgeryMasterIn,
    OtSurgeryMasterUpdate,
    OtSurgeryMasterOut,
    OtSurgeryMasterPageOut,
    OtTheaterMasterCreate,
    OtTheaterMasterUpdate,
    OtTheaterMasterOut,
    OtInstrumentMasterCreate,
    OtInstrumentMasterUpdate,
    OtInstrumentMasterOut,
    OtDeviceMasterCreate,
    OtDeviceMasterUpdate,
    OtDeviceMasterOut,
    
)

router = APIRouter(prefix="/ot", tags=["OT - Masters"])

DeviceCategory = Literal["AIRWAY", "MONITOR"]
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


def _norm(s: Optional[str]) -> Optional[str]:
    return s.strip() if isinstance(s, str) else s


# ============================================================
#  SURGERY MASTER (Legacy)
# ============================================================
@router.get("/surgeries", response_model=OtSurgeryMasterPageOut)
def list_ot_surgeries(
        q: Optional[str] = Query(None),
        active: Optional[bool] = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.surgeries.view"])

    qry = db.query(OtSurgeryMaster)
    if active is not None:
        qry = qry.filter(OtSurgeryMaster.active == active)

    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter((OtSurgeryMaster.code.ilike(like))
                         | (OtSurgeryMaster.name.ilike(like)))

    total = qry.count()
    items = (qry.order_by(OtSurgeryMaster.active.desc(),
                          OtSurgeryMaster.name.asc()).offset(
                              (page - 1) * page_size).limit(page_size).all())
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.post("/surgeries",
             response_model=OtSurgeryMasterOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_surgery(
        payload: OtSurgeryMasterIn,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.surgeries.create"])

    if db.query(OtSurgeryMaster).filter(
            OtSurgeryMaster.code == payload.code).first():
        raise HTTPException(400, "Surgery code already exists")
    if db.query(OtSurgeryMaster).filter(
            OtSurgeryMaster.name == payload.name).first():
        raise HTTPException(400, "Surgery name already exists")

    row = OtSurgeryMaster(
        code=payload.code.strip(),
        name=payload.name.strip(),
        default_cost=payload.default_cost or 0,
        hourly_cost=payload.hourly_cost or 0,
        description=payload.description or "",
        active=bool(payload.active),
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/surgeries/{surgery_id}", response_model=OtSurgeryMasterOut)
def update_ot_surgery(
        surgery_id: int,
        payload: OtSurgeryMasterUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.surgeries.update"])

    row = db.query(OtSurgeryMaster).get(surgery_id)
    if not row:
        raise HTTPException(404, "Surgery master not found")

    data = payload.model_dump(exclude_unset=True)

    new_code = data.get("code")
    if new_code and new_code != row.code:
        if db.query(OtSurgeryMaster).filter(
                OtSurgeryMaster.code == new_code).first():
            raise HTTPException(
                400, "Another surgery with this code already exists")

    new_name = data.get("name")
    if new_name and new_name != row.name:
        if db.query(OtSurgeryMaster).filter(
                OtSurgeryMaster.name == new_name).first():
            raise HTTPException(
                400, "Another surgery with this name already exists")

    for k, v in data.items():
        if k in ("code", "name") and isinstance(v, str):
            v = v.strip()
        setattr(row, k, v)

    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/surgeries/{surgery_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_surgery(
        surgery_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.delete", "ot.surgeries.delete"])
    row = db.query(OtSurgeryMaster).get(surgery_id)
    if not row:
        raise HTTPException(404, "Surgery master not found")
    row.active = False
    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    return None


# ============================================================
#  OT THEATERS (Hourly)
# ============================================================
@router.get("/theaters", response_model=List[OtTheaterMasterOut])
def list_ot_theaters(
        search: Optional[str] = Query(None),
        active: Optional[bool] = Query(None),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.theaters.view"])
    q = db.query(OtTheaterMaster)

    if active is not None:
        q = q.filter(OtTheaterMaster.is_active == active)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtTheaterMaster.code.ilike(like))
                     | (OtTheaterMaster.name.ilike(like)))

    return (q.order_by(OtTheaterMaster.is_active.desc(),
                       OtTheaterMaster.name.asc()).limit(limit).all())


@router.post("/theaters",
             response_model=OtTheaterMasterOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_theater(
        payload: OtTheaterMasterCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.theaters.create"])

    if db.query(OtTheaterMaster).filter(
            OtTheaterMaster.code == payload.code).first():
        raise HTTPException(400, "Theater code already exists")
    if db.query(OtTheaterMaster).filter(
            OtTheaterMaster.name == payload.name).first():
        raise HTTPException(400, "Theater name already exists")

    row = OtTheaterMaster(
        code=payload.code.strip(),
        name=payload.name.strip(),
        cost_per_hour=payload.cost_per_hour or 0,
        description=payload.description or "",
        is_active=payload.is_active,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/theaters/{theater_id}", response_model=OtTheaterMasterOut)
def update_ot_theater(
        theater_id: int,
        payload: OtTheaterMasterUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.theaters.update"])

    row = db.query(OtTheaterMaster).get(theater_id)
    if not row:
        raise HTTPException(404, "OT Theater not found")

    data = payload.model_dump(exclude_unset=True)

    new_code = data.get("code")
    if new_code and new_code != row.code:
        if db.query(OtTheaterMaster).filter(
                OtTheaterMaster.code == new_code).first():
            raise HTTPException(
                400, "Another theater with this code already exists")

    new_name = data.get("name")
    if new_name and new_name != row.name:
        if db.query(OtTheaterMaster).filter(
                OtTheaterMaster.name == new_name).first():
            raise HTTPException(
                400, "Another theater with this name already exists")

    for k, v in data.items():
        if k in ("code", "name") and isinstance(v, str):
            v = v.strip()
        setattr(row, k, v)

    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/theaters/{theater_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_theater(
        theater_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.delete", "ot.theaters.delete"])
    row = db.query(OtTheaterMaster).get(theater_id)
    if not row:
        raise HTTPException(404, "OT Theater not found")
    row.is_active = False
    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    return None


# ============================================================
#  OT INSTRUMENTS
# ============================================================
@router.get("/instruments", response_model=List[OtInstrumentMasterOut])
def list_ot_instruments(
        search: Optional[str] = Query(None),
        active: Optional[bool] = Query(None),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.instruments.view"])
    q = db.query(OtInstrumentMaster)

    if active is not None:
        q = q.filter(OtInstrumentMaster.is_active == active)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtInstrumentMaster.code.ilike(like))
                     | (OtInstrumentMaster.name.ilike(like)))

    return (q.order_by(OtInstrumentMaster.is_active.desc(),
                       OtInstrumentMaster.name.asc()).limit(limit).all())


@router.post("/instruments",
             response_model=OtInstrumentMasterOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_instrument(
        payload: OtInstrumentMasterCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.instruments.create"])

    if db.query(OtInstrumentMaster).filter(
            OtInstrumentMaster.code == payload.code).first():
        raise HTTPException(400, "Instrument code already exists")
    if db.query(OtInstrumentMaster).filter(
            OtInstrumentMaster.name == payload.name).first():
        raise HTTPException(400, "Instrument name already exists")

    row = OtInstrumentMaster(
        code=payload.code.strip(),
        name=payload.name.strip(),
        available_qty=payload.available_qty or 0,
        cost_per_qty=payload.cost_per_qty or 0,
        uom=(payload.uom or "Nos").strip(),
        description=payload.description or "",
        is_active=payload.is_active,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/instruments/{instrument_id}",
            response_model=OtInstrumentMasterOut)
def update_ot_instrument(
        instrument_id: int,
        payload: OtInstrumentMasterUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.instruments.update"])

    row = db.query(OtInstrumentMaster).get(instrument_id)
    if not row:
        raise HTTPException(404, "OT Instrument not found")

    data = payload.model_dump(exclude_unset=True)

    new_code = data.get("code")
    if new_code and new_code != row.code:
        if db.query(OtInstrumentMaster).filter(
                OtInstrumentMaster.code == new_code).first():
            raise HTTPException(
                400, "Another instrument with this code already exists")

    new_name = data.get("name")
    if new_name and new_name != row.name:
        if db.query(OtInstrumentMaster).filter(
                OtInstrumentMaster.name == new_name).first():
            raise HTTPException(
                400, "Another instrument with this name already exists")

    for k, v in data.items():
        if k in ("code", "name", "uom") and isinstance(v, str):
            v = v.strip()
        setattr(row, k, v)

    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/instruments/{instrument_id}",
               status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_instrument(
        instrument_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.delete", "ot.instruments.delete"])
    row = db.query(OtInstrumentMaster).get(instrument_id)
    if not row:
        raise HTTPException(404, "OT Instrument not found")
    row.is_active = False
    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    return None


# ============================================================
#  OT DEVICES (AIRWAY / MONITOR)
# ============================================================
@router.get("/device-masters", response_model=List[dict])
def list_device_masters(
        category: Optional[DeviceCategory] = Query(None),
        q: Optional[str] = Query(None),
        active: Optional[bool] = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(
        user,
        ["ot.masters.view", "ot.cases.view", "ot.anaesthesia_record.view"])

    qry = db.query(OtDeviceMaster)
    if category:
        qry = qry.filter(OtDeviceMaster.category == category)
    if active is not None:
        qry = qry.filter(OtDeviceMaster.is_active == active)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            func.lower(OtDeviceMaster.name).like(func.lower(like)))

    rows = qry.order_by(OtDeviceMaster.category.asc(),
                        OtDeviceMaster.name.asc()).all()
    return [{
        "id": r.id,
        "category": r.category,
        "code": r.code,
        "name": r.name,
        "cost": float(r.cost or 0),
        "description": r.description,
        "is_active": r.is_active,
    } for r in rows]


@router.get("/devices", response_model=List[OtDeviceMasterOut])
def list_ot_devices(
        category: Optional[str] = Query(None, description="AIRWAY or MONITOR"),
        search: Optional[str] = Query(None),
        active: Optional[bool] = Query(None),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.view", "ot.devices.view"])
    q = db.query(OtDeviceMaster)

    if category:
        q = q.filter(OtDeviceMaster.category == category.strip().upper())

    if active is not None:
        q = q.filter(OtDeviceMaster.is_active == active)

    if search:
        like = f"%{search.strip()}%"
        q = q.filter((OtDeviceMaster.code.ilike(like))
                     | (OtDeviceMaster.name.ilike(like)))

    return (q.order_by(OtDeviceMaster.is_active.desc(),
                       OtDeviceMaster.category.asc(),
                       OtDeviceMaster.name.asc()).limit(limit).all())


@router.post("/devices",
             response_model=OtDeviceMasterOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_device(
        payload: OtDeviceMasterCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.devices.create"])

    cat = payload.category.strip().upper()
    if cat not in ("AIRWAY", "MONITOR"):
        raise HTTPException(400, "Invalid category. Use AIRWAY or MONITOR")

    if db.query(OtDeviceMaster).filter(
            OtDeviceMaster.category == cat,
            OtDeviceMaster.code == payload.code).first():
        raise HTTPException(400,
                            "Device code already exists for this category")

    row = OtDeviceMaster(
        category=cat,
        code=payload.code.strip(),
        name=payload.name.strip(),
        cost=payload.cost or 0,
        description=payload.description or "",
        is_active=payload.is_active,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/devices/{device_id}", response_model=OtDeviceMasterOut)
def update_ot_device(
        device_id: int,
        payload: OtDeviceMasterUpdate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.update", "ot.devices.update"])

    row = db.query(OtDeviceMaster).get(device_id)
    if not row:
        raise HTTPException(404, "OT Device not found")

    data = payload.model_dump(exclude_unset=True)

    # normalize category
    if "category" in data and data["category"]:
        data["category"] = data["category"].strip().upper()
        if data["category"] not in ("AIRWAY", "MONITOR"):
            raise HTTPException(400, "Invalid category. Use AIRWAY or MONITOR")

    new_cat = data.get("category", row.category)
    new_code = data.get("code", row.code)

    # uniqueness on (category, code)
    if (new_cat != row.category) or (new_code != row.code):
        exists = db.query(OtDeviceMaster).filter(
            OtDeviceMaster.category == new_cat,
            OtDeviceMaster.code == new_code, OtDeviceMaster.id
            != row.id).first()
        if exists:
            raise HTTPException(
                400, "Another device with this category+code already exists")

    for k, v in data.items():
        if k in ("code", "name") and isinstance(v, str):
            v = v.strip()
        setattr(row, k, v)

    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ot_device(
        device_id: int,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.delete", "ot.devices.delete"])
    row = db.query(OtDeviceMaster).get(device_id)
    if not row:
        raise HTTPException(404, "OT Device not found")
    row.is_active = False
    row.updated_by = getattr(user, "id", None)
    db.add(row)
    db.commit()
    return None


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


@router.post("/specialities",
             response_model=OtSpecialityOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_speciality(
        payload: OtSpecialityCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.specialities.create"])

    existing = db.query(OtSpeciality).filter(
        OtSpeciality.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400,
                            detail="OT Speciality code already exists")

    speciality = OtSpeciality(
        code=payload.code.strip(),
        name=payload.name.strip(),
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

    new_code = data.get("code")
    if new_code and new_code != speciality.code:
        exists = db.query(OtSpeciality).filter(
            OtSpeciality.code == new_code).first()
        if exists:
            raise HTTPException(
                status_code=400,
                detail="Another OT Speciality with this code already exists")

    for field, value in data.items():
        if field in ("code", "name") and isinstance(value, str):
            value = value.strip()
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
    _need_any(user, ["ot.masters.delete", "ot.specialities.delete"])

    speciality = db.query(OtSpeciality).get(speciality_id)
    if not speciality:
        raise HTTPException(status_code=404, detail="OT Speciality not found")

    speciality.is_active = False
    db.add(speciality)
    db.commit()
    return None


# ============================================================
#  OT PROCEDURES MASTER (UPDATED create)
# ============================================================
@router.get("/procedures", response_model=List[OtProcedureOut])
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


@router.get("/procedures/{procedure_id}", response_model=OtProcedureOut)
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


@router.post("/procedures",
             response_model=OtProcedureOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_procedure(
        payload: OtProcedureCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.manage", "ot.procedures.create"])

    existing = db.query(OtProcedure).filter(
        OtProcedure.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400,
                            detail="Procedure code already exists")

    proc = OtProcedure(
        code=payload.code.strip(),
        name=payload.name.strip(),
        speciality_id=payload.speciality_id,
        default_duration_min=payload.default_duration_min,
        rate_per_hour=payload.rate_per_hour,
        description=payload.description,
        is_active=payload.is_active,

        # ✅ NEW cost split-up
        base_cost=payload.base_cost,
        anesthesia_cost=payload.anesthesia_cost,
        surgeon_cost=payload.surgeon_cost,
        petitory_cost=payload.petitory_cost,
        asst_doctor_cost=payload.asst_doctor_cost,
    )

    db.add(proc)
    db.commit()
    db.refresh(proc)
    return proc


@router.put("/procedures/{procedure_id}", response_model=OtProcedureOut)
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

    new_code = data.get("code")
    if new_code and new_code != proc.code:
        existing = db.query(OtProcedure).filter(
            OtProcedure.code == new_code).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Another procedure with this code already exists")

    for field, value in data.items():
        if field in ("code", "name") and isinstance(value, str):
            value = value.strip()
        setattr(proc, field, value)

    db.add(proc)
    db.commit()
    db.refresh(proc)
    return proc


@router.delete("/procedures/{procedure_id}",
               status_code=status.HTTP_204_NO_CONTENT)
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


@router.post("/equipment",
             response_model=OtEquipmentMasterOut,
             status_code=status.HTTP_201_CREATED)
def create_ot_equipment(
        payload: OtEquipmentMasterCreate,
        db: Session = Depends(get_db),
        user=Depends(current_user),
):
    _need_any(user, ["ot.masters.create", "ot.equipment.create"])

    existing = db.query(OtEquipmentMaster).filter(
        OtEquipmentMaster.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400,
                            detail="OT Equipment code already exists")

    equipment = OtEquipmentMaster(
        code=payload.code.strip(),
        name=payload.name.strip(),
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
        existing = db.query(OtEquipmentMaster).filter(
            OtEquipmentMaster.code == new_code).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Another OT Equipment with this code already exists")

    for field, value in data.items():
        if field in ("code", "name", "category") and isinstance(value, str):
            value = value.strip()
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
    _need_any(user, ["ot.masters.delete", "ot.equipment.delete"])

    equipment = db.query(OtEquipmentMaster).get(equipment_id)
    if not equipment:
        raise HTTPException(status_code=404, detail="OT Equipment not found")

    equipment.is_active = False
    db.add(equipment)
    db.commit()
    return None
