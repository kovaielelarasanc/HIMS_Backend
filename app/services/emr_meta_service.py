# FILE: app/services/emr_meta_service.py
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text

from app.models.emr_meta import (
    EmrClinicalPhase, EmrTemplatePreset, EmrSectionLibrary, EmrDepartmentTone
)

# You already have these in your system:
# - EmrDepartment
# - EmrRecordType
# Import them from your existing models module:
from app.models.emr_all import EmrDepartment, EmrRecordType  # adjust if your path differs


def _now() -> datetime:
    return datetime.utcnow()


def norm_code(v: Any) -> str:
    return str(v or "").strip().upper()


def _clean_label(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip())


def _make_code_from_label(label: str, max_len: int = 32) -> str:
    base = _clean_label(label).upper()
    base = base.replace("&", " AND ")
    base = re.sub(r"[^A-Z0-9]+", "_", base)
    base = re.sub(r"^_+|_+$", "", base)
    base = re.sub(r"_+", "_", base)
    if not base:
        base = "ITEM"
    if re.match(r"^\d", base):
        base = "X_" + base
    return base[:max_len].rstrip("_")


def generate_unique_code(db: Session, *, model, code_field: str, label: str, max_len: int = 32) -> str:
    base = _make_code_from_label(label, max_len=max_len)
    code = base
    n = 2
    while True:
        exists = db.query(model).filter(getattr(model, code_field) == code).first()
        if not exists:
            return code
        suffix = f"_{n}"
        code = (base[: max(1, max_len - len(suffix))] + suffix).rstrip("_")
        n += 1

def _has_column(db: Session, table: str, column: str) -> bool:
    sql = text("""
        SELECT COUNT(*) 
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = :t
          AND COLUMN_NAME = :c
    """)
    return (db.execute(sql, {"t": table, "c": column}).scalar() or 0) > 0

# ---------------- PHASES ----------------

def list_phases(db: Session, *, active: Optional[bool] = True) -> List[EmrClinicalPhase]:
    q = db.query(EmrClinicalPhase)
    if active is not None:
        q = q.filter(EmrClinicalPhase.is_active == bool(active))
    return q.order_by(EmrClinicalPhase.display_order.asc(), EmrClinicalPhase.label.asc()).all()


# ---------------- SECTION LIBRARY ----------------

def list_sections(
    db: Session,
    *,
    active: Optional[bool] = True,
    q: Optional[str] = None,
    dept_code: Optional[str] = None,
    record_type_code: Optional[str] = None,
    limit: int = 50,
) -> List[EmrSectionLibrary]:
    has_phase = _has_column(db, "emr_section_library", "phase_code")

    qry = db.query(EmrSectionLibrary)

    # IMPORTANT: avoid selecting missing columns
    if not has_phase:
        qry = qry.options(load_only(
            EmrSectionLibrary.id,
            EmrSectionLibrary.code,
            EmrSectionLibrary.label,
            EmrSectionLibrary.dept_code,
            EmrSectionLibrary.record_type_code,
            EmrSectionLibrary.group,
            EmrSectionLibrary.keywords,
            EmrSectionLibrary.is_active,
            EmrSectionLibrary.display_order,
            EmrSectionLibrary.created_at,
            EmrSectionLibrary.updated_at,
        ))

    if active is not None:
        qry = qry.filter(EmrSectionLibrary.is_active == bool(active))

    dc = norm_code(dept_code) if dept_code else None
    rt = norm_code(record_type_code) if record_type_code else None

    if dc or rt:
        qry = qry.filter(or_(EmrSectionLibrary.dept_code.is_(None), EmrSectionLibrary.dept_code == dc))
        qry = qry.filter(or_(EmrSectionLibrary.record_type_code.is_(None), EmrSectionLibrary.record_type_code == rt))

    if q:
        s = f"%{q.strip().lower()}%"
        qry = qry.filter(or_(
            func.lower(EmrSectionLibrary.label).like(s),
            func.lower(EmrSectionLibrary.code).like(s),
            func.lower(func.coalesce(EmrSectionLibrary.keywords, "")).like(s),
        ))

    return (
        qry.order_by(EmrSectionLibrary.display_order.asc(), EmrSectionLibrary.label.asc())
        .limit(int(limit))
        .all()
    )


def create_section(db: Session, *, inp: Dict[str, Any]) -> EmrSectionLibrary:
    has_phase = _has_column(db, "emr_section_library", "phase_code")

    label = _clean_label(inp.get("label"))
    if len(label) < 2:
        raise HTTPException(status_code=422, detail="label is required (min 2 chars)")

    code = norm_code(inp.get("code")) or generate_unique_code(db, model=EmrSectionLibrary, code_field="code", label=label)
    dept_code = norm_code(inp.get("dept_code")) or None
    record_type_code = norm_code(inp.get("record_type_code")) or None
    phase_code = norm_code(inp.get("phase_code")) or None

    if phase_code and has_phase:
        ph = db.query(EmrClinicalPhase).filter(
            EmrClinicalPhase.code == phase_code,
            EmrClinicalPhase.is_active == True
        ).first()
        if not ph:
            raise HTTPException(status_code=422, detail="phase_code not found/active")

    exists = db.query(EmrSectionLibrary).filter(EmrSectionLibrary.code == code).first()
    if exists:
        raise HTTPException(status_code=409, detail="section code already exists")

    kwargs = dict(
        code=code,
        label=label,
        dept_code=dept_code,
        record_type_code=record_type_code,
        group=(inp.get("group") or "CUSTOM"),
        keywords=(inp.get("keywords") or None),
        is_active=bool(inp.get("is_active", True)),
        display_order=int(inp.get("display_order", 1000)),
        created_at=_now(),
        updated_at=_now(),
    )

    # Only include phase_code if DB supports it
    if has_phase:
        kwargs["phase_code"] = phase_code

    row = EmrSectionLibrary(**kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------- PRESETS ----------------

def list_presets(
    db: Session,
    *,
    active: Optional[bool] = True,
    dept_code: Optional[str] = None,
    record_type_code: Optional[str] = None,
) -> List[EmrTemplatePreset]:
    qry = db.query(EmrTemplatePreset)
    if active is not None:
        qry = qry.filter(EmrTemplatePreset.is_active == bool(active))

    dc = norm_code(dept_code) if dept_code else None
    rt = norm_code(record_type_code) if record_type_code else None

    # match: global presets OR matching scope
    if dc:
        qry = qry.filter(or_(EmrTemplatePreset.dept_code.is_(None), EmrTemplatePreset.dept_code == dc))
    if rt:
        qry = qry.filter(or_(EmrTemplatePreset.record_type_code.is_(None), EmrTemplatePreset.record_type_code == rt))

    return qry.order_by(EmrTemplatePreset.display_order.asc(), EmrTemplatePreset.label.asc()).all()


def preset_to_out(p: EmrTemplatePreset) -> Dict[str, Any]:
    try:
        sections = json.loads(p.sections_json or "[]")
        if not isinstance(sections, list):
            sections = []
    except Exception:
        sections = []
    try:
        schema = json.loads(p.schema_json) if p.schema_json else None
    except Exception:
        schema = None

    return {
        "code": p.code,
        "label": p.label,
        "description": p.description,
        "dept_code": p.dept_code,
        "record_type_code": p.record_type_code,
        "sections": [str(x) for x in sections if str(x).strip()],
        "schema": schema,
        "display_order": p.display_order,
        "is_active": p.is_active,
    }


# ---------------- BOOTSTRAP META ----------------

def meta_bootstrap(db: Session, *, active: bool = True) -> Dict[str, Any]:
    deps = (
        db.query(EmrDepartment)
        .filter(EmrDepartment.is_active == bool(active))
        .order_by(EmrDepartment.display_order.asc(), EmrDepartment.name.asc())
        .all()
    )
    rts = (
        db.query(EmrRecordType)
        .filter(EmrRecordType.is_active == bool(active))
        .order_by(EmrRecordType.display_order.asc(), EmrRecordType.label.asc())
        .all()
    )

    # tones map
    tones = {t.dept_code: t for t in db.query(EmrDepartmentTone).all()}

    dep_out = []
    for d in deps:
        t = tones.get(d.code)
        dep_out.append({
            "id": d.id,
            "code": d.code,
            "name": d.name,
            "is_active": d.is_active,
            "display_order": d.display_order,
            "tone": {
                "bar": t.bar, "chip": t.chip, "glow": t.glow, "btn": t.btn
            } if t else None
        })

    rt_out = [{
        "id": r.id,
        "code": r.code,
        "label": r.label,
        "category": getattr(r, "category", None),
        "is_active": r.is_active,
        "display_order": r.display_order,
    } for r in rts]

    phases = list_phases(db, active=active)
    presets = [preset_to_out(x) for x in list_presets(db, active=active)]

    return {
        "departments": dep_out,
        "record_types": rt_out,
        "phases": [{
            "code": p.code, "label": p.label, "hint": p.hint,
            "display_order": p.display_order, "is_active": p.is_active
        } for p in phases],
        "presets": presets,
    }


# ---------------- PREVIEW (REVIEW PANEL) ----------------

def template_preview(
    db: Session,
    *,
    dept_code: str,
    record_type_code: str,
    sections: List[Any],
    schema_json: Any,
) -> Dict[str, Any]:
    has_phase = _has_column(db, "emr_section_library", "phase_code")

    dc = norm_code(dept_code)
    rt = norm_code(record_type_code)

    lib = list_sections(db, active=True, dept_code=dc, record_type_code=rt, limit=500)

    # build lookup maps without touching phase_code when missing
    by_code = {norm_code(x.code): x for x in lib}
    by_label = {x.label.strip().lower(): x for x in lib}

    normalized = []
    for s in sections or []:
        if isinstance(s, dict):
            code = norm_code(s.get("code"))
            label = _clean_label(s.get("label") or "")
        else:
            code = ""
            label = _clean_label(s)

        row = None
        if code and code in by_code:
            row = by_code[code]
        elif label and label.lower() in by_label:
            row = by_label[label.lower()]

        if row:
            phase_val = (row.phase_code if has_phase else None)  # only access if exists
            normalized.append({
                "code": row.code,
                "label": row.label,
                "phase": phase_val or "PLAN",
            })
        else:
            normalized.append({
                "code": code or None,
                "label": label or "(Unnamed)",
                "phase": "PLAN",
            })

    # basic phase grouping (still works even if phase_code missing)
    grouped: Dict[str, List[str]] = {}
    for x in normalized:
        ph = norm_code(x.get("phase") or "PLAN")
        grouped.setdefault(ph, []).append(x["label"])

    phases = db.query(EmrClinicalPhase).filter(EmrClinicalPhase.is_active == True).order_by(
        EmrClinicalPhase.display_order.asc()
    ).all()
    phase_order = [p.code for p in phases] or ["INTAKE", "HISTORY", "EXAM", "ASSESS", "PLAN", "DISCHARGE"]

    phase_summary = []
    for ph in phase_order:
        titles = grouped.get(ph, [])
        if titles:
            label = next((p.label for p in phases if p.code == ph), ph)
            hint = next((p.hint for p in phases if p.code == ph), None)
            phase_summary.append({"phase": ph, "label": label, "hint": hint, "count": len(titles), "titles": titles})

    warnings: List[str] = []
    try:
        if isinstance(schema_json, str):
            json.loads(schema_json)
    except Exception as e:
        warnings.append(f"Schema JSON invalid: {str(e)}")

    return {
        "sections": normalized,
        "phase_summary": phase_summary,
        "warnings": warnings,
        "publish_ready": len([w for w in warnings if "invalid" in w.lower()]) == 0,
        "meta": {"has_phase_code_column": has_phase},
    }