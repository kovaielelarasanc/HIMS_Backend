from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Any, Dict, Set

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.api.deps import get_db, current_user
from app.models.ipd_newborn import IpdNewbornResuscitation, utcnow
from app.models.vital_audit import VitalEventAudit
from app.schemas.ipd_newborn import (
    ApiResponse, NewbornCreate, NewbornUpdate, NewbornOut, ActionNote, VoidRequest
)
from app.services.pdfs.ipd_newborn_resuscitation import build_pdf

from app.models.ui_branding import UiBranding

# Optional: for auto-fill from BirthRegister (if model exists)
try:
    from app.models.vitals_registers import BirthRegister
except Exception:
    BirthRegister = None


router = APIRouter(prefix="/ipd", tags=["IPD - Newborn"])


# ----------------------------
# Helpers (works for dict/user object)
# ----------------------------
def _uget(user: Any, key: str, default=None):
    if user is None:
        return default
    if isinstance(user, dict):
        return user.get(key, default)
    return getattr(user, key, default)


def _json_safe(v: Any) -> Any:
    """
    Converts nested datetime/date inside dict/list into ISO strings (JSON-safe).
    This is the KEY fix for your 500 error.
    """
    return jsonable_encoder(
        v,
        custom_encoder={
            datetime: lambda d: d.isoformat(),
            date: lambda d: d.isoformat(),
        },
    )


def _as_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list, str, int, float, bool)):
        return v
    # pydantic v2
    if hasattr(v, "model_dump"):
        return v.model_dump()
    # pydantic v1 fallback
    if hasattr(v, "dict"):
        return v.dict()
    return v


def _is_admin_user(user: Any) -> bool:
    for attr in ("is_superuser", "is_admin", "superuser", "admin"):
        if bool(_uget(user, attr, False)):
            return True

    for attr in ("role", "role_name", "role_code", "user_type", "type"):
        v = _uget(user, attr, None)
        if isinstance(v, str) and v.strip().lower() in {"admin", "administrator", "superadmin", "super_admin"}:
            return True

    # If modules says ipd manage, treat as admin-like for module checks
    mods = _collect_module_actions(user)
    ipd = mods.get("ipd", set())
    if "*" in ipd or "manage" in ipd:
        return True

    return False


def _collect_code_perms(user: Any) -> Set[str]:
    perm_set: Set[str] = set()

    for attr in ("perm_codes", "permissions", "perms"):
        node = _uget(user, attr, None)
        if not node:
            continue

        # list
        if isinstance(node, list):
            for x in node:
                if isinstance(x, str):
                    perm_set.add(x)
                elif isinstance(x, dict) and x.get("code"):
                    perm_set.add(str(x["code"]))
            continue

        # dict modules->...
        if isinstance(node, dict):
            for _k, val in node.items():
                if isinstance(val, list):
                    for x in val:
                        if isinstance(x, str):
                            perm_set.add(x)
                        elif isinstance(x, dict) and x.get("code"):
                            perm_set.add(str(x["code"]))
            continue

    return perm_set


def _collect_module_actions(user: Any) -> dict[str, Set[str]]:
    for attr in ("modules", "module_perms", "access", "scopes"):
        node = _uget(user, attr, None)
        if isinstance(node, dict):
            out: dict[str, Set[str]] = {}
            for m, actions in node.items():
                if isinstance(actions, list):
                    out[str(m).lower()] = {str(a).lower() for a in actions}
                elif isinstance(actions, dict):
                    out[str(m).lower()] = {str(k).lower() for k, v in actions.items() if v}
            return out
    return {}


def _has_module_action(mod_actions: dict[str, Set[str]], module: str, action: str) -> bool:
    module = (module or "").lower()
    action = (action or "").lower()
    acts = mod_actions.get(module, set())
    if "*" in acts or "manage" in acts:
        return True
    return action in acts


def _need_any(user: Any, codes: list[str]):
    # ✅ admin bypass
    if _is_admin_user(user):
        return

    perm_set = _collect_code_perms(user)
    if "*" in perm_set:
        return

    mod_actions = _collect_module_actions(user)

    for c in codes:
        if c in perm_set:
            return

        parts = (c or "").split(".")
        if len(parts) >= 2:
            module = parts[0]
            action = parts[-1]
            if _has_module_action(mod_actions, module, action):
                return
            if _has_module_action(mod_actions, module, "manage"):
                return

        if len(parts) == 2:
            module, action = parts
            if _has_module_action(mod_actions, module, action):
                return

    raise HTTPException(status_code=403, detail="Permission denied")


def _audit(
    db: Session,
    request: Request,
    entity_id: int,
    action: str,
    actor_user_id: Optional[int],
    before: Optional[Dict] = None,
    after: Optional[Dict] = None,
    note: Optional[str] = None,
):
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    db.add(
        VitalEventAudit(
            entity_type="ipd_newborn_resuscitation",
            entity_id=entity_id,
            action=action,
            actor_user_id=actor_user_id,
            ip_addr=ip,
            user_agent=ua,
            before=before,
            after=after,
            note=note,
            created_at=utcnow(),
        )
    )


def _snap(x: IpdNewbornResuscitation) -> Dict[str, Any]:
    return {
        "id": x.id,
        "admission_id": x.admission_id,
        "baby_patient_id": x.baby_patient_id,
        "date_of_birth": x.date_of_birth.isoformat() if x.date_of_birth else None,
        "sex": x.sex,
        "apgar_1_min": x.apgar_1_min,
        "apgar_5_min": x.apgar_5_min,
        "status": x.status,
    }


def _ensure_editable(x: IpdNewbornResuscitation):
    if x.status in ("FINALIZED", "VOIDED"):
        raise HTTPException(status_code=409, detail=f"Record locked (status={x.status}).")


def _branding_to_hospital_dict(b: Optional[UiBranding]) -> Dict[str, Any]:
    if not b:
        return {"name": "NUTRYAH Facility", "address": "", "phone": "", "website": "", "logo_path": None}
    return {
        "name": b.org_name or "NUTRYAH Facility",
        "tagline": b.org_tagline or "",
        "address": b.org_address or "",
        "phone": b.org_phone or "",
        "website": b.org_website or "",
        "email": b.org_email or "",
        "gstin": b.org_gstin or "",
        "logo_path": b.logo_path,
        "letterhead_path": b.letterhead_path,
        "letterhead_type": (b.letterhead_type or "").lower(),
    }


# ----------------------------
# Routes
# ----------------------------
@router.get("/admissions/{admission_id}/newborn/resuscitation", response_model=ApiResponse[Optional[NewbornOut]])
def get_by_admission(
    admission_id: int,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.view", "ipd.newborn.manage", "ipd.nursing.view"])
    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    return {"status": True, "data": obj}


@router.post(
    "/admissions/{admission_id}/newborn/resuscitation",
    response_model=ApiResponse[NewbornOut],
)
def create_for_admission(
    admission_id: int,
    payload: "NewbornCreate | None" = Body(default=None),   # ✅ allow missing/null body
    request: Request = None,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.create", "ipd.newborn.manage", "ipd.nursing.manage"])

    # ✅ if frontend sends nothing or null, treat as empty payload
    if payload is None:
        payload = NewbornCreate()

    exists = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="Newborn record already exists for this admission")

    obj = IpdNewbornResuscitation(
        admission_id=admission_id,
        birth_register_id=payload.birth_register_id,
        baby_patient_id=payload.baby_patient_id,
        mother_patient_id=payload.mother_patient_id,
        created_by_user_id=_uget(user, "id", None),
        created_at=utcnow(),
        status="DRAFT",
    )

    # Autofill from BirthRegister
    if BirthRegister is not None and payload.birth_register_id:
        br = db.get(BirthRegister, payload.birth_register_id)
        if br and getattr(br, "birth_datetime", None):
            bd = br.birth_datetime
            obj.date_of_birth = bd.date()
            obj.time_of_birth = bd.strftime("%H:%M")
            obj.sex = getattr(br, "child_sex", None)
            obj.mother_name = getattr(br, "mother_name", None)
            obj.mother_age_years = getattr(br, "mother_age_years", None)
            obj.gestational_age_weeks = getattr(br, "gestation_weeks", None)
            try:
                obj.birth_weight_kg = float(getattr(br, "birth_weight_kg", None)) if getattr(br, "birth_weight_kg", None) else None
            except Exception:
                pass
            obj.mode_of_delivery = getattr(br, "delivery_method", None)

    data = payload.model_dump(exclude_unset=True)

    for k, v in data.items():
        if k in ("resuscitation", "vaccination") and v is not None:
            setattr(obj, k, _json_safe(_as_json(v)))
        else:
            setattr(obj, k, v)

    db.add(obj)
    db.flush()
    _audit(db, request, obj.id, "create", _uget(user, "id", None), after=_snap(obj))
    db.commit()
    db.refresh(obj)

    return {"status": True, "data": obj}


@router.patch("/admissions/{admission_id}/newborn/resuscitation", response_model=ApiResponse[NewbornOut])
def update_for_admission(
    admission_id: int,
    payload: NewbornUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.update", "ipd.newborn.manage", "ipd.nursing.manage"])

    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Newborn record not found")

    _ensure_editable(obj)
    before = _snap(obj)

    data = payload.model_dump(exclude_unset=True)

    for k, v in data.items():
        if k in ("admission_id", "status", "locked_at", "created_at", "created_by_user_id"):
            continue

        # ✅ IMPORTANT FIX: JSON-safe conversion on PATCH also
        if k in ("resuscitation", "vaccination"):
            setattr(obj, k, _json_safe(_as_json(v)))
        else:
            setattr(obj, k, v)

    obj.updated_at = utcnow()
    db.flush()
    _audit(db, request, obj.id, "update", _uget(user, "id", None), before=before, after=_snap(obj))
    db.commit()
    db.refresh(obj)
    return {"status": True, "data": obj}


@router.post("/admissions/{admission_id}/newborn/resuscitation/verify", response_model=ApiResponse[NewbornOut])
def verify(
    admission_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.verify", "ipd.newborn.manage", "ipd.nursing.manage"])

    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Newborn record not found")

    _ensure_editable(obj)

    before = _snap(obj)
    obj.status = "VERIFIED"
    obj.verified_by_user_id = _uget(user, "id", None)
    obj.verified_at = utcnow()
    obj.updated_at = utcnow()

    db.flush()
    _audit(db, request, obj.id, "verify", _uget(user, "id", None), before=before, after=_snap(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return {"status": True, "data": obj}


@router.post("/admissions/{admission_id}/newborn/resuscitation/finalize", response_model=ApiResponse[NewbornOut])
def finalize(
    admission_id: int,
    payload: ActionNote,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.finalize", "ipd.newborn.manage", "ipd.nursing.manage"])

    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Newborn record not found")

    if obj.status not in ("DRAFT", "VERIFIED"):
        raise HTTPException(status_code=409, detail=f"Cannot finalize from status={obj.status}")

    if not obj.date_of_birth or not obj.sex:
        raise HTTPException(status_code=422, detail="DOB and Sex are required to finalize")
    if obj.apgar_1_min is None or obj.apgar_5_min is None:
        raise HTTPException(status_code=422, detail="APGAR 1 min and 5 min are required to finalize")

    before = _snap(obj)
    obj.status = "FINALIZED"
    obj.locked_at = utcnow()
    obj.finalized_by_user_id = _uget(user, "id", None)
    obj.finalized_at = utcnow()
    obj.updated_at = utcnow()

    db.flush()
    _audit(db, request, obj.id, "finalize", _uget(user, "id", None), before=before, after=_snap(obj), note=payload.note)
    db.commit()
    db.refresh(obj)
    return {"status": True, "data": obj}


@router.post("/admissions/{admission_id}/newborn/resuscitation/void", response_model=ApiResponse[NewbornOut])
def void(
    admission_id: int,
    payload: VoidRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.void", "ipd.newborn.manage"])

    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Newborn record not found")

    if obj.status == "VOIDED":
        return {"status": True, "data": obj}

    before = _snap(obj)
    obj.status = "VOIDED"
    obj.locked_at = utcnow()
    obj.updated_at = utcnow()

    db.flush()
    _audit(db, request, obj.id, "void", _uget(user, "id", None), before=before, after=_snap(obj), note=payload.reason)
    db.commit()
    db.refresh(obj)
    return {"status": True, "data": obj}


@router.get("/admissions/{admission_id}/newborn/resuscitation/print.pdf")
def print_pdf(
    admission_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    _need_any(user, ["ipd.newborn.view", "ipd.newborn.print", "ipd.newborn.manage", "ipd.nursing.view"])

    obj = db.execute(
        select(IpdNewbornResuscitation).where(IpdNewbornResuscitation.admission_id == admission_id)
    ).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Newborn record not found")

    branding = db.execute(select(UiBranding).order_by(UiBranding.id.asc()).limit(1)).scalar_one_or_none()
    hospital = _branding_to_hospital_dict(branding)

    pdf = build_pdf(obj, hospital=hospital)

    _audit(db, request, obj.id, "print", _uget(user, "id", None), note="ipd_newborn_resuscitation_pdf")
    db.commit()

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="newborn_resuscitation_adm_{admission_id}.pdf"'},
    )
