# FILE: app/api/routes_emr_template_library.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.api.emr_router_utils import as_json_obj, as_list, code_or_none, need_any, norm_code
from app.models.user import User
from app.schemas.emr_template_library import (
    BlockCreateIn,
    BlockOut,
    BlockUpdateIn,
    SectionCreateIn,
    SectionOut,
    SectionUpdateIn,
    TemplateSchemaValidateIn,
)
from app.services.emr_all_service import patient_encounters, template_presets, template_preview
from app.services.emr_template_builder import (
    block_create,
    block_deactivate,
    block_list,
    block_update,
    normalize_template_schema,
    section_library_create,
    section_library_list,
    section_library_update,
    suggest_template_schema,
)
from app.utils.respo import err, ok

router = APIRouter(prefix="/emr", tags=["EMR Templates"])

def _safe_norm_code(v: str) -> str:
    # never throws; compatible with frontend expectations
    return (v or "").strip().upper().replace(" ", "_")


# -----------------------
# Request Models
# -----------------------
class TemplatePreviewIn(BaseModel):
    """
    Backward compatible with common frontend payload keys:
      dept_code or dept
      record_type_code or type
      sections or sections_json
      schema_json (can be dict OR JSON string)
    """
    model_config = ConfigDict(extra="ignore")

    dept_code: str = Field(..., validation_alias=AliasChoices("dept_code", "dept"))
    record_type_code: str = Field(..., validation_alias=AliasChoices("record_type_code", "record_type", "type"))

    sections: List[Any] = Field(default_factory=list, validation_alias=AliasChoices("sections", "sections_json"))
    schema_json: Any = Field(default_factory=dict, validation_alias=AliasChoices("schema_json", "schema_input", "schema"))

    @field_validator("dept_code", "record_type_code", mode="before")
    @classmethod
    def v_codes(cls, v: Any) -> str:
        return norm_code(v)

    @field_validator("sections", mode="before")
    @classmethod
    def v_sections(cls, v: Any) -> List[Any]:
        return as_list(v)

    @field_validator("schema_json", mode="before")
    @classmethod
    def v_schema(cls, v: Any) -> Any:
        return as_json_obj(v, default={})


def _dump_items(rows: Any, model) -> List[Dict[str, Any]]:
    """
    Accept list[ORM] or list[dict]; return list[dict].
    """
    out: List[Dict[str, Any]] = []
    for r in (rows or []):
        if isinstance(r, dict):
            out.append(r)
        else:
            out.append(model.model_validate(r).model_dump())
    return out


# -----------------------
# Templates: validate / preview / presets / suggest
# -----------------------
@router.post("/templates/validate")
def api_template_validate(
    payload: TemplateSchemaValidateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        norm = normalize_template_schema(
            db,
            dept_code=payload.dept_code,
            record_type_code=payload.record_type_code,
            schema_input=payload.schema_json,
            sections_input=payload.sections,
        )
        return ok(norm, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template validate failed: {ex}", 500)


@router.post("/templates/preview")
async def api_template_preview(
    request: Request,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    # ✅ fallback support if frontend accidentally sends query params
    dept_code: Optional[str] = Query(default=None),
    record_type_code: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])

        # ✅ if body is missing, try reading JSON manually (or fallback to empty)
        if payload is None:
            try:
                payload = await request.json()
            except Exception:
                payload = {}

        # ✅ accept multiple aliases from frontend
        dept = payload.get("dept_code") or payload.get("dept") or dept_code
        rtype = (
            payload.get("record_type_code")
            or payload.get("record_type")
            or payload.get("type")
            or record_type_code
        )

        if not dept or not rtype:
            raise HTTPException(
                status_code=422,
                detail="dept_code and record_type_code are required (aliases accepted: dept, type)",
            )

        dept = norm_code(dept)
        rtype = norm_code(rtype)

        sections_raw = payload.get("sections") or payload.get("sections_json") or []
        schema_raw = payload.get("schema_json") or payload.get("schema_input") or payload.get("schema") or {}

        sections = as_list(sections_raw)
        schema_obj = as_json_obj(schema_raw, default={})

        out = template_preview(
            db,
            dept_code=dept,
            record_type_code=rtype,
            schema_input=schema_obj,
            sections_input=sections,
        )
        return ok(out, 200)

    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template preview failed: {ex}", 500)


@router.get("/templates/presets")
def api_template_presets(
    dept_code: str = Query(..., max_length=64),
    record_type_code: str = Query(..., max_length=64),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.view", "emr.templates.view", "emr.templates.manage", "emr.manage"])

        # ✅ robust normalization (never 422 due to norm_code)
        try:
            d = norm_code(dept_code)
        except Exception:
            d = _safe_norm_code(dept_code)

        try:
            r = norm_code(record_type_code)
        except Exception:
            r = _safe_norm_code(record_type_code)

        out = template_presets(db, dept_code=d, record_type_code=r)
        return ok(out, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template presets failed: {ex}", 500)

@router.get("/templates/suggest")
def api_template_suggest(
    dept_code: str = Query(..., max_length=64),
    record_type_code: str = Query(..., max_length=64),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])

        # ✅ robust normalization (never 422 due to norm_code)
        try:
            d = norm_code(dept_code)
        except Exception:
            d = _safe_norm_code(dept_code)

        try:
            r = norm_code(record_type_code)
        except Exception:
            r = _safe_norm_code(record_type_code)

        out = suggest_template_schema(db, dept_code=d, record_type_code=r)
        return ok(out, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template suggest failed: {ex}", 500)


# -----------------------
# Section Library (canonical + legacy aliases)
# Canonical: /emr/library/sections
# Legacy:    /emr/sections/library
# -----------------------
@router.get("/section-library")
@router.get("/library/sections")
@router.get("/sections/library")
def api_section_library_list(
    q: str = Query(default="", max_length=120),
    dept_code: Optional[str] = Query(default=None, max_length=64),
    record_type_code: Optional[str] = Query(default=None, max_length=64),
    # frontend-friendly aliases
    dept: Optional[str] = Query(default=None, max_length=64),
    record_type: Optional[str] = Query(default=None, alias="type", max_length=64),
    active: Optional[bool] = Query(default=True),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.view", "emr.templates.view", "emr.templates.manage", "emr.manage"])
        d = code_or_none(dept_code or dept)
        t = code_or_none(record_type_code or record_type)

        rows = section_library_list(db, q=q, dept_code=d, record_type_code=t, active=active)
        if isinstance(rows, list) and limit:
            rows = rows[: int(limit)]

        items = _dump_items(rows, SectionOut)
        return ok({"items": items, "count": len(items)}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Section list failed: {ex}", 500)


@router.post("/library/sections")
@router.post("/sections/library")
def api_section_library_create(
    payload: SectionCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        row = section_library_create(db, payload=payload.to_db_dict())
        return ok(SectionOut.model_validate(row).model_dump(), 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Section create failed: {ex}", 500)


@router.put("/library/sections/{section_id}")
@router.put("/sections/library/{section_id}")
def api_section_library_update(
    section_id: int,
    payload: SectionUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        row = section_library_update(db, section_id=int(section_id), payload=payload.model_dump(exclude_unset=True))
        return ok(SectionOut.model_validate(row).model_dump(), 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Section update failed: {ex}", 500)


# -----------------------
# Block Library (canonical + legacy aliases)
# Canonical: /emr/library/blocks
# Legacy:    /emr/blocks/library
# -----------------------
@router.get("/library/blocks")
@router.get("/blocks/library")
def api_block_library_list(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    # legacy alias support
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    q: str = Query(default="", max_length=120),
    dept_code: Optional[str] = Query(default=None, max_length=64),
    record_type_code: Optional[str] = Query(default=None, max_length=64),
    dept: Optional[str] = Query(default=None, max_length=64),
    record_type: Optional[str] = Query(default=None, alias="type", max_length=64),
    group: Optional[str] = Query(default=None, max_length=50),
    active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.view", "emr.templates.view", "emr.templates.manage", "emr.manage"])
        size = int(limit or page_size)

        d = code_or_none(dept_code or dept)
        t = code_or_none(record_type_code or record_type)
        g = (group.strip() if isinstance(group, str) and group.strip() else None)

        data = block_list(
            db,
            q=q,
            dept_code=d,
            record_type_code=t,
            group=g,
            active=active,
            page=int(page),
            page_size=size,
        )

        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data["items"] = _dump_items(data["items"], BlockOut)

        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Block list failed: {ex}", 500)


@router.post("/library/blocks")
@router.post("/blocks/library")
def api_block_library_create(
    payload: BlockCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        row = block_create(db, payload=payload.to_db_dict())
        return ok(BlockOut.model_validate(row).model_dump(), 201)
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as ex:
        return err(f"Block create failed: {ex}", 500)


@router.put("/library/blocks/{block_id}")
@router.put("/blocks/library/{block_id}")
def api_block_library_update(
    block_id: int,
    payload: BlockUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        row = block_update(db, block_id=int(block_id), payload=payload.model_dump(exclude_unset=True))
        return ok(BlockOut.model_validate(row).model_dump(), 200)
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as ex:
        return err(f"Block update failed: {ex}", 500)


@router.delete("/library/blocks/{block_id}")
@router.delete("/blocks/library/{block_id}")
@router.post("/library/blocks/{block_id}/deactivate")
@router.post("/blocks/library/{block_id}/deactivate")
def api_block_library_deactivate(
    block_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.templates.manage", "emr.manage"])
        row = block_deactivate(db, block_id=int(block_id))
        # return updated row if available; else a simple flag
        if row is not None:
            return ok(BlockOut.model_validate(row).model_dump(), 200)
        return ok({"deactivated": True, "id": int(block_id)}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Block deactivate failed: {ex}", 500)


# -----------------------
# Patient Encounters (Create Record Flow)
# -----------------------
@router.get("/patients/{patient_id}/encounters")
def api_patient_encounters(
    patient_id: int,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        need_any(user, ["emr.view", "emr.records.view", "emr.manage"])
        items = patient_encounters(db, patient_id=int(patient_id), limit=int(limit))
        return ok({"patient_id": int(patient_id), "items": items}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Encounters fetch failed: {ex}", 500)
