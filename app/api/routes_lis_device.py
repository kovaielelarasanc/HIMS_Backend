# FILE: app/api/routes_lis_device.py
from __future__ import annotations

from typing import List

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
    Header,
)
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.api.deps import (
    get_db,                    # normal tenant DB (JWT)
    current_user as auth_current_user,
    get_connector_tenant_db,   # header-based tenant DB for connector
)
from app.models.user import User
from app.models.lis_device import (
    LabDevice,
    LabDeviceChannel,
    LabDeviceMessageLog,
    LabDeviceResult,
)
from app.schemas.lis_device import (
    LabDeviceCreate,
    LabDeviceUpdate,
    LabDeviceOut,
    LabDeviceChannelCreate,
    LabDeviceChannelUpdate,
    LabDeviceChannelOut,
    LabDeviceMessageLogOut,
    LabDeviceResultOut,
    DeviceResultBatchIn,
)
from app.services import lis_device as svc

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# =====================================================
#  Helper functions
# =====================================================

def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


def verify_api_key(plain: str, hashed: str) -> bool:
    """
    Safe wrapper around passlib verify for device API keys.
    """
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


# =====================================================
#  A. DEVICE MANAGEMENT (UI – secured by JWT + perms)
# =====================================================

# UI router (uses JWT + get_db / tenant middleware)
router = APIRouter(prefix="/lis", tags=["LIS - Devices"])


@router.get("/devices", response_model=List[LabDeviceOut])
def list_lab_devices(
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return svc.list_devices(db)


@router.post(
    "/devices",
    response_model=LabDeviceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_lab_device(
    payload: LabDeviceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    existing = svc.get_device_by_code(db, payload.code)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device code already exists",
        )
    return svc.create_device(db, payload)


@router.put(
    "/devices/{device_id}",
    response_model=LabDeviceOut,
)
def update_lab_device(
    device_id: int,
    payload: LabDeviceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return svc.update_device(db, device, payload)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_lab_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    svc.delete_device(db, device)
    return None


# ----- Channels -----


@router.get(
    "/devices/{device_id}/channels",
    response_model=List[LabDeviceChannelOut],
)
def list_device_channels(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return svc.list_channels_for_device(db, device_id)


@router.post(
    "/devices/{device_id}/channels",
    response_model=LabDeviceChannelOut,
    status_code=status.HTTP_201_CREATED,
)
def create_device_channel(
    device_id: int,
    payload: LabDeviceChannelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    payload.device_id = device_id  # avoid spoof from body

    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return svc.create_channel(db, payload)


@router.put(
    "/channels/{channel_id}",
    response_model=LabDeviceChannelOut,
)
def update_device_channel(
    channel_id: int,
    payload: LabDeviceChannelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    channel = svc.get_channel_by_id(db, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return svc.update_channel(db, channel, payload)


@router.delete(
    "/channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_device_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    channel = svc.get_channel_by_id(db, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    svc.delete_channel(db, channel)
    return None


# ----- Logs & staging -----


@router.get(
    "/devices/{device_id}/logs",
    response_model=List[LabDeviceMessageLogOut],
)
def list_device_logs(
    device_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    limit = max(1, min(limit, 500))
    return (
        db.query(LabDeviceMessageLog)
        .filter(LabDeviceMessageLog.device_id == device_id)
        .order_by(LabDeviceMessageLog.created_at.desc())
        .limit(limit)
        .all()
    )


@router.get(
    "/devices/{device_id}/results/staging",
    response_model=List[LabDeviceResultOut],
)
def list_staging_results_for_device(
    device_id: int,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.results.review"):
        raise HTTPException(status_code=403, detail="Not permitted")

    limit = max(1, min(limit, 500))
    return (
        db.query(LabDeviceResult)
        .filter(LabDeviceResult.device_id == device_id)
        .order_by(LabDeviceResult.received_at.desc())
        .limit(limit)
        .all()
    )


# =====================================================
#  B. (Optional) CONNECTOR AUTH helper (not used now)
# =====================================================

def get_lab_device_by_api_key_public(
    request: Request,
    db: Session = Depends(get_connector_tenant_db),
) -> LabDevice:
    """
    Authenticate connector using X-Device-Api-Key header
    against lab_devices.api_key_hash in the tenant DB.

    (Kept for future use; current connector endpoint does
    explicit lookup using payload.device_code.)
    """
    api_key = request.headers.get("X-Device-Api-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Device-Api-Key header",
        )

    devices = (
        db.query(LabDevice)
        .filter(LabDevice.is_active.is_(True))
        .all()
    )
    for dev in devices:
        if verify_api_key(api_key, dev.api_key_hash):
            return dev

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid device API key",
    )


# =====================================================
#  C. CONNECTOR ENDPOINT  (device -> HIMS, no JWT)
# =====================================================

connector_router = APIRouter(prefix="/connector/lis", tags=["LIS Connector"])


@connector_router.post(
    "/device-results",
    response_model=List[LabDeviceResultOut],
    status_code=status.HTTP_201_CREATED,
)
def receive_device_results_public(
    payload: DeviceResultBatchIn,
    db: Session = Depends(get_connector_tenant_db),
    x_device_api_key: str = Header(..., alias="X-Device-Api-Key"),
):
    """
    Analyzer connector entrypoint (multi-tenant):

    - Tenant chosen by X-Tenant-Code header  (smc001 -> nabh_hims_smc001)
    - Device authenticated by X-Device-Api-Key (bcrypt hash in lab_devices.api_key_hash)
    - No JWT token used here
    """

    # 1) Find device by code in this tenant DB
    device = (
        db.query(LabDevice)
        .filter(
            LabDevice.code == payload.device_code,
            LabDevice.is_active.is_(True),
        )
        .first()
    )
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found for this tenant",
        )

    # 2) Verify API key using bcrypt
    if not verify_api_key(x_device_api_key, device.api_key_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device API key",
        )

    # 3) Save results into staging table (uses svc from services)
    results = svc.save_device_results_batch(db, device=device, batch=payload)
    return results
