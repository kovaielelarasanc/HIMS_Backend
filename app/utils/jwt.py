# app/utils/jwt.py
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Tuple
from fastapi import Request
from typing import Optional
from jose import jwt, JWTError

from app.core.config import settings


def _create_token(
    *,
    subject: str,
    tenant_id: int,
    tenant_code: str,
    expires_delta: timedelta,
) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": subject,  # user email
        "tid": tenant_id,  # tenant id
        "tcode": tenant_code,  # tenant code (e.g. KGH001)
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def _now() -> datetime:
    return datetime.utcnow()


def create_access_refresh(
    *,
    user_id: int,
    tenant_id: int,
    tenant_code: str,
    session_id: str,
    token_version: int,
):
    """
    Access token: short lived
    Refresh token: long lived (stored in HttpOnly cookie)
    Both contain:
      uid, tid, tcode, sid, tv
    """
    now = _now()

    access_exp = now + timedelta(minutes=getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 60))
    refresh_exp = now + timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)

    access_payload = {
        "type": "access",
        "sub": str(user_id),
        "uid": int(user_id),
        "tid": int(tenant_id),
        "tcode": str(tenant_code),
        "sid": str(session_id),
        "tv": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int(access_exp.timestamp()),
    }

    refresh_payload = {
        "type": "refresh",
        "sub": str(user_id),
        "uid": int(user_id),
        "tid": int(tenant_id),
        "tcode": str(tenant_code),
        "sid": str(session_id),
        "tv": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int(refresh_exp.timestamp()),
    }

    access = jwt.encode(access_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)
    refresh = jwt.encode(refresh_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)
    return access, refresh



def extract_tenant_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
    except JWTError:
        return None
    # ✅ correct key
    return payload.get("tcode")