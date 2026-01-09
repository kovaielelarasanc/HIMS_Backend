# FILE: app/api/routes_ipd_master.py
from __future__ import annotations

from datetime import datetime, date
from typing import List, Optional, Dict, Any, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.ipd import IpdWard, IpdRoom, IpdBed, IpdPackage, IpdBedRate
from app.schemas.ipd import (
    WardIn,
    WardOut,
    RoomIn,
    RoomOut,
    BedIn,
    BedOut,
    PackageIn,
    PackageOut,
    BedRateIn,
    BedRateOut,
)

router = APIRouter()


# ---------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if p.code == code:
                return True
    return False


def _dump(pyd):
    # supports pydantic v1/v2 safely
    if hasattr(pyd, "model_dump"):
        return pyd.model_dump(exclude_none=True)
    return pyd.dict(exclude_none=True)


def _soft_delete(db: Session, obj):
    """
    Prefer soft delete if `is_active` exists, else hard delete.
    """
    if obj is None:
        return
    if hasattr(obj, "is_active"):
        setattr(obj, "is_active", False)
    else:
        db.delete(obj)


def _require_manage(user: User, code: str):
    if not has_perm(user, code):
        raise HTTPException(403, "Not permitted")


# ---------------------------------------------------------------------
# Canonical Room Type Normalization
# ---------------------------------------------------------------------
CANON_ROOM_TYPES = [
    "General",
    "Semi Private",
    "Private",
    "Deluxe",
    "ICU",
    "NICU",
    "PICU",
    "HDU",
    "Isolation",
]

ROOM_TYPE_ALIAS: Dict[str, str] = {
    "general ward": "General",
    "general": "General",
    "gen": "General",
    "gw": "General",
    "ward general": "General",
    "semi private": "Semi Private",
    "semiprivate": "Semi Private",
    "semi-private": "Semi Private",
    "private ward": "Private",
    "private": "Private",
    "pvt": "Private",
    "deluxe": "Deluxe",
    "dlx": "Deluxe",
    "delux": "Deluxe",
    "icu": "ICU",
    "icu ward": "ICU",
    "intensive care": "ICU",
    "nicu": "NICU",
    "nicu ward": "NICU",
    "picu": "PICU",
    "hdu": "HDU",
    "isolation": "Isolation",
    "isolation ward": "Isolation",
}

# ---------------------------------------------------------------------
# Bed Rate helpers (room_type + rate_basis)
# ---------------------------------------------------------------------
RATE_BASIS_ALIAS: Dict[str, str] = {
    "daily": "daily",
    "day": "daily",
    "per day": "daily",
    "perday": "daily",
    "d": "daily",
    "hourly": "hourly",
    "hour": "hourly",
    "per hour": "hourly",
    "perhour": "hourly",
    "hr": "hourly",
    "hrs": "hourly",
    "h": "hourly",
}


def norm_room_type(x: Optional[str]) -> str:
    s = (x or "General").strip()
    if not s:
        return "General"

    key = " ".join(s.lower().split())
    mapped = ROOM_TYPE_ALIAS.get(key)
    if mapped:
        return mapped

    if "nicu" in key:
        return "NICU"
    if "picu" in key:
        return "PICU"
    if "icu" in key:
        return "ICU"
    if "hdu" in key:
        return "HDU"
    if "deluxe" in key:
        return "Deluxe"
    if "private" in key and "semi" not in key:
        return "Private"
    if "semi" in key and "private" in key:
        return "Semi Private"
    if "general" in key:
        return "General"
    if "isolation" in key:
        return "Isolation"

    return s.title()


# --------------------------
# Bed Rate room_type normalize
# Keeps optional (Daily)/(Hourly) suffix
# --------------------------
def _parse_rate_basis(raw: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Backward compatible parser:
      "General (Daily)" -> ("General", "daily")
      "ICU (Hourly)" -> ("ICU", "hourly")
      "Private" -> ("Private", None)
    """
    s = (raw or "").strip()
    if not s:
        return ("General", None)

    lower = s.lower().strip()
    if lower.endswith("(daily)"):
        return (s[:-7].strip(), "daily")
    if lower.endswith("(hourly)"):
        return (s[:-8].strip(), "hourly")
    return (s, None)


def norm_rate_basis(x: Optional[str], default: str = "daily") -> str:
    s = (x or "").strip().lower()
    if not s:
        return default
    s = " ".join(s.replace("_", " ").replace("-", " ").split())
    mapped = RATE_BASIS_ALIAS.get(s)
    if mapped:
        return mapped
    raise HTTPException(400, "Invalid rate_basis. Use 'daily' or 'hourly'.")


def split_room_type_basis(room_type_raw: Optional[str],
                          basis_raw: Optional[str]) -> Tuple[str, str]:
    base, suffix_basis = _parse_rate_basis(room_type_raw)
    rt = norm_room_type(base)
    rb = norm_rate_basis(basis_raw or suffix_basis or "daily")
    return rt, rb


def _load_rate_map(db: Session,
                   on_date: date,
                   rate_basis: str = "daily") -> Dict[str, float]:
    """
    Returns: { normalized_room_type: rate_amount }
    Filters by rate_basis.
    """
    rb = norm_rate_basis(rate_basis)

    rows = (db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
        IpdBedRate.rate_basis == rb).filter(
            IpdBedRate.effective_from <= on_date).filter(
                or_(IpdBedRate.effective_to.is_(None),
                    IpdBedRate.effective_to >= on_date)).order_by(
                        IpdBedRate.room_type.asc(),
                        IpdBedRate.effective_from.desc()).all())

    rate_map: Dict[str, float] = {}
    for r in rows:
        rt = norm_room_type(r.room_type)
        if rt not in rate_map:
            rate_map[rt] = float(r.daily_rate)
    return rate_map


def _missing_rate_info(items_room_types: List[str],
                       rate_map: Dict[str, float]) -> Tuple[int, List[str]]:
    missing: Set[str] = set()
    for rt in items_room_types:
        if rt not in rate_map:
            missing.add(rt)
    return len(missing), sorted(missing)


# ---------------------------------------------------------------------
# Update payloads (partial update supported)
# ---------------------------------------------------------------------
class WardUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    floor: Optional[str] = None
    is_active: Optional[bool] = None


class RoomUpdate(BaseModel):
    ward_id: Optional[int] = None
    number: Optional[str] = None
    type: Optional[str] = None
    is_active: Optional[bool] = None


class BedUpdate(BaseModel):
    room_id: Optional[int] = None
    code: Optional[str] = None
    # NOTE: state is managed by /beds/{id}/state, not here


class PackageUpdate(BaseModel):
    name: Optional[str] = None
    included: Optional[str] = None
    excluded: Optional[str] = None
    charges: Optional[float] = None
    is_active: Optional[bool] = None


class BedRateUpdate(BaseModel):
    room_type: Optional[str] = None
    daily_rate: Optional[float] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------
# WARDS (CRUD)
# ---------------------------------------------------------------------
@router.post("/wards", response_model=WardOut)
def create_ward(payload: WardIn,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    code = payload.code.strip()
    name = payload.name.strip()

    if not code or not name:
        raise HTTPException(400, "Ward code and name are required")

    # optional duplicate guard
    exists = db.query(IpdWard).filter(IpdWard.code == code).filter(
        IpdWard.is_active.is_(True)).first()
    if exists:
        raise HTTPException(409, f"Ward code '{code}' already exists")

    w = IpdWard(name=name, code=code, floor=(payload.floor or "").strip())
    db.add(w)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Ward already exists (duplicate)")
    db.refresh(w)
    return w


@router.get("/wards", response_model=List[WardOut])
def list_wards(db: Session = Depends(get_db),
               user: User = Depends(auth_current_user),
               include_inactive: bool = False):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = db.query(IpdWard)
    if not include_inactive:
        q = q.filter(IpdWard.is_active.is_(True))
    return q.order_by(IpdWard.name.asc()).all()


@router.get("/wards/{ward_id}", response_model=WardOut)
def get_ward(ward_id: int,
             db: Session = Depends(get_db),
             user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    w = db.get(IpdWard, ward_id)
    if not w:
        raise HTTPException(404, "Ward not found")
    return w


@router.put("/wards/{ward_id}", response_model=WardOut)
def update_ward(
        ward_id: int,
        payload: WardUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    w = db.get(IpdWard, ward_id)
    if not w:
        raise HTTPException(404, "Ward not found")

    data = _dump(payload)
    if "code" in data and data["code"] is not None:
        data["code"] = data["code"].strip()
        if not data["code"]:
            raise HTTPException(400, "Ward code cannot be empty")
        # duplicate check (optional)
        dup = db.query(IpdWard).filter(IpdWard.code == data["code"], IpdWard.id
                                       != ward_id).first()
        if dup:
            raise HTTPException(409,
                                f"Ward code '{data['code']}' already exists")

    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "Ward name cannot be empty")

    if "floor" in data and data["floor"] is not None:
        data["floor"] = (data["floor"] or "").strip()

    for k, v in data.items():
        setattr(w, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Update failed (duplicate or constraint)")
    db.refresh(w)
    return w


@router.delete("/wards/{ward_id}")
def delete_ward(ward_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    w = db.get(IpdWard, ward_id)
    if not w:
        raise HTTPException(404, "Ward not found")

    # block if rooms exist
    room_q = db.query(IpdRoom).filter(IpdRoom.ward_id == ward_id)
    if hasattr(IpdRoom, "is_active"):
        room_q = room_q.filter(IpdRoom.is_active.is_(True))
    if room_q.count() > 0:
        raise HTTPException(
            400, "Cannot delete ward: rooms exist. Delete rooms first.")

    _soft_delete(db, w)
    db.commit()
    return {"message": "Deleted", "ward_id": ward_id}


# ---------------------------------------------------------------------
# ROOMS (CRUD)
# ---------------------------------------------------------------------
@router.post("/rooms", response_model=RoomOut)
def create_room(payload: RoomIn,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    ward = db.get(IpdWard, payload.ward_id)
    if not ward or (hasattr(ward, "is_active") and not ward.is_active):
        raise HTTPException(400, "Invalid ward")

    r = IpdRoom(
        ward_id=payload.ward_id,
        number=payload.number.strip(),
        type=norm_room_type(payload.type),
    )
    db.add(r)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,
                            "Room already exists (duplicate or constraint)")
    db.refresh(r)
    return r


@router.get("/rooms", response_model=List[RoomOut])
def list_rooms(
        ward_id: Optional[int] = None,
        include_inactive: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = db.query(IpdRoom)
    if not include_inactive and hasattr(IpdRoom, "is_active"):
        q = q.filter(IpdRoom.is_active.is_(True))
    if ward_id:
        q = q.filter(IpdRoom.ward_id == ward_id)
    return q.order_by(IpdRoom.number.asc()).all()


@router.get("/rooms/{room_id}", response_model=RoomOut)
def get_room(room_id: int,
             db: Session = Depends(get_db),
             user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    r = db.get(IpdRoom, room_id)
    if not r:
        raise HTTPException(404, "Room not found")
    return r


@router.put("/rooms/{room_id}", response_model=RoomOut)
def update_room(
        room_id: int,
        payload: RoomUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    r = db.get(IpdRoom, room_id)
    if not r:
        raise HTTPException(404, "Room not found")

    data = _dump(payload)

    if "ward_id" in data and data["ward_id"] is not None:
        w = db.get(IpdWard, int(data["ward_id"]))
        if not w or (hasattr(w, "is_active") and not w.is_active):
            raise HTTPException(400, "Invalid ward_id")

    if "number" in data and data["number"] is not None:
        data["number"] = data["number"].strip()
        if not data["number"]:
            raise HTTPException(400, "Room number cannot be empty")

    if "type" in data and data["type"] is not None:
        data["type"] = norm_room_type(data["type"])

    for k, v in data.items():
        setattr(r, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Update failed (duplicate or constraint)")
    db.refresh(r)
    return r


@router.delete("/rooms/{room_id}")
def delete_room(room_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    r = db.get(IpdRoom, room_id)
    if not r:
        raise HTTPException(404, "Room not found")

    bed_q = db.query(IpdBed).filter(IpdBed.room_id == room_id)
    if hasattr(IpdBed, "is_active"):
        bed_q = bed_q.filter(IpdBed.is_active.is_(True))
    if bed_q.count() > 0:
        raise HTTPException(
            400, "Cannot delete room: beds exist. Delete/move beds first.")

    _soft_delete(db, r)
    db.commit()
    return {"message": "Deleted", "room_id": room_id}


# ---------------------------------------------------------------------
# BEDS (CRUD)
# ---------------------------------------------------------------------
@router.post("/beds", response_model=BedOut)
def create_bed(payload: BedIn,
               db: Session = Depends(get_db),
               user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    room = db.get(IpdRoom, payload.room_id)
    if not room or (hasattr(room, "is_active") and not room.is_active):
        raise HTTPException(400, "Invalid room")

    b = IpdBed(room_id=payload.room_id,
               code=payload.code.strip(),
               state="vacant")
    db.add(b)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,
                            "Bed already exists (duplicate or constraint)")
    db.refresh(b)
    return b


@router.get("/beds", response_model=List[BedOut])
def list_beds(
        room_id: Optional[int] = None,
        ward_id: Optional[int] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = db.query(IpdBed).join(IpdRoom, IpdRoom.id == IpdBed.room_id)
    if ward_id:
        q = q.filter(IpdRoom.ward_id == ward_id)
    if room_id:
        q = q.filter(IpdBed.room_id == room_id)

    # if your bed model has is_active, keep list consistent
    if hasattr(IpdBed, "is_active"):
        q = q.filter(IpdBed.is_active.is_(True))

    return q.order_by(IpdBed.code.asc()).all()


@router.get("/beds/{bed_id}", response_model=BedOut)
def get_bed(bed_id: int,
            db: Session = Depends(get_db),
            user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")
    return b


@router.put("/beds/{bed_id}", response_model=BedOut)
def update_bed(
        bed_id: int,
        payload: BedUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")

    if (b.state or "").lower() == "occupied":
        raise HTTPException(
            400, "Cannot edit an occupied bed. Transfer/discharge first.")

    data = _dump(payload)

    if "room_id" in data and data["room_id"] is not None:
        room = db.get(IpdRoom, int(data["room_id"]))
        if not room or (hasattr(room, "is_active") and not room.is_active):
            raise HTTPException(400, "Invalid room_id")

    if "code" in data and data["code"] is not None:
        data["code"] = data["code"].strip()
        if not data["code"]:
            raise HTTPException(400, "Bed code cannot be empty")

    for k, v in data.items():
        setattr(b, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Update failed (duplicate or constraint)")
    db.refresh(b)
    return b


@router.delete("/beds/{bed_id}")
def delete_bed(bed_id: int,
               db: Session = Depends(get_db),
               user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.masters.manage")

    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")

    if (b.state or "").lower() == "occupied":
        raise HTTPException(
            400, "Cannot delete an occupied bed. Transfer/discharge first.")

    _soft_delete(db, b)
    db.commit()
    return {"message": "Deleted", "bed_id": bed_id}


class BedStateIn(BaseModel):
    state: str = Field(..., description="vacant/reserved/preoccupied")
    reserved_until: Optional[datetime] = None
    note: Optional[str] = None


@router.patch("/beds/{bed_id}/state")
def set_bed_state(
        bed_id: int,
        state: str = Query(..., pattern="^(vacant|reserved|preoccupied)$"),
        reserved_until: Optional[datetime] = Query(None),
        note: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")

    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")

    b.state = state
    b.reserved_until = reserved_until if state == "reserved" else None
    if note is not None:
        b.note = (note or "").strip()

    db.commit()
    return {
        "message": "Updated",
        "bed_id": b.id,
        "state": b.state,
        "reserved_until": b.reserved_until,
        "note": b.note,
    }


@router.post("/beds/{bed_id}/reserve")
def reserve_bed(
        bed_id: int,
        until_ts: Optional[datetime] = Query(
            None, description="Optional reservation expiry"),
        note: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")

    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")
    if b.state != "vacant":
        raise HTTPException(400, "Only vacant beds can be reserved")

    b.state = "reserved"
    b.reserved_until = until_ts
    if note is not None:
        b.note = (note or "").strip()

    db.commit()
    return {
        "message": "Reserved",
        "bed_id": b.id,
        "reserved_until": b.reserved_until,
        "note": b.note
    }


@router.post("/beds/{bed_id}/release")
def release_bed(
        bed_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")

    b = db.get(IpdBed, bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")
    if b.state not in ("reserved", "preoccupied"):
        raise HTTPException(
            400, "Only reserved/preoccupied beds can be released to vacant")

    b.state = "vacant"
    b.reserved_until = None
    db.commit()
    return {"message": "Released", "bed_id": b.id}


# ---------------------------------------------------------------------
# QUICK SNAPSHOT / TREE (unchanged)
# ---------------------------------------------------------------------
@router.get("/bedboard")
def bedboard_snapshot(
        ward_id: Optional[int] = None,
        on_date: date = Query(default_factory=date.today),
        include_rates: bool = True,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = (db.query(IpdBed).join(IpdRoom, IpdRoom.id == IpdBed.room_id).join(
        IpdWard, IpdWard.id == IpdRoom.ward_id))
    if ward_id:
        q = q.filter(IpdWard.id == ward_id)

    if hasattr(IpdBed, "is_active"):
        q = q.filter(IpdBed.is_active.is_(True))
    if hasattr(IpdRoom, "is_active"):
        q = q.filter(IpdRoom.is_active.is_(True))
    if hasattr(IpdWard, "is_active"):
        q = q.filter(IpdWard.is_active.is_(True))

    rows = q.add_columns(IpdRoom.number, IpdRoom.type, IpdWard.name).all()

    rate_map = _load_rate_map(db, on_date, rate_basis="daily")
    room_types_seen: List[str] = []

    counts: Dict[str, int] = {
        "vacant": 0,
        "occupied": 0,
        "reserved": 0,
        "preoccupied": 0
    }
    beds_out: List[Dict[str, Any]] = []

    for b, room_number, room_type, ward_name in rows:
        counts[b.state] = counts.get(b.state, 0) + 1
        rt = norm_room_type(room_type)
        room_types_seen.append(rt)

        item = {
            "id": b.id,
            "code": b.code,
            "state": b.state,
            "room_id": b.room_id,
            "room_number": room_number,
            "room_type": rt,
            "ward_name": ward_name,
            "reserved_until": b.reserved_until,
            "note": b.note,
        }
        if include_rates:
            item["daily_rate"] = rate_map.get(rt)
            item["rate_date"] = on_date.isoformat()

        beds_out.append(item)

    missing_count, missing_types = _missing_rate_info(
        room_types_seen, rate_map) if include_rates else (0, [])
    return {
        "beds": beds_out,
        "counts": counts,
        "rate_date": on_date.isoformat(),
        "missing_rate_count": missing_count,
        "missing_room_types": missing_types,
    }


@router.get("/tree")
def ward_room_bed_tree(
        only_active: bool = True,
        include_rates: bool = False,
        on_date: date = Query(default_factory=date.today),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    wq = db.query(IpdWard)
    if only_active and hasattr(IpdWard, "is_active"):
        wq = wq.filter(IpdWard.is_active.is_(True))
    wards = wq.order_by(IpdWard.name.asc()).all()

    rq = db.query(IpdRoom)
    if only_active and hasattr(IpdRoom, "is_active"):
        rq = rq.filter(IpdRoom.is_active.is_(True))
    rooms = rq.all()

    bq = db.query(IpdBed)
    if only_active and hasattr(IpdBed, "is_active"):
        bq = bq.filter(IpdBed.is_active.is_(True))
    beds = bq.all()

    room_map: Dict[int, List[IpdRoom]] = {}
    for r in rooms:
        room_map.setdefault(r.ward_id, []).append(r)
    for k in room_map:
        room_map[k].sort(key=lambda x: x.number)

    bed_map: Dict[int, List[IpdBed]] = {}
    for b in beds:
        bed_map.setdefault(b.room_id, []).append(b)
    for k in bed_map:
        bed_map[k].sort(key=lambda x: x.code)

    rate_map = _load_rate_map(db, on_date) if include_rates else {}
    room_types_seen: List[str] = []

    tree: List[Dict[str, Any]] = []
    for w in wards:
        r_nodes = []
        for r in room_map.get(w.id, []):
            rt = norm_room_type(r.type)
            room_types_seen.append(rt)

            beds_out = []
            for b in bed_map.get(r.id, []):
                node = {
                    "id": b.id,
                    "code": b.code,
                    "state": b.state,
                    "reserved_until": b.reserved_until,
                    "note": b.note,
                }
                if include_rates:
                    node["daily_rate"] = rate_map.get(rt)
                    node["rate_date"] = on_date.isoformat()
                beds_out.append(node)

            r_nodes.append({
                "id": r.id,
                "number": r.number,
                "type": rt,
                "beds": beds_out
            })

        tree.append({
            "id": w.id,
            "name": w.name,
            "code": w.code,
            "floor": w.floor,
            "rooms": r_nodes
        })

    missing_count, missing_types = _missing_rate_info(
        room_types_seen, rate_map) if include_rates else (0, [])
    return {
        "wards": tree,
        "rate_date": on_date.isoformat(),
        "missing_rate_count": missing_count,
        "missing_room_types": missing_types,
    }


# ---------------------------------------------------------------------
# PACKAGES (CRUD)
# ---------------------------------------------------------------------
@router.post("/packages", response_model=PackageOut)
def create_package(payload: PackageIn,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.packages.manage")
    pkg = IpdPackage(**_dump(payload))
    db.add(pkg)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "Package already exists (duplicate or constraint)")
    db.refresh(pkg)
    return pkg


@router.get("/packages", response_model=List[PackageOut])
def list_packages(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
        include_inactive: bool = False,
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdPackage)
    if hasattr(IpdPackage, "is_active") and not include_inactive:
        q = q.filter(IpdPackage.is_active.is_(True))
    return q.order_by(IpdPackage.name.asc()).all()


@router.get("/packages/{package_id}", response_model=PackageOut)
def get_package(package_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    pkg = db.get(IpdPackage, package_id)
    if not pkg:
        raise HTTPException(404, "Package not found")
    return pkg


@router.put("/packages/{package_id}", response_model=PackageOut)
def update_package(
        package_id: int,
        payload: PackageUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.packages.manage")

    pkg = db.get(IpdPackage, package_id)
    if not pkg:
        raise HTTPException(404, "Package not found")

    data = _dump(payload)
    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "Package name cannot be empty")

    for k, v in data.items():
        setattr(pkg, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Update failed (duplicate or constraint)")
    db.refresh(pkg)
    return pkg


@router.delete("/packages/{package_id}")
def delete_package(package_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    _require_manage(user, "ipd.packages.manage")

    pkg = db.get(IpdPackage, package_id)
    if not pkg:
        raise HTTPException(404, "Package not found")

    _soft_delete(db, pkg)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            400, "Cannot delete package (in use). Deactivate instead.")
    return {"message": "Deleted", "package_id": package_id}


# ---------------------------------------------------------------------
# BED RATES (CRUD)
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# BED RATES (CRUD)
# ---------------------------------------------------------------------
@router.post("/bed-rates", response_model=BedRateOut)
def create_bed_rate(
        payload: BedRateIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    data = _dump(payload)

    # ✅ accept both:
    # - new: room_type="Private", rate_basis="daily"
    # - old: room_type="Private (Daily)" without rate_basis
    rt, rb = split_room_type_basis(data.get("room_type"),
                                   data.get("rate_basis"))
    data["room_type"] = rt
    data["rate_basis"] = rb

    ef = data.get("effective_from")
    et = data.get("effective_to")
    if et and ef and et < ef:
        raise HTTPException(400,
                            "effective_to cannot be before effective_from")

    r = IpdBedRate(**data)
    db.add(r)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "Bed rate already exists (duplicate or constraint)")
    db.refresh(r)
    return r


@router.get("/bed-rates", response_model=List[BedRateOut])
def list_bed_rates(
        room_type: Optional[str] = None,
        rate_basis: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True))

    # filter by room_type (supports old "Private (Daily)" input)
    suffix_basis = None
    if room_type:
        base, suffix_basis = _parse_rate_basis(room_type)
        rt = norm_room_type(base)
        q = q.filter(IpdBedRate.room_type == rt)

    # filter by basis if explicitly provided OR if suffix had it
    basis_to_use = rate_basis or suffix_basis
    if basis_to_use:
        rb = norm_rate_basis(basis_to_use)
        q = q.filter(IpdBedRate.rate_basis == rb)

    return q.order_by(IpdBedRate.room_type.asc(),
                      IpdBedRate.effective_from.desc()).all()


@router.get("/bed-rates/{rate_id}", response_model=BedRateOut)
def get_bed_rate(
        rate_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    r = db.get(IpdBedRate, rate_id)
    if not r:
        raise HTTPException(404, "Bed rate not found")
    return r


@router.put("/bed-rates/{rate_id}", response_model=BedRateOut)
def update_bed_rate(
        rate_id: int,
        payload: BedRateUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    r = db.get(IpdBedRate, rate_id)
    if not r:
        raise HTTPException(404, "Bed rate not found")

    data = _dump(payload)

    # normalize fields
    if "room_type" in data and data["room_type"] is not None:
        base, suffix_basis = _parse_rate_basis(data["room_type"])
        data["room_type"] = norm_room_type(base)
        if suffix_basis and not data.get("rate_basis"):
            data["rate_basis"] = suffix_basis

    if "rate_basis" in data and data["rate_basis"] is not None:
        data["rate_basis"] = norm_rate_basis(data["rate_basis"])

    ef = data.get("effective_from", r.effective_from)
    et = data.get("effective_to", r.effective_to)
    if et and ef and et < ef:
        raise HTTPException(400,
                            "effective_to cannot be before effective_from")

    for k, v in data.items():
        setattr(r, k, v)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Update failed (duplicate or constraint)")
    db.refresh(r)
    return r


@router.delete("/bed-rates/{rate_id}")
def delete_bed_rate(
        rate_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _require_manage(user, "ipd.masters.manage")

    r = db.get(IpdBedRate, rate_id)
    if not r:
        raise HTTPException(404, "Bed rate not found")

    r.is_active = False
    db.commit()
    return {"message": "Deleted", "rate_id": rate_id}


@router.get("/bed-rates/resolve")
def resolve_bed_rate(
        room_type: str = Query(...),
        on_date: date = Query(...),
        rate_basis: str = Query("daily"),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    base, suffix_basis = _parse_rate_basis(room_type)
    rt = norm_room_type(base)
    rb = norm_rate_basis(rate_basis or suffix_basis or "daily")

    r = (db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
        IpdBedRate.room_type == rt).filter(IpdBedRate.rate_basis == rb).filter(
            IpdBedRate.effective_from <= on_date).filter(
                or_(IpdBedRate.effective_to.is_(None), IpdBedRate.effective_to
                    >= on_date)).order_by(
                        IpdBedRate.effective_from.desc()).first())

    if not r:
        return {
            "room_type":
            rt,
            "rate_basis":
            rb,
            "on_date":
            on_date,
            "daily_rate":
            None,
            "warning":
            f"No active bed rate found for room_type '{rt}' ({rb}) on {on_date.isoformat()}",
        }

    return {
        "room_type": rt,
        "rate_basis": rb,
        "on_date": on_date,
        "daily_rate": float(r.daily_rate),
    }



# ---------------------------------------------------------------------
# “WITH RATE” LISTS (unchanged)
# ---------------------------------------------------------------------
@router.get("/bedboard-with-rate")
def bedboard_with_rate(
        ward_id: Optional[int] = None,
        on_date: date = Query(default_factory=date.today),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return bedboard_snapshot(
        ward_id=ward_id,
        on_date=on_date,
        include_rates=True,
        db=db,
        user=user,
    )


@router.get("/beds-with-rate")
def list_beds_with_rate(
        room_id: Optional[int] = None,
        ward_id: Optional[int] = None,
        on_date: date = Query(default_factory=date.today),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = (db.query(IpdBed).join(IpdRoom, IpdRoom.id == IpdBed.room_id).join(
        IpdWard, IpdWard.id == IpdRoom.ward_id))
    if ward_id:
        q = q.filter(IpdWard.id == ward_id)
    if room_id:
        q = q.filter(IpdRoom.id == room_id)

    if hasattr(IpdBed, "is_active"):
        q = q.filter(IpdBed.is_active.is_(True))

    rows = q.add_columns(IpdRoom.number, IpdRoom.type,
                         IpdWard.name).order_by(IpdBed.code.asc()).all()

    rate_map = _load_rate_map(db, on_date, rate_basis="daily")
    room_types_seen: List[str] = []

    out: List[Dict[str, Any]] = []
    for b, room_number, room_type, ward_name in rows:
        rt = norm_room_type(room_type)
        room_types_seen.append(rt)
        out.append({
            "id": b.id,
            "code": b.code,
            "state": b.state,
            "reserved_until": b.reserved_until,
            "note": b.note,
            "room_id": b.room_id,
            "room_number": room_number,
            "room_type": rt,
            "ward_name": ward_name,
            "daily_rate": rate_map.get(rt),
            "rate_date": on_date.isoformat(),
        })

    missing_count, missing_types = _missing_rate_info(room_types_seen,
                                                      rate_map)
    return {
        "items": out,
        "rate_date": on_date.isoformat(),
        "missing_rate_count": missing_count,
        "missing_room_types": missing_types,
    }
