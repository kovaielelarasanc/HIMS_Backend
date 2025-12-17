# FILE: app/api/routes_lis_masters.py
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db
from app.api.deps_permissions import require_permission

from app.schemas.lab_masters import (
    LabDepartmentOut,
    LabDepartmentCreate,
    LabDepartmentUpdate,
    LabServiceOut,
    LabServiceCreate,
    LabServiceUpdate,
    LabServiceBulkCreateRequest,
)
from app.crud import crud_lab_masters

router = APIRouter(prefix="/lis/masters", tags=["LIS Masters"])


# ---------- Departments ----------

@router.get(
    "/departments",
    response_model=List[LabDepartmentOut],
    dependencies=[Depends(require_permission("lis.masters.departments.view"))],
)
def list_departments(
    db: Session = Depends(get_db),
    active_only: bool = Query(True),
):
    return crud_lab_masters.list_lab_departments(db, active_only=active_only)


@router.post(
    "/departments",
    response_model=LabDepartmentOut,
    dependencies=[Depends(require_permission("lis.masters.departments.create"))],
)
def create_department(
    data: LabDepartmentCreate,
    db: Session = Depends(get_db),
):
    return crud_lab_masters.create_lab_department(db, data)


@router.put(
    "/departments/{dept_id}",
    response_model=LabDepartmentOut,
    dependencies=[Depends(require_permission("lis.masters.departments.update"))],
)
def update_department(
    dept_id: int,
    data: LabDepartmentUpdate,
    db: Session = Depends(get_db),
):
    obj = crud_lab_masters.update_lab_department(db, dept_id, data)
    if not obj:
        raise HTTPException(status_code=404, detail="Department not found")
    return obj


@router.delete(
    "/departments/{dept_id}",
    dependencies=[Depends(require_permission("lis.masters.departments.delete"))],
)
def delete_department(
    dept_id: int,
    db: Session = Depends(get_db),
):
    ok = crud_lab_masters.soft_delete_lab_department(db, dept_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Department not found")
    return {"success": True}


# ---------- Services ----------

@router.get(
    "/services",
    response_model=List[LabServiceOut],
    dependencies=[Depends(require_permission("lis.masters.services.view"))],
)
def list_services(
    db: Session = Depends(get_db),
    department_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    active_only: bool = Query(True),
):
    return crud_lab_masters.list_lab_services(
        db,
        department_id=department_id,
        search=search,
        active_only=active_only,
    )


@router.post(
    "/services",
    response_model=LabServiceOut,
    dependencies=[Depends(require_permission("lis.masters.services.create"))],
)
def create_service(
    data: LabServiceCreate,
    db: Session = Depends(get_db),
):
    try:
        return crud_lab_masters.create_lab_service(db, data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Service name already exists in this department",
        )


@router.post(
    "/services/bulk",
    response_model=List[LabServiceOut],
    dependencies=[Depends(require_permission("lis.masters.services.create"))],
)
def bulk_create_services(
    payload: LabServiceBulkCreateRequest,
    db: Session = Depends(get_db),
):
    try:
        return crud_lab_masters.bulk_create_lab_services(db, payload.items)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Duplicate service name found in bulk upload for same department",
        )


@router.put(
    "/services/{service_id}",
    response_model=LabServiceOut,
    dependencies=[Depends(require_permission("lis.masters.services.update"))],
)
def update_service(
    service_id: int,
    payload: LabServiceUpdate,
    db: Session = Depends(get_db),
):
    try:
        obj = crud_lab_masters.update_lab_service(db, service_id, payload)
        if not obj:
            raise HTTPException(status_code=404, detail="Service not found")
        return obj
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Service name already exists in this department",
        )


@router.delete(
    "/services/{service_id}",
    dependencies=[Depends(require_permission("lis.masters.services.delete"))],
)
def delete_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    ok = crud_lab_masters.soft_delete_lab_service(db, service_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Service not found")
    return {"success": True}
