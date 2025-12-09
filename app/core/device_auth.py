# FILE: app/core/device_auth.py
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from app.api.deps import get_db, current_user

from app.models.lis_device import LabDevice

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

device_api_key_header = APIKeyHeader(name="X-Device-Api-Key", auto_error=False)


def verify_api_key(api_key_plain: str, api_key_hash: str) -> bool:
    return pwd_context.verify(api_key_plain, api_key_hash)


async def get_lab_device_by_api_key(
    api_key: str | None = Depends(device_api_key_header),
    db: Session = Depends(get_db),
) -> LabDevice:
    """
    Used by connector-facing endpoints.
    Devices send X-Device-Api-Key header.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing device API key",
        )

    # Simple lookup: we don't store plain key, only hash.
    device: LabDevice | None = db.query(LabDevice).filter(
        LabDevice.is_active.is_(True)
    ).first()

    # You can optimize by storing API key hash index or a separate mapping.
    # Here we just iterate active devices and verify.
    matched_device: LabDevice | None = None
    for dev in db.query(LabDevice).filter(LabDevice.is_active.is_(True)).all():
        if verify_api_key(api_key, dev.api_key_hash):
            matched_device = dev
            break

    if not matched_device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device API key",
        )

    return matched_device
