# # app/api/routes_pharmacy_reports.py
# from __future__ import annotations
# from datetime import date, timedelta
# from typing import List, Optional, Dict, Any

# from fastapi import APIRouter, Depends, HTTPException, Query
# from sqlalchemy.orm import Session
# from sqlalchemy import func

# from app.api.deps import get_db, current_user as auth_current_user
# from app.models.user import User
# from app.models.pharmacy import (
#     PharmacyMedicine,
#     PharmacyInventoryLot,
#     PharmacyLocation,
# )

# router = APIRouter()


# def has_perm(user: User, code: str) -> bool:
#     if getattr(user, "is_admin", False):
#         return True
#     for r in getattr(user, "roles", []) or []:
#         for p in getattr(r, "permissions", []) or []:
#             if getattr(p, "code", None) == code:
#                 return True
#     return False


# # ------------ helpers (DB queries → plain dicts) ------------


# def _low_stock_rows(db: Session,
#                     location_id: Optional[int]) -> List[Dict[str, Any]]:
#     """
#     Low stock per (medicine, location): sum(on_hand) <= medicine.reorder_level
#     Returns fields used by the UI: medicine_id/name, location_id/code, on_hand, reorder_level
#     """
#     q = (db.query(
#         PharmacyMedicine.id.label("medicine_id"),
#         PharmacyMedicine.code.label("medicine_code"),
#         PharmacyMedicine.name.label("medicine_name"),
#         PharmacyInventoryLot.location_id.label("location_id"),
#         PharmacyLocation.code.label("location_code"),
#         func.coalesce(func.sum(PharmacyInventoryLot.on_hand),
#                       0).label("on_hand"),
#         PharmacyMedicine.reorder_level.label("reorder_level"),
#     ).join(PharmacyInventoryLot,
#            PharmacyInventoryLot.medicine_id == PharmacyMedicine.id,
#            isouter=True).join(
#                PharmacyLocation,
#                PharmacyLocation.id == PharmacyInventoryLot.location_id,
#                isouter=True).filter(
#                    PharmacyMedicine.is_active.is_(True)).group_by(
#                        PharmacyMedicine.id,
#                        PharmacyMedicine.code,
#                        PharmacyMedicine.name,
#                        PharmacyInventoryLot.location_id,
#                        PharmacyLocation.code,
#                        PharmacyMedicine.reorder_level,
#                    ))
#     if location_id:
#         q = q.filter(PharmacyInventoryLot.location_id == location_id)

#     rows = q.all()
#     out: List[Dict[str, Any]] = []
#     for r in rows:
#         m = r._mapping
#         on_hand = int(m["on_hand"] or 0)
#         reorder = int(m["reorder_level"] or 0)
#         # only flag when we have a location row (skip pure None location groups) and on_hand <= reorder_level
#         if m["location_id"] is not None and on_hand <= reorder:
#             out.append({
#                 "medicine_id": m["medicine_id"],
#                 "medicine_code": m["medicine_code"],
#                 "medicine_name": m["medicine_name"],
#                 "location_id": m["location_id"],
#                 "location_code": m["location_code"],
#                 "on_hand": on_hand,
#                 "reorder_level": reorder,
#             })
#     return out


# def _expiry_rows(
#     db: Session,
#     within_days: int,
#     location_id: Optional[int],
# ) -> List[Dict[str, Any]]:
#     """
#     Lots expiring within N days (and already expired). Includes medicine/location fields
#     the UI renders: id, medicine_id/name, batch, expiry, on_hand, location_id/code.
#     """
#     cutoff = date.today() + timedelta(days=within_days)

#     q = (db.query(
#         PharmacyInventoryLot.id.label("id"),
#         PharmacyInventoryLot.medicine_id.label("medicine_id"),
#         PharmacyMedicine.code.label("medicine_code"),
#         PharmacyMedicine.name.label("medicine_name"),
#         PharmacyInventoryLot.location_id.label("location_id"),
#         PharmacyLocation.code.label("location_code"),
#         PharmacyInventoryLot.batch.label("batch"),
#         PharmacyInventoryLot.expiry.label("expiry"),
#         PharmacyInventoryLot.on_hand.label("on_hand"),
#     ).join(PharmacyMedicine,
#            PharmacyMedicine.id == PharmacyInventoryLot.medicine_id).join(
#                PharmacyLocation,
#                PharmacyLocation.id == PharmacyInventoryLot.location_id).filter(
#                    PharmacyInventoryLot.expiry <= cutoff))
#     if location_id:
#         q = q.filter(PharmacyInventoryLot.location_id == location_id)

#     q = q.order_by(PharmacyInventoryLot.expiry.asc())
#     rows = q.all()

#     return [{
#         "id":
#         m["id"],
#         "medicine_id":
#         m["medicine_id"],
#         "medicine_code":
#         m["medicine_code"],
#         "medicine_name":
#         m["medicine_name"],
#         "location_id":
#         m["location_id"],
#         "location_code":
#         m["location_code"],
#         "batch":
#         m["batch"],
#         "expiry":
#         m["expiry"].isoformat()
#         if hasattr(m["expiry"], "isoformat") else m["expiry"],
#         "on_hand":
#         int(m["on_hand"] or 0),
#     } for m in (r._mapping for r in rows)]


# # ----------------- Endpoints -----------------


# @router.get("/alerts")
# def alerts_combined(
#         within_days: int = Query(30, ge=1, le=365),
#         location_id: Optional[int] = None,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Combined alerts for the UI:
#     {
#       "low":   [ { medicine_id, medicine_name, location_id, location_code, on_hand, reorder_level }, ... ],
#       "expiry":[ { id, medicine_id, medicine_name, batch, location_id, location_code, expiry, on_hand }, ... ]
#     }
#     """
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     low = _low_stock_rows(db, location_id)
#     expiry = _expiry_rows(db, within_days, location_id)
#     return {"low": low, "expiry": expiry}


# @router.get("/alerts/low-stock")
# def low_stock(
#         location_id: Optional[int] = None,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     return _low_stock_rows(db, location_id)


# @router.get("/alerts/expiry")
# def expiry_alerts(
#         within_days: int = Query(60, ge=1, le=365),
#         location_id: Optional[int] = None,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     return _expiry_rows(db, within_days, location_id)
