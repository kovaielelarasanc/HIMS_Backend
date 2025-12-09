# FILE: app/services/lab_result_mapping.py
from __future__ import annotations

from typing import Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.lis_device import (
    LabDeviceResult,
    LabDeviceChannel,
    DeviceResultStatus,
    LabDevice,
)
from app.models.lis import LisOrder, LisOrderItem  # <-- your models


# ----------------- Core helpers -----------------


def find_channel_for_staging(
    db: Session,
    staging: LabDeviceResult,
) -> LabDeviceChannel | None:
    """
    Map (device_id + external_test_code) -> lis_test_id.

    Assumes:
      LabDeviceChannel.lis_test_id == LisOrderItem.test_id (lab_tests.id)
    """
    if staging.device_id is None:
        return None

    return (
        db.query(LabDeviceChannel)
        .filter(
            LabDeviceChannel.device_id == staging.device_id,
            LabDeviceChannel.external_test_code == staging.external_test_code,
            LabDeviceChannel.is_active.is_(True),
        )
        .first()
    )


def find_lis_order_item_for_staging(
    db: Session,
    staging: LabDeviceResult,
    channel: LabDeviceChannel,
) -> Tuple[LisOrderItem | None, LisOrder | None]:
    """
    Find the LisOrderItem that corresponds to this staging row.

    Matching logic:
      - sample_barcode == staging.sample_id   (tube barcode)
      - test_id == channel.lis_test_id
      - order.status != 'cancelled'

    We return:
      (LisOrderItem, LisOrder)
    """
    if not staging.sample_id:
        return None, None
    if channel.lis_test_id is None:
        return None, None

    q = (
        db.query(LisOrderItem)
        .join(LisOrder, LisOrderItem.order_id == LisOrder.id)
        .options(joinedload(LisOrderItem.order))
        .filter(
            LisOrderItem.sample_barcode == staging.sample_id,
            LisOrderItem.test_id == channel.lis_test_id,
            LisOrder.status != "cancelled",
        )
        .order_by(LisOrderItem.created_at.desc())
    )

    item: LisOrderItem | None = q.first()
    if not item:
        return None, None

    return item, item.order


def update_lis_order_status_aggregate(db: Session, order: LisOrder) -> None:
    """
    Recalculate LisOrder.status based on its items.

    Your statuses:
      - ordered / collected / in_progress / validated / reported / cancelled
    """
    db.refresh(order)  # refresh items relationship

    items = order.items or []
    if not items:
        return

    statuses = {i.status for i in items if i.status}

    # All cancelled -> order cancelled
    if statuses and statuses.issubset({"cancelled"}):
        order.status = "cancelled"

    # All reported or validated -> reported
    elif statuses.issubset({"reported", "validated"}):
        order.status = "reported"

    # Any in_progress / validated / reported -> in_progress (some work started)
    elif statuses & {"in_progress", "validated", "reported"}:
        # If already further (validated/reported) don't downgrade
        if order.status not in {"validated", "reported"}:
            order.status = "in_progress"

    else:
        # default fallback
        if not order.status:
            order.status = "ordered"

    db.add(order)


# ----------------- Core mapping -----------------


def map_single_staging_result(
    db: Session,
    staging: LabDeviceResult,
    auto_commit: bool = True,
) -> LabDeviceResult:
    """
    Map one LabDeviceResult STAGING row -> LisOrder / LisOrderItem.

    Updates:
      - staging.lis_order_id, lis_test_id, patient_id, status, error_message
      - LisOrderItem.result_value, result_at, status
      - LisOrder.status aggregate
    """
    # Only process staging/mapped
    if staging.status not in (DeviceResultStatus.STAGING, DeviceResultStatus.MAPPED):
        return staging

    staging.error_message = None

    # 1) Device test channel mapping
    channel = find_channel_for_staging(db, staging)
    if not channel:
        staging.status = DeviceResultStatus.ERROR
        staging.error_message = (
            f"No active LabDeviceChannel for device_id={staging.device_id}, "
            f"external_test_code={staging.external_test_code}"
        )
        db.add(staging)
        if auto_commit:
            db.commit()
        return staging

    # 2) Find LisOrderItem based on sample_barcode + test_id
    item, order = find_lis_order_item_for_staging(db, staging, channel)
    if not item or not order:
        staging.status = DeviceResultStatus.ERROR
        staging.error_message = (
            "No LisOrderItem found for "
            f"sample_barcode={staging.sample_id}, "
            f"test_id={channel.lis_test_id} "
            f"(from device test_code={staging.external_test_code})"
        )
        db.add(staging)
        if auto_commit:
            db.commit()
        return staging

    # 3) Update LisOrderItem with result from analyzer
    item.result_value = staging.result_value
    # mark result time
    item.result_at = staging.measured_at or staging.received_at
    # TODO: you can map critical flag from staging.flag (e.g., "CRIT")
    if staging.flag and staging.flag.upper() in {"CRIT", "C"}:
        item.is_critical = True
    # Don't auto-fill validated_by / reported_by – keep for human workflow

    # Set item status after machine result
    # You can choose "in_progress" or "validated" depending on your workflow.
    # Safer: in_progress (then lab tech validates in UI).
    if item.status not in ("validated", "reported", "cancelled"):
        item.status = "in_progress"

    db.add(item)

    # 4) Update LisOrder aggregate status
    update_lis_order_status_aggregate(db, order)

    # 5) Update staging row links
    staging.lis_order_id = order.id
    staging.lis_test_id = item.test_id
    staging.patient_id = order.patient_id
    staging.status = DeviceResultStatus.POSTED
    staging.error_message = None

    db.add(staging)

    if auto_commit:
        db.commit()
        db.refresh(staging)

    return staging


def auto_map_staging_for_device(
    db: Session,
    device_id: int,
    limit: int = 500,
) -> Sequence[LabDeviceResult]:
    """
    Map all STAGING rows for a given device (or up to `limit`).
    """
    rows: list[LabDeviceResult] = (
        db.query(LabDeviceResult)
        .filter(
            LabDeviceResult.device_id == device_id,
            LabDeviceResult.status.in_(
                [DeviceResultStatus.STAGING, DeviceResultStatus.MAPPED]
            ),
        )
        .order_by(LabDeviceResult.received_at.asc())
        .limit(limit)
        .all()
    )

    if not rows:
        return []

    for r in rows:
        map_single_staging_result(db, r, auto_commit=False)

    db.commit()
    for r in rows:
        db.refresh(r)

    return rows


def auto_map_staging_for_sample(
    db: Session,
    sample_id: str,
) -> Sequence[LabDeviceResult]:
    """
    Map all STAGING rows for a given sample_id (barcode across devices).
    """
    rows: list[LabDeviceResult] = (
        db.query(LabDeviceResult)
        .filter(
            LabDeviceResult.sample_id == sample_id,
            LabDeviceResult.status.in_(
                [DeviceResultStatus.STAGING, DeviceResultStatus.MAPPED]
            ),
        )
        .order_by(LabDeviceResult.received_at.asc())
        .all()
    )

    if not rows:
        return []

    for r in rows:
        map_single_staging_result(db, r, auto_commit=False)

    db.commit()
    for r in rows:
        db.refresh(r)

    return rows
