# FILE: backend/app/api/routes_settings_branding.py
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.ui_branding import UiBranding
from app.schemas.ui_branding import (
    UiBrandingOut,
    UiBrandingUpdate,
    UiBrandingPublicOut,
)
from app.services.ui_branding import get_or_create_default_ui_branding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["Settings"])

BRANDING_DIR = Path(settings.STORAGE_DIR).joinpath("branding")
BRANDING_DIR.mkdir(parents=True, exist_ok=True)


def has_perm(user: User, code: str) -> bool:
    """Same helper pattern used in patient routes."""
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _ensure_branding(db: Session,
                     current_user: Optional[User] = None) -> UiBranding:
    """
    Always return one row; create default if not exists.
    Scoped per-tenant via get_db.
    """
    updated_by_id = current_user.id if current_user else None
    branding = get_or_create_default_ui_branding(db,
                                                 updated_by_id=updated_by_id)
    return branding


def _to_url(rel_path: Optional[str]) -> Optional[str]:
    """Convert stored relative path to public /media URL."""
    if not rel_path:
        return None
    rel_path = str(rel_path).lstrip("/").replace("\\", "/")
    return f"/media/{rel_path}"


def _branding_common_payload(branding: UiBranding) -> Dict[str, Any]:
    """
    Common org + color payload with SAFE DEFAULTS.
    This is the key part that fixes 'null' color values.
    """
    # ---- COLORS WITH FALLBACKS ----
    primary = branding.primary_color or "#2563eb"
    primary_dark = branding.primary_color_dark or None

    sidebar_bg_color = branding.sidebar_bg_color or "#ffffff"
    content_bg_color = branding.content_bg_color or "#f9fafb"
    card_bg_color = branding.card_bg_color or "#ffffff"
    border_color = branding.border_color or "#e5e7eb"

    text_color = branding.text_color or "#111827"
    text_muted_color = branding.text_muted_color or "#9ca3af"

    icon_color = branding.icon_color or text_color
    icon_bg_color = branding.icon_bg_color or "rgba(37,99,235,0.08)"

    return {
        # org
        "org_name": branding.org_name,
        "org_tagline": branding.org_tagline,
        "org_address": branding.org_address,
        "org_phone": branding.org_phone,
        "org_email": branding.org_email,
        "org_website": branding.org_website,
        "org_gstin": branding.org_gstin,
        # colors (with defaults)
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
    }


def _branding_to_out(branding: UiBranding) -> UiBrandingOut:
    """
    Full admin view (used by settings page).
    Explicit mapping so we never lose fields.
    """
    base = _branding_common_payload(branding)

    return UiBrandingOut(
        id=branding.id,
        **base,
        # URLs
        logo_url=_to_url(branding.logo_path),
        login_logo_url=_to_url(branding.login_logo_path),
        favicon_url=_to_url(branding.favicon_path),
        pdf_header_url=_to_url(branding.pdf_header_path),
        pdf_footer_url=_to_url(branding.pdf_footer_path),
        # audit
        updated_at=branding.updated_at.isoformat() if isinstance(
            branding.updated_at, datetime) else None,
        updated_by_name=branding.updated_by.name
        if branding.updated_by else None,
    )


def _branding_to_public(branding: UiBranding) -> UiBrandingPublicOut:
    """
    Lightweight version for login page / normal screens.
    Uses SAME color defaults as _branding_to_out.
    """
    base = _branding_common_payload(branding)

    return UiBrandingPublicOut(
        **base,
        logo_url=_to_url(branding.logo_path),
        login_logo_url=_to_url(branding.login_logo_path),
        favicon_url=_to_url(branding.favicon_path),
    )


def _save_branding_file(upload: UploadFile, prefix: str) -> str:
    """Save uploaded image into STORAGE_DIR/branding and return relative path."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="Empty file")

    # Restrict to images for logo/header/footer
    if not (upload.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400,
                            detail="Only image files are allowed")

    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = upload.filename.replace(" ", "_")
    dest = BRANDING_DIR.joinpath(f"{prefix}_{ts}_{safe_name}")

    try:
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
    finally:
        upload.file.close()

    # store path relative to STORAGE_DIR so /media mount works
    rel_path = dest.relative_to(settings.STORAGE_DIR).as_posix()
    return rel_path


# ============================
#  PUBLIC: used by all users
# ============================


@router.get("/ui-branding/public", response_model=UiBrandingPublicOut)
def get_ui_branding_public(db: Session = Depends(get_db), ):
    """
    Public / tenant-wide branding, no special permission required.

    Used by:
      - Login page
      - Main app layout / sidebar / topbar for ALL users.

    Still tenant-scoped via get_db middleware.
    """
    branding = _ensure_branding(db)
    return _branding_to_public(branding)


# ============================
#  ADMIN: manage customization
# ============================


@router.get("/ui-branding", response_model=UiBrandingOut)
def get_ui_branding(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Full branding detail for admin settings screen.
    Requires customization.view permission.
    """
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
        logger.exception("Failed to update UI branding")
        raise HTTPException(status_code=500,
                            detail="Failed to update branding")

    return _branding_to_out(branding)


@router.post("/ui-branding/assets", response_model=UiBrandingOut)
def upload_ui_branding_assets(
        logo: UploadFile | None = File(None),
        pdf_header: UploadFile | None = File(None),
        pdf_footer: UploadFile | None = File(None),
        # NOTE: if later you want login_logo + favicon, add them here.
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Upload logo + PDF header/footer images.
    All are optional; send only what changed.
    """
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = _ensure_branding(db, current_user)

    try:
        changed = False

        if logo is not None:
            branding.logo_path = _save_branding_file(logo, "logo")
            changed = True

        if pdf_header is not None:
            branding.pdf_header_path = _save_branding_file(
                pdf_header, "pdf_header")
            changed = True

        if pdf_footer is not None:
            branding.pdf_footer_path = _save_branding_file(
                pdf_footer, "pdf_footer")
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
        raise HTTPException(status_code=500,
                            detail="Failed to upload branding assets")

    return _branding_to_out(branding)
