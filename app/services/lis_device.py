# FILE: app/services/lis_device.py
from __future__ import annotations

from typing import Sequence

from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.models.lis_device import (
    LabDevice,
    LabDeviceChannel,
    LabDeviceMessageLog,
    LabDeviceResult,
    DeviceResultStatus,
)
from app.schemas.lis_device import (
    LabDeviceCreate,
    LabDeviceUpdate,
    LabDeviceChannelCreate,
    LabDeviceChannelUpdate,
    DeviceResultBatchIn,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_api_key(api_key: str) -> str:
    return pwd_context.hash(api_key)


# ---------- Devices ----------

def create_device(db: Session, data: LabDeviceCreate) -> LabDevice:
    hashed = hash_api_key(data.api_key)
    obj = LabDevice(
        code=data.code,
        name=data.name,
        model=data.model,
        manufacturer=data.manufacturer,
        connection_type=data.connection_type,
        protocol=data.protocol,
        # the following are optional config hints; only keep if you added
        # matching columns in LabDevice model. If not, remove them here.
        ip_address=data.ip_address,
        port=data.port,
        serial_port=data.serial_port,
        baudrate=data.baudrate,
        data_bits=data.data_bits,
        stop_bits=data.stop_bits,
        parity=data.parity,
        file_drop_path=data.file_drop_path,
        is_active=data.is_active,
        allow_unmapped_tests=data.allow_unmapped_tests,
        api_key_hash=hashed,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_device(db: Session, device: LabDevice, data: LabDeviceUpdate) -> LabDevice:
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "api_key" and value:
            setattr(device, "api_key_hash", hash_api_key(value))
        else:
            setattr(device, field, value)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def list_devices(db: Session) -> Sequence[LabDevice]:
    return db.query(LabDevice).order_by(LabDevice.name.asc()).all()


def get_device_by_id(db: Session, device_id: int) -> LabDevice | None:
    return db.query(LabDevice).get(device_id)


def get_device_by_code(db: Session, code: str) -> LabDevice | None:
    return db.query(LabDevice).filter(LabDevice.code == code).first()


def delete_device(db: Session, device: LabDevice) -> None:
    db.delete(device)
    db.commit()


# ---------- Channels ----------

def create_channel(db: Session, data: LabDeviceChannelCreate) -> LabDeviceChannel:
    obj = LabDeviceChannel(
        device_id=data.device_id,
        external_test_code=data.external_test_code,
        external_test_name=data.external_test_name,
        lis_test_id=data.lis_test_id,
        default_unit=data.default_unit,
        reference_range=data.reference_range,
        is_active=data.is_active,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_channel(
    db: Session, channel: LabDeviceChannel, data: LabDeviceChannelUpdate
) -> LabDeviceChannel:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(channel, field, value)
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def list_channels_for_device(db: Session, device_id: int) -> Sequence[LabDeviceChannel]:
    return (
        db.query(LabDeviceChannel)
        .filter(LabDeviceChannel.device_id == device_id)
        .order_by(LabDeviceChannel.external_test_code.asc())
        .all()
    )


def get_channel_by_id(db: Session, channel_id: int) -> LabDeviceChannel | None:
    return db.query(LabDeviceChannel).get(channel_id)


def delete_channel(db: Session, channel: LabDeviceChannel) -> None:
    db.delete(channel)
    db.commit()


# ---------- Logs & Results from Connector ----------

def record_inbound_log(
    db: Session,
    device: LabDevice | None,
    raw_payload: str,
    direction: str = "in",
) -> LabDeviceMessageLog:
    log = LabDeviceMessageLog(
        device=device,
        direction=direction,
        raw_payload=raw_payload,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def save_device_results_batch(
    db: Session,
    device: LabDevice,
    batch: DeviceResultBatchIn,
) -> list[LabDeviceResult]:
    """
    Save incoming results into staging in a SAFE way.

    - Each result row is handled independently.
    - Invalid rows become LabDeviceResult with status=ERROR and error_message.
    - Valid rows become status=STAGING.
    - No unhandled exception should bubble out from here.
    """
    results: list[LabDeviceResult] = []

    # optional logging of raw payload
    if batch.raw_payload:
        try:
            record_inbound_log(
                db=db,
                device=device,
                raw_payload=batch.raw_payload,
                direction="in",
            )
        except Exception as e:
            # do not fail the whole batch if logging fails
            print("[LIS] Failed to record inbound log:", e)

    for idx, item in enumerate(batch.results):
        try:
            # Basic sanity checks (pydantic already validates, but extra layer is ok)
            if not item.sample_id or not item.external_test_code or not item.result_value:
                raise ValueError("Missing required fields (sample_id, external_test_code, result_value).")

            res = LabDeviceResult(
                device=device,
                sample_id=item.sample_id,
                external_test_code=item.external_test_code,
                external_test_name=item.external_test_name,
                result_value=item.result_value,
                unit=item.unit,
                flag=item.flag,
                reference_range=item.reference_range,
                measured_at=item.measured_at,
                status=DeviceResultStatus.STAGING,
            )

        except Exception as e:
            # If anything goes wrong, still create a row marked as ERROR
            res = LabDeviceResult(
                device=device,
                sample_id=getattr(item, "sample_id", "") or "",
                external_test_code=getattr(item, "external_test_code", "") or "",
                external_test_name=getattr(item, "external_test_name", None),
                result_value=getattr(item, "result_value", "") or "",
                unit=getattr(item, "unit", None),
                flag=getattr(item, "flag", None),
                reference_range=getattr(item, "reference_range", None),
                measured_at=getattr(item, "measured_at", None),
                status=DeviceResultStatus.ERROR,
                error_message=f"Row {idx}: {e}",
            )

        db.add(res)
        results.append(res)

    db.commit()
    for r in results:
        db.refresh(r)

    return results
