# app/api/routes_pharmacy_dispense.py
from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.pharmacy import (
    PharmacyInventoryLot,
    PharmacyInventoryTxn,
    PharmacySale,
    PharmacySaleItem,
    PharmacyMedicine,
    PharmacyPrescriptionItem,
    PharmacyPrescription,
)
from app.schemas.pharmacy import DispenseIn, SaleWithItemsOut
from app.models.opd import Visit  # ✅ correct model name/module
from app.models.ipd import IpdAdmission
from app.services.billing_auto import auto_add_item_for_event
router = APIRouter()

# ---------------- utilities ----------------


def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False


def _fefo_pick(db: Session, medicine_id: int, location_id: int,
               qty: int) -> List[Tuple[PharmacyInventoryLot, int]]:
    """
    Return list of (lot, pick_qty) using FEFO; raise if not enough usable stock.
    """
    lots = (db.query(PharmacyInventoryLot).filter(
        PharmacyInventoryLot.medicine_id == medicine_id).filter(
            PharmacyInventoryLot.location_id == location_id).filter(
                PharmacyInventoryLot.expiry >= date.today()).order_by(
                    PharmacyInventoryLot.expiry.asc()).all())
    need = qty
    picks: List[Tuple[PharmacyInventoryLot, int]] = []
    for lot in lots:
        if lot.on_hand <= 0:
            continue
        take = min(need, lot.on_hand)
        if take > 0:
            picks.append((lot, take))
            need -= take
            if need == 0:
                break
    if need > 0:
        raise HTTPException(400, "Insufficient stock (FEFO)")
    return picks


def _recompute_rx_status(db: Session, rx_id: int) -> None:
    """
    Recompute an Rx status based on item statuses/dispensed_qty.
    """
    items = (db.query(PharmacyPrescriptionItem).filter(
        PharmacyPrescriptionItem.rx_id == rx_id).all())
    if not items:
        return
    # Consider only non-cancelled lines for "fully" decision
    active_items = [i for i in items if (i.status or "pending") != "cancelled"]
    all_full = active_items and all(i.status == "fully_dispensed"
                                    for i in active_items)
    any_partial = any(i.status == "partially_dispensed" for i in active_items)

    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        return
    if all_full:
        rx.status = "fully_dispensed"
    elif any_partial:
        rx.status = "partially_dispensed"
    else:
        # if nothing dispensed, keep whatever status doctor set; default to 'new'
        rx.status = rx.status or "new"
    rx.updated_at = datetime.utcnow()


# ---------------- public endpoints ----------------
def _resolve_manual_context(db: Session, ctx: dict, patient_id: int):
    """
    Accepts variants:
      OPD: type='opd' and one of visit_id | id | opd_visit_id
      IPD: type='ipd' and one of admission_id | id | ipd_admission_id
    Validates that the referenced row exists.
    Returns (ctype, {"visit_id": ...} | {"admission_id": ...})
    """
    ctype = (ctx.get("type") or "").lower()
    if ctype not in ("opd", "ipd"):
        return None

    if ctype == "opd":
        vid = ctx.get("visit_id") or ctx.get("id") or ctx.get("opd_visit_id")
        if not vid:
            raise HTTPException(400, "OPD context requires visit_id")
        visit = db.query(Visit).get(int(vid))
        if not visit or visit.patient_id != patient_id:
            raise HTTPException(400, "OPD visit not found for patient")
        return "opd", {"visit_id": visit.id}

    if ctype == "ipd":
        aid = ctx.get("admission_id") or ctx.get("id") or ctx.get(
            "ipd_admission_id")
        if not aid:
            raise HTTPException(400, "IPD context requires admission_id")
        adm = db.query(IpdAdmission).get(int(aid))
        if not adm or adm.patient_id != patient_id or adm.status != "admitted":
            raise HTTPException(400,
                                "IPD admission not found/active for patient")
        return "ipd", {"admission_id": adm.id}

    return None


def _resolve_auto_context(db: Session, patient_id: int):
    adm = (db.query(IpdAdmission).filter(
        IpdAdmission.patient_id == patient_id,
        IpdAdmission.status == "admitted").order_by(
            IpdAdmission.id.desc()).first())
    if adm:
        return "ipd", {"admission_id": adm.id}

    visit = (db.query(Visit).filter(Visit.patient_id == patient_id).order_by(
        Visit.id.desc()).first())
    if visit:
        return "opd", {"visit_id": visit.id}

    raise HTTPException(400,
                        "Invalid context; no active IPD or OPD visit found")


@router.get("/active-context")
def get_active_context(
        patient_id: int = Query(..., ge=1),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Auto-detect the patient's current context:
      - IPD: latest admission with status 'admitted'
      - else last OPD visit (any)
    Returns {} when nothing found.  Shape matches Dispense.jsx expectations.
    """
    if not has_perm(user, "pharmacy.view"):
        raise HTTPException(403, "Not permitted")

    # Import here to avoid circulars at import time
    from app.models.ipd import IpdAdmission
    from app.models.opd import Visit

    adm = (db.query(IpdAdmission).filter(
        IpdAdmission.patient_id == patient_id,
        IpdAdmission.status == "admitted").order_by(
            IpdAdmission.id.desc()).first())
    if adm:
        return {"type": "ipd", "admission_id": adm.id}

    visit = (db.query(Visit).filter(Visit.patient_id == patient_id).order_by(
        Visit.id.desc()).first())
    if visit:
        return {"type": "opd", "visit_id": visit.id}

    try:
        ctype, ctx_ids = _resolve_auto_context(db, patient_id)
        return {"type": ctype, **ctx_ids}
    except HTTPException:
        return {}  # none found


@router.post("/dispense", response_model=SaleWithItemsOut)
def dispense(
        payload: DispenseIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create a sale, issue stock (FEFO by default), and (optionally) update linked prescription items.
    """
    if not (has_perm(user, "pharmacy.view")
            and has_perm(user, "pharmacy.dispense.create")):
        raise HTTPException(403, "Not permitted")

    # Resolve context (accept FE-provided, else auto-detect)
    ctx = payload.context or {}
    ctype = (ctx or {}).get("type")
    if ctype not in ("opd", "ipd"):
        from app.models.ipd import IpdAdmission
        from app.models.opd import Visit

        adm = (db.query(IpdAdmission).filter(
            IpdAdmission.patient_id == payload.patient_id,
            IpdAdmission.status == "admitted").order_by(
                IpdAdmission.id.desc()).first())
        if adm:
            ctype = "ipd"
            ctx = {"admission_id": adm.id}
        else:
            visit = (db.query(Visit).filter(
                Visit.patient_id == payload.patient_id).order_by(
                    Visit.id.desc()).first())
            if not visit:
                raise HTTPException(
                    400, "Invalid context; no active IPD or OPD visit found")
            ctype = "opd"
            ctx = {"visit_id": visit.id}

    sale = PharmacySale(
        patient_id=payload.patient_id,
        context_type=ctype,
        visit_id=ctx.get("visit_id") if ctype == "opd" else None,
        admission_id=ctx.get("admission_id") if ctype == "ipd" else None,
        location_id=payload.location_id,
        payment_mode=(payload.payment or {}).get("mode", "on-account"),
        created_by=user.id,
    )
    db.add(sale)
    db.flush()  # get sale.id

    total = Decimal("0.00")
    created_items: List[PharmacySaleItem] = []

    for it in payload.items:
        # Validate medicine
        med = db.query(PharmacyMedicine).get(it.medicine_id)
        if not med:
            raise HTTPException(400, f"Medicine {it.medicine_id} not found")

        # FEFO or validate chosen lot
        if it.lot_id:
            lot = db.query(PharmacyInventoryLot).get(it.lot_id)
            if not lot or lot.location_id != payload.location_id:
                raise HTTPException(400, "Lot not available at this location")
            if lot.expiry < date.today():
                raise HTTPException(400, "Lot expired")
            if lot.on_hand < it.qty:
                raise HTTPException(400, "Insufficient lot qty")
            picks = [(lot, it.qty)]
        else:
            picks = _fefo_pick(db, it.medicine_id, payload.location_id, it.qty)

        # Issue stock from lots
        remaining = it.qty
        for lot, take in picks:
            unit_price = lot.sell_price or med.default_price or Decimal("0.00")
            line_amount = (unit_price or Decimal("0.00")) * Decimal(take)
            total += line_amount

            lot.on_hand -= take

            si = PharmacySaleItem(
                sale_id=sale.id,
                medicine_id=it.medicine_id,
                lot_id=lot.id,
                qty=take,
                unit_price=unit_price,
                tax_percent=med.default_tax_percent,
                amount=line_amount,
                prescription_item_id=it.prescription_item_id,
            )
            db.add(si)
            created_items.append(si)

            db.add(
                PharmacyInventoryTxn(
                    ts=datetime.utcnow(),
                    medicine_id=it.medicine_id,
                    location_id=payload.location_id,
                    lot_id=lot.id,
                    type="dispense",
                    qty_change=-take,
                    ref_type="sale",
                    ref_id=sale.id,
                    user_id=user.id,
                ))
            remaining -= take

        if remaining != 0:
            # Should never happen unless picks logic breaks
            raise HTTPException(500, "Internal FEFO error")

        # If this line is tied to a prescription item, update that line
        if it.prescription_item_id:
            rx_item = db.query(PharmacyPrescriptionItem).get(
                it.prescription_item_id)
            if not rx_item:
                raise HTTPException(400, "Prescription item not found")
            if rx_item.medicine_id != it.medicine_id:
                raise HTTPException(400, "Prescription item medicine mismatch")

            ordered = int(rx_item.quantity or 0)
            already = int(rx_item.dispensed_qty or 0)
            new_dispensed = already + it.qty
            if new_dispensed > ordered:
                raise HTTPException(400,
                                    "Dispense exceeds prescribed quantity")

            rx_item.dispensed_qty = new_dispensed
            rx_item.status = "fully_dispensed" if new_dispensed == ordered else "partially_dispensed"
            _recompute_rx_status(db, rx_item.rx_id)

    sale.total_amount = total
    db.flush()
    
    auto_add_item_for_event(
        db,
        service_type="pharmacy",
        ref_id=sale.id,
        patient_id=sale.patient_id,
        context_type=sale.context_type,
        context_id=(sale.admission_id or sale.visit_id),
        user_id=user.id,
    )

    db.commit()
    db.refresh(sale)

    # Response with created items
    return {
        "id": sale.id,
        "patient_id": sale.patient_id,
        "context_type": sale.context_type,
        "visit_id": sale.visit_id,
        "admission_id": sale.admission_id,
        "location_id": sale.location_id,
        "total_amount": sale.total_amount,
        "created_at": sale.created_at,
        "items": created_items,
    }


@router.post("/dispense/return")
def sale_return(
        sale_id: int,
        sale_item_id: int,
        qty: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Return dispensed quantity to stock from a sale item.
    Also (optionally) roll back prescription item dispensed qty/status.
    """
    if not has_perm(user, "pharmacy.dispense.create"):
        raise HTTPException(403, "Not permitted")

    si = db.query(PharmacySaleItem).get(sale_item_id)
    if not si or si.sale_id != sale_id:
        raise HTTPException(404, "Sale item not found")
    if qty <= 0 or qty > si.qty:
        raise HTTPException(400, "Invalid qty to return")

    lot = db.query(PharmacyInventoryLot).get(si.lot_id)
    if not lot:
        raise HTTPException(404, "Lot not found")

    # Return stock
    lot.on_hand += qty
    db.add(
        PharmacyInventoryTxn(
            ts=datetime.utcnow(),
            medicine_id=si.medicine_id,
            location_id=lot.location_id,
            lot_id=lot.id,
            type="sale_return",
            qty_change=qty,
            ref_type="sale_return",
            ref_id=sale_id,
            user_id=user.id,
        ))

    # Roll back Rx dispensed qty if linked
    if si.prescription_item_id:
        rx_item = db.query(PharmacyPrescriptionItem).get(
            si.prescription_item_id)
        if rx_item:
            rx_item.dispensed_qty = max(0,
                                        int(rx_item.dispensed_qty or 0) - qty)
            if (rx_item.dispensed_qty or 0) == 0:
                rx_item.status = "pending"
            else:
                rx_item.status = ("partially_dispensed" if
                                  (rx_item.dispensed_qty
                                   or 0) < (rx_item.quantity
                                            or 0) else "fully_dispensed")
            _recompute_rx_status(db, rx_item.rx_id)

    db.commit()
    return {"message": "Return accepted", "lot_on_hand": lot.on_hand}
