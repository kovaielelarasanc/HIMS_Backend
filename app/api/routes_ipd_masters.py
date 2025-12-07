# FILE: app/api/routes_ipd_master.py
from __future__ import annotations
from datetime import datetime, date
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
    for r in getattr(user, "roles", []):
        for p in getattr(r, "permissions", []):
            if p.code == code:
                return True
    return False


# ---------------------------------------------------------------------
# WARDS
# ---------------------------------------------------------------------
@router.post("/wards", response_model=WardOut)
def create_ward(
    payload: WardIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    w = IpdWard(
        name=payload.name.strip(),
        code=payload.code.strip(),
        floor=payload.floor or "",
    )
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


@router.get("/wards", response_model=List[WardOut])
def list_wards(
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return (
        db.query(IpdWard)
        .filter(IpdWard.is_active.is_(True))
        .order_by(IpdWard.name.asc())
        .all()
    )


# ---------------------------------------------------------------------
# ROOMS
# ---------------------------------------------------------------------
@router.post("/rooms", response_model=RoomOut)
def create_room(
    payload: RoomIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    r = IpdRoom(
        ward_id=payload.ward_id,
        number=payload.number.strip(),
        type=(payload.type or "General").strip(),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.get("/rooms", response_model=List[RoomOut])
def list_rooms(
    ward_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdRoom).filter(IpdRoom.is_active.is_(True))
    if ward_id:
        q = q.filter(IpdRoom.ward_id == ward_id)
    return q.order_by(IpdRoom.number.asc()).all()


# ---------------------------------------------------------------------
# BEDS
# ---------------------------------------------------------------------
@router.post("/beds", response_model=BedOut)
def create_bed(
    payload: BedIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    b = IpdBed(
        room_id=payload.room_id,
        code=payload.code.strip(),
        state="vacant",
    )
    db.add(b)
    db.commit()
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
    return q.order_by(IpdBed.code.asc()).all()


class BedStateIn(BaseModel):
    state: str  # vacant / reserved / preoccupied
    reserved_until: Optional[datetime] = None
    note: Optional[str] = None


@router.patch("/beds/{bed_id}/state")
def set_bed_state(
    bed_id: int,
    state: str = Query(..., regex="^(vacant|reserved|preoccupied)$"),
    reserved_until: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    """
    Set bed state quickly from the board.
    NOTE: 'occupied' is intentionally blocked — use admission/transfer to occupy.
    """
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")

    b = db.query(IpdBed).get(bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")

    b.state = state
    b.reserved_until = reserved_until if state == "reserved" else None
    db.commit()
    return {
        "message": "Updated",
        "bed_id": b.id,
        "state": b.state,
        "reserved_until": b.reserved_until,
    }


@router.post("/beds/{bed_id}/reserve")
def reserve_bed(
    bed_id: int,
    until_ts: Optional[datetime] = Query(
        None, description="Optional reservation expiry"
    ),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    b = db.query(IpdBed).get(bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")
    if b.state != "vacant":
        raise HTTPException(400, "Only vacant beds can be reserved")
    b.state = "reserved"
    b.reserved_until = until_ts
    db.commit()
    return {
        "message": "Reserved",
        "bed_id": b.id,
        "reserved_until": b.reserved_until,
    }


@router.post("/beds/{bed_id}/release")
def release_bed(
    bed_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    b = db.query(IpdBed).get(bed_id)
    if not b:
        raise HTTPException(404, "Bed not found")
    if b.state not in ("reserved", "preoccupied"):
        raise HTTPException(
            400, "Only reserved/preoccupied beds can be released to vacant"
        )
    b.state = "vacant"
    b.reserved_until = None
    db.commit()
    return {"message": "Released", "bed_id": b.id}


# ---------------------------------------------------------------------
# QUICK SNAPSHOT / TREE
# ---------------------------------------------------------------------
@router.get("/bedboard")
def bedboard_snapshot(
    ward_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    """
    Lightweight board with enriched labels for UI cards.
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    q = (
        db.query(IpdBed)
        .join(IpdRoom, IpdRoom.id == IpdBed.room_id)
        .join(IpdWard, IpdWard.id == IpdRoom.ward_id)
    )
    if ward_id:
        q = q.filter(IpdWard.id == ward_id)

    rows = q.add_columns(IpdRoom.number, IpdRoom.type, IpdWard.name).all()
    counts: Dict[str, int] = {"vacant": 0, "occupied": 0, "reserved": 0, "preoccupied": 0}

    beds: List[Dict[str, Any]] = []
    for b, room_number, room_type, ward_name in rows:
        counts[b.state] = counts.get(b.state, 0) + 1
        beds.append(
            {
                "id": b.id,
                "code": b.code,
                "state": b.state,
                "room_id": b.room_id,
                "room_number": room_number,
                "room_type": room_type,
                "ward_name": ward_name,
                "reserved_until": b.reserved_until,
                "note": b.note,
            }
        )

    return {"beds": beds, "counts": counts}


@router.get("/tree")
def ward_room_bed_tree(
    only_active: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    """
    Convenience endpoint for cascading pickers:
    Ward -> Rooms -> Beds (with current state).
    """
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")

    wq = db.query(IpdWard)
    if only_active:
        wq = wq.filter(IpdWard.is_active.is_(True))
    wards = wq.order_by(IpdWard.name.asc()).all()

    room_map: Dict[int, List[IpdRoom]] = {}
    bed_map: Dict[int, List[IpdBed]] = {}

    rooms = db.query(IpdRoom).all()
    for r in rooms:
        room_map.setdefault(r.ward_id, []).append(r)
    for k in room_map:
        room_map[k].sort(key=lambda x: x.number)

    beds = db.query(IpdBed).all()
    for b in beds:
        bed_map.setdefault(b.room_id, []).append(b)
    for k in bed_map:
        bed_map[k].sort(key=lambda x: x.code)

    tree = []
    for w in wards:
        r_nodes = []
        for r in room_map.get(w.id, []):
            r_nodes.append(
                {
                    "id": r.id,
                    "number": r.number,
                    "type": r.type,
                    "beds": [
                        {
                            "id": b.id,
                            "code": b.code,
                            "state": b.state,
                            "reserved_until": b.reserved_until,
                            "note": b.note,
                        }
                        for b in bed_map.get(r.id, [])
                    ],
                }
            )
        tree.append(
            {
                "id": w.id,
                "name": w.name,
                "code": w.code,
                "floor": w.floor,
                "rooms": r_nodes,
            }
        )

    return {"wards": tree}


# ---------------------------------------------------------------------
# PACKAGES
# ---------------------------------------------------------------------
@router.post("/packages", response_model=PackageOut)
def create_package(
    payload: PackageIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.packages.manage"):
        raise HTTPException(403, "Not permitted")
    pkg = IpdPackage(**payload.dict())
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return pkg


@router.get("/packages", response_model=List[PackageOut])
def list_packages(
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    return db.query(IpdPackage).order_by(IpdPackage.name.asc()).all()


# ---------------------------------------------------------------------
# BED RATES
# ---------------------------------------------------------------------
@router.post("/bed-rates", response_model=BedRateOut)
def create_bed_rate(
    payload: BedRateIn,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.masters.manage"):
        raise HTTPException(403, "Not permitted")
    r = IpdBedRate(**payload.dict())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.get("/bed-rates", response_model=List[BedRateOut])
def list_bed_rates(
    room_type: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    q = db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True))
    if room_type:
        q = q.filter(IpdBedRate.room_type == room_type)
    return (
        q.order_by(IpdBedRate.room_type.asc(), IpdBedRate.effective_from.desc())
        .all()
    )


@router.get("/bed-rates/resolve")
def resolve_bed_rate(
    room_type: str = Query(...),
    on_date: date = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    if not has_perm(user, "ipd.view"):
        raise HTTPException(403, "Not permitted")
    r = (
        db.query(IpdBedRate)
        .filter(IpdBedRate.is_active.is_(True))
        .filter(IpdBedRate.room_type == room_type)
        .filter(IpdBedRate.effective_from <= on_date)
        .filter(
            (IpdBedRate.effective_to == None)  # noqa: E711
            | (IpdBedRate.effective_to >= on_date)
        )
        .order_by(IpdBedRate.effective_from.desc())
        .first()
    )
    if not r:
        return {"room_type": room_type, "on_date": on_date, "daily_rate": None}
    return {
        "room_type": room_type,
        "on_date": on_date,
        "daily_rate": float(r.daily_rate),
    }
