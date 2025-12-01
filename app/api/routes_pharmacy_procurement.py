# # FILE: app/api/routes_pharmacy_procurement.py
# from __future__ import annotations
# from typing import List, Optional
# from datetime import datetime
# import io

# from fastapi import APIRouter, Depends, HTTPException, Query
# from fastapi.responses import StreamingResponse
# from sqlalchemy import or_, cast, String, inspect
# from sqlalchemy.orm import Session

# from app.api.deps import get_db, current_user as auth_current_user
# from app.models.user import User
# from app.models.pharmacy import (
#     PharmacyPO,
#     PharmacyPOItem,
#     PharmacyGRN,
#     PharmacyGRNItem,
#     PharmacyInventoryLot,
#     PharmacyInventoryTxn,
#     PharmacyMedicine,
#     PharmacySupplier,
#     PharmacyLocation,
# )
# from app.schemas.pharmacy import (
#     PoIn,
#     PoOut,
#     PoDetailOut,
#     PoItemOut,
#     GrnIn,
#     GrnOut,
#     GrnDetailOut,
#     GrnItemOut,
# )
# from app.services.pdf_pharmacy import build_po_pdf, build_grn_pdf

# router = APIRouter()


# def has_perm(user: User, code: str) -> bool:
#     if getattr(user, "is_admin", False):
#         return True
#     for r in (getattr(user, "roles", None) or []):
#         for p in (getattr(r, "permissions", None) or []):
#             if getattr(p, "code", None) == code:
#                 return True
#     return False


# def has_table(db: Session, name: str) -> bool:
#     try:
#         insp = inspect(db.get_bind())
#         return insp.has_table(name)
#     except Exception:
#         return False


# # ---------- internal helpers ----------


# def _load_po_detail(db: Session, po_id: int) -> PoDetailOut:
#     po = db.query(PharmacyPO).get(po_id)
#     if not po:
#         raise HTTPException(404, "PO not found")

#     supplier = db.query(PharmacySupplier).get(
#         po.supplier_id) if po.supplier_id else None
#     location = db.query(PharmacyLocation).get(
#         po.location_id) if po.location_id else None

#     rows = (db.query(PharmacyPOItem, PharmacyMedicine).join(
#         PharmacyMedicine,
#         PharmacyMedicine.id == PharmacyPOItem.medicine_id).filter(
#             PharmacyPOItem.po_id == po_id).all())

#     items: List[PoItemOut] = []
#     for poi, med in rows:
#         items.append(
#             PoItemOut(
#                 id=poi.id,
#                 medicine_id=poi.medicine_id,
#                 medicine_code=med.code,
#                 medicine_name=med.name,
#                 qty=poi.qty,
#             ))

#     return PoDetailOut(
#         id=po.id,
#         supplier_id=po.supplier_id,
#         location_id=po.location_id,
#         status=po.status,
#         created_at=po.created_at,
#         approved_at=getattr(po, "approved_at", None),
#         supplier_name=getattr(supplier, "name", None) if supplier else None,
#         location_name=getattr(location, "name", None) if location else None,
#         items=items,
#     )


# def _load_grn_detail(db: Session, grn_id: int) -> GrnDetailOut:
#     grn = db.query(PharmacyGRN).get(grn_id)
#     if not grn:
#         raise HTTPException(404, "GRN not found")

#     supplier = db.query(PharmacySupplier).get(
#         grn.supplier_id) if grn.supplier_id else None
#     location = db.query(PharmacyLocation).get(
#         grn.location_id) if grn.location_id else None
#     po = db.query(PharmacyPO).get(grn.po_id) if grn.po_id else None

#     rows = (db.query(PharmacyGRNItem, PharmacyMedicine).join(
#         PharmacyMedicine,
#         PharmacyMedicine.id == PharmacyGRNItem.medicine_id).filter(
#             PharmacyGRNItem.grn_id == grn_id).all())

#     items: List[GrnItemOut] = []
#     for gi, med in rows:
#         items.append(
#             GrnItemOut(
#                 id=gi.id,
#                 medicine_id=gi.medicine_id,
#                 medicine_code=med.code,
#                 medicine_name=med.name,
#                 batch=gi.batch,
#                 expiry=gi.expiry,
#                 qty=gi.qty,
#                 unit_cost=gi.unit_cost,
#                 tax_percent=gi.tax_percent,
#                 mrp=gi.mrp,
#                 sell_price=gi.sell_price,
#             ))

#     return GrnDetailOut(
#         id=grn.id,
#         supplier_id=grn.supplier_id,
#         location_id=grn.location_id,
#         po_id=grn.po_id,
#         received_at=grn.received_at,
#         supplier_name=getattr(supplier, "name", None) if supplier else None,
#         location_name=getattr(location, "name", None) if location else None,
#         po_status=getattr(po, "status", None) if po else None,
#         items=items,
#     )


# # =========================
# # Purchase Orders (PO)
# # =========================


# @router.get("/po", response_model=List[PoOut])
# def list_po(
#         q: Optional[str] = Query(None, description="Search by PO id"),
#         status: Optional[str] = Query(
#             None, description="draft|approved|cancelled|closed"),
#         supplier_id: Optional[int] = None,
#         location_id: Optional[int] = None,
#         limit: int = Query(500, ge=1, le=2000),
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     qry = db.query(PharmacyPO).order_by(PharmacyPO.id.desc())
#     if status:
#         qry = qry.filter(PharmacyPO.status == status)
#     if supplier_id:
#         qry = qry.filter(PharmacyPO.supplier_id == supplier_id)
#     if location_id:
#         qry = qry.filter(PharmacyPO.location_id == location_id)
#     if q:
#         like = f"%{q.strip()}%"
#         qry = qry.filter(cast(PharmacyPO.id, String).ilike(like))

#     return qry.limit(limit).all()


# @router.get("/po/{po_id}", response_model=PoDetailOut)
# def get_po(
#         po_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     return _load_po_detail(db, po_id)


# @router.post("/po", response_model=PoOut)
# def create_po(
#         payload: PoIn,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.procure.manage"):
#         raise HTTPException(403, "Not permitted")

#     if not payload.items:
#         raise HTTPException(400, "PO needs at least one item")

#     po = PharmacyPO(
#         supplier_id=payload.supplier_id,
#         location_id=payload.location_id,
#         status="draft",
#         created_by=user.id,
#     )
#     db.add(po)
#     db.flush()

#     for it in payload.items:
#         if not db.query(PharmacyMedicine).get(it.medicine_id):
#             raise HTTPException(400, f"Medicine {it.medicine_id} not found")
#         db.add(
#             PharmacyPOItem(po_id=po.id, medicine_id=it.medicine_id,
#                            qty=it.qty))

#     db.commit()
#     db.refresh(po)
#     return po


# @router.post("/po/{po_id}/approve", response_model=PoOut)
# def approve_po(
#         po_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.procure.manage"):
#         raise HTTPException(403, "Not permitted")

#     po = db.query(PharmacyPO).get(po_id)
#     if not po:
#         raise HTTPException(404, "PO not found")
#     if po.status != "draft":
#         raise HTTPException(400, "PO not in draft")

#     po.status = "approved"
#     po.approved_at = datetime.utcnow()
#     po.approved_by = user.id
#     db.commit()
#     db.refresh(po)
#     return po


# @router.post("/po/{po_id}/cancel", response_model=PoOut)
# def cancel_po(
#         po_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.procure.manage"):
#         raise HTTPException(403, "Not permitted")

#     po = db.query(PharmacyPO).get(po_id)
#     if not po:
#         raise HTTPException(404, "PO not found")
#     if po.status == "cancelled":
#         return po
#     if po.status not in ("draft", "approved"):
#         raise HTTPException(400, f"Cannot cancel PO in status {po.status}")

#     po.status = "cancelled"
#     po.cancelled_at = datetime.utcnow()
#     po.cancelled_by = user.id
#     db.commit()
#     db.refresh(po)
#     return po


# @router.get("/po/{po_id}/print")
# def print_po(
#         po_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Download PO as PDF.
#     """
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     po = db.query(PharmacyPO).get(po_id)
#     if not po:
#         raise HTTPException(404, "PO not found")

#     supplier = db.query(PharmacySupplier).get(
#         po.supplier_id) if po.supplier_id else None
#     location = db.query(PharmacyLocation).get(
#         po.location_id) if po.location_id else None

#     rows = (db.query(PharmacyPOItem, PharmacyMedicine).join(
#         PharmacyMedicine,
#         PharmacyMedicine.id == PharmacyPOItem.medicine_id).filter(
#             PharmacyPOItem.po_id == po_id).all())

#     class _Row:

#         def __init__(self, code, name, qty):
#             self.medicine_code = code
#             self.medicine_name = name
#             self.qty = qty

#     items = [_Row(med.code, med.name, poi.qty) for poi, med in rows]

#     pdf_buf = build_po_pdf(po, supplier, location, items)
#     return StreamingResponse(
#         pdf_buf,
#         media_type="application/pdf",
#         headers={"Content-Disposition": f'inline; filename="PO_{po_id}.pdf"'},
#     )


# # =========================
# # Goods Receipt Notes (GRN)
# # =========================


# @router.get("/grn", response_model=List[GrnOut])
# def list_grn(
#         q: Optional[str] = Query(None,
#                                  description="Search by GRN id or PO id"),
#         supplier_id: Optional[int] = None,
#         location_id: Optional[int] = None,
#         po_id: Optional[int] = None,
#         limit: int = Query(500, ge=1, le=2000),
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     qry = db.query(PharmacyGRN).order_by(PharmacyGRN.id.desc())
#     if supplier_id:
#         qry = qry.filter(PharmacyGRN.supplier_id == supplier_id)
#     if location_id:
#         qry = qry.filter(PharmacyGRN.location_id == location_id)
#     if po_id:
#         qry = qry.filter(PharmacyGRN.po_id == po_id)
#     if q:
#         like = f"%{q.strip()}%"
#         qry = qry.filter(
#             or_(
#                 cast(PharmacyGRN.id, String).ilike(like),
#                 cast(PharmacyGRN.po_id, String).ilike(like),
#             ))
#     return qry.limit(limit).all()


# @router.get("/grn/{grn_id}", response_model=GrnDetailOut)
# def get_grn(
#         grn_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")
#     return _load_grn_detail(db, grn_id)


# @router.post("/grn", response_model=GrnOut)
# def create_grn(
#         payload: GrnIn,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     if not has_perm(user, "pharmacy.procure.manage"):
#         raise HTTPException(403, "Not permitted")

#     if not payload.items:
#         raise HTTPException(400, "GRN needs at least one item")

#     grn = PharmacyGRN(
#         supplier_id=payload.supplier_id,
#         location_id=payload.location_id,
#         po_id=payload.po_id,
#         created_by=user.id,
#     )
#     db.add(grn)
#     db.flush()

#     for it in payload.items:
#         db.add(
#             PharmacyGRNItem(
#                 grn_id=grn.id,
#                 medicine_id=it.medicine_id,
#                 batch=it.batch,
#                 expiry=it.expiry,
#                 qty=it.qty,
#                 unit_cost=it.unit_cost,
#                 tax_percent=it.tax_percent,
#                 mrp=it.mrp,
#                 sell_price=it.sell_price,
#             ))

#         lot = (db.query(PharmacyInventoryLot).filter(
#             PharmacyInventoryLot.medicine_id == it.medicine_id,
#             PharmacyInventoryLot.location_id == payload.location_id,
#             PharmacyInventoryLot.batch == it.batch,
#             PharmacyInventoryLot.expiry == it.expiry,
#         ).first())
#         if not lot:
#             lot = PharmacyInventoryLot(
#                 medicine_id=it.medicine_id,
#                 location_id=payload.location_id,
#                 batch=it.batch,
#                 expiry=it.expiry,
#                 on_hand=0,
#                 unit_cost=it.unit_cost,
#                 sell_price=it.sell_price,
#                 mrp=it.mrp,
#             )
#             db.add(lot)
#             db.flush()

#         lot.on_hand += it.qty
#         lot.unit_cost = it.unit_cost
#         if it.sell_price is not None:
#             lot.sell_price = it.sell_price
#         if it.mrp is not None:
#             lot.mrp = it.mrp

#         db.add(
#             PharmacyInventoryTxn(
#                 ts=datetime.utcnow(),
#                 medicine_id=it.medicine_id,
#                 location_id=payload.location_id,
#                 lot_id=lot.id,
#                 type="grn",
#                 qty_change=it.qty,
#                 ref_type="grn",
#                 ref_id=grn.id,
#                 user_id=user.id,
#             ))

#     db.commit()
#     db.refresh(grn)
#     return grn


# @router.get("/grn/{grn_id}/print")
# def print_grn(
#         grn_id: int,
#         db: Session = Depends(get_db),
#         user: User = Depends(auth_current_user),
# ):
#     """
#     Download GRN as PDF.
#     """
#     if not has_perm(user, "pharmacy.view"):
#         raise HTTPException(403, "Not permitted")

#     grn = db.query(PharmacyGRN).get(grn_id)
#     if not grn:
#         raise HTTPException(404, "GRN not found")

#     supplier = db.query(PharmacySupplier).get(
#         grn.supplier_id) if grn.supplier_id else None
#     location = db.query(PharmacyLocation).get(
#         grn.location_id) if grn.location_id else None
#     po = db.query(PharmacyPO).get(grn.po_id) if grn.po_id else None

#     rows = (db.query(PharmacyGRNItem, PharmacyMedicine).join(
#         PharmacyMedicine,
#         PharmacyMedicine.id == PharmacyGRNItem.medicine_id).filter(
#             PharmacyGRNItem.grn_id == grn_id).all())

#     class _Row:

#         def __init__(self, gi, med):
#             self.medicine_code = med.code
#             self.medicine_name = med.name
#             self.batch = gi.batch
#             self.expiry = gi.expiry
#             self.qty = gi.qty
#             self.unit_cost = gi.unit_cost

#     items = [_Row(gi, med) for gi, med in rows]

#     pdf_buf = build_grn_pdf(grn, supplier, location, po, items)
#     return StreamingResponse(
#         pdf_buf,
#         media_type="application/pdf",
#         headers={
#             "Content-Disposition": f'inline; filename="GRN_{grn_id}.pdf"'
#         },
#     )
