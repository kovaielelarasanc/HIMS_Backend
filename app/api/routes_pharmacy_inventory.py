# # app/api/routes_pharmacy_inventory.py
# from __future__ import annotations
# from datetime import date, datetime
# from typing import List, Optional, Tuple

# from fastapi import APIRouter, Depends, HTTPException, Query
# from sqlalchemy import func, or_, cast, String
# from sqlalchemy.orm import Session

# from app.api.deps import get_db, current_user as auth_current_user
# from app.models.user import User
# from app.models.pharmacy import (
#     PharmacyInventoryLot,
#     PharmacyInventoryTxn,
#     PharmacyAdjustment,
#     PharmacyTransfer,
#     PharmacyMedicine,
#     PharmacyLocation,
# )
# from app.schemas.pharmacy import LotOut, TxnOut, AdjustIn, TransferIn

# router = APIRouter()


# def has_perm(user: User, code: str) -> bool:
#     if getattr(user, "is_admin", False):
#         return True
#     for r in getattr(user, "roles", []) or []:
#         for p in getattr(r, "permissions", []) or []:
#             if getattr(p, "code", None) == code:
#                 return True
#     return False


# # ---------------- Lots ----------------


# @router.get("/inventory/lots", response_model=List[LotOut])
# def list_lots(
#         q: Optional[str] = Query(
#             None, description="search medicine code/name/batch"),
#         location_id: Optional[int] = Query(None),
#         medicine_id: Optional[int] = Query(None),
#         expiry_before: Optional[date] = Query(None),
#         only_low: Optional[bool] = Query(False),
#         limit: int = Query(500, ge=1, le=2000),
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Returns inventory lots with optional filters used by the Inventory page:
#       - q: matches medicine code/name or lot batch
#       - location_id, medicine_id
#       - expiry_before: show lots expiring before this date
#       - only_low: show lots for medicines that are below reorder level at that location
#     """
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     # Base query (join to enable search by med name/code)
#     qry = (db.query(PharmacyInventoryLot).join(
#         PharmacyMedicine,
#         PharmacyMedicine.id == PharmacyInventoryLot.medicine_id))

#     if location_id:
#         qry = qry.filter(PharmacyInventoryLot.location_id == location_id)
#     if medicine_id:
#         qry = qry.filter(PharmacyInventoryLot.medicine_id == medicine_id)
#     if expiry_before:
#         qry = qry.filter(PharmacyInventoryLot.expiry <= expiry_before)
#     if q:
#         like = f"%{q.strip()}%"
#         qry = qry.filter(
#             or_(
#                 PharmacyMedicine.code.ilike(like),
#                 PharmacyMedicine.name.ilike(like),
#                 PharmacyInventoryLot.batch.ilike(like),
#             ))

#     # Sort by earliest expiry first (FEFO friendly)
#     lots = qry.order_by(PharmacyInventoryLot.expiry.asc()).limit(
#         limit * 3).all()  # fetch extra for only_low filter

#     if only_low:
#         # Compute totals per (medicine_id, location_id) across ALL lots (ignoring q/expiry filters),
#         # then keep only those pairs where total_on_hand < medicine.reorder_level.
#         # 1) figure the pairs present in this page
#         pairs = {(l.medicine_id, l.location_id) for l in lots}
#         if not pairs:
#             return []

#         med_ids = {m for (m, _) in pairs}
#         # 2) get reorder levels for those meds
#         reorder_by_med = {
#             mid: (reorder or 0)
#             for (mid, reorder) in db.query(
#                 PharmacyMedicine.id, PharmacyMedicine.reorder_level).filter(
#                     PharmacyMedicine.id.in_(med_ids)).all()
#         }
#         # 3) totals per pair
#         totals = {
#             (mid, lid): qty
#             for (mid, lid, qty) in (db.query(
#                 PharmacyInventoryLot.medicine_id,
#                 PharmacyInventoryLot.location_id,
#                 func.coalesce(func.sum(PharmacyInventoryLot.on_hand), 0),
#             ).group_by(PharmacyInventoryLot.medicine_id,
#                        PharmacyInventoryLot.location_id).having(
#                            func.concat(PharmacyInventoryLot.medicine_id, ":",
#                                        PharmacyInventoryLot.location_id).in_(
#                                            [f"{m}:{l}"
#                                             for (m, l) in pairs])).all())
#         }
#         # 4) filter lots belonging to low pairs
#         low_pairs = {
#             pair
#             for pair, total in totals.items()
#             if total < (reorder_by_med.get(pair[0], 0) or 0)
#         }
#         lots = [l for l in lots if (l.medicine_id, l.location_id) in low_pairs]

#     # finally limit
#     return lots[:limit]


# # ---------------- Transactions ----------------


# @router.get("/inventory/txns", response_model=List[TxnOut])
# def list_txns(
#         q: Optional[str] = Query(None, description="search note/ref/batch"),
#         type: Optional[str] = Query(None, description="txn type"),
#         location_id: Optional[int] = None,
#         medicine_id: Optional[int] = None,
#         limit: int = Query(500, ge=1, le=2000),
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Returns inventory transactions with optional filters used by the UI.
#     """
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     qry = (db.query(PharmacyInventoryTxn).outerjoin(
#         PharmacyInventoryLot,
#         PharmacyInventoryLot.id == PharmacyInventoryTxn.lot_id).order_by(
#             PharmacyInventoryTxn.id.desc()))
#     if type:
#         qry = qry.filter(PharmacyInventoryTxn.type == type)
#     if location_id:
#         qry = qry.filter(PharmacyInventoryTxn.location_id == location_id)
#     if medicine_id:
#         qry = qry.filter(PharmacyInventoryTxn.medicine_id == medicine_id)
#     if q:
#         like = f"%{q.strip()}%"
#         qry = qry.filter(
#             or_(
#                 PharmacyInventoryTxn.ref_type.ilike(like),
#                 cast(PharmacyInventoryTxn.ref_id, String).ilike(like),
#                 PharmacyInventoryTxn.note.ilike(like),
#                 PharmacyInventoryLot.batch.ilike(like),
#             ))

#     return qry.limit(limit).all()


# # ---------------- Adjust ----------------


# @router.post("/inventory/adjust")
# def adjust_stock(
#         payload: AdjustIn,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Adjust a lot's on_hand by qty_change (positive or negative) and record a transaction.
#     """
#     if not has_perm(user, "pharmacy.inventory.manage"):
#         raise HTTPException(403, "Not permitted")

#     lot = db.query(PharmacyInventoryLot).get(payload.lot_id)
#     if not lot:
#         raise HTTPException(404, "Lot not found")

#     qty_change = int(payload.qty_change or 0)
#     lot.on_hand += qty_change

#     db.add(
#         PharmacyAdjustment(
#             lot_id=lot.id,
#             qty_change=qty_change,
#             reason=payload.reason or "stock_take",
#             user_id=user.id,
#         ))

#     db.add(
#         PharmacyInventoryTxn(
#             ts=datetime.utcnow(),
#             medicine_id=lot.medicine_id,
#             location_id=lot.location_id,
#             lot_id=lot.id,
#             type=("adjust_in" if qty_change >= 0 else "adjust_out"),
#             qty_change=qty_change,
#             ref_type="adjust",
#             ref_id=None,
#             note=payload.reason or None,
#             user_id=user.id,
#         ))

#     db.commit()
#     return {"message": "Adjusted", "on_hand": lot.on_hand}


# # ---------------- Transfer ----------------


# @router.post("/inventory/transfer")
# def transfer_stock(
#         payload: TransferIn,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Move quantity from one location to another (same medicine/batch/expiry).
#     Creates/uses a target lot at the destination and records both in/out txns.
#     """
#     if not has_perm(user, "pharmacy.inventory.manage"):
#         raise HTTPException(403, "Not permitted")

#     src = db.query(PharmacyInventoryLot).get(payload.lot_id)
#     if not src:
#         raise HTTPException(404, "Source lot not found")
#     if src.location_id != payload.from_location_id:
#         raise HTTPException(400, "Lot not at from_location")

#     qty = int(payload.qty or 0)
#     if qty <= 0 or qty > src.on_hand:
#         raise HTTPException(400, "Invalid qty")

#     # find/create target lot
#     tgt = (db.query(PharmacyInventoryLot).filter(
#         PharmacyInventoryLot.medicine_id == src.medicine_id,
#         PharmacyInventoryLot.location_id == payload.to_location_id,
#         PharmacyInventoryLot.batch == src.batch,
#         PharmacyInventoryLot.expiry == src.expiry,
#     ).first())
#     if not tgt:
#         tgt = PharmacyInventoryLot(
#             medicine_id=src.medicine_id,
#             location_id=payload.to_location_id,
#             batch=src.batch,
#             expiry=src.expiry,
#             on_hand=0,
#             unit_cost=src.unit_cost,
#             sell_price=src.sell_price,
#             mrp=src.mrp,
#         )
#         db.add(tgt)
#         db.flush()

#     # apply movement
#     src.on_hand -= qty
#     tgt.on_hand += qty

#     db.add(
#         PharmacyTransfer(
#             from_location_id=payload.from_location_id,
#             to_location_id=payload.to_location_id,
#             lot_id=src.id,
#             qty=qty,
#             user_id=user.id,
#         ))

#     # out
#     db.add(
#         PharmacyInventoryTxn(
#             ts=datetime.utcnow(),
#             medicine_id=src.medicine_id,
#             location_id=src.location_id,
#             lot_id=src.id,
#             type="transfer_out",
#             qty_change=-qty,
#             ref_type="transfer",
#             ref_id=None,
#             user_id=user.id,
#         ))
#     # in
#     db.add(
#         PharmacyInventoryTxn(
#             ts=datetime.utcnow(),
#             medicine_id=tgt.medicine_id,
#             location_id=tgt.location_id,
#             lot_id=tgt.id,
#             type="transfer_in",
#             qty_change=qty,
#             ref_type="transfer",
#             ref_id=None,
#             user_id=user.id,
#         ))

#     db.commit()
#     return {
#         "message": "Transferred",
#         "from_on_hand": src.on_hand,
#         "to_on_hand": tgt.on_hand
#     }


# # keep your existing list_lots and list_txns


# @router.get("/inventory/lots", response_model=List[LotOut])
# def list_lots_alias(
#         medicine_id: Optional[int] = None,
#         location_id: Optional[int] = None,
#         only_positive: bool = True,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     # same logic as list_lots
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     q = db.query(PharmacyInventoryLot)
#     if medicine_id:
#         q = q.filter(PharmacyInventoryLot.medicine_id == medicine_id)
#     if location_id:
#         q = q.filter(PharmacyInventoryLot.location_id == location_id)
#     if only_positive: q = q.filter(PharmacyInventoryLot.on_hand > 0)
#     return q.order_by(PharmacyInventoryLot.expiry.asc()).all()


# @router.get("/inventory/txns", response_model=List[TxnOut])
# def list_txns_alias(
#         medicine_id: Optional[int] = None,
#         location_id: Optional[int] = None,
#         lot_id: Optional[int] = None,
#         limit: int = Query(200, ge=1, le=1000),
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     # same logic as list_txns
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     q = db.query(PharmacyInventoryTxn).order_by(PharmacyInventoryTxn.id.desc())
#     if medicine_id:
#         q = q.filter(PharmacyInventoryTxn.medicine_id == medicine_id)
#     if location_id:
#         q = q.filter(PharmacyInventoryTxn.location_id == location_id)
#     if lot_id: q = q.filter(PharmacyInventoryTxn.lot_id == lot_id)
#     return q.limit(limit).all()
