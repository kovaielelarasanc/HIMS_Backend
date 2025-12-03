# app/utils/jwt.py
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


def create_access_refresh(
    subject: str,
    tenant_id: int,
    tenant_code: str,
) -> Tuple[str, str]:
    """
    Create access + refresh tokens WITH tenant info inside.
    """
    access_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_delta = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)

    access_token = _create_token(
        subject=subject,
        tenant_id=tenant_id,
        tenant_code=tenant_code,
        expires_delta=access_delta,
    )
    refresh_token = _create_token(
        subject=subject,
        tenant_id=tenant_id,
        tenant_code=tenant_code,
        expires_delta=refresh_delta,
    )
    return access_token, refresh_token



def extract_tenant_from_request(request: Request) -> Optional[str]:
    """
    Try to read tenant_code from Authorization Bearer token.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
    except JWTError:
        return None
    return payload.get("tenant_code")