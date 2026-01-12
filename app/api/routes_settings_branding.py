from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.ui_branding import UiBranding, UiBrandingContext
from app.schemas.ui_branding import (
    UiBrandingOut,
    UiBrandingUpdate,
    UiBrandingPublicOut,
    UiBrandingContextOut,
    UiBrandingContextUpdate,
)
from app.services.ui_branding import (
    get_or_create_default_ui_branding,
    get_branding_context,
    get_or_create_branding_context,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["Settings"])

BRANDING_DIR = Path(settings.STORAGE_DIR).joinpath("branding")
BRANDING_DIR.mkdir(parents=True, exist_ok=True)


# ----------------
# RBAC helper
# ----------------
def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


# ----------------
# Helpers
# ----------------
def _ensure_branding(db: Session, current_user: Optional[User] = None) -> UiBranding:
    updated_by_id = current_user.id if current_user else None
    return get_or_create_default_ui_branding(db, updated_by_id=updated_by_id)


def _to_url(rel_path: Optional[str]) -> Optional[str]:
    if not rel_path:
        return None
    rel_path = str(rel_path).lstrip("/").replace("\\", "/")
    return f"/media/{rel_path}"


def _asset_version(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _branding_common_payload(branding: UiBranding) -> Dict[str, Any]:
    primary = branding.primary_color or "#2563eb"
    primary_dark = branding.primary_color_dark or None

    sidebar_bg_color = branding.sidebar_bg_color or "#ffffff"
    content_bg_color = branding.content_bg_color or "#f9fafb"
    card_bg_color = branding.card_bg_color or "#ffffff"
    border_color = branding.border_color or "#e5e7eb"

    text_color = branding.text_color or "#111827"
    text_muted_color = branding.text_muted_color or "#6b7280"

    icon_color = branding.icon_color or text_color
    icon_bg_color = branding.icon_bg_color or "rgba(37,99,235,0.08)"

    return {
        "org_name": branding.org_name,
        "org_tagline": branding.org_tagline,
        "org_address": branding.org_address,
        "org_phone": branding.org_phone,
        "org_email": branding.org_email,
        "org_website": branding.org_website,
        "org_gstin": branding.org_gstin,

        "primary_color": primary,
        "primary_color_dark": primary_dark,
        "sidebar_bg_color": sidebar_bg_color,
        "content_bg_color": content_bg_color,
        "card_bg_color": card_bg_color,
        "border_color": border_color,
        "text_color": text_color,
        "text_muted_color": text_muted_color,
        "icon_color": icon_color,
        "icon_bg_color": icon_bg_color,

        "pdf_header_height_mm": branding.pdf_header_height_mm,
        "pdf_footer_height_mm": branding.pdf_footer_height_mm,
        "pdf_show_page_number": branding.pdf_show_page_number,

        "letterhead_position": branding.letterhead_position or "background",
    }


def _apply_context_overrides(payload: Dict[str, Any], ctx: UiBrandingContext) -> Dict[str, Any]:
    # Override org fields if ctx has value
    org_fields = [
        "org_name", "org_tagline", "org_address", "org_phone",
        "org_email", "org_website", "org_gstin"
    ]
    for f in org_fields:
        v = getattr(ctx, f, None)
        if v is not None and str(v).strip() != "":
            payload[f] = v

    # pharmacy legal extras
    payload["license_no"] = ctx.license_no
    payload["license_no2"] = ctx.license_no2
    payload["pharmacist_name"] = ctx.pharmacist_name
    payload["pharmacist_reg_no"] = ctx.pharmacist_reg_no

    # assets override
    if ctx.logo_path:
        payload["logo_url"] = _to_url(ctx.logo_path)
    if ctx.pdf_header_path:
        payload["pdf_header_url"] = _to_url(ctx.pdf_header_path)
    if ctx.pdf_footer_path:
        payload["pdf_footer_url"] = _to_url(ctx.pdf_footer_path)
    if ctx.letterhead_path:
        payload["letterhead_url"] = _to_url(ctx.letterhead_path)
        payload["letterhead_type"] = ctx.letterhead_type
    if ctx.letterhead_position:
        payload["letterhead_position"] = ctx.letterhead_position

    return payload


def _branding_to_out(branding: UiBranding) -> UiBrandingOut:
    base = _branding_common_payload(branding)
    return UiBrandingOut(
        id=branding.id,
        **base,

        logo_url=_to_url(branding.logo_path),
        login_logo_url=_to_url(branding.login_logo_path),
        favicon_url=_to_url(branding.favicon_path),

        pdf_header_url=_to_url(branding.pdf_header_path),
        pdf_footer_url=_to_url(branding.pdf_footer_path),

        letterhead_url=_to_url(branding.letterhead_path),
        letterhead_type=branding.letterhead_type,
        letterhead_position=branding.letterhead_position or "background",

        asset_version=_asset_version(branding.updated_at),

        updated_at=branding.updated_at.isoformat() if branding.updated_at else None,
        updated_by_name=branding.updated_by.name if branding.updated_by else None,
    )


def _branding_to_public(branding: UiBranding, context_code: Optional[str] = None) -> UiBrandingPublicOut:
    base = _branding_common_payload(branding)
    base.update({
        "context_code": context_code or "default",

        "logo_url": _to_url(branding.logo_path),
        "login_logo_url": _to_url(branding.login_logo_path),
        "favicon_url": _to_url(branding.favicon_path),

        "pdf_header_url": _to_url(branding.pdf_header_path),
        "pdf_footer_url": _to_url(branding.pdf_footer_path),

        "letterhead_url": _to_url(branding.letterhead_path),
        "letterhead_type": branding.letterhead_type,
        "letterhead_position": branding.letterhead_position or "background",

        "license_no": None,
        "license_no2": None,
        "pharmacist_name": None,
        "pharmacist_reg_no": None,

        "asset_version": _asset_version(branding.updated_at),
    })
    return UiBrandingPublicOut(**base)


def _context_to_out(ctx: UiBrandingContext) -> UiBrandingContextOut:
    return UiBrandingContextOut(
        id=ctx.id,
        code=ctx.code,

        org_name=ctx.org_name,
        org_tagline=ctx.org_tagline,
        org_address=ctx.org_address,
        org_phone=ctx.org_phone,
        org_email=ctx.org_email,
        org_website=ctx.org_website,
        org_gstin=ctx.org_gstin,

        license_no=ctx.license_no,
        license_no2=ctx.license_no2,
        pharmacist_name=ctx.pharmacist_name,
        pharmacist_reg_no=ctx.pharmacist_reg_no,

        logo_url=_to_url(ctx.logo_path),
        pdf_header_url=_to_url(ctx.pdf_header_path),
        pdf_footer_url=_to_url(ctx.pdf_footer_path),

        letterhead_url=_to_url(ctx.letterhead_path),
        letterhead_type=ctx.letterhead_type,
        letterhead_position=ctx.letterhead_position or "background",

        asset_version=_asset_version(ctx.updated_at),

        updated_at=ctx.updated_at.isoformat() if ctx.updated_at else None,
        updated_by_name=ctx.updated_by.name if ctx.updated_by else None,
    )


def _save_asset_file(
    upload: UploadFile,
    prefix: str,
    allow_types: Tuple[str, ...],
    allow_ext: Tuple[str, ...] = (),
) -> str:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="Empty file")

    ctype = (upload.content_type or "").lower()
    fn = (upload.filename or "").lower()

    ok_type = any(ctype.startswith(a) for a in allow_types)
    ok_ext = any(fn.endswith(ext) for ext in allow_ext) if allow_ext else False
    if not (ok_type or ok_ext):
        raise HTTPException(status_code=400, detail=f"Invalid file type for {prefix}")

    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = upload.filename.replace(" ", "_")
    dest = BRANDING_DIR.joinpath(f"{prefix}_{ts}_{safe_name}")

    try:
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
    finally:
        upload.file.close()

    return dest.relative_to(settings.STORAGE_DIR).as_posix()


def _detect_letterhead_type(upload: UploadFile) -> str:
    fn = (upload.filename or "").lower()
    ct = (upload.content_type or "").lower()
    if ct == "application/pdf" or fn.endswith(".pdf"):
        return "pdf"
    if fn.endswith(".docx"):
        return "docx"
    if fn.endswith(".doc"):
        return "doc"
    return "image"


# ============================
# PUBLIC
# ============================
@router.get("/ui-branding/public", response_model=UiBrandingPublicOut)
def get_ui_branding_public(
    context: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    branding = _ensure_branding(db)
    out = _branding_to_public(branding, context_code=context or "default")

    if context:
        ctx = get_branding_context(db, context)
        if ctx:
            merged = out.model_dump()
            merged = _apply_context_overrides(merged, ctx)
            merged["asset_version"] = (
                (ctx.updated_at or branding.updated_at).isoformat()
                if (ctx.updated_at or branding.updated_at) else None
            )
            return UiBrandingPublicOut(**merged)

    return out


# ============================
# ADMIN: Global branding
# ============================
@router.get("/ui-branding", response_model=UiBrandingOut)
def get_ui_branding(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    branding = _ensure_branding(db, current_user)
    return _branding_to_out(branding)


@router.put("/ui-branding", response_model=UiBrandingOut)
def update_ui_branding(
    payload: UiBrandingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = _ensure_branding(db, current_user)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(branding, field, value)

    branding.updated_by_id = current_user.id
    try:
        db.add(branding)
        db.commit()
        db.refresh(branding)
    except Exception:
        db.rollback()
        logger.exception("Failed to update branding")
        raise HTTPException(status_code=500, detail="Failed to update branding")

    return _branding_to_out(branding)


# ============================
# ADMIN: Global assets
# ============================
@router.post("/ui-branding/assets", response_model=UiBrandingOut)
def upload_ui_branding_assets(
    logo: UploadFile | None = File(None),
    login_logo: UploadFile | None = File(None),
    favicon: UploadFile | None = File(None),
    pdf_header: UploadFile | None = File(None),
    pdf_footer: UploadFile | None = File(None),
    letterhead: UploadFile | None = File(None),
    letterhead_position: Optional[str] = Form(None),

    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = _ensure_branding(db, current_user)

    try:
        changed = False

        if logo is not None:
            branding.logo_path = _save_asset_file(logo, "logo", allow_types=("image/",))
            changed = True
        if login_logo is not None:
            branding.login_logo_path = _save_asset_file(login_logo, "login_logo", allow_types=("image/",))
            changed = True
        if favicon is not None:
            branding.favicon_path = _save_asset_file(favicon, "favicon", allow_types=("image/",), allow_ext=(".ico",))
            changed = True
        if pdf_header is not None:
            branding.pdf_header_path = _save_asset_file(pdf_header, "pdf_header", allow_types=("image/",))
            changed = True
        if pdf_footer is not None:
            branding.pdf_footer_path = _save_asset_file(pdf_footer, "pdf_footer", allow_types=("image/",))
            changed = True
        if letterhead is not None:
            branding.letterhead_path = _save_asset_file(
                letterhead,
                "letterhead",
                allow_types=("image/", "application/pdf",
                             "application/msword",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                allow_ext=(".pdf", ".doc", ".docx"),
            )
            branding.letterhead_type = _detect_letterhead_type(letterhead)
            changed = True
        if letterhead_position is not None:
            branding.letterhead_position = letterhead_position
            changed = True

        if changed:
            branding.updated_by_id = current_user.id
            db.add(branding)
            db.commit()
            db.refresh(branding)

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to upload branding assets")
        raise HTTPException(status_code=500, detail="Failed to upload branding assets")

    return _branding_to_out(branding)


# ============================
# ADMIN: Context branding
# ============================
@router.get("/ui-branding/contexts", response_model=List[UiBrandingContextOut])
def list_ui_branding_contexts(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    rows = db.query(UiBrandingContext).order_by(UiBrandingContext.code.asc()).all()
    return [_context_to_out(x) for x in rows]


# ✅ THIS is the route your logs say is missing (405).
@router.get("/ui-branding/contexts/{code}", response_model=UiBrandingContextOut)
def get_ui_branding_context(
    code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    ctx = get_or_create_branding_context(db, code, updated_by_id=current_user.id)
    return _context_to_out(ctx)



@router.put("/ui-branding/contexts/{code}", response_model=UiBrandingContextOut)
def update_ui_branding_context(
    code: str,
    payload: UiBrandingContextUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    ctx = get_or_create_branding_context(db, code, updated_by_id=current_user.id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ctx, field, value)

    ctx.updated_by_id = current_user.id
    try:
        db.add(ctx)
        db.commit()
        db.refresh(ctx)
    except Exception:
        db.rollback()
        logger.exception("Failed to update branding context: %s", code)
        raise HTTPException(status_code=500, detail="Failed to update branding context")

    return _context_to_out(ctx)


@router.post("/ui-branding/contexts/{code}/assets", response_model=UiBrandingContextOut)
def upload_ui_branding_context_assets(
    code: str,
    logo: UploadFile | None = File(None),
    pdf_header: UploadFile | None = File(None),
    pdf_footer: UploadFile | None = File(None),
    letterhead: UploadFile | None = File(None),
    letterhead_position: Optional[str] = Form(None),

    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    ctx = get_or_create_branding_context(db, code, updated_by_id=current_user.id)

    try:
        changed = False

        if logo is not None:
            ctx.logo_path = _save_asset_file(logo, f"{code}_logo", allow_types=("image/",))
            changed = True
        if pdf_header is not None:
            ctx.pdf_header_path = _save_asset_file(pdf_header, f"{code}_pdf_header", allow_types=("image/",))
            changed = True
        if pdf_footer is not None:
            ctx.pdf_footer_path = _save_asset_file(pdf_footer, f"{code}_pdf_footer", allow_types=("image/",))
            changed = True
        if letterhead is not None:
            ctx.letterhead_path = _save_asset_file(
                letterhead,
                f"{code}_letterhead",
                allow_types=("image/", "application/pdf",
                             "application/msword",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                allow_ext=(".pdf", ".doc", ".docx"),
            )
            ctx.letterhead_type = _detect_letterhead_type(letterhead)
            changed = True
        if letterhead_position is not None:
            ctx.letterhead_position = letterhead_position
            changed = True

        if changed:
            ctx.updated_by_id = current_user.id
            db.add(ctx)
            db.commit()
            db.refresh(ctx)

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to upload branding context assets: %s", code)
        raise HTTPException(status_code=500, detail="Failed to upload branding context assets")

    return _context_to_out(ctx)
