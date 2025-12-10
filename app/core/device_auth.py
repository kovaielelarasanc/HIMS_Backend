# FILE: app/core/device_auth.py
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.api.deps import get_db
from app.models.lis_device import LabDevice

# Same bcrypt setup as lis_device service
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_api_key(plain_key: str, hashed_key: str | None) -> bool:
    """
    Safe bcrypt verify for device API keys.
    """
    if not plain_key or not hashed_key:
        return False
    try:
        return pwd_context.verify(plain_key, hashed_key)
    except Exception:
        return False


async def get_lab_device_by_api_key(
    x_device_api_key: str | None = Header(default=None, alias="X-Device-Api-Key"),
    db: Session = Depends(get_db),
) -> LabDevice:
    """
    Public-device auth ONLY.

    - NO JWT
    - NO 'Authorization' header
    - NO 'Missing token' messages

    Logic:
    1) Read X-Device-Api-Key from header
    2) Try to match against LabDevice.api_key_hash (bcrypt)
    3) If match → return that LabDevice
    4) Else → 401
    """
    # 1) Explicit error if header missing
    if not x_device_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device API key missing",
        )

    # 2) Check all active devices for a match
    devices = (
        db.query(LabDevice)
        .filter(LabDevice.is_active.is_(True))
        .all()
    )

    for dev in devices:
        if verify_api_key(x_device_api_key, dev.api_key_hash):
            return dev

    # 3) No match -> invalid
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid device API key",
    )
