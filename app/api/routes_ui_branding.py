# app/api/routes_ui_branding.py
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.core.config import settings
from app.models.user import User
from app.models.ui_branding import UiBranding
from app.schemas.ui_branding import (
    UiBrandingOut,
    UiBrandingPublicOut,
    UiBrandingUpdate,
)
from app.services.ui_branding import get_or_create_default_ui_branding, get_ui_branding

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/settings",
    tags=["Settings - Customization"],
)

BRANDING_DIR = Path(settings.STORAGE_DIR).joinpath("branding")
BRANDING_DIR.mkdir(parents=True, exist_ok=True)

# --- Helpers ---------------------------------------------------------------


def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def _path_to_url(rel_path: Optional[str]) -> Optional[str]:
    if not rel_path:
        return None
    rel_path = rel_path.lstrip("/").replace("\\", "/")
    return f"/media/{rel_path}"


def _branding_to_out(branding: UiBranding) -> UiBrandingOut:
    return UiBrandingOut(
        id=branding.id,
        org_name=branding.org_name,
        org_tagline=branding.org_tagline,
        org_address=branding.org_address,
        org_phone=branding.org_phone,
        org_email=branding.org_email,
        org_website=branding.org_website,
        org_gstin=branding.org_gstin,
        primary_color=branding.primary_color,
        primary_color_dark=branding.primary_color_dark,
        sidebar_bg_color=branding.sidebar_bg_color,
        content_bg_color=branding.content_bg_color,
        card_bg_color=branding.card_bg_color,
        border_color=branding.border_color,
        text_color=branding.text_color,
        text_muted_color=branding.text_muted_color,
        icon_color=branding.icon_color,
        icon_bg_color=branding.icon_bg_color,
        pdf_header_height_mm=branding.pdf_header_height_mm,
        pdf_footer_height_mm=branding.pdf_footer_height_mm,
        pdf_show_page_number=branding.pdf_show_page_number,
        logo_url=_path_to_url(branding.logo_path),
        login_logo_url=_path_to_url(branding.login_logo_path),
        favicon_url=_path_to_url(branding.favicon_path),
        pdf_header_url=_path_to_url(branding.pdf_header_path),
        pdf_footer_url=_path_to_url(branding.pdf_footer_path),
        updated_at=branding.updated_at.isoformat()
        if branding.updated_at else None,
        updated_by_name=branding.updated_by.email
        if branding.updated_by else None,
    )


def _branding_to_public(branding: UiBranding) -> UiBrandingPublicOut:
    return UiBrandingPublicOut(
        org_name=branding.org_name,
        org_tagline=branding.org_tagline,
        primary_color=branding.primary_color,
        sidebar_bg_color=branding.sidebar_bg_color,
        content_bg_color=branding.content_bg_color,
        text_color=branding.text_color,
        logo_url=_path_to_url(branding.logo_path),
        login_logo_url=_path_to_url(branding.login_logo_path),
        favicon_url=_path_to_url(branding.favicon_path),
    )


def _save_branding_file(upload: UploadFile, prefix: str) -> str:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="Empty file")

    content_type = upload.content_type or ""
    if not content_type.startswith("image/"):
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

    rel_path = dest.relative_to(settings.STORAGE_DIR).as_posix()
    return rel_path


# --- API Endpoints ---------------------------------------------------------


@router.get("/ui-branding", response_model=UiBrandingOut)
def get_ui_branding_admin(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Full branding object for authenticated admin screens.
    """
    if not has_perm(current_user, "settings.customization.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = get_or_create_default_ui_branding(db,
                                                 updated_by_id=current_user.id)
    return _branding_to_out(branding)


@router.get("/ui-branding/public", response_model=UiBrandingPublicOut)
def get_ui_branding_public(db: Session = Depends(get_db), ):
    """
    Lightweight branding without auth – for login page / marketing site.
    """
    branding = get_or_create_default_ui_branding(db)
    return _branding_to_public(branding)


@router.put("/ui-branding", response_model=UiBrandingOut)
def update_ui_branding(
        payload: UiBrandingUpdate,
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Update organisation details, colors & PDF options.
    File uploads are handled via /ui-branding/assets.
    """
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = get_or_create_default_ui_branding(db,
                                                 updated_by_id=current_user.id)

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
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
        login_logo: UploadFile | None = File(None),
        favicon: UploadFile | None = File(None),
        pdf_header: UploadFile | None = File(None),
        pdf_footer: UploadFile | None = File(None),
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Upload assets:
    - logo        -> sidebar/topbar
    - login_logo  -> login page (optional)
    - favicon     -> browser tab (future)
    - pdf_header  -> header on ALL PDFs (mandatory recommended)
    - pdf_footer  -> footer on ALL PDFs
    """
    if not has_perm(current_user, "settings.customization.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = get_or_create_default_ui_branding(db,
                                                 updated_by_id=current_user.id)

    try:
        changed = False

        if logo is not None:
            branding.logo_path = _save_branding_file(logo, "logo")
            changed = True

        if login_logo is not None:
            branding.login_logo_path = _save_branding_file(login_logo, "login")
            changed = True

        if favicon is not None:
            branding.favicon_path = _save_branding_file(favicon, "favicon")
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


@router.get("/ui-branding/sample-pdf")
def preview_branding_pdf(
        db: Session = Depends(get_db),
        current_user: User = Depends(auth_current_user),
):
    """
    Generate a 1-page sample PDF to validate header/footer + hospital details.
    """
    if not has_perm(current_user, "settings.customization.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    branding = get_or_create_default_ui_branding(db)
    buf = BytesIO()
    from reportlab.pdfgen import canvas  # import locally to avoid circular

    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    header_height_mm = branding.pdf_header_height_mm or 25
    footer_height_mm = branding.pdf_footer_height_mm or 20

    header_h = header_height_mm * mm
    footer_h = footer_height_mm * mm

    # HEADER IMAGE
    if branding.pdf_header_path:
        header_path = Path(settings.STORAGE_DIR).joinpath(
            branding.pdf_header_path)
        if header_path.exists():
            img = ImageReader(str(header_path))
            c.drawImage(
                img,
                x=15 * mm,
                y=height - header_h - 10 * mm,
                width=width - 30 * mm,
                height=header_h,
                preserveAspectRatio=True,
                mask="auto",
            )

    # FOOTER IMAGE
    if branding.pdf_footer_path:
        footer_path = Path(settings.STORAGE_DIR).joinpath(
            branding.pdf_footer_path)
        if footer_path.exists():
            img = ImageReader(str(footer_path))
            c.drawImage(
                img,
                x=15 * mm,
                y=10 * mm,
                width=width - 30 * mm,
                height=footer_h,
                preserveAspectRatio=True,
                mask="auto",
            )

    # ORG text + page number sample
    c.setFont("Helvetica-Bold", 11)
    y = height - (header_h + 20 * mm)
    if branding.org_name:
        c.drawString(20 * mm, y, branding.org_name)
        y -= 6 * mm
    if branding.org_tagline:
        c.setFont("Helvetica", 9)
        c.drawString(20 * mm, y, branding.org_tagline)
        y -= 6 * mm

    c.setFont("Helvetica", 9)
    c.drawString(
        20 * mm,
        height / 2,
        "This is a sample PDF to preview header/footer & hospital details.",
    )

    if branding.pdf_show_page_number:
        c.setFont("Helvetica", 8)
        text = "Page 1 of 1"
        c.drawRightString(width - 20 * mm, 15 * mm, text)

    c.showPage()
    c.save()
    buf.seek(0)

    headers = {
        "Content-Disposition": "inline; filename=branding-preview.pdf",
        "X-NABH-HIMS": "branding-preview",
    }
    return StreamingResponse(buf,
                             media_type="application/pdf",
                             headers=headers)
