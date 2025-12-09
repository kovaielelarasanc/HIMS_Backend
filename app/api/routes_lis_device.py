# FILE: app/api/routes_lis_device.py
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user 
from app.schemas.lis_device import (
    LabDeviceCreate,
    LabDeviceUpdate,
    LabDeviceOut,
    LabDeviceChannelBase,
    LabDeviceChannelCreate,
    LabDeviceChannelUpdate,
    LabDeviceChannelOut,
    LabDeviceMessageLogOut,
    LabDeviceResultOut,
    DeviceResultBatchIn,
)
from app.services import lis_device as svc
from app.models.lis_device import LabDevice, LabDeviceMessageLog, LabDeviceResult
from app.core.device_auth import get_lab_device_by_api_key

from app.models.user import User

router = APIRouter(prefix="/api/lis", tags=["LIS - Devices"])


# --------- Admin Device Management (UI) ---------
def has_perm(user: User, code: str) -> bool:
    if user.is_admin:
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False

@router.get(
    "/devices",
    response_model=List[LabDeviceOut],
)
async def list_lab_devices(
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
async def create_lab_device(
    payload: LabDeviceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # ensure unique code
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
async def update_lab_device(
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
async def delete_lab_device(
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


# --------- Device Channel Mapping ---------


@router.get(
    "/devices/{device_id}/channels",
    response_model=List[LabDeviceChannelOut],
)
async def list_device_channels(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # validate device
    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return svc.list_channels_for_device(db, device_id)


@router.post(
    "/devices/{device_id}/channels",
    response_model=LabDeviceChannelOut,
)
async def create_device_channel(
    device_id: int,
    payload: LabDeviceChannelBase,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.manage"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # ensure device exists
    device = svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # build full create DTO with device_id from path
    dto = LabDeviceChannelCreate(
        device_id=device_id,
        **payload.model_dump(),
    )
    return svc.create_channel(db, dto)


@router.put(
    "/channels/{channel_id}",
    response_model=LabDeviceChannelOut,
)
async def update_device_channel(
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
async def delete_device_channel(
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


# --------- Logs & Staging Results (UI read) ---------


@router.get(
    "/devices/{device_id}/logs",
    response_model=List[LabDeviceMessageLogOut],
)
async def list_device_logs(
    device_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.devices.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    limit = max(1, min(limit, 500))
    logs = (
        db.query(LabDeviceMessageLog)
        .filter(LabDeviceMessageLog.device_id == device_id)
        .order_by(LabDeviceMessageLog.created_at.desc())  # updated: created_at
        .limit(limit)
        .all()
    )
    return logs


@router.get(
    "/devices/{device_id}/results/staging",
    response_model=List[LabDeviceResultOut],
)
async def list_staging_results_for_device(
    device_id: int,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "lab.results.review"):
        raise HTTPException(status_code=403, detail="Not permitted")

    limit = max(1, min(limit, 500))
    rows = (
        db.query(LabDeviceResult)
        .filter(LabDeviceResult.device_id == device_id)
        .order_by(LabDeviceResult.received_at.desc())
        .limit(limit)
        .all()
    )
    return rows


# --------- Connector Endpoint (Machine → HIMS) ---------


@router.post(
    "/device-results",
    response_model=List[LabDeviceResultOut],
    status_code=status.HTTP_201_CREATED,
)
async def receive_device_results(
    payload: DeviceResultBatchIn,
    db: Session = Depends(get_db),
    # Device auth: X-Device-Api-Key header
    device: LabDevice = Depends(get_lab_device_by_api_key),
):
    """
    Connector on local PC calls this endpoint.

    Headers:
      X-Device-Api-Key: <device secret key>

    Body:
    {
      "device_code": "CBC1",
      "results": [
        {
          "sample_id": "SMP-00123",
          "external_test_code": "WBC",
          "external_test_name": "White Blood Cell Count",
          "result_value": "5.6",
          "unit": "10^3/uL",
          "flag": "",
          "reference_range": "4.0-11.0",
          "measured_at": "2025-12-08T12:30:00Z"
        }
      ],
      "raw_payload": "optional raw ASTM/HL7 string..."
    }
    """
    # Safety: check that payload.device_code matches authenticated device
    if payload.device_code != device.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device code mismatch for provided API key",
        )

    results = svc.save_device_results_batch(db, device=device, batch=payload)
    return results
