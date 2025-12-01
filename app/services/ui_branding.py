# app/services/ui_branding.py
from typing import Optional

from sqlalchemy.orm import Session

from app.models.ui_branding import UiBranding


def get_ui_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).first()


def get_or_create_default_ui_branding(db: Session,
                                      updated_by_id: Optional[int] = None
                                      ) -> UiBranding:
    branding = db.query(UiBranding).first()
    if branding:
        return branding

    # sensible defaults (Nutryah style)
    branding = UiBranding(
        org_name="Your Hospital Name",
        org_tagline="Smart • Secure • NABH-Standard",
        primary_color="#0f172a",  # slate-900
        primary_color_dark="#020617",  # slate-950
        sidebar_bg_color="#0f172a",
        content_bg_color="#f8fafc",
        card_bg_color="#ffffff",
        border_color="#e2e8f0",
        text_color="#0f172a",
        text_muted_color="#64748b",
        icon_color="#0f172a",
        icon_bg_color="#e2e8f0",
        pdf_header_height_mm=25,
        pdf_footer_height_mm=20,
        pdf_show_page_number=True,
        updated_by_id=updated_by_id,
    )
    db.add(branding)
    db.commit()
    db.refresh(branding)
    return branding
