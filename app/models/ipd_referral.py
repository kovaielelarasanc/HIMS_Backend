# FILE: app/models/ipd_referral.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class IpdReferral(Base):
    """
    IPD Referral (NABH-friendly):
    - ref_type: internal / external
    - category: clinical / service / co_manage / second_opinion / transfer
    - care_mode: opinion / co_manage / take_over / transfer
    - audit trail via events table
    - who/when: requested/accepted/responded/closed/cancelled + user ids
    """
    __tablename__ = "ipd_referrals"

    id = Column(Integer, primary_key=True)

    admission_id = Column(
        Integer,
        ForeignKey("ipd_admissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # internal / external
    ref_type = Column(String(20), nullable=False, default="internal", index=True)

    # clinical / service / co_manage / second_opinion / transfer
    category = Column(String(30), nullable=False, default="clinical", index=True)

    # opinion / co_manage / take_over / transfer
    care_mode = Column(String(30), nullable=False, default="opinion", index=True)

    # routine / urgent / stat
    priority = Column(String(20), nullable=False, default="routine", index=True)

    # requested / accepted / declined / responded / closed / cancelled
    status = Column(String(20), nullable=False, default="requested", index=True)

    # ---- requested ----
    requested_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    requested_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- target internal ----
    # If you have departments table, keep FK. If not, remove ForeignKey and keep nullable int.
    to_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    to_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # legacy string fallback (keeps backward compatibility)
    to_department = Column(String(120), nullable=False, default="")

    # service referral target (dietician/physio/wound_care/etc)
    to_service = Column(String(60), nullable=False, default="", index=True)

    # ---- target external ----
    external_org = Column(String(200), nullable=False, default="")
    external_contact_name = Column(String(120), nullable=False, default="")
    external_contact_phone = Column(String(30), nullable=False, default="")
    external_address = Column(String(250), nullable=False, default="")

    # ---- content ----
    reason = Column(Text, nullable=False, default="")
    clinical_summary = Column(Text, nullable=False, default="")
    attachments = Column(JSON, nullable=True)  # [{"name","url","type"}]

    # ---- acceptance ----
    accepted_at = Column(DateTime, nullable=True)
    accepted_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    decline_reason = Column(Text, nullable=False, default="")

    # ---- response ----
    responded_at = Column(DateTime, nullable=True)
    responded_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    response_note = Column(Text, nullable=False, default="")

    # ---- closure / cancel ----
    closed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(Text, nullable=False, default="")

    # Relationships
    admission = relationship("IpdAdmission", back_populates="referrals")

    requested_by = relationship("User", foreign_keys=[requested_by_user_id])
    to_user = relationship("User", foreign_keys=[to_user_id])
    accepted_by = relationship("User", foreign_keys=[accepted_by_user_id])
    responded_by = relationship("User", foreign_keys=[responded_by_user_id])

    # If you have Department model:
    to_department_rel = relationship("Department", foreign_keys=[to_department_id])

    events = relationship(
        "IpdReferralEvent",
        back_populates="referral",
        cascade="all, delete-orphan",
        order_by="IpdReferralEvent.id.desc()",
    )


class IpdReferralEvent(Base):
    """
    Audit trail for NABH:
    requested / accepted / declined / responded / closed / cancelled / note
    """
    __tablename__ = "ipd_referral_events"

    id = Column(Integer, primary_key=True)

    referral_id = Column(
        Integer,
        ForeignKey("ipd_referrals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type = Column(String(30), nullable=False, index=True)
    event_at = Column(DateTime, nullable=False, default=utcnow, index=True)

    by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    note = Column(Text, nullable=False, default="")
    meta = Column(JSON, nullable=True)

    referral = relationship("IpdReferral", back_populates="events")
    by_user = relationship("User")


Index("ix_ipd_referrals_adm_status", IpdReferral.admission_id, IpdReferral.status)
Index("ix_ipd_referrals_adm_category", IpdReferral.admission_id, IpdReferral.category)
Index("ix_ipd_referral_events_ref_type", IpdReferralEvent.referral_id, IpdReferralEvent.event_type)
