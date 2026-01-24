# FILE: app/services/emr_template_library_service.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.models.emr_all import EmrSectionLibrary
from app.models.emr_template_library import EmrTemplateBlock


def _norm(v: str) -> str:
    return (v or "").strip().upper().replace(" ", "_")


def _loads(s: Optional[str], default):
    try:
        return json.loads(s or "")
    except Exception:
        return default


def _dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


# ------------------------
# Sections
# ------------------------
def section_list(
    db: Session,
    *,
    q: str = "",
    dept_code: str = "ALL",
    record_type_code: str = "ALL",
    active: Optional[bool] = True,
) -> List[EmrSectionLibrary]:
    qry = db.query(EmrSectionLibrary)

    if active is not None:
        qry = qry.filter(EmrSectionLibrary.is_active.is_(bool(active)))

    if dept_code and dept_code.upper() != "ALL":
        qry = qry.filter(or_(EmrSectionLibrary.dept_code == _norm(dept_code), EmrSectionLibrary.dept_code.is_(None)))

    if record_type_code and record_type_code.upper() != "ALL":
        qry = qry.filter(or_(EmrSectionLibrary.record_type_code == _norm(record_type_code), EmrSectionLibrary.record_type_code.is_(None)))

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrSectionLibrary.label.ilike(qq), EmrSectionLibrary.code.ilike(qq)))

    return qry.order_by(EmrSectionLibrary.display_order.asc(), EmrSectionLibrary.label.asc()).all()


def section_create(db: Session, *, payload: Dict[str, Any]) -> EmrSectionLibrary:
    code = _norm(payload.get("code") or "")
    label = (payload.get("label") or "").strip()
    if not code or not label:
        raise HTTPException(status_code=422, detail="code and label are required")

    exists = db.query(EmrSectionLibrary.id).filter(func.upper(EmrSectionLibrary.code) == code).first()
    if exists:
        raise HTTPException(status_code=409, detail="Section code already exists")

    row = EmrSectionLibrary(
        code=code,
        label=label,
        dept_code=_norm(payload["dept_code"]) if payload.get("dept_code") else None,
        record_type_code=_norm(payload["record_type_code"]) if payload.get("record_type_code") else None,
        group=(payload.get("group") or None),
        is_active=bool(payload.get("is_active", True)),
        display_order=int(payload.get("display_order") or 1000),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def section_update(db: Session, *, section_id: int, payload: Dict[str, Any]) -> EmrSectionLibrary:
    row = db.query(EmrSectionLibrary).filter(EmrSectionLibrary.id == int(section_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Section not found")

    for k in ["label", "group"]:
        if payload.get(k) is not None:
            v = payload.get(k)
            setattr(row, k, (str(v).strip() if isinstance(v, str) else v))

    for k in ["dept_code", "record_type_code"]:
        if payload.get(k) is not None:
            v = payload.get(k)
            setattr(row, k, (_norm(v) if isinstance(v, str) and v.strip() else None))

    if payload.get("is_active") is not None:
        row.is_active = bool(payload.get("is_active"))

    if payload.get("display_order") is not None:
        row.display_order = int(payload.get("display_order") or 1000)

    db.commit()
    db.refresh(row)
    return row


def section_delete(db: Session, *, section_id: int) -> None:
    row = db.query(EmrSectionLibrary).filter(EmrSectionLibrary.id == int(section_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Section not found")
    db.delete(row)
    db.commit()


# ------------------------
# Blocks
# ------------------------
def block_list(
    db: Session,
    *,
    q: str = "",
    dept_code: str = "ALL",
    record_type_code: str = "ALL",
    category: str = "",
    active: Optional[bool] = True,
) -> List[EmrTemplateBlock]:
    qry = db.query(EmrTemplateBlock)

    if active is not None:
        qry = qry.filter(EmrTemplateBlock.is_active.is_(bool(active)))

    if dept_code and dept_code.upper() != "ALL":
        qry = qry.filter(or_(EmrTemplateBlock.dept_code == _norm(dept_code), EmrTemplateBlock.dept_code.is_(None)))

    if record_type_code and record_type_code.upper() != "ALL":
        qry = qry.filter(or_(EmrTemplateBlock.record_type_code == _norm(record_type_code), EmrTemplateBlock.record_type_code.is_(None)))

    if category:
        qry = qry.filter(func.upper(EmrTemplateBlock.category) == category.strip().upper())

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrTemplateBlock.label.ilike(qq), EmrTemplateBlock.code.ilike(qq), EmrTemplateBlock.description.ilike(qq)))

    return qry.order_by(EmrTemplateBlock.display_order.asc(), EmrTemplateBlock.label.asc()).all()


def block_create(db: Session, *, payload: Dict[str, Any]) -> EmrTemplateBlock:
    code = _norm(payload.get("code") or "")
    label = (payload.get("label") or "").strip()
    if not code or not label:
        raise HTTPException(status_code=422, detail="code and label are required")

    exists = db.query(EmrTemplateBlock.id).filter(func.upper(EmrTemplateBlock.code) == code).first()
    if exists:
        raise HTTPException(status_code=409, detail="Block code already exists")

    row = EmrTemplateBlock(
        code=code,
        label=label,
        description=(payload.get("description") or None),
        dept_code=_norm(payload["dept_code"]) if payload.get("dept_code") else None,
        record_type_code=_norm(payload["record_type_code"]) if payload.get("record_type_code") else None,
        category=(payload.get("category") or None),
        tags_json=(payload.get("tags_json") or "[]"),
        schema_json=(payload.get("schema_json") or "{}"),
        preview_json=(payload.get("preview_json") or None),
        is_active=bool(payload.get("is_active", True)),
        is_system=bool(payload.get("is_system", True)),
        display_order=int(payload.get("display_order") or 1000),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def block_update(db: Session, *, block_id: int, payload: Dict[str, Any]) -> EmrTemplateBlock:
    row = db.query(EmrTemplateBlock).filter(EmrTemplateBlock.id == int(block_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Block not found")

    for k in ["label", "description", "category"]:
        if payload.get(k) is not None:
            v = payload.get(k)
            setattr(row, k, (str(v).strip() if isinstance(v, str) else v))

    for k in ["dept_code", "record_type_code"]:
        if payload.get(k) is not None:
            v = payload.get(k)
            setattr(row, k, (_norm(v) if isinstance(v, str) and v.strip() else None))

    if payload.get("tags") is not None:
        row.tags_json = _dumps([t.strip() for t in (payload.get("tags") or []) if t and str(t).strip()])

    if payload.get("schema_json") is not None:
        row.schema_json = _dumps(payload.get("schema_json") or {})

    if payload.get("preview_json") is not None:
        row.preview_json = _dumps(payload.get("preview_json") or {})

    if payload.get("is_active") is not None:
        row.is_active = bool(payload.get("is_active"))

    if payload.get("is_system") is not None:
        row.is_system = bool(payload.get("is_system"))

    if payload.get("display_order") is not None:
        row.display_order = int(payload.get("display_order") or 1000)

    db.commit()
    db.refresh(row)
    return row


def block_delete(db: Session, *, block_id: int) -> None:
    row = db.query(EmrTemplateBlock).filter(EmrTemplateBlock.id == int(block_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Block not found")
    db.delete(row)
    db.commit()


# ------------------------
# Suggest (doctor-friendly)
# ------------------------
def suggest_for(
    *,
    dept_code: str,
    record_type_code: str,
) -> Dict[str, Any]:
    """
    Returns recommended sections + core blocks for “wizard”.
    Frontend can show: Suggested layout -> doctors can accept/customize.
    """
    rt = _norm(record_type_code)
    dept = _norm(dept_code)

    common = {
        "sections": ["PATIENT_HEADER", "VITALS", "SOAP", "DIAGNOSIS", "MEDICATIONS", "INVESTIGATIONS", "ADVICE", "SIGNATURES", "ATTACHMENTS"],
        "blocks": ["BLOCK_VITALS", "BLOCK_SOAP_MINI", "BLOCK_DIAGNOSIS_LIST", "BLOCK_MEDICATION_LIST", "BLOCK_SIGNATURES", "BLOCK_ATTACHMENTS"],
    }

    if rt == "NURSING_NOTE":
        return {
            "dept_code": dept,
            "record_type_code": rt,
            "sections": ["PATIENT_HEADER", "VITALS", "NURSING_ASSESSMENT", "PAIN", "FALL_RISK", "BRADEN", "INTAKE_OUTPUT", "SIGNATURES"],
            "blocks": ["BLOCK_VITALS", "BLOCK_NURSING_ASSESSMENT_BASIC", "BLOCK_PAIN_SCALE", "BLOCK_FALL_RISK_SIMPLE", "BLOCK_BRADEN_SCALE", "BLOCK_INTAKE_OUTPUT", "BLOCK_SIGNATURES"],
        }

    if rt == "DISCHARGE_SUMMARY":
        return {
            "dept_code": dept,
            "record_type_code": rt,
            "sections": ["PATIENT_HEADER", "DIAGNOSIS", "HOSPITAL_COURSE", "PROCEDURES", "DISCHARGE_MEDICATIONS", "DISCHARGE_INSTRUCTIONS", "FOLLOW_UP", "SIGNATURES", "ATTACHMENTS"],
            "blocks": ["BLOCK_DIAGNOSIS_LIST", "BLOCK_PROCEDURES_TABLE", "BLOCK_DISCHARGE_MEDICATIONS", "BLOCK_DISCHARGE_INSTRUCTIONS", "BLOCK_SIGNATURES", "BLOCK_ATTACHMENTS"],
        }

    if rt == "ANC_NOTE":
        return {
            "dept_code": dept,
            "record_type_code": rt,
            "sections": ["PATIENT_HEADER", "ANC_PROFILE", "MENSTRUAL_HISTORY", "OB_HISTORY", "VITALS", "FETAL_ASSESSMENT", "PLAN", "SIGNATURES"],
            "blocks": ["BLOCK_ANC_PROFILE", "BLOCK_MENSTRUAL_HISTORY", "BLOCK_OB_HISTORY_GTPAL", "BLOCK_VITALS", "BLOCK_FETAL_ASSESSMENT", "BLOCK_SIGNATURES"],
        }

    if rt == "OT_NOTE":
        return {
            "dept_code": dept,
            "record_type_code": rt,
            "sections": ["PATIENT_HEADER", "OT_CHECKLIST", "ANESTHESIA", "PROCEDURE_NOTE", "COUNTS", "POST_OP_ORDERS", "SIGNATURES", "ATTACHMENTS"],
            "blocks": ["BLOCK_OT_WHO_CHECKLIST", "BLOCK_ANESTHESIA_RECORD_BASIC", "BLOCK_PROCEDURES_TABLE", "BLOCK_SIGNATURES", "BLOCK_ATTACHMENTS"],
        }

    return {"dept_code": dept, "record_type_code": rt, **common}
