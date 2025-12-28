from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, current_user as auth_current_user
from app.utils.resp import ok, err
from app.models.user import User as UserModel
from app.models.ipd import (
    IpdAdmission,
    IpdBed,
    IpdRoom,
    IpdBedAssignment,
    IpdTransfer,
)

router = APIRouter(prefix="/ipd", tags=["IPD Transfers"])

# IST offset (Asia/Kolkata)
IST_OFFSET = timedelta(hours=5, minutes=30)


def has_perm(user: UserModel, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if p.code == code:
                return True
    return False


# -------------------------
# Time helpers (FIX timezone issues)
# -------------------------
def _now_utc_naive() -> datetime:
    """Store UTC as naive datetime in DB."""
    return datetime.utcnow()


def _parse_dt_to_utc_naive(v: Any) -> Optional[datetime]:
    """
    Accepts:
      - None
      - datetime
      - ISO string: 'YYYY-MM-DDTHH:MM[:SS]' (from datetime-local) => assumed IST
      - ISO string with timezone: '...Z' or '+HH:MM' => converted to UTC
    Returns naive UTC datetime for DB.
    """
    if not v:
        return None

    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None

    # timezone-aware => convert to UTC
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    # naive => treat as IST (datetime-local), convert to UTC
    return dt - IST_OFFSET


def _iso_utc_z(dt: Optional[datetime]) -> Optional[str]:
    """
    Convert DB datetime (naive UTC) to ISO string with 'Z' suffix,
    so frontend Date() parses correctly as UTC.
    """
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    aware = dt.replace(tzinfo=timezone.utc)
    s = aware.isoformat(timespec="seconds")
    return s.replace("+00:00", "Z")


# -------------------------
# Query helpers (FIX joinedload)
# -------------------------
def _get_adm(db: Session, admission_id: int) -> Optional[IpdAdmission]:
    return (
        db.query(IpdAdmission)
        .options(
            joinedload(IpdAdmission.current_bed)
            .joinedload(IpdBed.room)
            .joinedload(IpdRoom.ward)
        )
        .filter(IpdAdmission.id == admission_id)
        .first()
    )


def _get_bed(db: Session, bed_id: int) -> Optional[IpdBed]:
    return (
        db.query(IpdBed)
        .options(joinedload(IpdBed.room).joinedload(IpdRoom.ward))
        .filter(IpdBed.id == bed_id)
        .first()
    )


def _active_assignment(db: Session, admission_id: int) -> Optional[IpdBedAssignment]:
    return (
        db.query(IpdBedAssignment)
        .filter(IpdBedAssignment.admission_id == admission_id, IpdBedAssignment.to_ts.is_(None))
        .order_by(IpdBedAssignment.from_ts.desc())
        .first()
    )


def _bed_loc(bed: Optional[IpdBed]) -> Optional[dict]:
    if not bed:
        return None
    room = bed.room
    ward = room.ward if room else None
    return {
        "bed_id": bed.id,
        "bed_code": bed.code,
        "room_id": room.id if room else None,
        "room_number": room.number if room else None,
        "ward_id": ward.id if ward else None,
        "ward_name": ward.name if ward else None,
        "room_type": room.type if room else None,
        "bed_state": bed.state,
        "reserved_until": _iso_utc_z(getattr(bed, "reserved_until", None)),
    }


def _transfer_dict(t: IpdTransfer) -> dict:
    handover = None
    if t.handover_json:
        try:
            handover = json.loads(t.handover_json)
        except Exception:
            handover = None

    return {
        "id": t.id,
        "admission_id": t.admission_id,
        "status": t.status,
        "transfer_type": t.transfer_type,
        "priority": t.priority,
        "reason": t.reason or "",
        "request_note": t.request_note or "",
        "scheduled_at": _iso_utc_z(t.scheduled_at),
        "reserved_until": _iso_utc_z(t.reserved_until),
        "requested_by": t.requested_by,
        "requested_at": _iso_utc_z(t.requested_at),
        "approved_by": t.approved_by,
        "approved_at": _iso_utc_z(t.approved_at),
        "approval_note": t.approval_note or "",
        "rejected_reason": t.rejected_reason or "",
        "cancelled_by": t.cancelled_by,
        "cancelled_at": _iso_utc_z(t.cancelled_at),
        "cancel_reason": t.cancel_reason or "",
        "vacated_at": _iso_utc_z(t.vacated_at),
        "occupied_at": _iso_utc_z(t.occupied_at),
        "completed_by": t.completed_by,
        "completed_at": _iso_utc_z(t.completed_at),
        "from_assignment_id": t.from_assignment_id,
        "to_assignment_id": t.to_assignment_id,
        "from_location": _bed_loc(t.from_bed),
        "to_location": _bed_loc(t.to_bed),
        "handover": handover,
    }


def _ensure_bed_available(
    bed: IpdBed,
    allow_reserved_for_same: bool = False,
    transfer: Optional[IpdTransfer] = None,
) -> Optional[str]:
    now = _now_utc_naive()

    if (bed.state or "").lower() == "occupied":
        return "Bed is occupied"

    reserved_until = getattr(bed, "reserved_until", None)
    if reserved_until and reserved_until > now:
        if allow_reserved_for_same and transfer and transfer.to_bed_id == bed.id:
            return None
        return "Bed is reserved"

    if (bed.state or "").lower() in {"maintenance", "cleaning", "blocked"}:
        return f"Bed is not available ({bed.state})"

    return None


def _reserve_bed(bed: IpdBed, minutes: int, until_dt: Optional[datetime] = None) -> None:
    if minutes <= 0 and not until_dt:
        setattr(bed, "reserved_until", None)
        if (bed.state or "").lower() == "reserved":
            bed.state = "vacant"
        return

    setattr(bed, "reserved_until", until_dt or (_now_utc_naive() + timedelta(minutes=minutes)))
    bed.state = "reserved"


def _release_reservation(bed: IpdBed) -> None:
    setattr(bed, "reserved_until", None)
    if (bed.state or "").lower() == "reserved":
        bed.state = "vacant"


def _load_transfer_with_locations(db: Session, transfer_id: int) -> Optional[IpdTransfer]:
    return (
        db.query(IpdTransfer)
        .options(
            joinedload(IpdTransfer.from_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            joinedload(IpdTransfer.to_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
        )
        .filter(IpdTransfer.id == transfer_id)
        .first()
    )


# -------------------------
# Endpoints
# -------------------------
@router.get("/admissions/{admission_id}/transfers")
def list_transfers(
    admission_id: int,
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not (has_perm(user, "ipd.transfers.view") or has_perm(user, "ipd.admissions.view")):
        return err("Not permitted", 403)

    adm = _get_adm(db, admission_id)
    if not adm:
        return err("Admission not found", 404)

    rows = (
        db.query(IpdTransfer)
        .options(
            joinedload(IpdTransfer.from_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            joinedload(IpdTransfer.to_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
        )
        .filter(IpdTransfer.admission_id == admission_id)
        .order_by(IpdTransfer.requested_at.desc())
        .all()
    )

    return ok({"total": len(rows), "items": [_transfer_dict(r) for r in rows]})


@router.post("/admissions/{admission_id}/transfers")
def request_transfer(
    admission_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.transfers.create"):
        return err("Not permitted", 403)

    adm = _get_adm(db, admission_id)
    if not adm:
        return err("Admission not found", 404)

    if adm.status in {"discharged", "lama", "dama"}:
        return err(f"Admission is {adm.status}; transfer not allowed", 400)

    cur_asg = _active_assignment(db, admission_id)
    from_bed_id = adm.current_bed_id or (cur_asg.bed_id if cur_asg else None)

    to_bed_id = payload.get("to_bed_id")
    transfer_type = payload.get("transfer_type", "transfer")
    priority = payload.get("priority", "routine")
    reason = (payload.get("reason") or "").strip()
    request_note = payload.get("request_note") or ""
    reserve_minutes = int(payload.get("reserve_minutes") or 30)

    scheduled_at = _parse_dt_to_utc_naive(payload.get("scheduled_at"))
    if payload.get("scheduled_at") and scheduled_at is None:
        return err("Invalid scheduled_at ISO datetime", 400)

    t = IpdTransfer(
        admission_id=admission_id,
        from_bed_id=from_bed_id,
        to_bed_id=int(to_bed_id) if to_bed_id else None,
        from_assignment_id=cur_asg.id if cur_asg else None,
        transfer_type=transfer_type,
        priority=priority,
        status="requested",
        reason=reason,
        request_note=request_note,
        scheduled_at=scheduled_at,
        requested_by=user.id,
        requested_at=_now_utc_naive(),
    )

    # reserve target bed if selected
    if to_bed_id:
        bed = _get_bed(db, int(to_bed_id))
        if not bed:
            return err("Target bed not found", 404)
        msg = _ensure_bed_available(bed)
        if msg:
            return err(msg, 400)
        if reserve_minutes > 0:
            _reserve_bed(bed, reserve_minutes)

    db.add(t)
    db.commit()

    t2 = _load_transfer_with_locations(db, t.id)
    return ok(_transfer_dict(t2), 201)


@router.post("/transfers/{transfer_id}/approve")
def approve_or_reject_transfer(
    transfer_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.transfers.approve"):
        return err("Not permitted", 403)

    t = db.query(IpdTransfer).filter(IpdTransfer.id == transfer_id).first()
    if not t:
        return err("Transfer not found", 404)

    if t.status not in {"requested", "scheduled"}:
        return err(f"Cannot approve/reject when status is {t.status}", 400)

    approve = bool(payload.get("approve", True))
    approval_note = payload.get("approval_note") or ""
    rejected_reason = payload.get("rejected_reason") or ""

    t.approved_by = user.id
    t.approved_at = _now_utc_naive()
    t.approval_note = approval_note

    if approve:
        t.status = "approved"
        t.rejected_reason = ""
    else:
        t.status = "rejected"
        t.rejected_reason = rejected_reason or "Rejected"

        if t.to_bed_id:
            bed = db.query(IpdBed).filter(IpdBed.id == t.to_bed_id).first()
            if bed:
                _release_reservation(bed)

    db.commit()
    t2 = _load_transfer_with_locations(db, transfer_id)
    return ok(_transfer_dict(t2))


@router.post("/transfers/{transfer_id}/assign")
def assign_target_bed(
    transfer_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.transfers.approve"):
        return err("Not permitted", 403)

    t = db.query(IpdTransfer).filter(IpdTransfer.id == transfer_id).first()
    if not t:
        return err("Transfer not found", 404)

    if t.status not in {"requested", "approved", "scheduled"}:
        return err(f"Cannot assign bed when status is {t.status}", 400)

    to_bed_id = payload.get("to_bed_id")
    if not to_bed_id:
        return err("to_bed_id is required", 400)

    reserve_minutes = int(payload.get("reserve_minutes") or 30)
    scheduled_at = _parse_dt_to_utc_naive(payload.get("scheduled_at"))
    if payload.get("scheduled_at") and scheduled_at is None:
        return err("Invalid scheduled_at ISO datetime", 400)

    new_bed = _get_bed(db, int(to_bed_id))
    if not new_bed:
        return err("Target bed not found", 404)

    # release old reservation if changing target
    if t.to_bed_id and t.to_bed_id != new_bed.id:
        old_bed = db.query(IpdBed).filter(IpdBed.id == t.to_bed_id).first()
        if old_bed:
            _release_reservation(old_bed)

    msg = _ensure_bed_available(new_bed)
    if msg:
        return err(msg, 400)

    t.to_bed_id = new_bed.id
    t.scheduled_at = scheduled_at

    if reserve_minutes > 0:
        until_dt = scheduled_at + timedelta(minutes=reserve_minutes) if scheduled_at else None
        _reserve_bed(new_bed, reserve_minutes, until_dt=until_dt)

    if scheduled_at and scheduled_at > _now_utc_naive():
        t.status = "scheduled"

    db.commit()
    t2 = _load_transfer_with_locations(db, transfer_id)
    return ok(_transfer_dict(t2))


@router.post("/transfers/{transfer_id}/complete")
def complete_transfer(
    transfer_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.transfers.complete"):
        return err("Not permitted", 403)

    t = (
        db.query(IpdTransfer)
        .options(
            joinedload(IpdTransfer.admission),
            joinedload(IpdTransfer.from_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
            joinedload(IpdTransfer.to_bed).joinedload(IpdBed.room).joinedload(IpdRoom.ward),
        )
        .filter(IpdTransfer.id == transfer_id)
        .first()
    )
    if not t:
        return err("Transfer not found", 404)

    if t.status not in {"approved", "scheduled"}:
        return err(f"Cannot complete when status is {t.status}", 400)

    if not t.to_bed_id:
        return err("Target bed not assigned", 400)

    adm = db.query(IpdAdmission).filter(IpdAdmission.id == t.admission_id).first()
    if not adm:
        return err("Admission not found", 404)

    vacated_at = _parse_dt_to_utc_naive(payload.get("vacated_at")) or _now_utc_naive()
    occupied_at = _parse_dt_to_utc_naive(payload.get("occupied_at")) or vacated_at

    to_bed = db.query(IpdBed).filter(IpdBed.id == t.to_bed_id).first()
    if not to_bed:
        return err("Target bed not found", 404)

    msg = _ensure_bed_available(to_bed, allow_reserved_for_same=True, transfer=t)
    if msg:
        return err(msg, 400)

    cur_asg = _active_assignment(db, adm.id)
    if not cur_asg:
        return err("No active bed assignment found for this admission", 400)

    from_bed = db.query(IpdBed).filter(IpdBed.id == cur_asg.bed_id).first() if cur_asg.bed_id else None
    if from_bed and to_bed.id == from_bed.id:
        return err("Target bed must be different from current bed", 400)

    # close current assignment
    cur_asg.to_ts = vacated_at
    cur_asg.reason = "transfer_out"
    t.from_assignment_id = cur_asg.id

    # free old bed
    if from_bed:
        from_bed.state = "vacant"
        setattr(from_bed, "reserved_until", None)

    # create new assignment
    new_asg = IpdBedAssignment(
        admission_id=adm.id,
        bed_id=to_bed.id,
        from_ts=occupied_at,
        to_ts=None,
        reason="transfer_in",
    )
    db.add(new_asg)
    db.flush()

    # occupy new bed
    to_bed.state = "occupied"
    setattr(to_bed, "reserved_until", None)

    # update admission current bed
    adm.current_bed_id = to_bed.id
    adm.status = "transferred"

    # handover JSON
    handover = payload.get("handover")
    if handover is not None:
        try:
            t.handover_json = json.dumps(handover, ensure_ascii=False)
        except Exception:
            t.handover_json = ""

    # complete transfer
    t.vacated_at = vacated_at
    t.occupied_at = occupied_at
    t.to_assignment_id = new_asg.id
    t.completed_by = user.id
    t.completed_at = _now_utc_naive()
    t.status = "completed"

    db.commit()
    t2 = _load_transfer_with_locations(db, transfer_id)
    return ok(_transfer_dict(t2))


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(
    transfer_id: int,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    user: UserModel = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.transfers.cancel"):
        return err("Not permitted", 403)

    t = db.query(IpdTransfer).filter(IpdTransfer.id == transfer_id).first()
    if not t:
        return err("Transfer not found", 404)

    if t.status in {"completed", "cancelled"}:
        return err(f"Cannot cancel when status is {t.status}", 400)

    reason = payload.get("reason") or ""

    if t.to_bed_id:
        bed = db.query(IpdBed).filter(IpdBed.id == t.to_bed_id).first()
        if bed:
            _release_reservation(bed)

    t.status = "cancelled"
    t.cancelled_by = user.id
    t.cancelled_at = _now_utc_naive()
    t.cancel_reason = reason

    db.commit()
    t2 = _load_transfer_with_locations(db, transfer_id)
    return ok(_transfer_dict(t2))
