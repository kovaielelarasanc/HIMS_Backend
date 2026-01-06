# FILE: app/services/otp_service.py
from __future__ import annotations

from typing import Optional
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.core.emailer import send_email
from app.core.config import settings
from app.utils.otp_tokens import issue_otp, verify_otp  # ✅ schema-safe helpers


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, dom = email.split("@", 1)
    if len(name) <= 2:
        return f"{name[0]}***@{dom}"
    return f"{name[:2]}***@{dom}"


def send_email_verify_otp(db: Session, user, ttl_minutes: int = 10) -> dict:
    """
    ✅ Sends OTP to email for email verification
    - inserts otp row using issue_otp() (schema-safe)
    - sends email using send_email()
    - returns meta for UI
    """
    if not getattr(user, "email", None):
        raise HTTPException(status_code=400, detail="Email is required to send OTP")

    otp = issue_otp(
        db,
        user_id=int(user.id),
        purpose="email_verify",
        email=str(user.email),
        ttl_minutes=ttl_minutes,
    )

    send_email(
        to_email=str(user.email),
        subject=f"{settings.PROJECT_NAME} — Verify Email",
        body=f"Your OTP is {otp}. It will expire in {ttl_minutes} minutes.",
    )

    return {
        "otp_required": True,
        "purpose": "email_verify",
        "masked_email": _mask_email(str(user.email)),
    }


def send_login_otp(db: Session, user, ttl_minutes: int = 10) -> dict:
    """
    ✅ Sends OTP to email for login (2FA)
    - requires email + verified
    """
    if not getattr(user, "email", None):
        raise HTTPException(status_code=400, detail="Email is required for 2FA")
    if not bool(getattr(user, "email_verified", False)):
        # Important: do not block login silently; return instruction
        raise HTTPException(
            status_code=400,
            detail="Email is not verified. Verify email before login with 2FA.",
        )

    otp = issue_otp(
        db,
        user_id=int(user.id),
        purpose="login",
        email=str(user.email),
        ttl_minutes=ttl_minutes,
    )

    send_email(
        to_email=str(user.email),
        subject=f"{settings.PROJECT_NAME} — Login OTP",
        body=f"Your OTP is {otp}. It will expire in {ttl_minutes} minutes.",
    )

    return {
        "otp_required": True,
        "purpose": "login",
        "masked_email": _mask_email(str(user.email)),
    }


def verify_and_consume(db: Session, *, user_id: int, purpose: str, otp_code: str) -> bool:
    """
    ✅ Verify OTP (works with varying schemas)
    """
    return verify_otp(
        db,
        user_id=int(user_id),
        purpose=str(purpose),
        otp_code=str(otp_code).strip(),
    )
