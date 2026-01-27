from __future__ import annotations

import json
import re
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.models.emr_meta import EmrSectionLibrary  # you already have this model
from app.models.emr_template_library import EmrTemplateBlock


ALLOWED_FIELD_TYPES = {
    "text",
    "textarea",
    "number",
    "date",
    "time",
    "datetime",
    "boolean",
    "select",
    "multiselect",
    "radio",
    "chips",
    "table",
    "group",
    "signature",
    "file",
    "image",
    "calculation",
    "graph",
    "chart",
}

MAX_SECTIONS = 60
MAX_FIELDS = 1200  # supports huge case sheets safely


def norm_code(s: str) -> str:
    return (s or "").strip().upper().replace(" ", "_")


def norm_key(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-zA-Z0-9_\.]", "", s)
    return s


def _loads_any(v: Any, default: Any):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default
    return default


def _dumps_canon(obj: Any) -> str:
    # stable JSON for hashing
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_schema(schema_obj: Dict[str, Any]) -> str:
    return hashlib.sha256(_dumps_canon(schema_obj).encode("utf-8")).hexdigest()


def _section_label_from_library(
    db: Session, *, code: str, dept_code: str, record_type_code: str
) -> Optional[str]:
    # priority: exact scoped match -> dept-only -> type-only -> global
    row = (
        db.query(EmrSectionLibrary)
        .filter(
            EmrSectionLibrary.code == code,
            EmrSectionLibrary.is_active.is_(True),
            or_(
                # exact
                (EmrSectionLibrary.dept_code == dept_code) & (EmrSectionLibrary.record_type_code == record_type_code),
                # dept-only
                (EmrSectionLibrary.dept_code == dept_code) & (EmrSectionLibrary.record_type_code.is_(None)),
                # type-only
                (EmrSectionLibrary.dept_code.is_(None)) & (EmrSectionLibrary.record_type_code == record_type_code),
                # global
                (EmrSectionLibrary.dept_code.is_(None)) & (EmrSectionLibrary.record_type_code.is_(None)),
            ),
        )
        .order_by(
            # prefer most specific
            (EmrSectionLibrary.dept_code.isnot(None)).desc(),
            (EmrSectionLibrary.record_type_code.isnot(None)).desc(),
            EmrSectionLibrary.display_order.asc(),
        )
        .first()
    )
    return row.label if row else None


def _get_block_schema(db: Session, *, block_code: str, dept_code: str, record_type_code: str) -> Dict[str, Any]:
    bc = norm_code(block_code)
    b = (
        db.query(EmrTemplateBlock)
        .filter(
            EmrTemplateBlock.code == bc,
            EmrTemplateBlock.is_active.is_(True),
            or_(
                # allow scoped/global blocks
                (EmrTemplateBlock.dept_code == dept_code) | (EmrTemplateBlock.dept_code.is_(None)),
            ),
            or_(
                (EmrTemplateBlock.record_type_code == record_type_code) | (EmrTemplateBlock.record_type_code.is_(None)),
            ),
        )
        .order_by(
            (EmrTemplateBlock.dept_code.isnot(None)).desc(),
            (EmrTemplateBlock.record_type_code.isnot(None)).desc(),
            EmrTemplateBlock.display_order.asc(),
        )
        .first()
    )
    if not b:
        raise HTTPException(status_code=404, detail=f"Block not found: {bc}")

    obj = _loads_any(b.schema_json, {})
    if not isinstance(obj, dict):
        raise HTTPException(status_code=422, detail=f"Block schema invalid: {bc}")
    return obj

PHASE_SET = {
    "INTAKE",
    "HISTORY",
    "EXAM",
    "ASSESSMENT",
    "PLAN",
    "ORDERS",
    "NURSING",
    "DISCHARGE",
    "ATTACHMENTS",
    "SIGN_OFF",
    "OTHER",
}

def normalize_template_schema(
    db: Session,
    *,
    dept_code: str,
    record_type_code: str,
    schema_input: Any,
    sections_input: Any,
    strict: bool = False,
    **_ignored: Any,
) -> Dict[str, Any]:
    """
    Accepts:
      - schema_input: dict or JSON string (preferred)
      - sections_input: list or CSV/JSON string (fallback)
    Returns a normalized schema dict.

    Supported schema format (v1):
    {
      "schema_version": 1,
      "title": "...",
      "sections": [
         {
           "code": "HPI",
           "label": "History of Present Illness",
           "items": [
              {"kind":"field","key":"chief_complaint","type":"text","label":"Chief Complaint","required":true},
              {"kind":"block","code":"BLOCK_VITALS"}
           ]
         }
      ],
      "rules": [
         {"if":{"field":"pregnant","eq":true},"then":{"show":["ga_weeks","edd"],"require":["ga_weeks"]}}
      ],
      "ui": {"layout":"two-column"}
    }
    """
    dept_code = norm_code(dept_code)
    record_type_code = norm_code(record_type_code)

    # sections_input normalize
    sec_codes: List[str] = []
    raw_sections = sections_input
    if isinstance(raw_sections, list):
        sec_codes = [norm_code(str(x)) for x in raw_sections if str(x).strip()]
    elif isinstance(raw_sections, str):
        s = raw_sections.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    sec_codes = [norm_code(str(x)) for x in arr if str(x).strip()]
            except Exception:
                sec_codes = []
        if not sec_codes:
            sec_codes = [norm_code(x) for x in s.split(",") if x.strip()]

    # schema_input normalize
    schema_obj = _loads_any(schema_input, {})
    if not isinstance(schema_obj, dict):
        raise HTTPException(status_code=422, detail="schema_json must be an object")

    schema_version = int(schema_obj.get("schema_version") or 1)
    if schema_version != 1:
        raise HTTPException(status_code=422, detail="Unsupported schema_version (only v1 supported now)")

    # if frontend didn’t send sections in schema, build from selected sections
    sections = schema_obj.get("sections")
    if not isinstance(sections, list) or not sections:
        # build skeleton from sec_codes
        if not sec_codes:
            raise HTTPException(status_code=422, detail="Add at least one section (sections or schema.sections)")
        sections = [{"code": c, "label": None, "items": []} for c in sec_codes]

    if len(sections) > MAX_SECTIONS:
        raise HTTPException(status_code=413, detail=f"Too many sections (max {MAX_SECTIONS})")

    normalized_sections: List[Dict[str, Any]] = []
    seen_section_codes = set()
    all_field_keys = set()
    field_count = 0
    warnings: List[str] = []

    def add_field(sec_code: str, f: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a single field (including nested group items / table columns).
        Keeps only supported keys and enforces a stable, backend-friendly shape.
        """
        if not isinstance(f, dict):
            raise ValueError("Field must be an object")

        ftype = str(f.get("type") or "text").strip().lower()
        if ftype == "graph":
            ftype = "chart"

        if ftype not in ALLOWED_FIELD_TYPES:
            raise ValueError(f"Unknown field type: {ftype}")

        key = str(f.get("key") or "").strip()
        label = str(f.get("label") or "").strip()
        if not key:
            raise ValueError("Field key is required")

        # Common base
        out: Dict[str, Any] = {
            "kind": "field",
            "type": ftype,
            "key": key,
            "label": label or key.replace("_", " ").title(),
        }

        # common optional props
        if f.get("help_text") not in (None, ""):
            out["help_text"] = str(f.get("help_text"))

        if f.get("placeholder") not in (None, ""):
            out["placeholder"] = str(f.get("placeholder"))

        if "default_value" in f:
            out["default_value"] = f.get("default_value")

        if f.get("required") is not None:
            out["required"] = bool(f.get("required"))

        if f.get("readonly") is not None:
            out["readonly"] = bool(f.get("readonly"))

        # Rules/UI/Clinical
        rules = f.get("rules")
        if isinstance(rules, dict) and rules:
            out["rules"] = rules

        ui = f.get("ui")
        if isinstance(ui, dict) and ui:
            out["ui"] = ui

        clinical = f.get("clinical")
        if isinstance(clinical, dict) and clinical:
            out["clinical"] = clinical

        # Choice fields
        if ftype in {"select", "multiselect", "radio", "chips"}:
            options = f.get("options") or []
            if not isinstance(options, list):
                raise ValueError("options must be a list")
            norm_opts = []
            for o in options:
                if isinstance(o, dict):
                    ov = str(o.get("value") or "").strip()
                    ol = str(o.get("label") or "").strip()
                else:
                    ov = str(o).strip()
                    ol = str(o).strip()
                if not ov:
                    continue
                norm_opts.append({"value": ov, "label": ol or ov})
            out["options"] = norm_opts

            choice = f.get("choice")
            if isinstance(choice, dict) and choice:
                out["choice"] = choice

        # Table fields
        if ftype == "table":
            table = f.get("table") or {}
            if not isinstance(table, dict):
                raise ValueError("table must be an object")

            cols = table.get("columns") or []
            if not isinstance(cols, list):
                raise ValueError("table.columns must be a list")

            norm_cols = []
            for c in cols:
                if not isinstance(c, dict):
                    raise ValueError("table column must be an object")
                ctype = str(c.get("type") or "text").strip().lower()
                if ctype == "graph":
                    ctype = "chart"
                # table columns are limited (keep it conservative)
                if ctype not in {"text", "number", "date", "time", "datetime", "boolean", "select", "radio"}:
                    raise ValueError(f"Invalid table column type: {ctype}")

                ckey = str(c.get("key") or "").strip()
                clabel = str(c.get("label") or "").strip()
                if not ckey:
                    raise ValueError("table column key is required")

                col_out: Dict[str, Any] = {
                    "key": ckey,
                    "label": clabel or ckey.replace("_", " ").title(),
                    "type": ctype,
                    "required": bool(c.get("required") or False),
                }

                if ctype in {"select", "radio"}:
                    copts = c.get("options") or []
                    if not isinstance(copts, list):
                        raise ValueError("table column options must be a list")
                    norm_copts = []
                    for o in copts:
                        if isinstance(o, dict):
                            ov = str(o.get("value") or "").strip()
                            ol = str(o.get("label") or "").strip()
                        else:
                            ov = str(o).strip()
                            ol = str(o).strip()
                        if not ov:
                            continue
                        norm_copts.append({"value": ov, "label": ol or ov})
                    col_out["options"] = norm_copts

                norm_cols.append(col_out)

            out["table"] = {
                "min_rows": int(table.get("min_rows") or 0),
                "max_rows": int(table.get("max_rows") or 0),
                "allow_add_row": bool(table.get("allow_add_row", True)),
                "allow_delete_row": bool(table.get("allow_delete_row", True)),
                "columns": norm_cols,
            }

        # Group fields (nested items)
        if ftype == "group":
            group = f.get("group") or {}
            if group is not None and not isinstance(group, dict):
                raise ValueError("group must be an object")

            items = f.get("items") or []
            if not isinstance(items, list):
                raise ValueError("group.items must be a list")

            # Normalize children recursively
            norm_items = []
            seen_keys = set()
            for child in items:
                child_norm = add_field(sec_code, child)
                ck = child_norm.get("key")
                if ck in seen_keys:
                    raise ValueError(f"Duplicate key inside group: {ck}")
                seen_keys.add(ck)
                norm_items.append(child_norm)

            out["group"] = {
                "layout": str((group or {}).get("layout") or "STACK"),
                "collapsible": bool((group or {}).get("collapsible", False)),
                "collapsed_by_default": bool((group or {}).get("collapsed_by_default", False)),
            }
            out["items"] = norm_items

        # Signature/File/Image/Calculation/Chart (keep config blobs; runtime can validate deeper)
        if ftype == "signature":
            sig = f.get("signature") or {}
            if sig is not None and not isinstance(sig, dict):
                raise ValueError("signature must be an object")
            if sig:
                out["signature"] = sig

        if ftype == "file":
            cfg = f.get("file") or {}
            if cfg is not None and not isinstance(cfg, dict):
                raise ValueError("file must be an object")
            if cfg:
                out["file"] = cfg

        if ftype == "image":
            cfg = f.get("image") or {}
            if cfg is not None and not isinstance(cfg, dict):
                raise ValueError("image must be an object")
            if cfg:
                out["image"] = cfg

        if ftype == "calculation":
            cfg = f.get("calculation") or {}
            if cfg is not None and not isinstance(cfg, dict):
                raise ValueError("calculation must be an object")
            if cfg:
                out["calculation"] = cfg

        if ftype == "chart":
            cfg = f.get("chart") or {}
            if cfg is not None and not isinstance(cfg, dict):
                raise ValueError("chart must be an object")
            if cfg:
                out["chart"] = cfg

        return out

    for s in sections:
        if not isinstance(s, dict):
            raise HTTPException(status_code=422, detail="Each section must be an object")
        sec_code = norm_code(str(s.get("code") or ""))
        if not sec_code:
            raise HTTPException(status_code=422, detail="Section code is required")
        if sec_code in seen_section_codes:
            raise HTTPException(status_code=409, detail=f"Duplicate section code: {sec_code}")
        seen_section_codes.add(sec_code)

        sec_label = (s.get("label") or "").strip() if isinstance(s.get("label"), str) else ""
        if not sec_label:
            lib_label = _section_label_from_library(db, code=sec_code, dept_code=dept_code, record_type_code=record_type_code)
            sec_label = lib_label or sec_code

        # items can be in "items" or "fields"
        items = s.get("items")
        if items is None:
            items = s.get("fields")
        items = items if isinstance(items, list) else []

        norm_items: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            kind = str(it.get("kind") or "").strip().lower()

            # block reference support
            if kind == "block" or it.get("code") and it.get("kind") == "block":
                bcode = it.get("code") or it.get("block_code")
                if not bcode:
                    raise HTTPException(status_code=422, detail=f"Block code missing in section {sec_code}")
                bobj = _get_block_schema(db, block_code=str(bcode), dept_code=dept_code, record_type_code=record_type_code)
                # block schema may have "items" or "fields"
                b_items = bobj.get("items") if isinstance(bobj.get("items"), list) else bobj.get("fields")
                b_items = b_items if isinstance(b_items, list) else []
                for bf in b_items:
                    if isinstance(bf, dict):
                        norm_items.append(add_field(sec_code, bf))
                continue

            # normal field
            norm_items.append(add_field(sec_code, it))

        sec_phase = str(s.get("phase") or "").strip().upper()
        if sec_phase and sec_phase not in PHASE_SET:
            raise HTTPException(status_code=422, detail=f"Invalid phase '{sec_phase}' for section {sec_code}")
        # default phase if not provided (safe for old templates)
        if not sec_phase:
            sec_phase = "HISTORY"

        normalized_sections.append({
            "code": sec_code,
            "label": sec_label,
            "phase": sec_phase,  # ✅ NEW: keep phase
            "layout": s.get("layout") or None,
            "repeatable": bool(s.get("repeatable") or False),
            "items": norm_items,
        })

    # rules + ui are optional
    rules = schema_obj.get("rules")
    if rules is not None and not isinstance(rules, list):
        raise HTTPException(status_code=422, detail="rules must be an array")
    ui = schema_obj.get("ui")
    if ui is not None and not isinstance(ui, dict):
        raise HTTPException(status_code=422, detail="ui must be an object")

    # helpful warning
    if field_count == 0:
        warnings.append("No fields found. Add fields or insert blocks to make this template usable.")

    out = {
        "schema_version": 1,
        "dept_code": dept_code,
        "record_type_code": record_type_code,
        "title": schema_obj.get("title"),
        "sections": normalized_sections,
        "rules": rules or [],
        "ui": ui or {},
        "stats": {
            "sections": len(normalized_sections),
            "fields": field_count,
        },
        "schema_hash": _hash_schema({
            "schema_version": 1,
            "sections": normalized_sections,
            "rules": rules or [],
            "ui": ui or {},
        }),
        "warnings": warnings,
    }
    return out


# -------------------------
# Section Library services
# -------------------------
def section_library_list(
    db: Session,
    *,
    q: str = "",
    dept_code: str = "ALL",
    record_type_code: str = "ALL",
    active: Optional[bool] = True,
    limit: int = 200
) -> List[EmrSectionLibrary]:
    qry = db.query(EmrSectionLibrary)
    if active is not None:
        qry = qry.filter(EmrSectionLibrary.is_active.is_(bool(active)))

    if dept_code and dept_code.upper() != "ALL":
        qry = qry.filter(or_(EmrSectionLibrary.dept_code == norm_code(dept_code), EmrSectionLibrary.dept_code.is_(None)))
    if record_type_code and record_type_code.upper() != "ALL":
        qry = qry.filter(or_(EmrSectionLibrary.record_type_code == norm_code(record_type_code), EmrSectionLibrary.record_type_code.is_(None)))

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrSectionLibrary.code.ilike(qq), EmrSectionLibrary.label.ilike(qq), EmrSectionLibrary.group.ilike(qq)))

    return qry.order_by(EmrSectionLibrary.display_order.asc(), EmrSectionLibrary.label.asc()).limit(int(limit)).all()


def section_library_create(db: Session, *, payload: Dict[str, Any]) -> EmrSectionLibrary:
    code = norm_code(str(payload.get("code") or ""))
    label = (payload.get("label") or "").strip()
    if not code or not label:
        raise HTTPException(status_code=422, detail="code and label required")

    exists = db.query(EmrSectionLibrary.id).filter(func.upper(EmrSectionLibrary.code) == code).first()
    if exists:
        raise HTTPException(status_code=409, detail="Section code already exists")

    row = EmrSectionLibrary(
        code=code,
        label=label,
        dept_code=norm_code(payload["dept_code"]) if payload.get("dept_code") else None,
        record_type_code=norm_code(payload["record_type_code"]) if payload.get("record_type_code") else None,
        group=(payload.get("group") or None),
        is_active=bool(payload.get("is_active", True)),
        display_order=int(payload.get("display_order") or 1000),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def section_library_update(db: Session, *, section_id: int, payload: Dict[str, Any]) -> EmrSectionLibrary:
    row = db.query(EmrSectionLibrary).filter(EmrSectionLibrary.id == int(section_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Section not found")

    if payload.get("label") is not None:
        lab = (payload.get("label") or "").strip()
        if not lab:
            raise HTTPException(status_code=422, detail="label required")
        row.label = lab

    for k in ("dept_code", "record_type_code", "group"):
        if payload.get(k) is not None:
            val = payload.get(k)
            if val is None or (isinstance(val, str) and not val.strip()):
                setattr(row, k, None)
            else:
                setattr(row, k, norm_code(val) if k in ("dept_code", "record_type_code") else str(val).strip())

    if payload.get("is_active") is not None:
        row.is_active = bool(payload.get("is_active"))

    if payload.get("display_order") is not None:
        row.display_order = int(payload.get("display_order"))

    db.commit()
    db.refresh(row)
    return row


# -------------------------
# Block Library services
# -------------------------
def block_list(
    db: Session,
    *,
    q: str = "",
    dept_code: str = "ALL",
    record_type_code: str = "ALL",
    active: Optional[bool] = True,
    page: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)

    qry = db.query(EmrTemplateBlock)

    if active is not None:
        qry = qry.filter(EmrTemplateBlock.is_active.is_(bool(active)))

    if dept_code and dept_code.upper() != "ALL":
        dc = norm_code(dept_code)
        qry = qry.filter(or_(EmrTemplateBlock.dept_code == dc, EmrTemplateBlock.dept_code.is_(None)))

    if record_type_code and record_type_code.upper() != "ALL":
        tc = norm_code(record_type_code)
        qry = qry.filter(or_(EmrTemplateBlock.record_type_code == tc, EmrTemplateBlock.record_type_code.is_(None)))

    if q:
        qq = f"%{q.strip()}%"
        qry = qry.filter(or_(EmrTemplateBlock.code.ilike(qq), EmrTemplateBlock.label.ilike(qq), EmrTemplateBlock.group.ilike(qq)))

    total = int(qry.count())
    rows = (
        qry.order_by(EmrTemplateBlock.display_order.asc(), EmrTemplateBlock.label.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [{
        "id": int(b.id),
        "code": b.code,
        "label": b.label,
        "description": b.description,
        "dept_code": b.dept_code,
        "record_type_code": b.record_type_code,
        "group": b.group,
        "is_active": bool(b.is_active),
        "display_order": int(b.display_order),
        "schema_json": b.schema_json,
        "created_at": b.created_at,
        "updated_at": b.updated_at,
    } for b in rows]

    return {"items": items, "page": page, "page_size": page_size, "total": total}


def block_create(db: Session, *, payload: Dict[str, Any]) -> EmrTemplateBlock:
    code = norm_code(str(payload.get("code") or ""))
    label = (payload.get("label") or "").strip()
    if not code or not label:
        raise HTTPException(status_code=422, detail="code and label required")

    ex = db.query(EmrTemplateBlock.id).filter(EmrTemplateBlock.code == code).first()
    if ex:
        raise HTTPException(status_code=409, detail="Block code already exists")

    # schema must be valid JSON object
    obj = _loads_any(payload.get("schema_json"), {})
    if not isinstance(obj, dict):
        raise HTTPException(status_code=422, detail="schema_json must be an object")

    row = EmrTemplateBlock(
        code=code,
        label=label,
        description=(payload.get("description") or None),
        dept_code=norm_code(payload["dept_code"]) if payload.get("dept_code") else None,
        record_type_code=norm_code(payload["record_type_code"]) if payload.get("record_type_code") else None,
        group=(payload.get("group") or None),
        is_active=bool(payload.get("is_active", True)),
        display_order=int(payload.get("display_order") or 1000),
        schema_json=json.dumps(obj, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def block_update(db: Session, *, block_id: int, payload: Dict[str, Any]) -> EmrTemplateBlock:
    row = db.query(EmrTemplateBlock).filter(EmrTemplateBlock.id == int(block_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Block not found")

    for k in ("label", "description", "group"):
        if payload.get(k) is not None:
            v = payload.get(k)
            setattr(row, k, (str(v).strip() if v is not None else None) or None)

    if payload.get("is_active") is not None:
        row.is_active = bool(payload.get("is_active"))

    if payload.get("display_order") is not None:
        row.display_order = int(payload.get("display_order"))

    if payload.get("schema_json") is not None:
        obj = _loads_any(payload.get("schema_json"), {})
        if not isinstance(obj, dict):
            raise HTTPException(status_code=422, detail="schema_json must be an object")
        row.schema_json = json.dumps(obj, ensure_ascii=False)

    db.commit()
    db.refresh(row)
    return row


def block_deactivate(db: Session, *, block_id: int) -> None:
    row = db.query(EmrTemplateBlock).filter(EmrTemplateBlock.id == int(block_id)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Block not found")
    row.is_active = False
    db.commit()


# -------------------------
# Suggested skeleton builder
# -------------------------
DEFAULT_TEMPLATE_SECTIONS_BY_TYPE = {
    "OPD_NOTE": ["VITALS", "HPI", "ROS", "EXAM", "ASSESSMENT", "PLAN"],
    "IPD_NOTE": ["VITALS", "PROGRESS", "EXAM", "ASSESSMENT", "PLAN"],
    "DISCHARGE_SUMMARY": ["DIAGNOSIS", "COURSE", "PROCEDURES", "MEDICATIONS", "FOLLOW_UP"],
    "NURSING_NOTE": ["NURSING_ASSESSMENT", "VITALS", "INTAKE_OUTPUT", "CARE_PLAN"],
}


def suggest_template_schema(db: Session, *, dept_code: str, record_type_code: str) -> Dict[str, Any]:
    rt = norm_code(record_type_code)
    secs = DEFAULT_TEMPLATE_SECTIONS_BY_TYPE.get(rt) or ["VITALS", "NOTE"]

    # build minimal schema (fields empty) – doctor can insert blocks/fields
    schema = {
        "schema_version": 1,
        "title": f"{rt} Template",
        "sections": [{"code": s, "items": []} for s in secs],
        "rules": [],
        "ui": {"layout": "two-column"},
    }

    # normalize + attach labels from section library
    return normalize_template_schema(
        db,
        dept_code=dept_code,
        record_type_code=record_type_code,
        schema_input=schema,
        sections_input=secs,
    )
