from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel, ConfigDict


# ============================
# Global branding schemas
# ============================
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

    # pdf behaviour
    pdf_header_height_mm: Optional[int] = None
    pdf_footer_height_mm: Optional[int] = None
    pdf_show_page_number: Optional[bool] = None

    # letterhead behaviour
    letterhead_position: Optional[str] = None


class UiBrandingUpdate(UiBrandingBase):
    pass


class UiBrandingOut(UiBrandingBase):
    model_config = ConfigDict(from_attributes=True)

    id: int

    # asset URLs (computed)
    logo_url: Optional[str] = None
    login_logo_url: Optional[str] = None
    favicon_url: Optional[str] = None

    pdf_header_url: Optional[str] = None
    pdf_footer_url: Optional[str] = None

    letterhead_url: Optional[str] = None
    letterhead_type: Optional[str] = None
    letterhead_position: Optional[str] = None

    # cache buster
    asset_version: Optional[str] = None

    updated_at: Optional[str] = None
    updated_by_name: Optional[str] = None


class UiBrandingPublicOut(UiBrandingBase):
    """
    Used by ALL users for layout/topbar/sidebar.
    Supports optional context overrides (?context=pharmacy)
    """
    model_config = ConfigDict(from_attributes=True)

    context_code: Optional[str] = None

    logo_url: Optional[str] = None
    login_logo_url: Optional[str] = None
    favicon_url: Optional[str] = None

    pdf_header_url: Optional[str] = None
    pdf_footer_url: Optional[str] = None

    letterhead_url: Optional[str] = None
    letterhead_type: Optional[str] = None
    letterhead_position: Optional[str] = None

    # context-only legal extras
    license_no: Optional[str] = None
    license_no2: Optional[str] = None
    pharmacist_name: Optional[str] = None
    pharmacist_reg_no: Optional[str] = None

    asset_version: Optional[str] = None


# ============================
# Context schemas (pharmacy etc.)
# ============================
class UiBrandingContextBase(BaseModel):
    # org overrides
    org_name: Optional[str] = None
    org_tagline: Optional[str] = None
    org_address: Optional[str] = None
    org_phone: Optional[str] = None
    org_email: Optional[str] = None
    org_website: Optional[str] = None
    org_gstin: Optional[str] = None

    # legal extras
    license_no: Optional[str] = None
    license_no2: Optional[str] = None
    pharmacist_name: Optional[str] = None
    pharmacist_reg_no: Optional[str] = None

    # letterhead behaviour
    letterhead_position: Optional[str] = None


class UiBrandingContextUpdate(UiBrandingContextBase):
    pass


class UiBrandingContextOut(UiBrandingContextBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str

    logo_url: Optional[str] = None
    pdf_header_url: Optional[str] = None
    pdf_footer_url: Optional[str] = None

    letterhead_url: Optional[str] = None
    letterhead_type: Optional[str] = None
    letterhead_position: Optional[str] = None

    asset_version: Optional[str] = None

    updated_at: Optional[str] = None
    updated_by_name: Optional[str] = None
