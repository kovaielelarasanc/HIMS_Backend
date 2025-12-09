# FILE: app/api/routes_lis_mapping.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_tenant_db
from app.core.security import get_current_user
from app.core.permissions import require_permission

from app.models.lis_device import LabDeviceResult, LabDevice
from app.schemas.lis_device import LabDeviceResultOut
from app.services import lab_result_mapping as mapping_svc
from app.services import lis_device as lis_svc  # for get_device_by_id

router = APIRouter(
    prefix="/api/lis/mapping",
    tags=["LIS - Result Mapping"],
)


@router.post(
    "/devices/{device_id}/auto-map",
    response_model=list[LabDeviceResultOut],
)
async def auto_map_for_device(
    device_id: int,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_tenant_db),
    user=Depends(get_current_user),
    _=Depends(require_permission("lab.results.map")),
):
    device: LabDevice | None = lis_svc.get_device_by_id(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    rows = mapping_svc.auto_map_staging_for_device(db, device_id=device_id, limit=limit)
    return rows


@router.post(
    "/samples/{sample_id}/auto-map",
    response_model=list[LabDeviceResultOut],
)
async def auto_map_for_sample(
    sample_id: str,
    db: Session = Depends(get_tenant_db),
    user=Depends(get_current_user),
    _=Depends(require_permission("lab.results.map")),
):
    rows = mapping_svc.auto_map_staging_for_sample(db, sample_id=sample_id)
    return rows


@router.post(
    "/staging/{staging_id}/map",
    response_model=LabDeviceResultOut,
)
async def map_single_staging(
    staging_id: int,
    db: Session = Depends(get_tenant_db),
    user=Depends(get_current_user),
    _=Depends(require_permission("lab.results.map")),
):
    staging: LabDeviceResult | None = (
        db.query(LabDeviceResult).filter(LabDeviceResult.id == staging_id).first()
    )
    if not staging:
        raise HTTPException(status_code=404, detail="Staging result not found")

    updated = mapping_svc.map_single_staging_result(db, staging, auto_commit=True)
    return updated
