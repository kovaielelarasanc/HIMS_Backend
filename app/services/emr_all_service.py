# FILE: app/services/emr_all_service.py
from __future__ import annotations

import json
import hashlib
import secrets
from datetime import datetime, timedelta, date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable
from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func, desc
from sqlalchemy.exc import IntegrityError

from app.models.emr_all import (
    EmrDepartment,
    EmrRecordType,
    EmrTemplate,
    EmrTemplateVersion,
    EmrTemplateStatus,
    EmrRecord,
    EmrRecordStatus,
    EmrDraftStage,
    EmrRecordAuditLog,
    EmrRecordAuditAction,
    EmrPinnedPatient,
    EmrPinnedRecord,
    EmrRecentView,
    EmrInboxItem,
    EmrInboxSource,
    EmrInboxStatus,
    EmrExportBundle,
    EmrExportStatus,
    EmrShareLink,
    EmrExportAuditLog,
    EmrExportAuditAction,
)

# If your model module defines these (you showed NAME_120 in emr_all.py earlier)
try:
    from app.models.emr_all import NAME_120  # type: ignore
except Exception:
    NAME_120 = 120  # safe fallback

from app.services.emr_template_builder import normalize_template_schema

# ✅ Adjust import to your patient model path if different
from app.models.patient import Patient

from app.services.emr_export_pdf import build_export_pdf_bytes

ENCOUNTER_TYPES = {"OP", "IP", "ER", "OT"}

RECENT_LIMIT = 50
RECENT_RETURN_LIMIT = 30
PIN_LIMIT = 30
RESUME_DRAFTS_LIMIT = 20

PHASES = [
    {"code": "INTAKE", "label": "Intake", "hint": "Reason for visit, triage, vitals"},
    {"code": "HISTORY", "label": "History", "hint": "HPI, past history, meds, allergies"},
    {"code": "EXAM", "label": "Examination", "hint": "Physical / system exam findings"},
    {"code": "ASSESSMENT", "label": "Assessment", "hint": "Diagnosis, differential, severity"},
    {"code": "PLAN", "label": "Plan", "hint": "Treatment plan, Rx, follow-up"},
    {"code": "ORDERS", "label": "Orders", "hint": "Lab / Radiology / Procedures"},
    {"code": "NURSING", "label": "Nursing", "hint": "Nursing notes & vitals"},
    {"code": "DISCHARGE", "label": "Discharge", "hint": "Discharge summary & instructions"},
    {"code": "ATTACHMENTS", "label": "Attachments", "hint": "Images, reports, documents"},
    {"code": "SIGN_OFF", "label": "Sign-off", "hint": "Signature, stamp, completion"},
]
PHASE_SET = {p["code"] for p in PHASES}

# -------------------------
# Helpers
# -------------------------
def now() -> datetime:
    # Prefer UTC for DB timestamps; your project likely standardizes this already
    return datetime.utcnow()


def _json_loads_safe(text: Any, default: Any):
    try:
        if text is None:
            return default
        if isinstance(text, (dict, list)):
            return text
        s = str(text).strip()
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def _json_dumps_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}" if isinstance(obj, dict) else "[]"


def dumps(x: Any) -> str:
    # keep old name used across code
    return _json_dumps_safe(x)


def loads(s: Optional[str], default):
    return _json_loads_safe(s, default)


def norm_code(v: str) -> str:
    s = str(v or "").strip().upper()
    if not s:
        return ""
    s = s.replace(" ", "_")
    out: List[str] = []
    for ch in s:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch)
    return "".join(out).strip("_")


def safe_commit(db: Session):
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _set_any(obj: Any, names: Iterable[str], value: Any) -> None:
    """Set first matching attribute from names on obj."""
    for n in names:
        if hasattr(obj, n):
            setattr(obj, n, value)
            return


def _require(condition: bool, status: int, detail: str) -> None:
    if not condition:
        raise HTTPException(status_code=status, detail=detail)


def _parse_encounter_type(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s not in ENCOUNTER_TYPES:
        raise HTTPException(status_code=422, detail="Invalid encounter_type")
    return s


def _as_sections(v: Any) -> List[str]:
    """
    Accepts:
      - list[str]
      - comma-separated string
      - None
    Returns normalized unique list[str].
    """
    items: List[str] = []
    if v is None:
        return []
    if isinstance(v, list):
        for x in v:
            s = str(x or "").strip()
            if s:
                items.append(s)
    elif isinstance(v, str):
        for part in v.split(","):
            s = part.strip()
            if s:
                items.append(s)
    else:
        s = str(v).strip()
        if s:
            items.append(s)

    seen = set()
    out: List[str] = []
    for s in items:
        key = s.strip()
        if not key:
            continue
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _derive_sections_from_schema(schema_obj: Any) -> List[str]:
    """
    Best-effort: many template schemas contain sections in one of these:
      - schema["sections"] = ["A","B"] OR [{"title":...},{"name":...}]
      - schema["layout"]["sections"]
      - schema["tabs"]
      - schema["groups"]
    """
    if not isinstance(schema_obj, dict):
        return []

    candidates = []
    if "sections" in schema_obj:
        candidates.append(schema_obj.get("sections"))
    layout = schema_obj.get("layout")
    if isinstance(layout, dict) and "sections" in layout:
        candidates.append(layout.get("sections"))
    if "tabs" in schema_obj:
        candidates.append(schema_obj.get("tabs"))
    if "groups" in schema_obj:
        candidates.append(schema_obj.get("groups"))

    out: List[str] = []
    for c in candidates:
        if isinstance(c, list):
            if c and isinstance(c[0], str):
                out.extend(_as_sections(c))
            elif c and isinstance(c[0], dict):
                for obj in c:
                    if not isinstance(obj, dict):
                        continue
                    for k in ("code", "key", "name", "title", "label"):
                        val = obj.get(k)
                        if val:
                            out.append(str(val).strip())
                            break
        elif isinstance(c, str):
            out.extend(_as_sections(c))

    return _as_sections(out)


def _pick_display_version_no(active_no: Optional[int], published_no: Optional[int]) -> int:
    return int(active_no or published_no or 1)


def ensure_patient(db: Session, patient_id: int):
    p = db.query(Patient).filter(Patient.id == int(patient_id)).one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")
    return p


def ensure_dept(db: Session, dept_code: str):
    code = norm_code(dept_code)
    d = (
        db.query(EmrDepartment)
        .filter(EmrDepartment.code == code, EmrDepartment.is_active.is_(True))
        .one_or_none()
    )
    if not d:
        raise HTTPException(status_code=400, detail=f"Invalid department: {dept_code}")
    return d


def ensure_type(db: Session, type_code: str):
    code = norm_code(type_code)
    t = (
        db.query(EmrRecordType)
        .filter(EmrRecordType.code == code, EmrRecordType.is_active.is_(True))
        .one_or_none()
    )
    if not t:
        raise HTTPException(status_code=400, detail=f"Invalid record type: {type_code}")
    return t

def _set_if_attr(obj: Any, name: str, value: Any) -> None:
    if hasattr(obj, name):
        setattr(obj, name, value)

def _get_template(db: Session, template_id: int) -> EmrTemplate:
    t = db.query(EmrTemplate).filter(EmrTemplate.id == int(template_id)).one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


def _get_version(db: Session, version_id: int) -> EmrTemplateVersion:
    v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(version_id)).one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="Template version not found")
    return v


def audit_record(
    db: Session,
    record_id: int,
    action: EmrRecordAuditAction,
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
    meta: Optional[Dict[str, Any]] = None,
):
    db.add(
        EmrRecordAuditLog(
            record_id=int(record_id),
            action=action,
            user_id=int(user_id) if user_id else None,
            ip=ip,
            user_agent=ua,
            meta_json=dumps(meta) if meta else None,
            created_at=now(),
        )
    )


def export_audit(
    db: Session,
    bundle_id: int,
    action: EmrExportAuditAction,
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
    meta: Optional[Dict[str, Any]] = None,
):
    db.add(
        EmrExportAuditLog(
            bundle_id=int(bundle_id),
            action=action,
            user_id=int(user_id) if user_id else None,
            ip=ip,
            user_agent=ua,
            meta_json=dumps(meta) if meta else None,
            created_at=now(),
        )
    )


# =========================
# 0) META (departments/types)
# =========================
def meta(db: Session) -> Dict[str, Any]:
    deps = (
        db.query(EmrDepartment)
        .filter(EmrDepartment.is_active.is_(True))
        .order_by(EmrDepartment.display_order.asc(), EmrDepartment.name.asc())
        .all()
    )
    types = (
        db.query(EmrRecordType)
        .filter(EmrRecordType.is_active.is_(True))
        .order_by(EmrRecordType.display_order.asc(), EmrRecordType.label.asc())
        .all()
    )

    return {
        "departments": [{"code": d.code, "name": d.name} for d in deps],
        "record_types": [{"code": t.code, "label": t.label, "category": t.category} for t in types],
        "encounter_types": sorted(list(ENCOUNTER_TYPES)),
    }


def list_departments(db: Session, *, active: Optional[bool] = True) -> List[EmrDepartment]:
    q = db.query(EmrDepartment)
    if active is not None:
        q = q.filter(EmrDepartment.is_active.is_(bool(active)))
    return q.order_by(EmrDepartment.display_order.asc(), EmrDepartment.name.asc()).all()


def create_department(db: Session, *, code: str, name: str, is_active: bool, display_order: int) -> EmrDepartment:
    code_n = norm_code(code)
    _require(bool(code_n), 422, "Department code is required")
    _require(bool(str(name or "").strip()), 422, "Department name is required")

    row = EmrDepartment(
        code=code_n,
        name=str(name).strip(),
        is_active=bool(is_active),
        display_order=int(display_order or 1000),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Department code already exists")
    db.refresh(row)
    return row


def update_department(db: Session, *, dept_id: int, name=None, is_active=None, display_order=None) -> EmrDepartment:
    row = db.query(EmrDepartment).filter(EmrDepartment.id == int(dept_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Department not found")

    if name is not None:
        nm = str(name).strip()
        _require(bool(nm), 422, "Department name is required")
        row.name = nm
    if is_active is not None:
        row.is_active = bool(is_active)
    if display_order is not None:
        row.display_order = int(display_order)

    safe_commit(db)
    db.refresh(row)
    return row


def delete_department(db: Session, *, dept_id: int) -> None:
    row = db.query(EmrDepartment).filter(EmrDepartment.id == int(dept_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Department not found")

    used_tpl = db.query(EmrTemplate.id).filter(EmrTemplate.dept_code == row.code).first()
    used_rec = db.query(EmrRecord.id).filter(EmrRecord.dept_code == row.code).first()
    if used_tpl or used_rec:
        raise HTTPException(status_code=409, detail="Cannot delete: department is used in templates/records")

    db.delete(row)
    safe_commit(db)


def list_record_types(db: Session, *, active: Optional[bool] = True) -> List[EmrRecordType]:
    q = db.query(EmrRecordType)
    if active is not None:
        q = q.filter(EmrRecordType.is_active.is_(bool(active)))
    return q.order_by(EmrRecordType.display_order.asc(), EmrRecordType.label.asc()).all()


def create_record_type(
    db: Session,
    *,
    code: str,
    label: str,
    category: str | None,
    is_active: bool,
    display_order: int,
) -> EmrRecordType:
    code_n = norm_code(code)
    _require(bool(code_n), 422, "Record type code is required")
    _require(bool(str(label or "").strip()), 422, "Record type label is required")

    row = EmrRecordType(
        code=code_n,
        label=str(label).strip(),
        category=(str(category).strip() if category else None),
        is_active=bool(is_active),
        display_order=int(display_order or 1000),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Record type code already exists")
    db.refresh(row)
    return row


def update_record_type(db: Session, *, type_id: int, label=None, category=None, is_active=None, display_order=None) -> EmrRecordType:
    row = db.query(EmrRecordType).filter(EmrRecordType.id == int(type_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Record type not found")

    if label is not None:
        lb = str(label).strip()
        _require(bool(lb), 422, "Record type label is required")
        row.label = lb
    if category is not None:
        row.category = (str(category).strip() if category else None)
    if is_active is not None:
        row.is_active = bool(is_active)
    if display_order is not None:
        row.display_order = int(display_order)

    safe_commit(db)
    db.refresh(row)
    return row


def delete_record_type(db: Session, *, type_id: int) -> None:
    row = db.query(EmrRecordType).filter(EmrRecordType.id == int(type_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Record type not found")

    used_tpl = db.query(EmrTemplate.id).filter(EmrTemplate.record_type_code == row.code).first()
    used_rec = db.query(EmrRecord.id).filter(EmrRecord.record_type_code == row.code).first()
    if used_tpl or used_rec:
        raise HTTPException(status_code=409, detail="Cannot delete: record type is used in templates/records")

    db.delete(row)
    safe_commit(db)


# =========================
# 1) TEMPLATE LIBRARY
# =========================
def template_list(
    db: Session,
    *,
    q: str,
    dept_code: str,
    record_type_code: str,
    status: str,
    premium: Optional[bool] = None,
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)

    qry = db.query(EmrTemplate)

    if premium is not None:
        qry = qry.filter(EmrTemplate.premium == bool(premium))

    if dept_code and dept_code.upper() != "ALL":
        qry = qry.filter(EmrTemplate.dept_code == norm_code(dept_code))

    if record_type_code and record_type_code.upper() != "ALL":
        qry = qry.filter(EmrTemplate.record_type_code == norm_code(record_type_code))

    if status and status.upper() != "ALL":
        try:
            st = EmrTemplateStatus(status.upper())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid template status")
        qry = qry.filter(EmrTemplate.status == st)

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrTemplate.name.ilike(qq), EmrTemplate.description.ilike(qq)))

    total = int(qry.count())
    rows = (
        qry.order_by(EmrTemplate.updated_at.desc(), EmrTemplate.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    ver_ids: List[int] = []
    for t in rows:
        if t.active_version_id:
            ver_ids.append(int(t.active_version_id))
        if t.published_version_id:
            ver_ids.append(int(t.published_version_id))
    ver_ids = sorted(set(ver_ids))

    ver_no_map: Dict[int, int] = {}
    ver_sections_map: Dict[int, List[str]] = {}

    if ver_ids:
        vs = (
            db.query(
                EmrTemplateVersion.id,
                EmrTemplateVersion.version_no,
                EmrTemplateVersion.sections_json,
                EmrTemplateVersion.schema_json,
            )
            .filter(EmrTemplateVersion.id.in_(ver_ids))
            .all()
        )
        for vid, vno, sections_json, schema_json in vs:
            vid_i = int(vid)
            ver_no_map[vid_i] = int(vno or 1)

            secs = _json_loads_safe(sections_json, [])
            secs_list = _as_sections(secs)
            if not secs_list:
                schema_obj = _json_loads_safe(schema_json, {})
                secs_list = _derive_sections_from_schema(schema_obj)

            ver_sections_map[vid_i] = secs_list

    items: List[Dict[str, Any]] = []
    for t in rows:
        active_no = ver_no_map.get(int(t.active_version_id)) if t.active_version_id else None
        published_no = ver_no_map.get(int(t.published_version_id)) if t.published_version_id else None

        sections = []
        if t.active_version_id:
            sections = ver_sections_map.get(int(t.active_version_id), [])
        if not sections and t.published_version_id:
            sections = ver_sections_map.get(int(t.published_version_id), [])

        items.append(
            {
                "id": int(t.id),
                "dept_code": t.dept_code,
                "record_type_code": t.record_type_code,
                "name": t.name,
                "description": t.description,
                "restricted": bool(t.restricted),
                "premium": bool(t.premium),
                "is_default": bool(t.is_default),
                "status": t.status.value,

                # display version
                "version": _pick_display_version_no(active_no, published_no),

                "active_version_id": t.active_version_id,
                "published_version_id": t.published_version_id,
                "active_version_no": active_no,
                "published_version_no": published_no,

                "sections": sections,

                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
        )

    return {"items": items, "page": page, "page_size": page_size, "total": total}


def template_get(db: Session, *, template_id: int) -> Dict[str, Any]:
    t = db.query(EmrTemplate).filter(EmrTemplate.id == int(template_id)).first()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")

    versions = (
        db.query(EmrTemplateVersion)
        .filter(EmrTemplateVersion.template_id == int(t.id))
        .order_by(EmrTemplateVersion.version_no.desc(), EmrTemplateVersion.id.desc())
        .all()
    )

    active_vid = int(t.active_version_id) if t.active_version_id else None
    published_vid = int(t.published_version_id) if t.published_version_id else None

    active_v: Optional[EmrTemplateVersion] = None
    if active_vid:
        active_v = next((v for v in versions if int(v.id) == active_vid), None)
    if not active_v and published_vid:
        active_v = next((v for v in versions if int(v.id) == published_vid), None)
    if not active_v and versions:
        active_v = versions[0]

    top_schema_obj = _json_loads_safe(getattr(active_v, "schema_json", None), {}) if active_v else {}
    top_sections = _json_loads_safe(getattr(active_v, "sections_json", None), []) if active_v else []
    top_sections_list = _as_sections(top_sections)
    if not top_sections_list:
        top_sections_list = _derive_sections_from_schema(top_schema_obj)

    out_versions: List[Dict[str, Any]] = []
    for v in versions:
        schema_obj = _json_loads_safe(v.schema_json, {})
        secs = _json_loads_safe(v.sections_json, [])
        secs_list = _as_sections(secs)
        if not secs_list:
            secs_list = _derive_sections_from_schema(schema_obj)

        out_versions.append(
            {
                "id": int(v.id),
                "version_no": int(v.version_no or 1),
                "status": t.status.value,
                "changelog": getattr(v, "changelog", None),
                "sections": secs_list,
                "schema_json": schema_obj,
                "created_at": v.created_at,
                "created_by": getattr(v, "created_by_user_id", None) or getattr(v, "created_by", None),
            }
        )

    return {
        "id": int(t.id),
        "dept_code": t.dept_code,
        "record_type_code": t.record_type_code,
        "name": t.name,
        "description": t.description,
        "restricted": bool(t.restricted),
        "premium": bool(t.premium),
        "is_default": bool(t.is_default),
        "status": t.status.value,
        "active_version_id": t.active_version_id,
        "published_version_id": t.published_version_id,
        "created_at": t.created_at,
        "updated_at": t.updated_at,

        "sections": top_sections_list,
        "schema_json": top_schema_obj,

        "versions": out_versions,
    }


def template_create(db: Session, *, payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    dept_code = norm_code(str(payload.get("dept_code") or ""))
    rt_code = norm_code(str(payload.get("record_type_code") or ""))
    name = (payload.get("name") or "").strip()

    if not dept_code or not rt_code or not name:
        raise HTTPException(status_code=422, detail="dept_code, record_type_code, name are required")

    ensure_dept(db, dept_code)
    ensure_type(db, rt_code)

    dup = (
        db.query(EmrTemplate.id)
        .filter(
            EmrTemplate.dept_code == dept_code,
            EmrTemplate.record_type_code == rt_code,
            func.lower(EmrTemplate.name) == name.lower(),
        )
        .first()
    )
    if dup:
        raise HTTPException(status_code=409, detail="Template name already exists for this department/type")

    publish = bool(payload.get("publish") or False)
    status = EmrTemplateStatus.PUBLISHED if publish else EmrTemplateStatus.DRAFT

    tpl = EmrTemplate(
        dept_code=dept_code,
        record_type_code=rt_code,
        name=name,
        description=(str(payload.get("description")).strip() if payload.get("description") else None),
        restricted=bool(payload.get("restricted") or False),
        premium=bool(payload.get("premium") or False),
        is_default=bool(payload.get("is_default") or False),
        status=status,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    db.add(tpl)
    db.flush()

    norm = normalize_template_schema(
        db,
        dept_code=dept_code,
        record_type_code=rt_code,
        schema_input=payload.get("schema_json"),
        sections_input=payload.get("sections_json") or payload.get("sections") or [],
    )

    # store only section codes in sections_json (fast list + stable)
    section_codes = [str(s.get("code") or "").strip() for s in (norm.get("sections") or []) if str(s.get("code") or "").strip()]

    ver = EmrTemplateVersion(
        template_id=tpl.id,
        version_no=1,
        changelog=(payload.get("changelog") or "Initial version"),
        sections_json=json.dumps(section_codes, ensure_ascii=False),
        schema_json=json.dumps(norm, ensure_ascii=False),
        created_by_user_id=user_id,
        created_at=now(),
    )
    # if your model has updated_by_user_id / updated_at
    _set_any(ver, ["updated_by_user_id", "updated_by"], user_id)
    _set_any(ver, ["updated_at"], now())

    db.add(ver)
    db.flush()

    tpl.active_version_id = int(ver.id)
    if publish:
        tpl.published_version_id = int(ver.id)
        _set_any(tpl, ["published_at"], now())
        _set_any(tpl, ["published_by_user_id", "published_by"], user_id)

    if tpl.is_default:
        db.query(EmrTemplate).filter(
            EmrTemplate.dept_code == dept_code,
            EmrTemplate.record_type_code == rt_code,
            EmrTemplate.id != tpl.id,
        ).update({"is_default": False})

    safe_commit(db)
    db.refresh(tpl)
    db.refresh(ver)

    schema_obj = _json_loads_safe(ver.schema_json, {})
    return {
        "id": int(tpl.id),
        "dept_code": tpl.dept_code,
        "record_type_code": tpl.record_type_code,
        "name": tpl.name,
        "description": tpl.description,
        "restricted": bool(tpl.restricted),
        "premium": bool(tpl.premium),
        "is_default": bool(tpl.is_default),
        "status": tpl.status.value,
        "version_no": int(ver.version_no),
        "schema": schema_obj,
        "warnings": schema_obj.get("warnings") if isinstance(schema_obj, dict) else [],
    }


def template_update(db: Session, *, template_id: int, payload: Dict[str, Any], user_id: Optional[int]) -> Dict[str, Any]:
    t = db.query(EmrTemplate).filter(EmrTemplate.id == int(template_id)).one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")

    if payload.get("name") is not None:
        nm = (payload.get("name") or "").strip()
        if len(nm) < 3:
            raise HTTPException(status_code=400, detail="Template name min 3 chars")

        dup = (
            db.query(EmrTemplate.id)
            .filter(
                EmrTemplate.dept_code == t.dept_code,
                EmrTemplate.record_type_code == t.record_type_code,
                func.lower(EmrTemplate.name) == nm.lower(),
                EmrTemplate.id != int(t.id),
            )
            .first()
        )
        if dup:
            raise HTTPException(status_code=409, detail="Template name already exists for this department/type")

        t.name = nm

    if payload.get("description") is not None:
        desc = payload.get("description")
        t.description = (str(desc).strip() if isinstance(desc, str) and desc.strip() else None)

    for k in ["restricted", "premium", "is_default"]:
        if payload.get(k) is not None:
            setattr(t, k, bool(payload.get(k)))

    t.updated_by_user_id = int(user_id) if user_id else None
    t.updated_at = now()

    if t.is_default:
        db.query(EmrTemplate).filter(
            EmrTemplate.id != int(t.id),
            EmrTemplate.dept_code == t.dept_code,
            EmrTemplate.record_type_code == t.record_type_code,
            EmrTemplate.is_default.is_(True),
        ).update({"is_default": False})

    safe_commit(db)
    return {"updated": True}


def template_new_version(db: Session, *, template_id: int, payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    t = _get_template(db, template_id)

    # Prevent changing dept/type for an existing template (safer + consistent keys)
    if payload.get("dept_code"):
        if norm_code(payload.get("dept_code")) != (t.dept_code or ""):
            raise HTTPException(status_code=409, detail="Cannot change dept_code for an existing template. Create a new template instead.")
    if payload.get("record_type_code"):
        if norm_code(payload.get("record_type_code")) != (t.record_type_code or ""):
            raise HTTPException(status_code=409, detail="Cannot change record_type_code for an existing template. Create a new template instead.")

    # name (optional)
    if isinstance(payload.get("name"), str):
        name = payload["name"].strip()
        if name:
            if len(name) < 3:
                raise HTTPException(status_code=422, detail="name must be at least 3 characters")
            if len(name) > NAME_120:
                raise HTTPException(status_code=422, detail=f"name must be at most {NAME_120} characters")
            exists = (
                db.query(EmrTemplate.id)
                .filter(
                    EmrTemplate.dept_code == t.dept_code,
                    EmrTemplate.record_type_code == t.record_type_code,
                    func.lower(EmrTemplate.name) == name.lower(),
                    EmrTemplate.id != t.id,
                )
                .first()
            )
            if exists:
                raise HTTPException(status_code=409, detail="Template name already exists for this department and record type")
            t.name = name

    # description (optional)
    if "description" in payload:
        desc = payload.get("description")
        if desc is None:
            t.description = None
        elif isinstance(desc, str):
            t.description = (desc.strip() or None)

    # flags (optional)
    for k in ("premium", "restricted", "is_default"):
        if k in payload and payload.get(k) is not None:
            setattr(t, k, bool(payload.get(k)))

    if t.is_default:
        (
            db.query(EmrTemplate)
            .filter(
                EmrTemplate.dept_code == t.dept_code,
                EmrTemplate.record_type_code == t.record_type_code,
                EmrTemplate.id != t.id,
            )
            .update({"is_default": False})
        )

    keep_same = bool(payload.get("keep_same_version") or False)

    # Never mutate published version in place
    if keep_same and t.status != EmrTemplateStatus.PUBLISHED and t.active_version_id:
        v = _get_version(db, int(t.active_version_id))

        norm = normalize_template_schema(
            db,
            dept_code=t.dept_code,
            record_type_code=t.record_type_code,
            sections_input=payload.get("sections_json") or payload.get("sections") or [],
            schema_input=payload.get("schema_json") or {},
        )

        section_codes = [str(s.get("code") or "").strip() for s in (norm.get("sections") or []) if str(s.get("code") or "").strip()]

        v.schema_json = json.dumps(norm, ensure_ascii=False)
        v.sections_json = json.dumps(section_codes, ensure_ascii=False)
        if payload.get("changelog"):
            v.changelog = payload.get("changelog")
        _set_any(v, ["updated_at"], now())
        _set_any(v, ["updated_by_user_id", "updated_by"], user_id)

        t.updated_at = now()
        t.active_version_id = v.id

        if payload.get("publish"):
            t.status = EmrTemplateStatus.PUBLISHED
            t.published_version_id = v.id
            _set_any(t, ["published_at"], now())
            _set_any(t, ["published_by_user_id", "published_by"], user_id)

        safe_commit(db)
        return {
            "template_id": int(t.id),
            "version_id": int(v.id),
            "version_no": int(v.version_no),
            "active_version_id": int(t.active_version_id) if t.active_version_id else None,
            "published_version_id": int(t.published_version_id) if t.published_version_id else None,
            "status": t.status.value if hasattr(t.status, "value") else t.status,
        }

    last_no = (
        db.query(func.coalesce(func.max(EmrTemplateVersion.version_no), 0))
        .filter(EmrTemplateVersion.template_id == t.id)
        .scalar()
        or 0
    )
    new_no = int(last_no) + 1

    norm = normalize_template_schema(
        db,
        dept_code=t.dept_code,
        record_type_code=t.record_type_code,
        sections_input=payload.get("sections_json") or payload.get("sections") or [],
        schema_input=payload.get("schema_json") or {},
    )
    section_codes = [str(s.get("code") or "").strip() for s in (norm.get("sections") or []) if str(s.get("code") or "").strip()]

    v = EmrTemplateVersion(
        template_id=t.id,
        version_no=new_no,
        schema_json=json.dumps(norm, ensure_ascii=False),
        sections_json=json.dumps(section_codes, ensure_ascii=False),
        changelog=(payload.get("changelog") or None),
    )
    _set_any(v, ["created_by_user_id", "created_by"], user_id)
    _set_any(v, ["updated_by_user_id", "updated_by"], user_id)
    _set_any(v, ["created_at"], now())
    _set_any(v, ["updated_at"], now())

    db.add(v)
    db.flush()

    t.active_version_id = v.id
    t.updated_at = now()

    if payload.get("publish"):
        t.status = EmrTemplateStatus.PUBLISHED
        t.published_version_id = v.id
        _set_any(t, ["published_at"], now())
        _set_any(t, ["published_by_user_id", "published_by"], user_id)

    safe_commit(db)

    return {
        "template_id": int(t.id),
        "version_id": int(v.id),
        "version_no": int(v.version_no),
        "active_version_id": int(t.active_version_id) if t.active_version_id else None,
        "published_version_id": int(t.published_version_id) if t.published_version_id else None,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
    }


def template_publish_toggle(db: Session, *, template_id: int, publish: bool, user_id: Optional[int]) -> Dict[str, Any]:
    t = _get_template(db, template_id)

    if publish:
        if not t.active_version_id:
            raise HTTPException(status_code=400, detail="Template has no active version")
        t.status = EmrTemplateStatus.PUBLISHED
        t.published_version_id = int(t.active_version_id)
        _set_any(t, ["published_at"], now())
        _set_any(t, ["published_by_user_id", "published_by"], int(user_id) if user_id else None)
    else:
        t.status = EmrTemplateStatus.DRAFT

    t.updated_by_user_id = int(user_id) if user_id else None
    t.updated_at = now()
    safe_commit(db)
    return {"status": t.status.value, "published_version_id": t.published_version_id}


def resolve_template_version_for_record(
    db: Session,
    *,
    template_id: Optional[int],
    template_version_id: Optional[int],
    allow_unpublished: bool,
) -> Tuple[Optional[EmrTemplate], Optional[EmrTemplateVersion]]:
    if not template_id and not template_version_id:
        return None, None

    if template_version_id:
        v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(template_version_id)).one_or_none()
        if not v:
            raise HTTPException(status_code=404, detail="Template version not found")
        t = db.query(EmrTemplate).filter(EmrTemplate.id == int(v.template_id)).one_or_none()
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
        if template_id and int(t.id) != int(template_id):
            raise HTTPException(status_code=400, detail="Template mismatch")
        if t.status != EmrTemplateStatus.PUBLISHED and (not allow_unpublished):
            raise HTTPException(status_code=400, detail="Template not published")
        return t, v

    t = _get_template(db, int(template_id))

    if t.status == EmrTemplateStatus.PUBLISHED:
        if not t.published_version_id:
            raise HTTPException(status_code=400, detail="Template has no published version")
        v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(t.published_version_id)).one_or_none()
        if not v:
            raise HTTPException(status_code=400, detail="Published version missing")
        return t, v

    if not allow_unpublished:
        raise HTTPException(status_code=400, detail="Template not published")

    if not t.active_version_id:
        raise HTTPException(status_code=400, detail="Template has no active version")
    v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(t.active_version_id)).one_or_none()
    if not v:
        raise HTTPException(status_code=400, detail="Active version missing")
    return t, v


# =========================
# 2) RECORDS
# =========================
def record_create_draft(
    db: Session,
    *,
    payload: Dict[str, Any],
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
    allow_unpublished_template: bool,
) -> Dict[str, Any]:
    _require("patient_id" in payload, 422, "patient_id is required")
    _require("dept_code" in payload, 422, "dept_code is required")
    _require("record_type_code" in payload, 422, "record_type_code is required")
    _require("encounter_type" in payload, 422, "encounter_type is required")
    _require("encounter_id" in payload, 422, "encounter_id is required")
    _require(str(payload.get("encounter_id") or "").strip(), 422, "encounter_id is required")
    
    ensure_patient(db, int(payload["patient_id"]))
    ensure_dept(db, payload["dept_code"])
    ensure_type(db, payload["record_type_code"])

    enc_type = _parse_encounter_type(payload.get("encounter_type"))

    title = (payload.get("title") or "").strip()
    if len(title) < 3:
        raise HTTPException(status_code=422, detail="Title required (min 3 chars)")

    t, v = resolve_template_version_for_record(
        db,
        template_id=payload.get("template_id"),
        template_version_id=payload.get("template_version_id"),
        allow_unpublished=allow_unpublished_template,
    )

    content = payload.get("content") or {}
    if not isinstance(content, (dict, list)):
        raise HTTPException(status_code=422, detail="content must be an object/array")
    content_json = dumps(content)

    stage = (payload.get("draft_stage") or "INCOMPLETE").upper()
    stage_enum = EmrDraftStage.READY if stage == "READY" else EmrDraftStage.INCOMPLETE

    r = EmrRecord(
        patient_id=int(payload["patient_id"]),
        encounter_type=enc_type,
        encounter_id=str(payload.get("encounter_id") or "").strip(),
        dept_code=norm_code(payload["dept_code"]),
        record_type_code=norm_code(payload["record_type_code"]),
        template_id=int(t.id) if t else None,
        template_version_id=int(v.id) if v else None,
        title=title,
        note=(payload.get("note") or None),
        confidential=bool(payload.get("confidential")),
        content_json=content_json,
        status=EmrRecordStatus.DRAFT,
        draft_stage=stage_enum,
        created_by_user_id=int(user_id) if user_id else None,
        created_at=now(),
        updated_at=now(),
    )
    db.add(r)
    db.flush()

    audit_record(
        db,
        int(r.id),
        EmrRecordAuditAction.CREATE_DRAFT,
        user_id,
        ip,
        ua,
        meta={"template_id": r.template_id, "template_version_id": r.template_version_id},
    )
    safe_commit(db)
    return {"record_id": int(r.id)}


def record_update_draft(
    db: Session,
    *,
    record_id: int,
    payload: Dict[str, Any],
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
) -> Dict[str, Any]:
    r = db.query(EmrRecord).filter(EmrRecord.id == int(record_id)).one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    if r.status != EmrRecordStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Record locked (only DRAFT can be updated)")

    updated: List[str] = []

    if payload.get("title") is not None:
        t = (payload.get("title") or "").strip()
        if len(t) < 3:
            raise HTTPException(status_code=422, detail="Title min 3 chars")
        r.title = t
        updated.append("title")

    if payload.get("note") is not None:
        r.note = (payload.get("note") or None)
        updated.append("note")

    if payload.get("confidential") is not None:
        r.confidential = bool(payload.get("confidential"))
        updated.append("confidential")

    if payload.get("content") is not None:
        c = payload.get("content") or {}
        if not isinstance(c, (dict, list)):
            raise HTTPException(status_code=422, detail="content must be an object/array")
        r.content_json = dumps(c)
        updated.append("content")

    if payload.get("draft_stage") is not None:
        ds = (payload.get("draft_stage") or "").upper()
        r.draft_stage = EmrDraftStage.READY if ds == "READY" else EmrDraftStage.INCOMPLETE
        updated.append("draft_stage")

    r.updated_at = now()
    audit_record(db, int(r.id), EmrRecordAuditAction.UPDATE_DRAFT, user_id, ip, ua, meta={"updated_fields": updated})
    safe_commit(db)
    return {"updated": True}


def record_sign(
    db: Session,
    *,
    record_id: int,
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
    sign_note: Optional[str],
) -> Dict[str, Any]:
    r = db.query(EmrRecord).filter(EmrRecord.id == int(record_id)).one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    if r.status != EmrRecordStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only DRAFT can be signed")
    if r.draft_stage != EmrDraftStage.READY:
        raise HTTPException(status_code=400, detail="Draft is not READY for signature")
    if len((r.title or "").strip()) < 3:
        raise HTTPException(status_code=400, detail="Title required to sign")

    r.status = EmrRecordStatus.SIGNED
    r.signed_by_user_id = int(user_id) if user_id else None
    r.signed_at = now()
    r.updated_at = now()

    audit_record(
        db,
        int(r.id),
        EmrRecordAuditAction.SIGN,
        user_id,
        ip,
        ua,
        meta={"sign_note": (sign_note or "").strip() or None},
    )
    safe_commit(db)
    return {"signed": True}


def record_void(
    db: Session,
    *,
    record_id: int,
    user_id: Optional[int],
    ip: Optional[str],
    ua: Optional[str],
    reason: str,
) -> Dict[str, Any]:
    r = db.query(EmrRecord).filter(EmrRecord.id == int(record_id)).one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")
    if r.status == EmrRecordStatus.VOID:
        raise HTTPException(status_code=409, detail="Record already voided")

    rr = (reason or "").strip()
    if len(rr) < 3:
        raise HTTPException(status_code=422, detail="Void reason required (min 3 chars)")

    r.status = EmrRecordStatus.VOID
    r.void_reason = rr
    r.voided_by_user_id = int(user_id) if user_id else None
    r.voided_at = now()
    r.updated_at = now()

    audit_record(db, int(r.id), EmrRecordAuditAction.VOID, user_id, ip, ua, meta={"reason": rr})
    safe_commit(db)
    return {"voided": True}


def record_get(db: Session, *, record_id: int, user_id: Optional[int], ip: Optional[str], ua: Optional[str]) -> Dict[str, Any]:
    r = db.query(EmrRecord).filter(EmrRecord.id == int(record_id)).one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")

    audit_record(db, int(r.id), EmrRecordAuditAction.VIEW, user_id, ip, ua)

    tpl_sections: List[str] = []
    tpl_version_no = None
    if r.template_version_id:
        v = db.query(EmrTemplateVersion).filter(EmrTemplateVersion.id == int(r.template_version_id)).one_or_none()
        if v:
            tpl_version_no = int(v.version_no or 1)
            tpl_sections = _as_sections(loads(v.sections_json, []))

    out = {
        "id": int(r.id),
        "patient_id": int(r.patient_id),
        "encounter_type": r.encounter_type,
        "encounter_id": r.encounter_id,
        "dept_code": r.dept_code,
        "record_type_code": r.record_type_code,
        "template_id": r.template_id,
        "template_version_id": r.template_version_id,
        "template_version_no": tpl_version_no,
        "template_sections": tpl_sections,
        "title": r.title,
        "note": r.note,
        "confidential": bool(r.confidential),
        "content": loads(r.content_json, {}),
        "status": r.status.value,
        "draft_stage": r.draft_stage.value,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "signed_at": r.signed_at,
    }

    safe_commit(db)  # commit audit log
    return {"record": out}


def record_list(
    db: Session,
    *,
    patient_id: Optional[int],
    q: str,
    status: str,
    stage: str,
    dept_code: str,
    record_type_code: str,
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)

    qry = db.query(EmrRecord)

    if patient_id:
        qry = qry.filter(EmrRecord.patient_id == int(patient_id))

    if dept_code and dept_code.upper() != "ALL":
        qry = qry.filter(EmrRecord.dept_code == norm_code(dept_code))

    if record_type_code and record_type_code.upper() != "ALL":
        qry = qry.filter(EmrRecord.record_type_code == norm_code(record_type_code))

    if status and status.upper() != "ALL":
        try:
            st = EmrRecordStatus(status.upper())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid record status")
        qry = qry.filter(EmrRecord.status == st)

    if stage and stage.upper() != "ALL":
        try:
            sg = EmrDraftStage(stage.upper())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid draft stage")
        qry = qry.filter(EmrRecord.draft_stage == sg)

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrRecord.title.ilike(qq), EmrRecord.note.ilike(qq)))

    total = int(qry.count())
    rows = (
        qry.order_by(EmrRecord.created_at.desc(), EmrRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [
        {
            "id": int(r.id),
            "patient_id": int(r.patient_id),
            "encounter_type": r.encounter_type,
            "encounter_id": r.encounter_id,
            "dept_code": r.dept_code,
            "record_type_code": r.record_type_code,
            "title": r.title,
            "status": r.status.value,
            "draft_stage": r.draft_stage.value,
            "confidential": bool(r.confidential),
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "signed_at": r.signed_at,
        }
        for r in rows
    ]

    return {"items": items, "page": page, "page_size": page_size, "total": total}


# =========================
# 3) QUICK ACCESS (Recent & Pinned)
# =========================
def upsert_recent(db: Session, *, user_id: int, patient_id: int, record_id: Optional[int]) -> None:
    """
    Model uses record_id NOT NULL with 0 as patient-only key.
    Optimized cleanup (no full table scan).
    """
    ensure_patient(db, int(patient_id))

    rk = int(record_id) if record_id else 0

    row = (
        db.query(EmrRecentView)
        .filter(
            EmrRecentView.user_id == int(user_id),
            EmrRecentView.patient_id == int(patient_id),
            EmrRecentView.record_id == int(rk),
        )
        .one_or_none()
    )
    if row:
        row.last_seen_at = now()
    else:
        db.add(
            EmrRecentView(
                user_id=int(user_id),
                patient_id=int(patient_id),
                record_id=int(rk),
                last_seen_at=now(),
            )
        )

    # delete older than RECENT_LIMIT for that user (efficient)
    old_ids = (
        db.query(EmrRecentView.id)
        .filter(EmrRecentView.user_id == int(user_id))
        .order_by(EmrRecentView.last_seen_at.desc(), EmrRecentView.id.desc())
        .offset(RECENT_LIMIT)
        .all()
    )
    if old_ids:
        ids = [int(x[0]) for x in old_ids]
        db.query(EmrRecentView).filter(EmrRecentView.id.in_(ids)).delete(synchronize_session=False)

    safe_commit(db)


def pin_patient(db: Session, *, user_id: int, patient_id: int, pinned: bool):
    ensure_patient(db, int(patient_id))

    if pinned:
        ex = (
            db.query(EmrPinnedPatient)
            .filter(EmrPinnedPatient.user_id == int(user_id), EmrPinnedPatient.patient_id == int(patient_id))
            .one_or_none()
        )
        if not ex:
            db.add(EmrPinnedPatient(user_id=int(user_id), patient_id=int(patient_id), created_at=now()))
    else:
        db.query(EmrPinnedPatient).filter(
            EmrPinnedPatient.user_id == int(user_id),
            EmrPinnedPatient.patient_id == int(patient_id),
        ).delete(synchronize_session=False)

    safe_commit(db)
    return {"pinned": pinned}


def pin_record(db: Session, *, user_id: int, record_id: int, pinned: bool):
    # ensure record exists to avoid dangling pins
    r = db.query(EmrRecord.id).filter(EmrRecord.id == int(record_id)).first()
    if not r:
        raise HTTPException(status_code=404, detail="Record not found")

    if pinned:
        ex = (
            db.query(EmrPinnedRecord)
            .filter(EmrPinnedRecord.user_id == int(user_id), EmrPinnedRecord.record_id == int(record_id))
            .one_or_none()
        )
        if not ex:
            db.add(EmrPinnedRecord(user_id=int(user_id), record_id=int(record_id), created_at=now()))
    else:
        db.query(EmrPinnedRecord).filter(
            EmrPinnedRecord.user_id == int(user_id),
            EmrPinnedRecord.record_id == int(record_id),
        ).delete(synchronize_session=False)

    safe_commit(db)
    return {"pinned": pinned}


def quick_get(db: Session, *, user_id: int) -> Dict[str, Any]:
    recents = (
        db.query(EmrRecentView)
        .filter(EmrRecentView.user_id == int(user_id))
        .order_by(EmrRecentView.last_seen_at.desc(), EmrRecentView.id.desc())
        .limit(RECENT_RETURN_LIMIT)
        .all()
    )

    pins_pat = (
        db.query(EmrPinnedPatient)
        .filter(EmrPinnedPatient.user_id == int(user_id))
        .order_by(EmrPinnedPatient.created_at.desc())
        .limit(PIN_LIMIT)
        .all()
    )
    pins_rec = (
        db.query(EmrPinnedRecord)
        .filter(EmrPinnedRecord.user_id == int(user_id))
        .order_by(EmrPinnedRecord.created_at.desc())
        .limit(PIN_LIMIT)
        .all()
    )

    recent_items = [
        {
            "patient_id": int(rv.patient_id),
            "record_id": (int(rv.record_id) if int(rv.record_id) != 0 else None),
            "last_seen_at": rv.last_seen_at,
        }
        for rv in recents
    ]

    pinned_patients = [{"patient_id": int(x.patient_id), "created_at": x.created_at} for x in pins_pat]
    pinned_records = [{"record_id": int(x.record_id), "created_at": x.created_at} for x in pins_rec]

    drafts = (
        db.query(EmrRecord)
        .filter(EmrRecord.created_by_user_id == int(user_id), EmrRecord.status == EmrRecordStatus.DRAFT)
        .order_by(EmrRecord.updated_at.desc(), EmrRecord.id.desc())
        .limit(RESUME_DRAFTS_LIMIT)
        .all()
    )
    resume = [
        {
            "record_id": int(r.id),
            "patient_id": int(r.patient_id),
            "title": r.title,
            "dept_code": r.dept_code,
            "record_type_code": r.record_type_code,
            "draft_stage": r.draft_stage.value,
            "updated_at": r.updated_at,
        }
        for r in drafts
    ]

    # Optional enrichment (non-breaking): small maps for UI (if fields exist)
    patient_ids = {int(x["patient_id"]) for x in recent_items} | {int(x["patient_id"]) for x in pinned_patients} | {int(x["patient_id"]) for x in resume}
    record_ids = {int(x["record_id"]) for x in pinned_records if x.get("record_id")} | {int(x["record_id"]) for x in recent_items if x.get("record_id")}

    patient_map: Dict[int, Dict[str, Any]] = {}
    if patient_ids:
        pats = db.query(Patient).filter(Patient.id.in_(sorted(patient_ids))).all()
        for p in pats:
            pid = int(getattr(p, "id", 0) or 0)
            if not pid:
                continue
            first = getattr(p, "first_name", None) or getattr(p, "name", None) or ""
            last = getattr(p, "last_name", None) or ""
            full = (str(first).strip() + " " + str(last).strip()).strip()
            patient_map[pid] = {
                "id": pid,
                "uhid": getattr(p, "uhid", None) or getattr(p, "patient_code", None),
                "name": full or None,
                "phone": getattr(p, "phone", None),
            }

    record_map: Dict[int, Dict[str, Any]] = {}
    if record_ids:
        recs = db.query(EmrRecord).filter(EmrRecord.id.in_(sorted(record_ids))).all()
        for r in recs:
            rid = int(getattr(r, "id", 0) or 0)
            if not rid:
                continue
            record_map[rid] = {
                "id": rid,
                "patient_id": int(r.patient_id),
                "title": r.title,
                "status": r.status.value,
                "updated_at": r.updated_at,
            }

    return {
        "recents": recent_items,
        "pinned_patients": pinned_patients,
        "pinned_records": pinned_records,
        "resume_drafts": resume,
        "patient_map": patient_map,
        "record_map": record_map,
    }


# =========================
# 4) INBOX (Daily Work Queue)
# =========================
def inbox_push(db: Session, *, payload: Dict[str, Any], user_id: Optional[int]) -> Dict[str, Any]:
    _require("patient_id" in payload, 422, "patient_id is required")
    ensure_patient(db, int(payload["patient_id"]))

    src = str(payload.get("source") or "LAB").strip().upper()
    try:
        src_enum = EmrInboxSource(src)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid source")

    title = (payload.get("title") or "").strip()
    if len(title) < 3:
        raise HTTPException(status_code=422, detail="Title min 3 chars")

    item = EmrInboxItem(
        source=src_enum,
        status=EmrInboxStatus.NEW,
        patient_id=int(payload["patient_id"]),
        encounter_type=str(payload.get("encounter_type") or "").strip() or None,
        encounter_id=str(payload.get("encounter_id") or "").strip() or None,
        title=title,
        source_ref_type=payload.get("source_ref_type"),
        source_ref_id=payload.get("source_ref_id"),
        payload_json=dumps(payload.get("payload") or {}),
        created_at=now(),
    )
    db.add(item)
    db.flush()
    safe_commit(db)
    return {"inbox_id": int(item.id)}


def inbox_list(db: Session, *, bucket: str, q: str, page: int, page_size: int) -> Dict[str, Any]:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    bucket = (bucket or "pending_signature").strip()

    if bucket in ("pending_signature", "drafts_to_complete"):
        qry = db.query(EmrRecord).filter(EmrRecord.status == EmrRecordStatus.DRAFT)
        if bucket == "pending_signature":
            qry = qry.filter(EmrRecord.draft_stage == EmrDraftStage.READY)
        else:
            qry = qry.filter(EmrRecord.draft_stage == EmrDraftStage.INCOMPLETE)

        if q:
            qq = f"%{q.strip()}%"
            qry = qry.filter(or_(EmrRecord.title.ilike(qq), EmrRecord.note.ilike(qq)))

        total = int(qry.count())
        rows = (
            qry.order_by(EmrRecord.updated_at.desc(), EmrRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [
            {
                "kind": "RECORD",
                "record_id": int(r.id),
                "patient_id": int(r.patient_id),
                "title": r.title,
                "dept_code": r.dept_code,
                "record_type_code": r.record_type_code,
                "draft_stage": r.draft_stage.value,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
        return {"items": items, "page": page, "page_size": page_size, "total": total, "bucket": bucket}

    if bucket in ("new_lab_results", "new_radiology_reports"):
        src = EmrInboxSource.LAB if bucket == "new_lab_results" else EmrInboxSource.RIS
        qry = db.query(EmrInboxItem).filter(EmrInboxItem.source == src, EmrInboxItem.status == EmrInboxStatus.NEW)

        if q:
            qq = f"%{q.strip()}%"
            qry = qry.filter(EmrInboxItem.title.ilike(qq))

        total = int(qry.count())
        rows = (
            qry.order_by(EmrInboxItem.created_at.desc(), EmrInboxItem.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [
            {
                "kind": "RESULT",
                "inbox_id": int(x.id),
                "source": x.source.value,
                "patient_id": int(x.patient_id),
                "title": x.title,
                "source_ref_type": x.source_ref_type,
                "source_ref_id": x.source_ref_id,
                "payload": loads(x.payload_json, {}),
                "created_at": x.created_at,
            }
            for x in rows
        ]
        return {"items": items, "page": page, "page_size": page_size, "total": total, "bucket": bucket}

    raise HTTPException(status_code=400, detail="Invalid bucket")


def inbox_ack(db: Session, *, inbox_id: int, user_id: int) -> Dict[str, Any]:
    x = db.query(EmrInboxItem).filter(EmrInboxItem.id == int(inbox_id)).one_or_none()
    if not x:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if x.status == EmrInboxStatus.ACK:
        return {"acknowledged": True}

    x.status = EmrInboxStatus.ACK
    x.acknowledged_by_user_id = int(user_id)
    x.acknowledged_at = now()
    safe_commit(db)
    return {"acknowledged": True}


# =========================
# 5) EXPORTS
# =========================
def export_create_bundle(db: Session, *, payload: Dict[str, Any], user_id: int, ip: Optional[str], ua: Optional[str]) -> Dict[str, Any]:
    _require("patient_id" in payload, 422, "patient_id is required")
    ensure_patient(db, int(payload["patient_id"]))

    title = (payload.get("title") or "").strip()
    if len(title) < 3:
        raise HTTPException(status_code=422, detail="Title min 3 chars")

    filters = {
        "record_ids": payload.get("record_ids") or [],
        "from_date": payload.get("from_date"),
        "to_date": payload.get("to_date"),
    }

    b = EmrExportBundle(
        patient_id=int(payload["patient_id"]),
        encounter_type=str(payload.get("encounter_type") or "").strip() or None,
        encounter_id=str(payload.get("encounter_id") or "").strip() or None,
        title=title,
        filters_json=dumps(filters),
        watermark_text=payload.get("watermark_text"),
        status=EmrExportStatus.DRAFT,
        created_by_user_id=int(user_id),
        created_at=now(),
    )
    db.add(b)
    db.flush()

    export_audit(db, int(b.id), EmrExportAuditAction.CREATE_BUNDLE, user_id, ip, ua, meta={"filters": filters})
    safe_commit(db)
    return {"bundle_id": int(b.id)}


def export_update_bundle(db: Session, *, bundle_id: int, payload: Dict[str, Any], user_id: int, ip: Optional[str], ua: Optional[str]) -> Dict[str, Any]:
    b = db.query(EmrExportBundle).filter(EmrExportBundle.id == int(bundle_id)).one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bundle not found")
    if b.status in (EmrExportStatus.RELEASED,):
        raise HTTPException(status_code=400, detail="Released bundle cannot be edited")

    if payload.get("title") is not None:
        t = (payload.get("title") or "").strip()
        if len(t) < 3:
            raise HTTPException(status_code=422, detail="Title min 3 chars")
        b.title = t

    if payload.get("watermark_text") is not None:
        b.watermark_text = payload.get("watermark_text")

    if payload.get("record_ids") is not None:
        f = loads(b.filters_json, {})
        f["record_ids"] = payload.get("record_ids") or []
        b.filters_json = dumps(f)

    export_audit(db, int(b.id), EmrExportAuditAction.UPDATE_BUNDLE, user_id, ip, ua)
    safe_commit(db)
    return {"updated": True}


def export_generate_pdf(
    db: Session,
    *,
    bundle_id: int,
    user_id: int,
    ip: Optional[str],
    ua: Optional[str],
    storage_dir: str = "storage/emr_exports",
) -> Dict[str, Any]:
    b = db.query(EmrExportBundle).filter(EmrExportBundle.id == int(bundle_id)).one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bundle not found")

    f = loads(b.filters_json, {})
    record_ids = [int(x) for x in (f.get("record_ids") or []) if str(x).isdigit()]

    qry = db.query(EmrRecord).filter(EmrRecord.patient_id == int(b.patient_id))
    if record_ids:
        qry = qry.filter(EmrRecord.id.in_(record_ids))

    def parse_ymd(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        try:
            y, m, d = s.split("-")
            return date(int(y), int(m), int(d))
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid date format: {s} (expected YYYY-MM-DD)")

    fd = parse_ymd(f.get("from_date"))
    td = parse_ymd(f.get("to_date"))

    if fd:
        qry = qry.filter(EmrRecord.created_at >= datetime(fd.year, fd.month, fd.day))
    if td:
        qry = qry.filter(EmrRecord.created_at < datetime(td.year, td.month, td.day) + timedelta(days=1))

    records = qry.order_by(EmrRecord.created_at.asc(), EmrRecord.id.asc()).all()
    p = db.query(Patient).filter(Patient.id == int(b.patient_id)).one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    try:
        pdf_bytes = build_export_pdf_bytes(
            patient=p,
            bundle_title=b.title,
            records=records,
            watermark=b.watermark_text,
            db=db,
        )
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {ex}")

    Path(storage_dir).mkdir(parents=True, exist_ok=True)
    file_path = Path(storage_dir) / f"bundle_{int(b.id)}.pdf"
    file_path.write_bytes(pdf_bytes)

    b.pdf_file_key = str(file_path)
    b.status = EmrExportStatus.GENERATED
    b.generated_at = now()

    export_audit(db, int(b.id), EmrExportAuditAction.GENERATE_PDF, user_id, ip, ua, meta={"pdf_file_key": b.pdf_file_key})
    safe_commit(db)
    return {"pdf_file_key": b.pdf_file_key, "status": b.status.value}


def export_create_share(
    db: Session,
    *,
    bundle_id: int,
    user_id: int,
    ip: Optional[str],
    ua: Optional[str],
    expires_in_days: int,
    max_downloads: int,
) -> Dict[str, Any]:
    b = db.query(EmrExportBundle).filter(EmrExportBundle.id == int(bundle_id)).one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="Bundle not found")
    if not b.pdf_file_key:
        raise HTTPException(status_code=400, detail="Generate PDF first")

    expires_in_days = int(expires_in_days or 0)
    max_downloads = int(max_downloads or 0)

    if expires_in_days < 0:
        raise HTTPException(status_code=422, detail="expires_in_days must be >= 0")
    if max_downloads < 0:
        raise HTTPException(status_code=422, detail="max_downloads must be >= 0")

    token_plain = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token_plain.encode("utf-8")).hexdigest()

    expires_at = now() + timedelta(days=expires_in_days) if expires_in_days else None
    md = max_downloads if max_downloads else None

    s = EmrShareLink(
        bundle_id=int(b.id),
        token_hash=token_hash,
        expires_at=expires_at,
        max_downloads=md,
        download_count=0,
        created_by_user_id=int(user_id),
        created_at=now(),
    )
    db.add(s)
    db.flush()

    export_audit(db, int(b.id), EmrExportAuditAction.CREATE_SHARE, user_id, ip, ua, meta={"share_id": int(s.id)})
    safe_commit(db)

    return {"share_token": token_plain, "share_id": int(s.id), "expires_at": expires_at, "max_downloads": s.max_downloads}


def export_revoke_share(db: Session, *, share_id: int, user_id: int, ip: Optional[str], ua: Optional[str]) -> Dict[str, Any]:
    s = db.query(EmrShareLink).filter(EmrShareLink.id == int(share_id)).one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Share not found")
    if getattr(s, "revoked_at", None):
        return {"revoked": True}

    s.revoked_at = now()
    export_audit(db, int(s.bundle_id), EmrExportAuditAction.REVOKE_SHARE, user_id, ip, ua, meta={"share_id": int(s.id)})
    safe_commit(db)
    return {"revoked": True}


def export_download_by_token(db: Session, *, token_plain: str) -> Tuple[str, bytes, int]:
    h = hashlib.sha256((token_plain or "").encode("utf-8")).hexdigest()

    # lock row to avoid race on download_count
    s = (
        db.query(EmrShareLink)
        .filter(EmrShareLink.token_hash == h)
        .with_for_update()
        .one_or_none()
    )
    if not s:
        raise HTTPException(status_code=404, detail="Invalid share token")
    if getattr(s, "revoked_at", None):
        raise HTTPException(status_code=410, detail="Share revoked")
    if s.expires_at and now() > s.expires_at:
        raise HTTPException(status_code=410, detail="Share expired")
    if s.max_downloads is not None and int(s.download_count) >= int(s.max_downloads):
        raise HTTPException(status_code=429, detail="Download limit reached")

    b = db.query(EmrExportBundle).filter(EmrExportBundle.id == int(s.bundle_id)).one_or_none()
    if not b or not b.pdf_file_key:
        raise HTTPException(status_code=404, detail="Bundle PDF not found")

    p = Path(b.pdf_file_key)
    if not p.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on server")

    s.download_count = int(s.download_count) + 1
    safe_commit(db)

    return (f"EMR_Bundle_{int(b.id)}.pdf", p.read_bytes(), 200)

def _phase_summary_from_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    sections: [{code,label,phase,...}]
    return: [{phase, count, titles[]}]
    """
    by_phase: Dict[str, List[str]] = {}
    for s in sections or []:
        ph = str(s.get("phase") or "HISTORY").strip().upper()
        if ph not in PHASE_SET:
            ph = "HISTORY"
        title = str(s.get("label") or s.get("title") or s.get("name") or s.get("code") or "").strip()
        if not title:
            continue
        by_phase.setdefault(ph, []).append(title)

    out = []
    for p in PHASES:
        titles = by_phase.get(p["code"], [])
        if titles:
            out.append({"phase": p["code"], "label": p["label"], "hint": p["hint"], "count": len(titles), "titles": titles})
    return out


def template_presets(db: Session, *, dept_code: str, record_type_code: str) -> Dict[str, Any]:
    """
    Quick-add presets for your builder UI.
    Keep this simple now; later you can move to DB table EmrTemplatePreset.
    """
    d = norm_code(dept_code)
    t = norm_code(record_type_code)

    # Base OPD Consultation (works for any dept)
    opd_base = [
        {"code": "CHIEF_COMPLAINT", "label": "Chief Complaint", "phase": "INTAKE"},
        {"code": "VITALS", "label": "Vitals", "phase": "INTAKE"},
        {"code": "HPI", "label": "History of Present Illness", "phase": "HISTORY"},
        {"code": "PAST_FAMILY_SOCIAL", "label": "Past / Family / Social History", "phase": "HISTORY"},
        {"code": "ALLERGIES", "label": "Allergies", "phase": "HISTORY"},
        {"code": "MEDICATION_HISTORY", "label": "Medication History", "phase": "HISTORY"},
        {"code": "EXAMINATION", "label": "Examination", "phase": "EXAM"},
        {"code": "ASSESSMENT_DIAGNOSIS", "label": "Assessment / Diagnosis", "phase": "ASSESSMENT"},
        {"code": "PLAN", "label": "Plan", "phase": "PLAN"},
        {"code": "PRESCRIPTION", "label": "Prescription", "phase": "PLAN"},
        {"code": "INVESTIGATIONS_ORDERS", "label": "Investigations / Orders", "phase": "ORDERS"},
        {"code": "ADVICE_FOLLOWUP", "label": "Advice & Follow-up", "phase": "PLAN"},
    ]

    # Ortho adds musculoskeletal specifics
    ortho_addon = [
        {"code": "PAIN_SCALE", "label": "Pain Score / Functional Limitation", "phase": "INTAKE"},
        {"code": "MSK_EXAM", "label": "MSK Exam (Joint/Spine)", "phase": "EXAM"},
        {"code": "IMAGING_REVIEW", "label": "Imaging Review", "phase": "ORDERS"},
    ]

    presets = []

    # SOAP preset (generic)
    presets.append({
        "code": "SOAP",
        "label": "SOAP (Fast)",
        "description": "Classic SOAP note layout for rapid documentation",
        "sections": [
            {"code": "SUBJECTIVE", "label": "Subjective", "phase": "HISTORY"},
            {"code": "OBJECTIVE", "label": "Objective", "phase": "EXAM"},
            {"code": "ASSESSMENT", "label": "Assessment", "phase": "ASSESSMENT"},
            {"code": "PLAN", "label": "Plan", "phase": "PLAN"},
        ],
    })

    # OPD Consultation presets
    if t == "OPD_CONSULTATION":
        presets.append({
            "code": "OPD_BASE",
            "label": "OPD Consultation (Recommended)",
            "description": "Balanced OPD flow with vitals, diagnosis, plan, Rx, orders",
            "sections": opd_base,
        })
        if d == "ORTHOPEDICS":
            presets.append({
                "code": "ORTHO_OPD_PLUS",
                "label": "Orthopedics OPD (Plus)",
                "description": "OPD flow + Ortho-specific MSK exam and imaging review",
                "sections": opd_base[:2] + ortho_addon[:1] + opd_base[2:7] + ortho_addon[1:] + opd_base[7:],
            })

    return {"dept_code": d, "record_type_code": t, "phases": PHASES, "presets": presets}


def template_preview(db: Session, *, dept_code: str, record_type_code: str, schema_input: Any, sections_input: Any) -> Dict[str, Any]:
    """
    Runs normalize + returns clinical phase summary + warnings for Review step.
    """
    norm = normalize_template_schema(
        db,
        dept_code=norm_code(dept_code),
        record_type_code=norm_code(record_type_code),
        schema_input=schema_input,
        sections_input=sections_input or [],
    )

    sections = norm.get("sections") or []
    # Ensure phases are normalized
    for s in sections:
        ph = str(s.get("phase") or "HISTORY").strip().upper()
        if ph not in PHASE_SET:
            ph = "HISTORY"
        s["phase"] = ph

    phase_summary = _phase_summary_from_sections(sections)

    # practical, doctor-friendly warnings
    codes = {str(s.get("code") or "").strip().upper() for s in sections}
    warnings = list(norm.get("warnings") or [])
    if "VITALS" not in codes:
        warnings.append("Vitals section missing (recommended for OPD/IPD).")
    if not ({"ASSESSMENT_DIAGNOSIS", "ASSESSMENT", "DIAGNOSIS"} & codes):
        warnings.append("Diagnosis/Assessment section missing (required for clinical completeness).")
    if not ({"PLAN", "ADVICE_FOLLOWUP"} & codes):
        warnings.append("Plan / Follow-up missing (recommended).")

    return {
        "normalized": norm,
        "phases": PHASES,
        "phase_summary": phase_summary,
        "warnings": warnings,
        "publish_ready": len([w for w in warnings if "missing" in w.lower()]) == 0,
    }


# =========================
# FIXED: template_new_version
# =========================
def template_new_version(db: Session, *, template_id: int, payload: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    t = _get_template(db, template_id)

    # Block changing dept/type for existing template
    if payload.get("dept_code") and norm_code(payload.get("dept_code")) != (t.dept_code or ""):
        raise HTTPException(status_code=409, detail="Cannot change dept_code for an existing template. Create a new template instead.")
    if payload.get("record_type_code") and norm_code(payload.get("record_type_code")) != (t.record_type_code or ""):
        raise HTTPException(status_code=409, detail="Cannot change record_type_code for an existing template. Create a new template instead.")

    # name
    if isinstance(payload.get("name"), str):
        name = payload["name"].strip()
        if name:
            if len(name) < 3:
                raise HTTPException(status_code=422, detail="name must be at least 3 characters")
            if len(name) > NAME_120:
                raise HTTPException(status_code=422, detail=f"name must be at most {NAME_120} characters")
            exists = (
                db.query(EmrTemplate.id)
                .filter(
                    EmrTemplate.dept_code == t.dept_code,
                    EmrTemplate.record_type_code == t.record_type_code,
                    func.lower(EmrTemplate.name) == name.lower(),
                    EmrTemplate.id != t.id,
                )
                .first()
            )
            if exists:
                raise HTTPException(status_code=409, detail="Template name already exists for this department and record type")
            t.name = name

    # description
    if "description" in payload:
        desc = payload.get("description")
        t.description = (desc.strip() or None) if isinstance(desc, str) else None

    # flags
    for k in ("premium", "restricted", "is_default"):
        if k in payload and payload.get(k) is not None:
            setattr(t, k, bool(payload.get(k)))

    # If set default, unset other defaults in same dept+type
    if t.is_default:
        (
            db.query(EmrTemplate)
            .filter(
                EmrTemplate.dept_code == t.dept_code,
                EmrTemplate.record_type_code == t.record_type_code,
                EmrTemplate.id != t.id,
            )
            .update({"is_default": False})
        )

    # Always create a new version (audit-safe)
    last_no = (
        db.query(func.coalesce(func.max(EmrTemplateVersion.version_no), 0))
        .filter(EmrTemplateVersion.template_id == t.id)
        .scalar()
        or 0
    )
    new_no = int(last_no) + 1

    norm = normalize_template_schema(
        db,
        dept_code=t.dept_code,
        record_type_code=t.record_type_code,
        sections_input=payload.get("sections_json") or payload.get("sections") or [],
        schema_input=payload.get("schema_json") or {},
    )

    # IMPORTANT: store sections_json as list of section codes (stable + fast)
    section_codes = [str(s.get("code") or "").strip() for s in (norm.get("sections") or []) if str(s.get("code") or "").strip()]

    v = EmrTemplateVersion(
        template_id=t.id,
        version_no=new_no,
        schema_json=json.dumps(norm, ensure_ascii=False),
        sections_json=json.dumps(section_codes, ensure_ascii=False),
        changelog=(payload.get("changelog") or None),
        created_by_user_id=user_id,
        created_at=now(),
    )
    db.add(v)
    db.flush()

    t.active_version_id = v.id
    t.updated_at = now()
    _set_if_attr(t, "updated_by_user_id", user_id)

    if payload.get("publish"):
        t.status = EmrTemplateStatus.PUBLISHED
        t.published_version_id = v.id
        _set_if_attr(t, "published_at", now())
        _set_if_attr(t, "published_by_user_id", user_id)
        _set_if_attr(t, "published_by", user_id)  # fallback for older column names

    safe_commit(db)

    return {
        "template_id": int(t.id),
        "version_id": int(v.id),
        "version_no": int(v.version_no),
        "active_version_id": int(t.active_version_id) if t.active_version_id else None,
        "published_version_id": int(t.published_version_id) if t.published_version_id else None,
        "status": t.status.value if hasattr(t.status, "value") else str(t.status),
    }

# ===========================
# Patient Encounters (Unified)
# ===========================


def patient_encounters(db: Session, *, patient_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Unified encounter list for EMR Create Record Flow.
    Output fields your React uses:
      encounter_type, encounter_id, encounter_code, encounter_at,
      dept_code, department_id, department_name,
      doctor_user_id, doctor_name, status, source

    Uses your REAL models:
      - OPD: app.models.opd.Visit (+ appointment status if present)
      - IPD: IpdAdmission (doctor = practitioner_user_id)
      - OT:  OtSchedule (dept/location = ot_theater_id -> OtTheaterMaster)
            doctor = surgeon_user_id primary (also returns OT team)
    """
    ensure_patient(db, int(patient_id))

    # -------- limit clamp --------
    try:
        limit = int(limit)
    except Exception:
        limit = 100
    limit = min(max(limit, 1), 200)

    # -------- small helpers --------
    def _iso(dt: Any) -> Optional[str]:
        try:
            return dt.isoformat() if dt else None
        except Exception:
            return None

    def _safe_int(v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            return int(v)
        except Exception:
            return None

    def _safe_str(v: Any) -> str:
        try:
            return str(v) if v is not None else ""
        except Exception:
            return ""

    def _pick_dt(obj: Any, fields: List[str]) -> Any:
        for f in fields:
            try:
                v = getattr(obj, f, None)
                if v:
                    return v
            except Exception:
                continue
        return None

    def _combine_date_time(d: Any, t: Any) -> Optional[datetime]:
        try:
            if isinstance(d, date) and isinstance(t, time):
                return datetime.combine(d, t)
        except Exception:
            pass
        return None

    def _to_code(name_or_code: Any) -> str:
        """Best-effort dept_code for UI syncing (no DB code column in your Department)."""
        s = _safe_str(name_or_code).strip()
        if not s:
            return "COMMON"
        out = []
        prev_us = False
        for ch in s.upper():
            ok = ("A" <= ch <= "Z") or ("0" <= ch <= "9")
            if ok:
                out.append(ch)
                prev_us = False
            else:
                if not prev_us:
                    out.append("_")
                    prev_us = True
        code = "".join(out).strip("_")
        return code or "COMMON"

    def _user_label(u: Any) -> Optional[str]:
        if not u:
            return None
        for attr in ("full_name", "name", "display_name"):
            try:
                v = getattr(u, attr, None)
                if v:
                    return _safe_str(v)
            except Exception:
                pass
        try:
            fn = getattr(u, "first_name", None)
            ln = getattr(u, "last_name", None)
            nm = " ".join([x for x in [_safe_str(fn).strip(), _safe_str(ln).strip()] if x])
            return nm or None
        except Exception:
            return None

    def _dept_label(d: Any) -> Optional[str]:
        if not d:
            return None
        for attr in ("name", "dept_name", "title"):
            try:
                v = getattr(d, attr, None)
                if v:
                    return _safe_str(v)
            except Exception:
                pass
        return None

    def _try_import(mod: str, cls: str):
        try:
            m = __import__(mod, fromlist=[cls])
            return getattr(m, cls)
        except Exception:
            return None

    def _batch_users_name_map(user_ids: List[int]) -> Dict[int, str]:
        if not user_ids:
            return {}
        User = _try_import("app.models.user", "User") or _try_import("app.models.users", "User")
        if User is None:
            return {}
        rows = db.query(User).filter(getattr(User, "id").in_(list(set(user_ids)))).all()
        out: Dict[int, str] = {}
        for u in rows:
            uid = _safe_int(getattr(u, "id", None))
            if uid:
                out[uid] = _user_label(u) or _safe_str(getattr(u, "name", None)) or f"User-{uid}"
        return out

    def _batch_depts_name_map(dept_ids: List[int]) -> Dict[int, str]:
        if not dept_ids:
            return {}
        Department = (
            _try_import("app.models.department", "Department")
            or _try_import("app.models.departments", "Department")
        )
        if Department is None:
            return {}
        rows = db.query(Department).filter(getattr(Department, "id").in_(list(set(dept_ids)))).all()
        out: Dict[int, str] = {}
        for d in rows:
            did = _safe_int(getattr(d, "id", None))
            if did:
                out[did] = _dept_label(d) or f"Dept-{did}"
        return out

    items: List[Dict[str, Any]] = []

    # =========================================================
    # OPD (Visit)  ✅ uses Visit.visit_at + department/doctor rels
    # =========================================================
    try:
        from app.models.opd import Visit  # ✅ your model

        opts = []
        for rel in ("department", "doctor", "appointment"):
            if hasattr(Visit, rel):
                opts.append(joinedload(getattr(Visit, rel)))

        q = db.query(Visit).filter(Visit.patient_id == int(patient_id))
        q = q.options(*opts)
        q = q.order_by(
            desc(getattr(Visit, "visit_at", getattr(Visit, "created_at", Visit.id))),
            desc(getattr(Visit, "id")),
        ).limit(limit)

        rows = q.all()

        for r in rows:
            dt = _pick_dt(r, ["visit_at", "created_at", "updated_at"])

            # OP code: prefer op_no property if present else episode_id
            code = None
            try:
                code = getattr(r, "op_no", None)
            except Exception:
                code = None
            if not code:
                code = getattr(r, "episode_id", None) or f"OP-{int(getattr(r,'id')):06d}"

            dept_obj = getattr(r, "department", None)
            doc_obj = getattr(r, "doctor", None)
            appt_obj = getattr(r, "appointment", None)

            dept_name = _dept_label(dept_obj)
            dept_code = _to_code(getattr(dept_obj, "code", None) or dept_name)

            doc_name = _user_label(doc_obj)
            status = None
            try:
                status = _safe_str(getattr(appt_obj, "status", None)) or None
            except Exception:
                status = None

            items.append(
                {
                    "encounter_type": "OP",
                    "encounter_id": _safe_int(getattr(r, "id", None)),
                    "encounter_code": _safe_str(code),
                    "encounter_at": _iso(dt),

                    "dept_code": dept_code,
                    "department_id": _safe_int(getattr(r, "department_id", None)),
                    "department_name": dept_name,

                    "doctor_user_id": _safe_int(getattr(r, "doctor_user_id", None)),
                    "doctor_name": doc_name,

                    "status": status,
                    "source": "opd_visits",
                }
            )
    except Exception:
        # keep silent (your UI should still work with IP/OT)
        pass

    # =========================================================
    # IPD (IpdAdmission)  ✅ doctor = practitioner_user_id
    # =========================================================
    IpdAdmission = (
        _try_import("app.models.ipd", "IpdAdmission")
        or _try_import("app.models.ipd_admission", "IpdAdmission")
        or _try_import("app.models.ipd_admissions", "IpdAdmission")
    )

    if IpdAdmission is not None:
        try:
            # try to eager-load if relationships exist (safe)
            ipd_opts = []
            for rel in ("department", "practitioner", "patient"):
                if hasattr(IpdAdmission, rel):
                    ipd_opts.append(joinedload(getattr(IpdAdmission, rel)))

            q = db.query(IpdAdmission).filter(getattr(IpdAdmission, "patient_id") == int(patient_id))
            if ipd_opts:
                q = q.options(*ipd_opts)

            q = q.order_by(
                desc(getattr(IpdAdmission, "admitted_at", getattr(IpdAdmission, "created_at", IpdAdmission.id))),
                desc(getattr(IpdAdmission, "id")),
            ).limit(limit)

            rows = q.all()

            # batch maps for names if no relationship is present
            dept_ids = [_safe_int(getattr(r, "department_id", None)) for r in rows]
            dept_ids = [x for x in dept_ids if x]
            user_ids = [_safe_int(getattr(r, "practitioner_user_id", None)) for r in rows]
            user_ids = [x for x in user_ids if x]

            dept_map = _batch_depts_name_map(dept_ids)
            user_map = _batch_users_name_map(user_ids)

            for r in rows:
                dt = _pick_dt(r, ["admitted_at", "created_at", "updated_at"])
                code = None
                try:
                    code = getattr(r, "display_code", None)
                except Exception:
                    code = None
                if not code:
                    code = getattr(r, "admission_code", None) or f"IP-{int(getattr(r,'id')):06d}"

                # dept name
                dept_name = None
                dept_code = "COMMON"
                if hasattr(r, "department") and getattr(r, "department", None) is not None:
                    dept_name = _dept_label(getattr(r, "department", None))
                    dept_code = _to_code(getattr(getattr(r, "department", None), "code", None) or dept_name)
                else:
                    did = _safe_int(getattr(r, "department_id", None))
                    dept_name = dept_map.get(did) if did else None
                    dept_code = _to_code(dept_name)

                # doctor name (practitioner_user_id)
                doc_name = None
                if hasattr(r, "practitioner") and getattr(r, "practitioner", None) is not None:
                    doc_name = _user_label(getattr(r, "practitioner", None))
                else:
                    uid = _safe_int(getattr(r, "practitioner_user_id", None))
                    doc_name = user_map.get(uid) if uid else None

                items.append(
                    {
                        "encounter_type": "IP",
                        "encounter_id": _safe_int(getattr(r, "id", None)),
                        "encounter_code": _safe_str(code),
                        "encounter_at": _iso(dt),

                        "dept_code": dept_code,
                        "department_id": _safe_int(getattr(r, "department_id", None)),
                        "department_name": dept_name,

                        "doctor_user_id": _safe_int(getattr(r, "practitioner_user_id", None)),
                        "doctor_name": doc_name,

                        "status": _safe_str(getattr(r, "status", None)) or None,
                        "source": "ipd_admissions",
                    }
                )
        except Exception:
            pass

    # =========================================================
    # OT (OtSchedule)  ✅ dept/location = ot_theater_id
    # =========================================================
    OtSchedule = (
        _try_import("app.models.ot", "OtSchedule")
        or _try_import("app.models.ot_schedule", "OtSchedule")
        or _try_import("app.models.ot_schedules", "OtSchedule")
    )

    if OtSchedule is not None:
        try:
            ot_opts = []
            for rel in ("theater", "surgeon", "anaesthetist", "asst_doctor", "petitory", "case", "admission"):
                if hasattr(OtSchedule, rel):
                    ot_opts.append(joinedload(getattr(OtSchedule, rel)))

            q = db.query(OtSchedule).filter(getattr(OtSchedule, "patient_id") == int(patient_id))
            if ot_opts:
                q = q.options(*ot_opts)

            # Your model has date + planned_start_time (no scheduled_at)
            q = q.order_by(
                desc(getattr(OtSchedule, "date", getattr(OtSchedule, "created_at", OtSchedule.id))),
                desc(getattr(OtSchedule, "planned_start_time", getattr(OtSchedule, "created_at", OtSchedule.id))),
                desc(getattr(OtSchedule, "id")),
            ).limit(limit)

            rows = q.all()

            for r in rows:
                # best encounter_at for OT: combine schedule date + planned_start_time
                dt = _combine_date_time(getattr(r, "date", None), getattr(r, "planned_start_time", None))
                if not dt:
                    dt = _pick_dt(r, ["created_at", "updated_at"])

                # encounter_code: prefer case_id if present else schedule id
                cid = getattr(r, "case_id", None)
                if cid:
                    code = f"OTC-{int(cid):06d}"
                else:
                    code = f"OT-{int(getattr(r,'id')):06d}"

                # dept/location = theater
                theater = getattr(r, "theater", None)
                theater_name = None
                theater_code = None
                if theater is not None:
                    theater_name = _safe_str(getattr(theater, "name", None)) or None
                    theater_code = _safe_str(getattr(theater, "code", None)) or None

                dept_name = theater_name or theater_code
                dept_code = _to_code(theater_code or theater_name or "OT")

                # doctor/team
                team: List[Dict[str, Any]] = []
                def _add(role: str, rel_obj: Any, uid_field: str):
                    uid = _safe_int(getattr(r, uid_field, None))
                    nm = _user_label(rel_obj) if rel_obj is not None else None
                    if uid or nm:
                        team.append({"role": role, "user_id": uid, "name": nm})

                _add("Surgeon", getattr(r, "surgeon", None), "surgeon_user_id")
                _add("Anaesthetist", getattr(r, "anaesthetist", None), "anaesthetist_user_id")
                _add("Asst Doctor", getattr(r, "asst_doctor", None), "asst_doctor_user_id")
                _add("Petitory", getattr(r, "petitory", None), "petitory_user_id")

                primary = team[0] if team else {}
                primary_uid = primary.get("user_id")
                primary_name = primary.get("name")

                items.append(
                    {
                        "encounter_type": "OT",
                        "encounter_id": _safe_int(getattr(r, "id", None)),
                        "encounter_code": code,
                        "encounter_at": _iso(dt),

                        # ✅ IMPORTANT: OT "department" is OT theater id
                        "dept_code": dept_code,
                        "department_id": _safe_int(getattr(r, "ot_theater_id", None)),
                        "department_name": dept_name,
                        "department_type": "OT_THEATER",

                        # ✅ primary doctor shown in list; full team also returned
                        "doctor_user_id": primary_uid,
                        "doctor_name": primary_name,
                        "team": team,

                        "status": _safe_str(getattr(r, "status", None)) or None,
                        "source": "ot_schedules",
                    }
                )
        except Exception:
            pass

    # =========================================================
    # Sort + dedupe + trim
    # =========================================================
    # Sort DESC by encounter_at string (ISO) is OK if all are ISO;
    # still safe because we always use isoformat().
    items.sort(key=lambda x: (x.get("encounter_at") or ""), reverse=True)

    seen: set[Tuple[Any, Any]] = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        k = (it.get("encounter_type"), it.get("encounter_id"))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= limit:
            break

    return out