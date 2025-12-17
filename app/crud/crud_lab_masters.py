# FILE: app/crud/crud_lab_masters.py
from __future__ import annotations

from typing import List, Optional
import re
import html as _html

from sqlalchemy.orm import Session

from app.models.lis import LabDepartment, LabService
from app.schemas.lab_masters import (
    LabDepartmentCreate,
    LabDepartmentUpdate,
    LabServiceCreate,
    LabServiceUpdate,
    LabServiceBulkCreateItem,
)

# Only strip real HTML tags like <b> </p> etc.
# This will NOT remove values like "< 5.00" or "<= 10"
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

MAX_NORMAL_RANGE_LEN = 60000
MAX_UNIT_LEN = 64


def _strip_html_tags_if_present(s: str) -> str:
    if not s:
        return s
    if _HTML_TAG_RE.search(s):
        s = _HTML_TAG_RE.sub("", s)
        s = _html.unescape(s)
    return s


def _clean_multiline_text(s: str) -> str:
    # normalize newlines + trim outside
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _strip_html_tags_if_present(s).strip()
    # keep user line breaks, just remove trailing spaces
    lines = [ln.rstrip() for ln in s.split("\n")]
    # trim empty start/end lines
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(lines).strip()


def _normalize(unit: Optional[str], normal: Optional[str]):
    unit = (unit or "").strip()
    normal = _clean_multiline_text(normal or "")

    if not unit:
        unit = "-"
    if not normal:
        normal = "-"

    # protect DB (VARCHAR sizes)
    unit = unit[:MAX_UNIT_LEN]
    normal = normal[:MAX_NORMAL_RANGE_LEN]

    return unit, normal


# ---------------- Departments ----------------

def list_lab_departments(db: Session, *, active_only: bool = True):
    q = db.query(LabDepartment)
    if active_only:
        q = q.filter(LabDepartment.is_active.is_(True))

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


def update_lab_department(db: Session, dept_id: int, data: LabDepartmentUpdate):
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
        # MySQL: ilike works via lower() emulation in SQLAlchemy
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


def bulk_create_lab_services(db: Session, items: List[LabServiceBulkCreateItem]):
    created = []
    for item in items:
        if not (item.name or "").strip():
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

    # normalize if unit/normal_range included
    if "unit" in payload or "normal_range" in payload:
        unit = payload.get("unit", obj.unit)
        normal = payload.get("normal_range", obj.normal_range)
        payload["unit"], payload["normal_range"] = _normalize(unit, normal)

    # name trim
    if "name" in payload and payload["name"] is not None:
        payload["name"] = payload["name"].strip()

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
