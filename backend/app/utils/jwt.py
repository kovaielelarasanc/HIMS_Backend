from datetime import datetime, timedelta
from jose import jwt
from app.core.config import settings


def create_token(subject: str, minutes: int):
    now = datetime.utcnow()
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def create_access_refresh(sub: str):
    return (
        create_token(sub, settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        create_token(sub, settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )