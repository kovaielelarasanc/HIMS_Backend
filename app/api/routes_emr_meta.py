# FILE: app/api/routes_emr_meta.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from typing import List
from app.api.deps import get_db, current_user
from app.models.user import User

from app.services.emr_meta_service import (
    meta_bootstrap, list_sections, create_section,
    list_presets, preset_to_out, template_preview,
)

router = APIRouter(prefix="/emr", tags=["EMR Meta"])


# Use your existing permission helpers (replace these calls to match your project)
def _need_any(user: User, codes: List[str]) -> None:
    if bool(getattr(user, "is_admin", False)):
        return
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


from app.utils.respo import err, ok


@router.get("/meta/bootstrap")
def api_meta_bootstrap(
    active: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "emr.manage", "emr.templates.manage"])
    return ok(meta_bootstrap(db, active=active))





