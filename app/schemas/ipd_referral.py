# FILE: app/schemas/ipd_referral.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field, validator


ReferralType = Literal["internal", "external"]
ReferralCategory = Literal["clinical", "service", "co_manage", "second_opinion", "transfer"]
ReferralCareMode = Literal["opinion", "co_manage", "take_over", "transfer"]
ReferralPriority = Literal["routine", "urgent", "stat"]
ReferralStatus = Literal["requested", "accepted", "declined", "responded", "closed", "cancelled"]


class ReferralAttachment(BaseModel):
    name: str
    url: str
    type: Optional[str] = ""


class ReferralCreate(BaseModel):
    ref_type: ReferralType = "internal"
    category: ReferralCategory = "clinical"
    care_mode: ReferralCareMode = "opinion"
    priority: ReferralPriority = "routine"

    # internal target
    to_department_id: Optional[int] = None
    to_user_id: Optional[int] = None
    to_department: Optional[str] = ""   # legacy fallback
    to_service: Optional[str] = ""      # for service referrals (dietician/physio/etc)

    # external target
    external_org: Optional[str] = ""
    external_contact_name: Optional[str] = ""
    external_contact_phone: Optional[str] = ""
    external_address: Optional[str] = ""

    # content
    reason: str = ""
    clinical_summary: Optional[str] = ""
    attachments: Optional[List[ReferralAttachment]] = None

    @validator(
        "to_department",
        "to_service",
        "external_org",
        "external_contact_name",
        "external_contact_phone",
        "external_address",
        "reason",
        "clinical_summary",
        pre=True,
    )
    def _strip(cls, v):
        return (v or "").strip()

    @validator("attachments", pre=True)
    def _attachments_none_or_list(cls, v):
        return v or None


class ReferralDecision(BaseModel):
    note: Optional[str] = ""


class ReferralRespond(BaseModel):
    response_note: str = Field(..., min_length=2)


class ReferralCancel(BaseModel):
    reason: str = Field(..., min_length=2)


class ReferralEventOut(BaseModel):
    id: int
    event_type: str
    event_at: datetime
    by_user_id: Optional[int] = None
    note: str = ""
    meta: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes=True


class ReferralOut(BaseModel):
    id: int
    admission_id: int

    ref_type: str
    category: str
    care_mode: str
    priority: str
    status: str

    requested_at: datetime
    requested_by_user_id: Optional[int] = None

    to_department_id: Optional[int] = None
    to_user_id: Optional[int] = None
    to_department: str = ""
    to_service: str = ""

    external_org: str = ""
    external_contact_name: str = ""
    external_contact_phone: str = ""
    external_address: str = ""

    reason: str = ""
    clinical_summary: str = ""
    attachments: Optional[List[ReferralAttachment]] = None

    accepted_at: Optional[datetime] = None
    accepted_by_user_id: Optional[int] = None
    decline_reason: str = ""

    responded_at: Optional[datetime] = None
    responded_by_user_id: Optional[int] = None
    response_note: str = ""

    closed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancel_reason: str = ""

    events: List[ReferralEventOut] = []

    class Config:
        from_attributes=True
