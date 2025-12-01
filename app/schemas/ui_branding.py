# app/schemas/ui_branding.py
from typing import Optional

from pydantic import BaseModel, ConfigDict


class UiBrandingBase(BaseModel):
    # org info
    org_name: Optional[str] = None
    org_tagline: Optional[str] = None
    org_address: Optional[str] = None
    org_phone: Optional[str] = None
    org_email: Optional[str] = None
    org_website: Optional[str] = None
    org_gstin: Optional[str] = None

    # colors
    primary_color: Optional[str] = None
    primary_color_dark: Optional[str] = None

    sidebar_bg_color: Optional[str] = None
    content_bg_color: Optional[str] = None
    card_bg_color: Optional[str] = None
    border_color: Optional[str] = None

    text_color: Optional[str] = None
    text_muted_color: Optional[str] = None

    icon_color: Optional[str] = None
    icon_bg_color: Optional[str] = None

    # pdf options
    pdf_header_height_mm: Optional[int] = None
    pdf_footer_height_mm: Optional[int] = None
    pdf_show_page_number: Optional[bool] = None


class UiBrandingUpdate(UiBrandingBase):
    """Used for PUT /settings/ui-branding (partial update)."""
    pass


class UiBrandingOut(UiBrandingBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

    # resolved URLs for frontend
    logo_url: Optional[str] = None
    login_logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    pdf_header_url: Optional[str] = None
    pdf_footer_url: Optional[str] = None

    updated_at: Optional[str] = None
    updated_by_name: Optional[str] = None


class UiBrandingPublicOut(BaseModel):
    """
    Lightweight version for login page / marketing site.
    No internal IDs or audit info.
    """
    model_config = ConfigDict(from_attributes=True)

    org_name: Optional[str] = None
    org_tagline: Optional[str] = None
    primary_color: Optional[str] = None
    sidebar_bg_color: Optional[str] = None
    content_bg_color: Optional[str] = None
    text_color: Optional[str] = None

    logo_url: Optional[str] = None
    login_logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
