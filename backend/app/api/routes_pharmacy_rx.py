# app/api/routes_pharmacy_rx.py
from __future__ import annotations

from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, constr
from sqlalchemy import or_
from sqlalchemy.orm import Session
from decimal import Decimal
from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.pharmacy import (
    PharmacyPrescription,
    PharmacyPrescriptionItem,
    PharmacyMedicine,
    PharmacySaleItem,
    PharmacySale,
    PharmacyInventoryTxn,
    PharmacyInventoryLot,
)
from app.models.ipd import IpdAdmission
from app.models.opd import Visit
from app.schemas.pharmacy import RxIn, RxOut, RxItemIn, RxItemOut, SaleWithItemsOut  
from app.services.billing_auto import auto_add_item_for_event
router = APIRouter()

# ----------------- auth helpers -----------------


def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False


def _need(user: User, *codes: str) -> None:
    if getattr(user, "is_admin", False):
        return
    if any(has_perm(user, c) for c in codes):
        return
    raise HTTPException(403, "Not permitted")


# ----------------- status mapping (legacy → UI) -----------------

UI_STATUSES = {"new", "in_progress", "dispensed", "cancelled"}


def _map_item_status_for_ui(db_status: Optional[str], dispensed_qty: int,
                            ordered_qty: int) -> str:
    s = (db_status or "").strip().lower()

    # --- explicit legacy → UI mappings for items ---
    if s == "pending":
        return "new"
    if s == "fully_dispensed":
        return "dispensed"
    if s == "partially_dispensed":
        return "in_progress"
    if s == "cancelled":
        return "cancelled"

    # Already a UI status? pass through.
    if s in UI_STATUSES:
        return s

    # --- derive from quantities when status is blank/unknown ---
    dq = int(dispensed_qty or 0)
    oq = int(ordered_qty or 0)
    if dq <= 0:
        return "new"
    if oq and 0 < dq < oq:
        return "in_progress"
    if oq and dq >= oq:
        return "dispensed"
    return "new"


def _map_rx_status_for_ui(db_status: Optional[str],
                          item_ui_statuses: List[str]) -> str:
    s = (db_status or "").strip().lower()

    # --- explicit legacy → UI mappings for prescription header ---
    if s in ("draft", ):
        return "new"
    if s in ("signed", "sent"):
        return "in_progress"
    if s == "fully_dispensed":
        return "dispensed"
    if s == "partially_dispensed":
        return "in_progress"

    # Already a UI status? pass through.
    if s in UI_STATUSES:
        return s

    # --- derive from items if no clear header status ---
    if item_ui_statuses:
        if all(x == "dispensed" for x in item_ui_statuses):
            return "dispensed"
        if any(x == "in_progress" for x in item_ui_statuses):
            return "in_progress"
        if all(x == "cancelled" for x in item_ui_statuses):
            return "cancelled"
    return "new"


def _recompute_rx_status_simple(db: Session, rx_id: int) -> None:
    """Keep Rx.status coherent with item statuses in UI terms."""
    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        return
    items = db.query(PharmacyPrescriptionItem).filter(
        PharmacyPrescriptionItem.rx_id == rx_id).all()
    ui_stats = [
        _map_item_status_for_ui(
            it.status,
            int(it.dispensed_qty or 0),
            int(it.quantity or 0),
        ) for it in items if (it.status or "").lower() != "cancelled"
    ]
    rx.status = _map_rx_status_for_ui(rx.status, ui_stats)
    rx.updated_at = datetime.utcnow()


def _recompute_rx_status_simple(db: Session, rx_id: int) -> None:
    """Keep Rx.status coherent with item statuses in UI terms."""
    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        return
    items = db.query(PharmacyPrescriptionItem).filter(
        PharmacyPrescriptionItem.rx_id == rx_id).all()
    ui_stats = [
        _map_item_status_for_ui(
            it.status,
            int(it.dispensed_qty or 0),
            int(it.quantity or 0),
        ) for it in items if it.status != "cancelled"
    ]
    rx.status = _map_rx_status_for_ui(rx.status, ui_stats)
    rx.updated_at = datetime.utcnow()


# ----------------- internal helpers -----------------


def _resolve_context_auto(db: Session, patient_id: int):
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
    raise HTTPException(
        400, "No active IPD admission or OPD visit found for patient")


# app/api/routes_pharmacy_rx.py  (add/replace _attach_items)
# app/api/routes_pharmacy_rx.py
def _attach_items(db: Session, rx_id: int, items: List[RxItemIn]):
    for it in items:
        med = db.query(PharmacyMedicine).get(it.medicine_id)
        if not med or not med.is_active:
            raise HTTPException(400,
                                f"Medicine {it.medicine_id} not available")

        tod_count = int(it.am) + int(it.af) + int(it.pm) + int(it.night)
        qty = it.quantity
        if qty is None:
            if tod_count == 0:
                raise HTTPException(400, "Select at least one time-of-day")
            qty = tod_count * max(1, it.duration_days or 1)

        # keep frequency human friendly too
        freq = it.frequency
        if not freq:
            parts = []
            if it.am: parts.append("AM")
            if it.af: parts.append("AF")
            if it.pm: parts.append("PM")
            if it.night: parts.append("Night")
            freq = "+".join(parts) if parts else "custom"

        db.add(
            PharmacyPrescriptionItem(
                rx_id=rx_id,
                medicine_id=it.medicine_id,
                dose=it.dose or "",
                frequency=freq,
                route=it.route or "po",
                am=bool(it.am),
                af=bool(it.af),
                pm=bool(it.pm),
                night=bool(it.night),
                duration_days=it.duration_days or 1,
                quantity=qty,
                instructions=it.instructions or "",
                status="pending",  # DB term → UI will map to "new"
                dispensed_qty=0,
            ))


# app/api/routes_pharmacy_rx.py


def _load_rx_out_with_ui_status(db: Session, rx_id: int) -> RxOut:
    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        raise HTTPException(404, "Not found")

    items = (db.query(PharmacyPrescriptionItem).filter(
        PharmacyPrescriptionItem.rx_id == rx_id).order_by(
            PharmacyPrescriptionItem.id.asc()).all())

    # v2: use model_validate(from_attributes=True) instead of from_orm
    out = RxOut.model_validate(rx, from_attributes=True)
    out.items = [
        RxItemOut.model_validate(i, from_attributes=True) for i in items
    ]

    # Mutate statuses to UI-friendly values
    ui_items = []
    for it in out.items or []:
        ui_s = _map_item_status_for_ui(
            getattr(it, "status", None),
            int(getattr(it, "dispensed_qty", 0) or 0),
            int(getattr(it, "quantity", 0) or 0),
        )
        it.status = ui_s
        ui_items.append(ui_s)

    out.status = _map_rx_status_for_ui(getattr(out, "status", None), ui_items)
    return out


# ----------------- request bodies -----------------


class StatusUpdate(BaseModel):
    status: constr(strip_whitespace=True,
                   to_lower=True)  # new | in_progress | dispensed | cancelled


# ----------------- endpoints -----------------


@router.post("/prescriptions", response_model=RxOut)
def create_prescription(
        payload: RxIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # doctors or users with rx create permission
    _need(user, "ipd.doctor", "prescriptions.create")
    ctx = payload.context or {}
    if not ctx:
        ctype, ctx_ids = _resolve_context_auto(db, payload.patient_id)
    else:
        ctype = ctx.get("type")
        if ctype not in ("opd", "ipd"):
            raise HTTPException(400, "Invalid context.type")
        ctx_ids = {
            "visit_id": ctx.get("visit_id")
        } if ctype == "opd" else {
            "admission_id": ctx.get("admission_id")
        }
        if (ctype == "opd" and not ctx_ids.get("visit_id")) or (
                ctype == "ipd" and not ctx_ids.get("admission_id")):
            ctype, ctx_ids = _resolve_context_auto(db, payload.patient_id)

    rx = PharmacyPrescription(
        patient_id=payload.patient_id,
        context_type=ctype,
        visit_id=ctx_ids.get("visit_id"),
        admission_id=ctx_ids.get("admission_id"),
        prescriber_user_id=payload.prescriber_user_id or user.id,
        status="new",  # UI term
        notes=payload.notes or "",
    )
    db.add(rx)
    db.flush()

    _attach_items(db, rx.id, payload.items or [])
    db.commit()
    return _load_rx_out_with_ui_status(db, rx.id)


@router.get("/prescriptions", response_model=List[RxOut])
def list_prescriptions(
        q: Optional[str] = Query(
            None, description="search by medicine code/name or patient id"),
        status: Optional[str] = Query(
            None, description="new|in_progress|dispensed|cancelled"),
        patient_id: Optional[int] = None,
        context_type: Optional[str] = Query(None, description="opd|ipd"),
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need(user, "pharmacy.view", "ipd.doctor", "prescriptions.create")

    qry = db.query(PharmacyPrescription)

    if status:
        # Store whatever is in DB but compare against UI-equivalent by filtering later
        pass

    if patient_id:
        qry = qry.filter(PharmacyPrescription.patient_id == patient_id)

    if context_type in ("opd", "ipd"):
        qry = qry.filter(PharmacyPrescription.context_type == context_type)

    if q:
        like = f"%{q.strip()}%"
        # Try to parse patient id
        patient_num = None
        try:
            patient_num = int(q.strip())
        except Exception:
            patient_num = None

        qry = (qry.join(
            PharmacyPrescriptionItem,
            PharmacyPrescriptionItem.rx_id == PharmacyPrescription.id,
            isouter=True).join(
                PharmacyMedicine,
                PharmacyMedicine.id == PharmacyPrescriptionItem.medicine_id,
                isouter=True).filter(
                    or_(
                        PharmacyMedicine.name.ilike(like),
                        PharmacyMedicine.code.ilike(like),
                        PharmacyPrescription.patient_id == patient_num
                        if patient_num is not None else False,
                    )).distinct())

    rows = qry.order_by(PharmacyPrescription.id.desc()).limit(limit).all()

    out: List[RxOut] = []
    for r in rows:
        rx_out = _load_rx_out_with_ui_status(db, r.id)
        if status:
            # only keep matching UI status if filter provided
            if rx_out.status != status:
                continue
        out.append(rx_out)
    return out


@router.get("/prescriptions/{rx_id}", response_model=RxOut)
def get_prescription(
        rx_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need(user, "pharmacy.view", "ipd.doctor", "prescriptions.create")
    return _load_rx_out_with_ui_status(db, rx_id)


@router.post("/prescriptions/{rx_id}/items", response_model=RxOut)
def add_prescription_items(
        rx_id: int,
        items: List[RxItemIn],
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need(user, "ipd.doctor", "prescriptions.create")
    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        raise HTTPException(404, "Prescription not found")
    if rx.status not in ("new", "in_progress", "", None,
                         "draft"):  # allow legacy draft
        raise HTTPException(
            400, "Can add items only to a new/in_progress prescription")
    _attach_items(db, rx_id, items or [])
    rx.updated_at = datetime.utcnow()
    db.commit()
    return _load_rx_out_with_ui_status(db, rx_id)


@router.post("/prescriptions/items/{item_id}/status", response_model=RxOut)
def update_prescription_item_status(
        item_id: int,
        patch: StatusUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    # your UI gates the buttons behind pharmacy.dispense.manage
    _need(user, "pharmacy.dispense.manage")

    if patch.status not in UI_STATUSES:
        raise HTTPException(400, "Invalid status")

    it = db.query(PharmacyPrescriptionItem).get(item_id)
    if not it:
        raise HTTPException(404, "Item not found")

    it.status = patch.status
    rx_id = it.rx_id
    db.flush()
    _recompute_rx_status_simple(db, rx_id)
    db.commit()
    return _load_rx_out_with_ui_status(db, rx_id)


@router.post("/prescriptions/{rx_id}/status", response_model=RxOut)
def update_prescription_status(
        rx_id: int,
        patch: StatusUpdate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need(user, "pharmacy.dispense.manage")

    if patch.status not in UI_STATUSES:
        raise HTTPException(400, "Invalid status")

    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        raise HTTPException(404, "Prescription not found")

    rx.status = patch.status
    rx.updated_at = datetime.utcnow()

    # If cancelling, mark non-dispensed items as cancelled too (quality-of-life)
    if patch.status == "cancelled":
        db.query(PharmacyPrescriptionItem).filter(
            PharmacyPrescriptionItem.rx_id == rx_id,
            PharmacyPrescriptionItem.status.in_(
                ["new", "in_progress", "pending", "partially_dispensed"]),
        ).update({PharmacyPrescriptionItem.status: "cancelled"})

    db.commit()
    return _load_rx_out_with_ui_status(db, rx_id)


# local FEFO helper (avoid circular imports)
def _fefo_pick_for_rx(db: Session, medicine_id: int, location_id: int,
                      qty: int):
    lots = (db.query(PharmacyInventoryLot).filter(
        PharmacyInventoryLot.medicine_id == medicine_id,
        PharmacyInventoryLot.location_id == location_id,
        PharmacyInventoryLot.expiry
        >= date.today()).order_by(PharmacyInventoryLot.expiry.asc()).all())
    need = int(qty)
    picks = []
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


@router.post("/prescriptions/{rx_id}/dispense",
             response_model=SaleWithItemsOut)
def dispense_prescription(
        rx_id: int,
        location_id: int = Query(..., ge=1),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Convert an Rx into a sale: issues FEFO stock for each item’s remaining
    (quantity - dispensed_qty) and updates item/rx statuses.
    """
    _need(user, "pharmacy.view", "pharmacy.dispense.create")

    rx = db.query(PharmacyPrescription).get(rx_id)
    if not rx:
        raise HTTPException(404, "Prescription not found")

    # build sale header from Rx context
    sale = PharmacySale(
        patient_id=rx.patient_id,
        context_type=rx.context_type,
        visit_id=rx.visit_id if rx.context_type == "opd" else None,
        admission_id=rx.admission_id if rx.context_type == "ipd" else None,
        location_id=location_id,
        payment_mode="on-account",
        created_by=user.id,
    )
    db.add(sale)
    db.flush()

    total = Decimal("0.00")
    created_items: list[PharmacySaleItem] = []

    rx_items = (db.query(PharmacyPrescriptionItem).filter(
        PharmacyPrescriptionItem.rx_id == rx_id).order_by(
            PharmacyPrescriptionItem.id.asc()).all())

    for rit in rx_items:
        ordered = int(rit.quantity or 0)
        already = int(rit.dispensed_qty or 0)
        remaining = max(0, ordered - already)
        if remaining <= 0 or (rit.status or "").lower() == "cancelled":
            continue

        # FEFO picks
        picks = _fefo_pick_for_rx(db, rit.medicine_id, location_id, remaining)
        med = db.query(PharmacyMedicine).get(rit.medicine_id)

        # issue from lots
        left = remaining
        for lot, take in picks:
            unit_price = lot.sell_price or med.default_price or Decimal("0.00")
            line_amount = (unit_price or Decimal("0.00")) * Decimal(take)
            total += line_amount

            lot.on_hand -= take

            si = PharmacySaleItem(
                sale_id=sale.id,
                medicine_id=rit.medicine_id,
                lot_id=lot.id,
                qty=take,
                unit_price=unit_price,
                tax_percent=med.default_tax_percent,
                amount=line_amount,
                prescription_item_id=rit.id,  # <-- link back to Rx item
            )
            db.add(si)
            created_items.append(si)

            db.add(
                PharmacyInventoryTxn(
                    ts=datetime.utcnow(),
                    medicine_id=rit.medicine_id,
                    location_id=location_id,
                    lot_id=lot.id,
                    type="dispense",
                    qty_change=-take,
                    ref_type="sale",
                    ref_id=sale.id,
                    user_id=user.id,
                ))

            left -= take

        # update Rx item
        new_disp = already + remaining
        rit.dispensed_qty = new_disp
        rit.status = "fully_dispensed" if new_disp >= ordered else "partially_dispensed"

    # recompute Rx header status
    _recompute_rx_status_simple(db, rx_id)

    sale.total_amount = total

    # ✅ make sure total is flushed before we bill it
    db.flush()

    # ✅ Auto-billing for Rx→Sale conversion
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
