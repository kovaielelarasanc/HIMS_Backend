# FILE: app/services/pharmacy.py
from __future__ import annotations

import enum
from datetime import datetime, date as dt_date
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Any, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func, case, or_

from app.services.drug_schedules import get_schedule_meta
from app.services.inventory import create_stock_transaction

from app.models.user import User

from app.models.pharmacy_prescription import (
    PharmacyPrescription,
    PharmacyPrescriptionLine,
    PharmacySale,
    PharmacySaleItem,
    PharmacyPayment,
)

from app.models.pharmacy_inventory import (
    InventoryItem,
    ItemBatch,
    StockTransaction,
)

from app.schemas.pharmacy_prescription import (
    PrescriptionCreate,
    PrescriptionUpdate,
    RxLineCreate,
    RxLineUpdate,
    DispenseFromRxIn,
    CounterSaleCreateIn,
    PaymentCreate,
)

# ============================================================
# ✅ NEW BILLING / INVOICE MODELS (auto-detect + works with your new schema)
# - Update the import paths below ONLY if your project uses different file names.
# ============================================================

# We alias everything to BillingInvoice / BillingInvoiceLine to keep code stable.
ServiceGroup = None  # will be imported
BillingInvoice = None
BillingInvoiceLine = None

try:
    # Common new naming
    from app.models.billing import BillingInvoice as BillingInvoice  # type: ignore
    from app.models.billing import BillingInvoiceLine as BillingInvoiceLine  # type: ignore
    from app.models.billing import ServiceGroup as ServiceGroup  # type: ignore
except Exception:
    try:
        from app.models.billing import BillingInvoice as BillingInvoice  # type: ignore
        from app.models.billing import BillingInvoiceLine as BillingInvoiceLine  # type: ignore
        from app.models.billing import ServiceGroup as ServiceGroup  # type: ignore
    except Exception:
        # Fallback to old models (keeps runtime safe if older deployments still exist)
        from app.models.billing import BillingInvoice as BillingInvoice  # type: ignore
        from app.models.billing import BillingInvoiceLine as BillingInvoiceLine  # type: ignore
        try:
            from app.models.billing import ServiceGroup as ServiceGroup  # type: ignore
        except Exception:
            try:
                from app.models.billing import ServiceGroup as ServiceGroup  # type: ignore
            except Exception:
                # last resort: local enum to avoid crash (you SHOULD have ServiceGroup in your project)
                class ServiceGroup(str, enum.Enum):  # type: ignore
                    CONSULT = "CONSULT"
                    LAB = "LAB"
                    RAD = "RAD"
                    PHARM = "PHARM"
                    OT = "OT"
                    PROC = "PROC"
                    ROOM = "ROOM"
                    NURSING = "NURSING"
                    MISC = "MISC"


# ============================================================
# Money helpers
# ============================================================

MONEY = Decimal("0.01")


def _d(v) -> Decimal:
    try:
        return Decimal(str(v if v is not None else 0))
    except Exception:
        return Decimal("0")


def _round_money(value: Decimal) -> Decimal:
    return _d(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _compute_tax(line_amount: Decimal, tax_percent: Decimal) -> Decimal:
    tax_percent = _d(tax_percent)
    if tax_percent <= 0:
        return Decimal("0.00")
    return _round_money(_d(line_amount) * tax_percent / Decimal("100"))


# ============================================================
# Number generators
# ============================================================


def _generate_prescription_number(db: Session, rx_type: str) -> str:
    """
    RX-<TYPE>-YYYYMMDD-<seq>
    """
    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"RX-{rx_type}-{today_str}"

    last_number = (db.query(PharmacyPrescription.prescription_number).filter(
        PharmacyPrescription.prescription_number.like(f"{prefix}-%")).order_by(
            PharmacyPrescription.prescription_number.desc()).first())

    next_seq = 1
    if last_number and last_number[0]:
        try:
            next_seq = int(str(last_number[0]).split("-")[-1]) + 1
        except Exception:
            next_seq = 1

    return f"{prefix}-{next_seq:04d}"


def _generate_sale_number(db: Session, context_type: str) -> str:
    """
    PH-<CTX>-YYYYMMDD-<seq>
    """
    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"PH-{context_type}-{today_str}"

    last_number = (db.query(PharmacySale.bill_number).filter(
        PharmacySale.bill_number.like(f"{prefix}-%")).order_by(
            PharmacySale.bill_number.desc()).first())

    next_seq = 1
    if last_number and last_number[0]:
        try:
            next_seq = int(str(last_number[0]).split("-")[-1]) + 1
        except Exception:
            next_seq = 1

    return f"{prefix}-{next_seq:04d}"


# ============================================================
# InventoryItem snapshot helpers (IMPORTANT FIX)
# Your InventoryItem has dosage_form (NOT form)
# ============================================================


def _item_form(item: InventoryItem) -> str:
    return (getattr(item, "dosage_form", "") or "").strip()


def _item_strength(item: InventoryItem) -> str:
    return (getattr(item, "strength", "") or "").strip()


def _item_type(item: InventoryItem) -> str:
    # InventoryItem.item_type = DRUG | CONSUMABLE | EQUIPMENT
    t = (getattr(item, "item_type", "") or "").strip().upper()
    if t == "CONSUMABLE" or bool(getattr(item, "is_consumable", False)):
        return "consumable"
    return "drug"


# ============================================================
# Stock helpers
# ============================================================


def _snapshot_available_stock(db: Session, location_id: int | None,
                              item_id: int) -> Decimal | None:
    """
    Sum of ItemBatch.current_qty at a location.
    Returns None if location_id is not provided (inventory not linked).
    """
    if not location_id:
        return None
    total = (db.query(func.coalesce(func.sum(ItemBatch.current_qty),
                                    0)).filter(
                                        ItemBatch.item_id == item_id,
                                        ItemBatch.location_id == location_id,
                                        ItemBatch.is_active.is_(True),
                                        ItemBatch.is_saleable.is_(True),
                                    ).scalar())
    return _d(total)


def _validate_batch_for_line(batch: ItemBatch, *, item_id: int,
                             location_id: int) -> None:
    if not batch:
        raise HTTPException(status_code=400,
                            detail="Selected batch not found.")
    if int(batch.item_id) != int(item_id):
        raise HTTPException(
            status_code=400,
            detail="Selected batch does not belong to this item.")
    if int(batch.location_id) != int(location_id):
        raise HTTPException(
            status_code=400,
            detail="Selected batch does not belong to this location.")
    if not batch.is_active or not batch.is_saleable or str(
            batch.status) != "ACTIVE":
        raise HTTPException(status_code=400,
                            detail="Selected batch is not saleable.")
    if _d(batch.current_qty) <= 0:
        raise HTTPException(status_code=400,
                            detail="Selected batch has no stock.")

    today = dt_date.today()
    if batch.expiry_date is not None and batch.expiry_date < today:
        raise HTTPException(status_code=400,
                            detail="Selected batch is expired.")


class AllocatedBatch:

    def __init__(
        self,
        batch: ItemBatch | None,
        qty: Decimal,
        mrp: Decimal,
        tax_percent: Decimal,
        stock_txn: StockTransaction | None,
    ) -> None:
        self.batch = batch
        self.qty = _d(qty)
        self.mrp = _d(mrp)
        self.tax_percent = _d(tax_percent)
        self.stock_txn = stock_txn


def _allocate_from_selected_batch(
    db: Session,
    *,
    location_id: int,
    item: InventoryItem,
    batch_id: int,
    qty: Decimal,
    patient_id: int | None,
    visit_id: int | None,
    ipd_admission_id: int | None,
    ref_type: str,
    ref_id: int,
    user: User,
) -> List[AllocatedBatch]:
    """
    ✅ STRICT: allocate ONLY from the selected batch_id
    ✅ STRICT: billing price uses selected batch.mrp only
    """
    qty = _d(qty)
    if qty <= 0:
        return []

    batch: ItemBatch | None = (db.query(ItemBatch).filter(
        ItemBatch.id == int(batch_id)).with_for_update().first())
    if not batch:
        raise HTTPException(status_code=400,
                            detail="Selected batch not found.")

    _validate_batch_for_line(batch,
                             item_id=int(item.id),
                             location_id=int(location_id))

    available = _d(batch.current_qty)
    if qty > available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Insufficient stock in selected batch {batch.batch_no}. "
                    f"Available {available}, requested {qty}."),
        )

    mrp = _d(batch.mrp)
    if mrp <= 0:
        raise HTTPException(
            status_code=400,
            detail=
            f"MRP is missing/zero for batch {batch.batch_no}. Please fix batch MRP.",
        )

    tax_percent = _d(batch.tax_percent)
    unit_cost = _d(batch.unit_cost)

    batch.current_qty = available - qty

    stock_txn = create_stock_transaction(
        db=db,
        user=user,
        location_id=location_id,
        item_id=item.id,
        batch_id=batch.id,
        qty_delta=-qty,
        txn_type="DISPENSE",
        ref_type=ref_type,
        ref_id=ref_id,
        unit_cost=unit_cost,
        mrp=mrp,
        remark=f"Dispense from {ref_type} {ref_id}",
        patient_id=patient_id,
        visit_id=visit_id,
    )
    db.flush()
    db.add(stock_txn)

    return [
        AllocatedBatch(batch=batch,
                       qty=qty,
                       mrp=mrp,
                       tax_percent=tax_percent,
                       stock_txn=stock_txn)
    ]


def _allocate_stock_fefo(
    db: Session,
    *,
    location_id: int,
    item: InventoryItem,
    qty: Decimal,
    patient_id: int | None,
    visit_id: int | None,
    ipd_admission_id: int | None,
    ref_type: str,
    ref_id: int,
    user: User,
) -> List[AllocatedBatch]:
    """
    FEFO allocation across ItemBatch rows, creating StockTransaction entries.
    ✅ Billing uses each batch.mrp (batch-wise MRP accuracy).
    """
    qty = _d(qty)
    if qty <= 0:
        return []

    today = dt_date.today()

    q = (db.query(ItemBatch).filter(
        ItemBatch.item_id == item.id,
        ItemBatch.location_id == location_id,
        ItemBatch.current_qty > 0,
        ItemBatch.is_active.is_(True),
        ItemBatch.is_saleable.is_(True),
        or_(
            ItemBatch.expiry_date.is_(None),
            ItemBatch.expiry_date >= today,
        ),
    ))

    nulls_last_expr = case(
        (ItemBatch.expiry_date.is_(None), 1),
        else_=0,
    )

    batches: List[ItemBatch] = (q.order_by(
        nulls_last_expr.asc(),
        ItemBatch.expiry_date.asc(),
        ItemBatch.id.asc(),
    ).with_for_update().all())

    remaining = qty
    allocations: List[AllocatedBatch] = []

    for batch in batches:
        if remaining <= 0:
            break

        available = _d(batch.current_qty)
        if available <= 0:
            continue

        use_qty = min(available, remaining)
        if use_qty <= 0:
            continue

        mrp = _d(batch.mrp)
        if mrp <= 0:
            raise HTTPException(
                status_code=400,
                detail=
                f"MRP is missing/zero for batch {batch.batch_no}. Please fix batch MRP.",
            )

        tax_percent = _d(batch.tax_percent)
        unit_cost = _d(batch.unit_cost)

        batch.current_qty = available - use_qty

        stock_txn = create_stock_transaction(
            db=db,
            user=user,
            location_id=location_id,
            item_id=item.id,
            batch_id=batch.id,
            qty_delta=-use_qty,
            txn_type="DISPENSE",
            ref_type=ref_type,
            ref_id=ref_id,
            unit_cost=unit_cost,
            mrp=mrp,
            remark=f"Dispense from {ref_type} {ref_id}",
            patient_id=patient_id,
            visit_id=visit_id,
        )
        db.flush()
        db.add(stock_txn)

        allocations.append(
            AllocatedBatch(
                batch=batch,
                qty=use_qty,
                mrp=mrp,
                tax_percent=tax_percent,
                stock_txn=stock_txn,
            ))
        remaining -= use_qty

    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Insufficient stock for item {item.id}. "
                    f"Required {qty}, allocated {qty - remaining}."),
        )

    return allocations


# ============================================================
# Batch assignment on SEND/ISSUE (locks batch_id for each line)
# ============================================================


def _pick_fefo_batch(db: Session, item_id: int,
                     location_id: int) -> ItemBatch | None:
    today = dt_date.today()
    return (db.query(ItemBatch).filter(
        ItemBatch.item_id == item_id,
        ItemBatch.location_id == location_id,
        ItemBatch.is_active.is_(True),
        ItemBatch.is_saleable.is_(True),
        ItemBatch.status == "ACTIVE",
        ItemBatch.current_qty > 0,
        or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
    ).order_by(
        case((ItemBatch.expiry_date.is_(None), 1), else_=0),
        ItemBatch.expiry_date.asc(),
        ItemBatch.id.asc(),
    ).first())


def _total_available_qty(db: Session, item_id: int,
                         location_id: int) -> Decimal:
    today = dt_date.today()
    rows = (db.query(ItemBatch.current_qty).filter(
        ItemBatch.item_id == item_id,
        ItemBatch.location_id == location_id,
        ItemBatch.is_active.is_(True),
        ItemBatch.is_saleable.is_(True),
        ItemBatch.status == "ACTIVE",
        ItemBatch.current_qty > 0,
        or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
    ).all())
    total = Decimal("0")
    for (q, ) in rows:
        total += _d(q)
    return total


def assign_batches_on_send(db: Session, rx: PharmacyPrescription) -> None:
    if not getattr(rx, "location_id", None):
        return

    for ln in (rx.lines or []):
        if not getattr(ln, "item_id", None):
            continue
        if getattr(ln, "batch_id", None):
            continue

        b = _pick_fefo_batch(db, int(ln.item_id), int(rx.location_id))
        if not b:
            continue

        if hasattr(ln, "batch_id"):
            ln.batch_id = b.id

        if hasattr(ln, "batch_no_snapshot"):
            ln.batch_no_snapshot = b.batch_no

        if hasattr(ln, "expiry_date_snapshot"):
            ln.expiry_date_snapshot = b.expiry_date

        try:
            ln.available_qty_snapshot = _total_available_qty(
                db, int(ln.item_id), int(rx.location_id))
            need = _d(getattr(ln, "requested_qty", 0))
            ln.is_out_of_stock = bool(_d(ln.available_qty_snapshot) < need)
        except Exception:
            pass


# ============================================================
# Rx CRUD
# ============================================================


def create_prescription(db: Session, data: PrescriptionCreate,
                        current_user: User) -> PharmacyPrescription:
    if data.type in ("OPD", "IPD") and not data.patient_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="patient_id is required for OPD/IPD prescriptions.",
        )

    if data.type == "OPD" and not data.visit_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="visit_id is required for OPD prescriptions.",
        )

    if data.type == "IPD" and not data.ipd_admission_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ipd_admission_id is required for IPD prescriptions.",
        )

    rx = PharmacyPrescription(
        prescription_number=_generate_prescription_number(db, data.type),
        type=data.type,
        patient_id=data.patient_id,
        visit_id=data.visit_id,
        ipd_admission_id=data.ipd_admission_id,
        location_id=data.location_id,
        doctor_user_id=data.doctor_user_id,
        notes=data.notes,
        status="DRAFT",
        created_by_id=current_user.id,
    )
    db.add(rx)
    db.flush()

    for line_data in (data.lines or []):
        _add_rx_line_internal(db, rx, line_data)

    db.commit()
    db.refresh(rx)
    return rx


def _add_rx_line_internal(db: Session, rx: PharmacyPrescription,
                          line_data: RxLineCreate) -> PharmacyPrescriptionLine:
    item: InventoryItem | None = db.get(InventoryItem, int(line_data.item_id))
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inventory item {line_data.item_id} not found.",
        )

    snap = _snapshot_available_stock(db, rx.location_id, item.id)
    is_ooo = snap is not None and snap <= 0

    line = PharmacyPrescriptionLine(
        prescription_id=rx.id,
        item_id=item.id,
        requested_qty=_d(line_data.requested_qty),
        dispensed_qty=Decimal("0"),
        status="WAITING",
        dose_text=line_data.dose_text,
        frequency_code=line_data.frequency_code,
        times_per_day=line_data.times_per_day,
        duration_days=line_data.duration_days,
        route=line_data.route,
        timing=line_data.timing,
        instructions=line_data.instructions,
        start_date=line_data.start_date,
        end_date=line_data.end_date,
        schedule_pattern=line_data.schedule_pattern,
        is_prn=line_data.is_prn,
        is_stat=line_data.is_stat,
        available_qty_snapshot=snap,
        is_out_of_stock=is_ooo,
        item_name=item.name,
        item_form=_item_form(item),
        item_strength=_item_strength(item),
        item_type=_item_type(item),
    )
    db.add(line)
    return line


def update_prescription(db: Session, rx_id: int, data: PrescriptionUpdate,
                        current_user: User) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).filter(
            PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only DRAFT prescriptions can be updated.",
        )

    if data.location_id is not None:
        rx.location_id = data.location_id
    if data.doctor_user_id is not None:
        rx.doctor_user_id = data.doctor_user_id
    if data.notes is not None:
        rx.notes = data.notes

    db.commit()
    db.refresh(rx)
    return rx


def add_rx_line(db: Session, rx_id: int, line_data: RxLineCreate,
                current_user: User) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).filter(
            PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lines can only be added while prescription is DRAFT.",
        )

    _add_rx_line_internal(db, rx, line_data)
    db.commit()
    db.refresh(rx)
    return rx


def update_rx_line(db: Session, line_id: int, data: RxLineUpdate,
                   current_user: User) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, int(line_id))
    if not line:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Rx line not found.")

    rx = db.get(PharmacyPrescription, int(line.prescription_id))
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rx lines can only be edited while prescription is DRAFT.",
        )

    if data.requested_qty is not None:
        new_req = _d(data.requested_qty)
        if _d(line.dispensed_qty) and new_req < _d(line.dispensed_qty):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="requested_qty cannot be less than dispensed_qty.",
            )
        line.requested_qty = new_req

    for field in [
            "dose_text",
            "frequency_code",
            "times_per_day",
            "duration_days",
            "route",
            "timing",
            "instructions",
            "start_date",
            "end_date",
            "schedule_pattern",
            "is_prn",
            "is_stat",
    ]:
        value = getattr(data, field, None)
        if value is not None:
            setattr(line, field, value)

    if data.status is not None:
        if data.status not in ("WAITING", "CANCELLED"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Line status can only be WAITING or CANCELLED.",
            )
        if _d(line.dispensed_qty) and data.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel a line that has already been dispensed.",
            )
        line.status = data.status

    db.commit()
    db.refresh(rx)
    return rx


def delete_rx_line(db: Session, line_id: int,
                   current_user: User) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, int(line_id))
    if not line:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Rx line not found.")

    rx = db.get(PharmacyPrescription, int(line.prescription_id))
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rx lines can only be deleted while prescription is DRAFT.",
        )

    db.delete(line)
    db.commit()
    db.refresh(rx)
    return rx


def sign_prescription(db: Session, rx_id: int,
                      current_user: User) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).filter(
            PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only DRAFT prescriptions can be signed.",
        )

    if not rx.lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot sign an empty prescription.",
        )

    assign_batches_on_send(db, rx)

    rx.status = "ISSUED"
    rx.signed_at = datetime.utcnow()
    rx.signed_by_id = current_user.id

    db.commit()
    db.refresh(rx)
    return rx


def cancel_prescription(db: Session, rx_id: int, reason: str,
                        current_user: User) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).filter(
            PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    if any(_d(l.dispensed_qty) > 0 for l in (rx.lines or [])):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel prescription with dispensed lines.",
        )

    rx.status = "CANCELLED"
    rx.cancel_reason = reason
    rx.cancelled_at = datetime.utcnow()
    rx.cancelled_by_id = current_user.id

    db.commit()
    db.refresh(rx)
    return rx


# ============================================================
# Sale helpers
# ============================================================


def _recalc_sale_totals(sale: PharmacySale) -> None:
    gross = Decimal("0")
    tax_total = Decimal("0")
    discount_total = Decimal("0")

    for it in (sale.items or []):
        discount_total += _d(getattr(it, "discount_amount", 0))
        gross += _d(getattr(it, "line_amount", 0))
        tax_total += _d(getattr(it, "tax_amount", 0))

    unrounded_net = gross + tax_total - discount_total
    rounded_net = _round_money(unrounded_net)
    rounding = _round_money(rounded_net - unrounded_net)

    sale.gross_amount = _round_money(gross)
    sale.total_tax = _round_money(tax_total)
    sale.discount_amount_total = _round_money(discount_total)
    sale.rounding_adjustment = rounding
    sale.net_amount = rounded_net


def _update_payment_status(db: Session, sale: PharmacySale) -> None:
    total_paid = (db.query(
        func.coalesce(func.sum(PharmacyPayment.amount),
                      0)).filter(PharmacyPayment.sale_id == sale.id).scalar())
    total_paid = _d(total_paid)

    if total_paid <= 0:
        sale.payment_status = "UNPAID"
    elif total_paid < _d(sale.net_amount):
        sale.payment_status = "PARTIALLY_PAID"
    else:
        sale.payment_status = "PAID"


# ============================================================
# ✅ NEW BILLING INVOICE CREATION (ServiceGroup.PHARM + new model-safe fields)
# ============================================================


def _set_if_has(obj: Any, field: str, value: Any) -> None:
    if hasattr(obj, field):
        setattr(obj, field, value)


def _get_attr(obj: Any, field: str, default=None):
    return getattr(obj, field, default)


def _generate_invoice_number(db: Session) -> str | None:
    # supports invoice_number OR number OR code
    num_field = None
    for f in ("invoice_number", "number", "code"):
        if hasattr(BillingInvoice, f):
            num_field = getattr(BillingInvoice, f)
            break
    if not num_field:
        return None

    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"INV-{today_str}"

    last = (db.query(num_field).filter(num_field.like(f"{prefix}-%")).order_by(
        num_field.desc()).first())
    seq = 1
    if last and last[0]:
        try:
            seq = int(str(last[0]).split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"{prefix}-{seq:04d}"


def _find_existing_invoice_for_sale(db: Session,
                                    sale: PharmacySale) -> Any | None:
    # 1) direct FK link on sale
    if hasattr(sale, "billing_invoice_id") and getattr(
            sale, "billing_invoice_id", None):
        inv = db.get(BillingInvoice, int(sale.billing_invoice_id))
        if inv:
            return inv

    # 2) context lookup (new billing standard)
    if hasattr(BillingInvoice, "context_type") and hasattr(
            BillingInvoice, "context_id"):
        inv = (db.query(BillingInvoice).filter(
            getattr(BillingInvoice, "context_type") == "pharmacy_sale",
            getattr(BillingInvoice, "context_id") == sale.id,
        ).first())
        if inv:
            if hasattr(sale, "billing_invoice_id"):
                sale.billing_invoice_id = inv.id
            return inv

    # 3) alt context fields
    for ct_field, cid_field in (("ref_type", "ref_id"), ("source_type",
                                                         "source_id")):
        if hasattr(BillingInvoice, ct_field) and hasattr(
                BillingInvoice, cid_field):
            inv = (db.query(BillingInvoice).filter(
                getattr(BillingInvoice, ct_field) == "pharmacy_sale",
                getattr(BillingInvoice, cid_field) == sale.id,
            ).first())
            if inv:
                if hasattr(sale, "billing_invoice_id"):
                    sale.billing_invoice_id = inv.id
                return inv

    return None


def _sync_invoice_totals_from_sale(inv: Any, sale: PharmacySale) -> None:
    gross = _d(getattr(sale, "gross_amount", 0))
    tax = _d(getattr(sale, "total_tax", 0))
    discount_total = _d(getattr(sale, "discount_amount_total", 0))
    net = _d(getattr(sale, "net_amount", 0)) or (gross + tax - discount_total)

    # common totals naming
    _set_if_has(inv, "gross_total", gross)
    _set_if_has(inv, "gross_amount", gross)
    _set_if_has(inv, "tax_total", tax)
    _set_if_has(inv, "total_tax", tax)
    _set_if_has(inv, "discount_total", discount_total)
    _set_if_has(inv, "discount_amount_total", discount_total)
    _set_if_has(inv, "net_total", net)
    _set_if_has(inv, "net_amount", net)

    # payment / balance (kept as 0 here; your billing module may update it later)
    if hasattr(inv, "amount_paid"):
        inv.amount_paid = _d(getattr(inv, "amount_paid", 0))
    if hasattr(inv, "balance_due"):
        inv.balance_due = _round_money(net -
                                       _d(getattr(inv, "amount_paid", 0)))


def _create_billing_invoice_for_sale(db: Session, sale: PharmacySale,
                                     current_user: User) -> Any:
    """
    Creates billing invoice + lines using your NEW billing model style:
      - service_group = ServiceGroup.PHARM
      - context_type/context_id = pharmacy_sale/<sale.id> (if available)
      - lines mapped from PharmacySaleItem
    """
    existing = _find_existing_invoice_for_sale(db, sale)
    if existing:
        # Keep totals in sync (important when sale items change)
        _sync_invoice_totals_from_sale(existing, sale)
        return existing

    inv_kwargs: dict[str, Any] = {}

    # patient / encounter links
    if hasattr(BillingInvoice, "patient_id"):
        inv_kwargs["patient_id"] = sale.patient_id
    if hasattr(BillingInvoice, "visit_id"):
        inv_kwargs["visit_id"] = getattr(sale, "visit_id", None)
    if hasattr(BillingInvoice, "ipd_admission_id"):
        inv_kwargs["ipd_admission_id"] = getattr(sale, "ipd_admission_id",
                                                 None)

    # new standard context fields
    if hasattr(BillingInvoice, "context_type") and hasattr(
            BillingInvoice, "context_id"):
        inv_kwargs["context_type"] = "pharmacy_sale"
        inv_kwargs["context_id"] = sale.id
    elif hasattr(BillingInvoice, "ref_type") and hasattr(
            BillingInvoice, "ref_id"):
        inv_kwargs["ref_type"] = "pharmacy_sale"
        inv_kwargs["ref_id"] = sale.id
    elif hasattr(BillingInvoice, "source_type") and hasattr(
            BillingInvoice, "source_id"):
        inv_kwargs["source_type"] = "pharmacy_sale"
        inv_kwargs["source_id"] = sale.id

    # status (support enum/string)
    if hasattr(BillingInvoice, "status"):
        inv_kwargs["status"] = "DRAFT"

    # service group / billing type
    if hasattr(BillingInvoice, "service_group"):
        inv_kwargs["service_group"] = ServiceGroup.PHARM
    if hasattr(BillingInvoice, "billing_type"):
        inv_kwargs["billing_type"] = "pharmacy"

    inv = BillingInvoice(**inv_kwargs)  # type: ignore

    # created by fields
    for f in ("created_by_id", "created_by", "created_user_id"):
        if hasattr(inv, f):
            setattr(inv, f, getattr(current_user, "id", None))
            break

    # invoice number field variations
    inv_no = _generate_invoice_number(db)
    if inv_no:
        for f in ("invoice_number", "number", "code"):
            if hasattr(inv, f):
                setattr(inv, f, inv_no)
                break

    db.add(inv)
    db.flush()

    if hasattr(sale, "billing_invoice_id"):
        sale.billing_invoice_id = inv.id

    # fetch sale items
    sale_items: List[PharmacySaleItem] = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())

    seq = 1
    for it in sale_items:
        qty = _d(it.quantity)
        unit_price = _d(it.unit_price)
        tax_percent = _d(it.tax_percent)
        discount_amt = _d(getattr(it, "discount_amount", 0))

        line_subtotal = _d(
            it.line_amount) if it.line_amount is not None else _round_money(
                qty * unit_price)
        tax_amount = _d(
            it.tax_amount) if it.tax_amount is not None else _compute_tax(
                line_subtotal, tax_percent)
        line_total = _d(
            it.total_amount) if it.total_amount is not None else _round_money(
                line_subtotal + tax_amount - discount_amt)

        line_kwargs: dict[str, Any] = {}

        # fk
        if hasattr(BillingInvoiceLine, "invoice_id"):
            line_kwargs["invoice_id"] = inv.id

        # ordering
        if hasattr(BillingInvoiceLine, "seq"):
            line_kwargs["seq"] = seq
        elif hasattr(BillingInvoiceLine, "line_no"):
            line_kwargs["line_no"] = seq

        # group/type
        if hasattr(BillingInvoiceLine, "service_group"):
            line_kwargs["service_group"] = ServiceGroup.PHARM
        if hasattr(BillingInvoiceLine, "service_type"):
            line_kwargs["service_type"] = "pharmacy"

        # refs
        if hasattr(BillingInvoiceLine, "service_ref_id"):
            line_kwargs["service_ref_id"] = it.id
        if hasattr(BillingInvoiceLine, "ref_type") and hasattr(
                BillingInvoiceLine, "ref_id"):
            line_kwargs["ref_type"] = "pharmacy_sale_item"
            line_kwargs["ref_id"] = it.id

        # description
        if hasattr(BillingInvoiceLine, "description"):
            line_kwargs["description"] = (it.item_name or "")
        elif hasattr(BillingInvoiceLine, "name"):
            line_kwargs["name"] = (it.item_name or "")

        # qty/price/tax/discount
        if hasattr(BillingInvoiceLine, "quantity"):
            line_kwargs["quantity"] = qty
        if hasattr(BillingInvoiceLine, "unit_price"):
            line_kwargs["unit_price"] = unit_price
        if hasattr(BillingInvoiceLine, "tax_rate"):
            line_kwargs["tax_rate"] = tax_percent
        if hasattr(BillingInvoiceLine, "tax_percent"):
            line_kwargs["tax_percent"] = tax_percent

        if hasattr(BillingInvoiceLine, "discount_amount"):
            line_kwargs["discount_amount"] = discount_amt
        if hasattr(BillingInvoiceLine, "discount_total"):
            line_kwargs["discount_total"] = discount_amt

        # totals naming variations
        if hasattr(BillingInvoiceLine, "line_amount"):
            line_kwargs["line_amount"] = line_subtotal
        if hasattr(BillingInvoiceLine, "sub_total"):
            line_kwargs["sub_total"] = line_subtotal
        if hasattr(BillingInvoiceLine, "tax_amount"):
            line_kwargs["tax_amount"] = tax_amount
        if hasattr(BillingInvoiceLine, "line_total"):
            line_kwargs["line_total"] = line_total
        if hasattr(BillingInvoiceLine, "total_amount"):
            line_kwargs["total_amount"] = line_total

        # void flag
        if hasattr(BillingInvoiceLine, "is_voided"):
            line_kwargs["is_voided"] = False
        if hasattr(BillingInvoiceLine, "is_cancelled"):
            line_kwargs["is_cancelled"] = False

        inv_line = BillingInvoiceLine(**line_kwargs)  # type: ignore

        # created by fields for line
        for f in ("created_by_id", "created_by", "created_user_id"):
            if hasattr(inv_line, f):
                setattr(inv_line, f, getattr(current_user, "id", None))
                break

        db.add(inv_line)
        seq += 1

    # sync totals (header)
    _sync_invoice_totals_from_sale(inv, sale)

    return inv


# ============================================================
# Dispense from Rx (Batch-wise MRP strict) + Schedule enforcement
# ============================================================


def _enforce_item_schedule_for_dispense(*, item: InventoryItem,
                                        rx: PharmacyPrescription) -> None:
    item_type = (getattr(item, "item_type", "") or "").upper()
    if item_type not in ("DRUG", "MEDICINE", ""):
        return

    system = getattr(item, "schedule_system", None)
    code = getattr(item, "schedule_code", None)
    meta = get_schedule_meta(system, code)

    sch_code = (meta.get("code") or "").strip()
    if not sch_code:
        return

    sch_system = (meta.get("system") or "").strip()

    doctor_id = getattr(rx, "doctor_user_id", None)
    patient_id = getattr(rx, "patient_id", None)

    if sch_system == "IN_DCA":
        if sch_code in ("H", ):
            if not doctor_id:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Item '{item.name}' is Schedule {sch_code} and requires a doctor prescription.",
                )
        elif sch_code in ("H1", "X"):
            if not doctor_id:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Item '{item.name}' is Schedule {sch_code} and requires a doctor prescription.",
                )
            if not patient_id:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Item '{item.name}' is Schedule {sch_code} and requires a patient record (register compliance).",
                )
        return

    if sch_system == "US_CSA":
        if sch_code == "I":
            raise HTTPException(
                status_code=400,
                detail=
                f"Item '{item.name}' is US Schedule I and cannot be dispensed.",
            )

        requires_prescription = bool(meta.get("requires_prescription", False))
        requires_register = bool(meta.get("requires_register", False))

        if requires_prescription and not doctor_id:
            raise HTTPException(
                status_code=400,
                detail=
                f"Item '{item.name}' is US Schedule {sch_code} and requires a doctor prescription.",
            )
        if requires_register and not patient_id:
            raise HTTPException(
                status_code=400,
                detail=
                f"Item '{item.name}' is US Schedule {sch_code} and requires a patient record (register compliance).",
            )
        return

    return


def dispense_from_rx(
    db: Session,
    rx_id: int,
    payload: DispenseFromRxIn,
    current_user: User,
) -> tuple[PharmacyPrescription, PharmacySale | None]:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).filter(
            PharmacyPrescription.id == rx_id).first())
    if not rx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Prescription not found.")

    allowed_statuses = {"DRAFT", "ISSUED", "PARTIALLY_DISPENSED"}
    if rx.status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=
            "Only DRAFT, ISSUED or PARTIALLY_DISPENSED prescriptions can be dispensed.",
        )

    if rx.status == "DRAFT":
        rx.status = "ISSUED"

    location_id = getattr(payload, "location_id", None) or getattr(
        rx, "location_id", None)

    line_map = {int(l.id): l for l in (rx.lines or [])}
    lines_to_process: List[tuple[PharmacyPrescriptionLine, Decimal,
                                 Optional[int]]] = []

    for entry in (payload.lines or []):
        line_id = getattr(entry, "line_id", None)
        if line_id is None and isinstance(entry, dict):
            line_id = entry.get("line_id")

        disp_qty = getattr(entry, "dispense_qty", None)
        if disp_qty is None and isinstance(entry, dict):
            disp_qty = entry.get("dispense_qty")

        batch_id = getattr(entry, "batch_id", None)
        if batch_id is None and isinstance(entry, dict):
            batch_id = entry.get("batch_id")

        line = line_map.get(int(line_id or 0))
        if not line:
            raise HTTPException(
                status_code=400,
                detail=f"Rx line {line_id} not found in prescription.")

        if line.status in ("DISPENSED", "CANCELLED"):
            raise HTTPException(
                status_code=400,
                detail=
                f"Rx line {line.id} is {line.status} and cannot be dispensed.")

        remaining = _d(line.requested_qty) - _d(line.dispensed_qty)
        disp_qty_dec = _d(disp_qty)

        if disp_qty_dec > remaining:
            raise HTTPException(
                status_code=400,
                detail=
                f"Dispense quantity {disp_qty_dec} exceeds remaining {remaining} for line {line.id}."
            )
        if disp_qty_dec <= 0:
            continue

        chosen_batch_id = int(batch_id) if batch_id else (
            int(getattr(line, "batch_id", 0) or 0) or None)
        lines_to_process.append((line, disp_qty_dec, chosen_batch_id))

    if not lines_to_process:
        raise HTTPException(status_code=400,
                            detail="No valid lines to dispense.")

    sale: PharmacySale | None = None
    context_type = (getattr(payload, "context_type", None)
                    or getattr(rx, "type", None) or "COUNTER").upper()

    if getattr(payload, "create_sale", False):
        sale = PharmacySale(
            bill_number=_generate_sale_number(db, context_type),
            context_type=context_type,
            prescription_id=rx.id,
            patient_id=rx.patient_id,
            visit_id=rx.visit_id,
            ipd_admission_id=rx.ipd_admission_id,
            location_id=location_id,
            bill_datetime=datetime.utcnow(),
            invoice_status="DRAFT",
            payment_status="UNPAID",
            created_by_id=current_user.id,
        )
        db.add(sale)
        db.flush()

    for line, disp_qty, chosen_batch_id in lines_to_process:
        item: InventoryItem | None = db.get(InventoryItem, int(line.item_id))
        if not item:
            raise HTTPException(
                status_code=404,
                detail=f"Inventory item {line.item_id} not found.")

        _enforce_item_schedule_for_dispense(item=item, rx=rx)

        if not location_id:
            raise HTTPException(
                status_code=400,
                detail=
                "location_id is required to dispense with batch-wise MRP accuracy."
            )

        if chosen_batch_id:
            allocations = _allocate_from_selected_batch(
                db=db,
                location_id=int(location_id),
                item=item,
                batch_id=int(chosen_batch_id),
                qty=disp_qty,
                patient_id=rx.patient_id,
                visit_id=rx.visit_id,
                ipd_admission_id=rx.ipd_admission_id,
                ref_type="PHARMACY_RX",
                ref_id=int(line.id),
                user=current_user,
            )
        else:
            allocations = _allocate_stock_fefo(
                db=db,
                location_id=int(location_id),
                item=item,
                qty=disp_qty,
                patient_id=rx.patient_id,
                visit_id=rx.visit_id,
                ipd_admission_id=rx.ipd_admission_id,
                ref_type="PHARMACY_RX",
                ref_id=int(line.id),
                user=current_user,
            )

        line.dispensed_qty = _d(line.dispensed_qty) + disp_qty
        if _d(line.dispensed_qty) >= _d(line.requested_qty):
            line.status = "DISPENSED"
        else:
            line.status = "PARTIAL"

        if sale:
            for alloc in allocations:
                line_amount = _round_money(alloc.qty * alloc.mrp)
                tax_amount = _compute_tax(line_amount, alloc.tax_percent)
                total_amount = _round_money(line_amount + tax_amount)

                batch = alloc.batch
                stock_txn = alloc.stock_txn

                sale_item = PharmacySaleItem(
                    sale_id=sale.id,
                    rx_line_id=line.id,
                    item_id=item.id,
                    batch_id=batch.id if batch else None,
                    item_name=(getattr(line, "item_name", None) or item.name),
                    batch_no=batch.batch_no if batch else None,
                    expiry_date=batch.expiry_date if batch else None,
                    quantity=alloc.qty,
                    unit_price=alloc.mrp,
                    tax_percent=alloc.tax_percent,
                    line_amount=line_amount,
                    tax_amount=tax_amount,
                    discount_amount=Decimal("0.00"),
                    total_amount=total_amount,
                    stock_txn_id=stock_txn.id if stock_txn else None,
                )
                db.add(sale_item)

    if all(l.status in ("DISPENSED", "CANCELLED") for l in (rx.lines or [])):
        rx.status = "DISPENSED"
    else:
        rx.status = "PARTIALLY_DISPENSED"

    if sale:
        db.flush()
        sale = (db.query(PharmacySale).options(selectinload(
            PharmacySale.items)).filter(PharmacySale.id == sale.id).first())
        _recalc_sale_totals(sale)
        db.flush()

        # ✅ NEW billing invoice creation
        _create_billing_invoice_for_sale(db, sale, current_user)

    db.commit()
    db.refresh(rx)
    if sale:
        db.refresh(sale)

    return rx, sale


# ============================================================
# Counter sale (Counter Rx + invoice in one shot)
# ============================================================


def create_counter_sale(
    db: Session,
    payload: CounterSaleCreateIn,
    current_user: User,
) -> tuple[PharmacyPrescription, PharmacySale]:
    rx_data = PrescriptionCreate(
        type="COUNTER",
        patient_id=payload.patient_id,
        visit_id=payload.visit_id,
        ipd_admission_id=None,
        location_id=payload.location_id,
        doctor_user_id=None,
        notes=payload.notes,
        lines=[
            RxLineCreate(
                item_id=item.item_id,
                requested_qty=item.quantity,
                dose_text=item.dose_text,
                frequency_code=item.frequency_code,
                duration_days=item.duration_days,
                route=item.route,
                timing=item.timing,
                instructions=item.instructions,
            ) for item in (payload.items or [])
        ],
    )

    rx = create_prescription(db, rx_data, current_user)
    rx = sign_prescription(db, rx.id, current_user)

    lines = [{
        "line_id": l.id,
        "dispense_qty": l.requested_qty,
        "batch_id": getattr(l, "batch_id", None)
    } for l in (rx.lines or []) if l.status != "CANCELLED"]

    dispense_payload = DispenseFromRxIn(
        location_id=payload.location_id,
        lines=lines,
        create_sale=True,
        context_type="COUNTER",
    )

    rx, sale = dispense_from_rx(db, rx.id, dispense_payload, current_user)
    assert sale is not None
    return rx, sale


# ============================================================
# Sale operations
# ============================================================


def finalize_sale(db: Session, sale_id: int,
                  current_user: User) -> PharmacySale:
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).filter(PharmacySale.id == sale_id).first())
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status == "CANCELLED":
        raise HTTPException(status_code=400,
                            detail="Cancelled sale cannot be finalized.")

    if sale.invoice_status == "FINALIZED":
        return sale

    _recalc_sale_totals(sale)
    sale.invoice_status = "FINALIZED"

    # keep billing header totals in sync (and create if missing)
    db.flush()
    _create_billing_invoice_for_sale(db, sale, current_user)

    db.commit()
    db.refresh(sale)
    return sale


def cancel_sale(db: Session, sale_id: int, reason: str,
                current_user: User) -> PharmacySale:
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).filter(PharmacySale.id == sale_id).first())
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status == "CANCELLED":
        return sale

    _recalc_sale_totals(sale)
    _update_payment_status(db, sale)

    if sale.payment_status == "PAID":
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel fully paid sale without supervisor override."
        )

    sale.invoice_status = "CANCELLED"
    sale.cancel_reason = reason
    sale.cancelled_at = datetime.utcnow()
    sale.cancelled_by_id = current_user.id

    # optionally mark billing invoice cancelled if your model supports it
    inv = _find_existing_invoice_for_sale(db, sale)
    if inv and hasattr(inv, "status"):
        try:
            inv.status = "CANCELLED"
        except Exception:
            pass

    db.commit()
    db.refresh(sale)
    return sale


def add_payment_to_sale(db: Session, sale_id: int, payload: PaymentCreate,
                        current_user: User) -> PharmacyPayment:
    sale = db.get(PharmacySale, int(sale_id))
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status != "FINALIZED":
        raise HTTPException(
            status_code=400,
            detail="Payments can only be added to FINALIZED sales.")

    paid_on = payload.paid_on or datetime.utcnow()
    payment = PharmacyPayment(
        sale_id=sale.id,
        amount=_round_money(_d(payload.amount)),
        mode=payload.mode,
        reference=payload.reference,
        paid_on=paid_on,
        note=payload.note,
        created_by_id=current_user.id,
    )
    db.add(payment)

    _update_payment_status(db, sale)

    # keep billing header balance sync (optional; your billing module may compute itself)
    inv = _find_existing_invoice_for_sale(db, sale)
    if inv:
        try:
            _sync_invoice_totals_from_sale(inv, sale)
        except Exception:
            pass

    db.commit()
    db.refresh(payment)
    db.refresh(sale)
    return payment
