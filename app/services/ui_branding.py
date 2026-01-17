from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import asc
from app.models.ui_branding import UiBranding, UiBrandingContext


def get_ui_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).first()


def get_or_create_default_ui_branding(db: Session, updated_by_id: Optional[int] = None) -> UiBranding:
    """
    Singleton-ish global branding.
    We always use the first row (lowest id). If none exists, create one.
    """
    row = db.query(UiBranding).order_by(asc(UiBranding.id)).first()
    if row:
        return row

    row = UiBranding(
        org_name=None,
        org_tagline=None,
        primary_color="#2563eb",
        sidebar_bg_color="#ffffff",
        content_bg_color="#f9fafb",
        card_bg_color="#ffffff",
        border_color="#e5e7eb",
        text_color="#111827",
        text_muted_color="#6b7280",
        pdf_show_page_number=True,
        letterhead_position="background",
        updated_by_id=updated_by_id,
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_branding_context(db: Session, code: str) -> Optional[UiBrandingContext]:
    if not code:
        return None
    code = str(code).strip().lower()
    return db.query(UiBrandingContext).filter(UiBrandingContext.code == code).first()


def get_or_create_branding_context(db: Session, code: str, updated_by_id: Optional[int] = None) -> UiBrandingContext:
    code = str(code).strip().lower()
    row = db.query(UiBrandingContext).filter(UiBrandingContext.code == code).first()
    if row:
        return row

    row = UiBrandingContext(
        code=code,
        letterhead_position="background",
        updated_by_id=updated_by_id,
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

