from typing import Optional
from pydantic import BaseModel, ConfigDict


class UiBrandingBase(BaseModel):
    primary_color: Optional[str] = None
    sidebar_bg_color: Optional[str] = None
    content_bg_color: Optional[str] = None
    text_color: Optional[str] = None
    icon_color: Optional[str] = None
    icon_bg_color: Optional[str] = None


class UiBrandingOut(UiBrandingBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    logo_url: Optional[str] = None
    pdf_header_url: Optional[str] = None
    pdf_footer_url: Optional[str] = None


class UiBrandingUpdate(UiBrandingBase):
    """Update only colors (files handled via separate upload endpoint)"""
    pass
