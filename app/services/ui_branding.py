from __future__ import annotations

from typing import Optional
from sqlalchemy.orm import Session

from app.models.ui_branding import UiBranding, UiBrandingContext


def get_ui_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).first()


def get_or_create_default_ui_branding(db: Session, updated_by_id: Optional[int] = None) -> UiBranding:
    branding = db.query(UiBranding).first()
    if branding:
        return branding

    branding = UiBranding(
        org_name="Your Hospital Name",
        org_tagline="Smart • Secure • NABH-Standard",

        primary_color="#2563eb",
        primary_color_dark=None,

        sidebar_bg_color="#ffffff",
        content_bg_color="#f9fafb",
        card_bg_color="#ffffff",
        border_color="#e5e7eb",

        text_color="#111827",
        text_muted_color="#6b7280",

        icon_color="#111827",
        icon_bg_color="rgba(37,99,235,0.08)",

        pdf_header_height_mm=None,
        pdf_footer_height_mm=None,
        pdf_show_page_number=True,

        letterhead_position="background",

        updated_by_id=updated_by_id,
    )
    db.add(branding)
    db.commit()
    db.refresh(branding)
    return branding


def get_branding_context(db: Session, code: str) -> Optional[UiBrandingContext]:
    c = (code or "").strip().lower()
    if not c:
        return None
    return db.query(UiBrandingContext).filter(UiBrandingContext.code == c).first()


def get_or_create_branding_context(
    db: Session,
    code: str,
    updated_by_id: Optional[int] = None,
) -> UiBrandingContext:
    c = (code or "").strip().lower()
    if not c:
        raise ValueError("context code is required")

    ctx = get_branding_context(db, c)
    if ctx:
        return ctx

    ctx = UiBrandingContext(
        code=c,
        letterhead_position="background",
        updated_by_id=updated_by_id,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx
