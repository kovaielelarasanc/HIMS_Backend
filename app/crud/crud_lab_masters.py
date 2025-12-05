# FILE: app/crud/crud_lab_masters.py
from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.lis import LabDepartment, LabService
from app.schemas.lab_masters import (
    LabDepartmentCreate,
    LabDepartmentUpdate,
    LabServiceCreate,
    LabServiceUpdate,
    LabServiceBulkCreateItem,
)

# ---------------- Departments ----------------


def list_lab_departments(db: Session, *, active_only: bool = True):
    q = db.query(LabDepartment)
    if active_only:
        q = q.filter(LabDepartment.is_active.is_(True))

    # MySQL-safe ordering
    return q.order_by(
        LabDepartment.display_order.asc(),
        LabDepartment.name.asc(),
    ).all()


def create_lab_department(db: Session, data: LabDepartmentCreate):
    obj = LabDepartment(**data.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_lab_department(db: Session, dept_id: int,
                          data: LabDepartmentUpdate):
    obj = db.query(LabDepartment).filter(LabDepartment.id == dept_id).first()
    if not obj:
        return None
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    db.commit()
    db.refresh(obj)
    return obj


def soft_delete_lab_department(db: Session, dept_id: int):
    obj = db.query(LabDepartment).filter(LabDepartment.id == dept_id).first()
    if not obj:
        return False
    obj.is_active = False
    db.commit()
    return True


# ---------------- Services ----------------


def _normalize(unit: Optional[str], normal: Optional[str]):
    unit = (unit or "").strip()
    normal = (normal or "").strip()
    return (unit or "-"), (normal or "-")


def list_lab_services(
    db: Session,
    *,
    department_id: Optional[int] = None,
    search: Optional[str] = None,
    active_only: bool = True,
):
    q = db.query(LabService)

    if department_id:
        q = q.filter(LabService.department_id == department_id)
    if search:
        q = q.filter(LabService.name.ilike(f"%{search}%"))
    if active_only:
        q = q.filter(LabService.is_active.is_(True))

    return q.order_by(
        LabService.display_order.asc(),
        LabService.name.asc(),
    ).all()


def create_lab_service(db: Session, data: LabServiceCreate):
    unit, normal = _normalize(data.unit, data.normal_range)
    payload = data.model_dump()
    payload["unit"] = unit
    payload["normal_range"] = normal

    obj = LabService(**payload)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def bulk_create_lab_services(db: Session,
                             items: List[LabServiceBulkCreateItem]):
    created = []
    for item in items:
        if not item.name.strip():
            continue

        unit, normal = _normalize(item.unit, item.normal_range)
        payload = item.model_dump()
        payload["unit"] = unit
        payload["normal_range"] = normal

        obj = LabService(**payload)
        db.add(obj)
        created.append(obj)

    db.commit()
    for obj in created:
        db.refresh(obj)

    return created


def update_lab_service(db: Session, service_id: int, data: LabServiceUpdate):
    obj = db.query(LabService).filter(LabService.id == service_id).first()
    if not obj:
        return None

    payload = data.model_dump(exclude_unset=True)

    if "unit" in payload or "normal_range" in payload:
        unit = payload.get("unit", obj.unit)
        normal = payload.get("normal_range", obj.normal_range)
        payload["unit"], payload["normal_range"] = _normalize(unit, normal)

    for key, value in payload.items():
        setattr(obj, key, value)

    db.commit()
    db.refresh(obj)
    return obj


def soft_delete_lab_service(db: Session, service_id: int):
    obj = db.query(LabService).filter(LabService.id == service_id).first()
    if not obj:
        return False
    obj.is_active = False
    db.commit()
    return True
