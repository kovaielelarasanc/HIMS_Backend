# FILE: app/api/routes_patient_types.py
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import PatientType
from app.schemas.patient import (
    PatientTypeCreate,
    PatientTypeUpdate,
    PatientTypeOut,
)
from app.services.audit_logger import log_audit  # adjust if your pkg is `services`

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def get_request_meta(request: Request):
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


def instance_to_audit_dict(obj):
    if obj is None:
        return {}
    data = {}
    for col in obj.__table__.columns:  # type: ignore[attr-defined]
        val = getattr(obj, col.name)
        from datetime import date, datetime as dt

        if isinstance(val, (date, dt)):
            val = val.isoformat()
        data[col.name] = val
    return data


@router.get("", response_model=List[PatientTypeOut])
@router.get("/", response_model=List[PatientTypeOut])
def list_patient_types(
        include_inactive: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "patients.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    qry = db.query(PatientType)
    if not include_inactive:
        qry = qry.filter(PatientType.is_active.is_(True))

    return (qry.order_by(PatientType.sort_order.asc(),
                         PatientType.name.asc()).all())


@router.post("", response_model=PatientTypeOut)
@router.post("/", response_model=PatientTypeOut)
def create_patient_type(
        payload: PatientTypeCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
        request: Request = None,
):
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    pt = PatientType(
        code=payload.code.strip(),
        name=payload.name.strip(),
        description=payload.description,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    db.add(pt)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        msg = str(e.orig).lower()
        if "code" in msg:
            raise HTTPException(status_code=400,
                                detail="Patient type code already exists")
        if "name" in msg:
            raise HTTPException(status_code=400,
                                detail="Patient type name already exists")
        raise
    db.refresh(pt)

    # --- Audit log (CREATE patient type) ---
    ip, ua = get_request_meta(request)
    new_data = instance_to_audit_dict(pt)
    log_audit(
        db=db,
        user_id=user.id,
        action="CREATE",
        table_name="patient_types",
        record_id=pt.id,
        old_values=None,
        new_values=new_data,
        ip_address=ip,
        user_agent=ua,
    )

    return pt


@router.put("/{pt_id}", response_model=PatientTypeOut)
def update_patient_type(
        pt_id: int,
        payload: PatientTypeUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
        request: Request = None,
):
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    pt = db.query(PatientType).get(pt_id)
    if not pt:
        raise HTTPException(status_code=404, detail="Not found")

    old_data = instance_to_audit_dict(pt)

    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(pt, k, v)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        msg = str(e.orig).lower()
        if "code" in msg:
            raise HTTPException(status_code=400,
                                detail="Patient type code already exists")
        if "name" in msg:
            raise HTTPException(status_code=400,
                                detail="Patient type name already exists")
        raise
    db.refresh(pt)

    # --- Audit log (UPDATE patient type) ---
    ip, ua = get_request_meta(request)
    new_data = instance_to_audit_dict(pt)
    log_audit(
        db=db,
        user_id=user.id,
        action="UPDATE",
        table_name="patient_types",
        record_id=pt.id,
        old_values=old_data,
        new_values=new_data,
        ip_address=ip,
        user_agent=ua,
    )

    return pt


@router.delete("/{pt_id}")
def deactivate_patient_type(
        pt_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
        request: Request = None,
):
    """
    Soft delete: mark is_active = False.
    """
    if not has_perm(user, "patients.update"):
        raise HTTPException(status_code=403, detail="Not permitted")

    pt = db.query(PatientType).get(pt_id)
    if not pt:
        raise HTTPException(status_code=404, detail="Not found")

    old_data = instance_to_audit_dict(pt)

    pt.is_active = False
    db.commit()
    db.refresh(pt)

    # --- Audit log (DELETE patient type) ---
    ip, ua = get_request_meta(request)
    new_data = instance_to_audit_dict(pt)
    log_audit(
        db=db,
        user_id=user.id,
        action="DELETE",
        table_name="patient_types",
        record_id=pt.id,
        old_values=old_data,
        new_values=new_data,
        ip_address=ip,
        user_agent=ua,
    )

    return {"message": "Deactivated"}
