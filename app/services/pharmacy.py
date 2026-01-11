# FILE: app/services/pharmacy.py
from __future__ import annotations

import enum
from datetime import datetime, date as dt_date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, List, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.user import User

from app.services.drug_schedules import get_schedule_meta
from app.services.inventory import create_stock_transaction
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
    BatchStatus,
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
# ✅ NEW BILLING MODELS (your updated billing.py)
# ============================================================
from app.models.billing import (
    BillingNumberSeries,
    NumberDocType,
    NumberResetPeriod,
    EncounterType,
    BillingCase,
    BillingCaseLink,
    BillingCaseStatus,
    PayerMode,
    BillingInvoice,
    BillingInvoiceLine,
    InvoiceType,
    DocStatus,
    PayerType,
    ServiceGroup,
    BillingPayment,
    PayMode,
)

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


def _compute_tax(amount: Decimal, tax_percent: Decimal) -> Decimal:
    tax_percent = _d(tax_percent)
    if tax_percent <= 0:
        return Decimal("0.00")
    return _round_money(_d(amount) * tax_percent / Decimal("100"))


# ============================================================
# Simple local numbering (uses BillingNumberSeries, safe in MySQL)
# ============================================================
def _period_key(reset_period: NumberResetPeriod, today: dt_date) -> Optional[str]:
    if reset_period == NumberResetPeriod.NONE:
        return None
    if reset_period == NumberResetPeriod.YEAR:
        return f"{today.year:04d}"
    if reset_period == NumberResetPeriod.MONTH:
        return f"{today.year:04d}-{today.month:02d}"
    return None


def _next_series_number(
    db: Session,
    *,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
) -> str:
    """
    Allocates a unique number using billing_number_series with SELECT ... FOR UPDATE.

    Output format:
      - NONE  : <prefix><padded>
      - YEAR  : <prefix><YYYY>-<padded>
      - MONTH : <prefix><YYYY-MM>-<padded>
    Example: INV-PHARM-2026-000001
    """
    today = dt_date.today()
    pkey = _period_key(reset_period, today)

    def _get_row_for_update():
        stmt = (
            select(BillingNumberSeries)
            .where(
                BillingNumberSeries.doc_type == doc_type,
                BillingNumberSeries.reset_period == reset_period,
                BillingNumberSeries.prefix == prefix,
                BillingNumberSeries.is_active.is_(True),
            )
            .with_for_update()
        )
        return db.execute(stmt).scalar_one_or_none()

    row = _get_row_for_update()
    if row is None:
        row = BillingNumberSeries(
            doc_type=doc_type,
            prefix=prefix,
            reset_period=reset_period,
            padding=padding,
            next_number=1,
            last_period_key=pkey,
            is_active=True,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = _get_row_for_update()
            if row is None:
                raise

    # reset logic
    if reset_period != NumberResetPeriod.NONE:
        if row.last_period_key != pkey:
            row.next_number = 1
            row.last_period_key = pkey

    n = int(row.next_number or 1)
    row.next_number = n + 1
    db.flush()

    padded = str(n).zfill(int(row.padding or padding))

    if reset_period == NumberResetPeriod.NONE or not pkey:
        return f"{prefix}{padded}"
    return f"{prefix}{pkey}-{padded}"


# ============================================================
# Rx / Sale number generators (pharmacy-side, independent)
# ============================================================
def _generate_prescription_number(db: Session, rx_type: str) -> str:
    """
    RX-<TYPE>-YYYYMMDD-<seq>
    """
    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"RX-{rx_type}-{today_str}"

    last_number = (
        db.query(PharmacyPrescription.prescription_number)
        .filter(PharmacyPrescription.prescription_number.like(f"{prefix}-%"))
        .order_by(PharmacyPrescription.prescription_number.desc())
        .first()
    )

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

    last_number = (
        db.query(PharmacySale.bill_number)
        .filter(PharmacySale.bill_number.like(f"{prefix}-%"))
        .order_by(PharmacySale.bill_number.desc())
        .first()
    )

    next_seq = 1
    if last_number and last_number[0]:
        try:
            next_seq = int(str(last_number[0]).split("-")[-1]) + 1
        except Exception:
            next_seq = 1

    return f"{prefix}-{next_seq:04d}"


# ============================================================
# InventoryItem snapshot helpers
# ============================================================
def _item_form(item: InventoryItem) -> str:
    return (getattr(item, "dosage_form", "") or "").strip()


def _item_strength(item: InventoryItem) -> str:
    return (getattr(item, "strength", "") or "").strip()


def _item_type_str(item: InventoryItem) -> str:
    t = (getattr(item, "item_type", "") or "").strip().upper()
    if t == "CONSUMABLE" or bool(getattr(item, "is_consumable", False)):
        return "CONSUMABLE"
    return "DRUG"


# ============================================================
# Stock helpers
# ============================================================
def _snapshot_available_stock(db: Session, location_id: int | None, item_id: int) -> Decimal | None:
    if not location_id:
        return None
    total = (
        db.query(func.coalesce(func.sum(ItemBatch.current_qty), 0))
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
        )
        .scalar()
    )
    return _d(total)


def _validate_batch_for_line(batch: ItemBatch, *, item_id: int, location_id: int) -> None:
    if not batch:
        raise HTTPException(status_code=400, detail="Selected batch not found.")
    if int(batch.item_id) != int(item_id):
        raise HTTPException(status_code=400, detail="Selected batch does not belong to this item.")
    if int(batch.location_id) != int(location_id):
        raise HTTPException(status_code=400, detail="Selected batch does not belong to this location.")

    if (not batch.is_active) or (not batch.is_saleable) or (batch.status != BatchStatus.ACTIVE):
        raise HTTPException(status_code=400, detail="Selected batch is not saleable.")

    if _d(batch.current_qty) <= 0:
        raise HTTPException(status_code=400, detail="Selected batch has no stock.")

    today = dt_date.today()
    if batch.expiry_date is not None and batch.expiry_date < today:
        raise HTTPException(status_code=400, detail="Selected batch is expired.")


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
    qty = _d(qty)
    if qty <= 0:
        return []

    batch: ItemBatch | None = (
        db.query(ItemBatch).filter(ItemBatch.id == int(batch_id)).with_for_update().first()
    )
    if not batch:
        raise HTTPException(status_code=400, detail="Selected batch not found.")

    _validate_batch_for_line(batch, item_id=int(item.id), location_id=int(location_id))

    available = _d(batch.current_qty)
    if qty > available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Insufficient stock in selected batch {batch.batch_no}. "
                f"Available {available}, requested {qty}."
            ),
        )

    mrp = _d(batch.mrp)
    if mrp <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"MRP is missing/zero for batch {batch.batch_no}. Please fix batch MRP.",
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

    return [AllocatedBatch(batch=batch, qty=qty, mrp=mrp, tax_percent=tax_percent, stock_txn=stock_txn)]


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
    qty = _d(qty)
    if qty <= 0:
        return []

    today = dt_date.today()

    q = (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == item.id,
            ItemBatch.location_id == location_id,
            ItemBatch.current_qty > 0,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == BatchStatus.ACTIVE,
            or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
        )
    )

    nulls_last_expr = case((ItemBatch.expiry_date.is_(None), 1), else_=0)

    batches: List[ItemBatch] = (
        q.order_by(
            nulls_last_expr.asc(),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
        .with_for_update()
        .all()
    )

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
                detail=f"MRP is missing/zero for batch {batch.batch_no}. Please fix batch MRP.",
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
            AllocatedBatch(batch=batch, qty=use_qty, mrp=mrp, tax_percent=tax_percent, stock_txn=stock_txn)
        )
        remaining -= use_qty

    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Insufficient stock for item {item.id}. Required {qty}, allocated {qty - remaining}."),
        )

    return allocations


# ============================================================
# Batch assignment on SEND/ISSUE (locks batch_id for each line)
# ============================================================
def _pick_fefo_batch(db: Session, item_id: int, location_id: int) -> ItemBatch | None:
    today = dt_date.today()
    return (
        db.query(ItemBatch)
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == BatchStatus.ACTIVE,
            ItemBatch.current_qty > 0,
            or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
        )
        .order_by(
            case((ItemBatch.expiry_date.is_(None), 1), else_=0),
            ItemBatch.expiry_date.asc(),
            ItemBatch.id.asc(),
        )
        .first()
    )


def _total_available_qty(db: Session, item_id: int, location_id: int) -> Decimal:
    today = dt_date.today()
    rows = (
        db.query(ItemBatch.current_qty)
        .filter(
            ItemBatch.item_id == item_id,
            ItemBatch.location_id == location_id,
            ItemBatch.is_active.is_(True),
            ItemBatch.is_saleable.is_(True),
            ItemBatch.status == BatchStatus.ACTIVE,
            ItemBatch.current_qty > 0,
            or_(ItemBatch.expiry_date.is_(None), ItemBatch.expiry_date >= today),
        )
        .all()
    )
    total = Decimal("0")
    for (q,) in rows:
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
            ln.available_qty_snapshot = _total_available_qty(db, int(ln.item_id), int(rx.location_id))
            need = _d(getattr(ln, "requested_qty", 0))
            ln.is_out_of_stock = bool(_d(ln.available_qty_snapshot) < need)
        except Exception:
            pass


# ============================================================
# Rx CRUD
# ============================================================
def create_prescription(db: Session, data: PrescriptionCreate, current_user: User) -> PharmacyPrescription:
    if data.type in ("OPD", "IPD") and not data.patient_id:
        raise HTTPException(status_code=400, detail="patient_id is required for OPD/IPD prescriptions.")
    if data.type == "OPD" and not data.visit_id:
        raise HTTPException(status_code=400, detail="visit_id is required for OPD prescriptions.")
    if data.type == "IPD" and not data.ipd_admission_id:
        raise HTTPException(status_code=400, detail="ipd_admission_id is required for IPD prescriptions.")

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


def _add_rx_line_internal(db: Session, rx: PharmacyPrescription, line_data: RxLineCreate) -> PharmacyPrescriptionLine:
    item: InventoryItem | None = db.get(InventoryItem, int(line_data.item_id))
    if not item:
        raise HTTPException(status_code=404, detail=f"Inventory item {line_data.item_id} not found.")

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
        item_type=_item_type_str(item).lower(),
    )
    db.add(line)
    return line


def update_prescription(db: Session, rx_id: int, data: PrescriptionUpdate, current_user: User) -> PharmacyPrescription:
    rx = (
        db.query(PharmacyPrescription)
        .options(selectinload(PharmacyPrescription.lines))
        .filter(PharmacyPrescription.id == rx_id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT prescriptions can be updated.")

    if data.location_id is not None:
        rx.location_id = data.location_id
    if data.doctor_user_id is not None:
        rx.doctor_user_id = data.doctor_user_id
    if data.notes is not None:
        rx.notes = data.notes

    db.commit()
    db.refresh(rx)
    return rx


def add_rx_line(db: Session, rx_id: int, line_data: RxLineCreate, current_user: User) -> PharmacyPrescription:
    rx = (
        db.query(PharmacyPrescription)
        .options(selectinload(PharmacyPrescription.lines))
        .filter(PharmacyPrescription.id == rx_id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Lines can only be added while prescription is DRAFT.")

    _add_rx_line_internal(db, rx, line_data)
    db.commit()
    db.refresh(rx)
    return rx


def update_rx_line(db: Session, line_id: int, data: RxLineUpdate, current_user: User) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, int(line_id))
    if not line:
        raise HTTPException(status_code=404, detail="Rx line not found.")

    rx = db.get(PharmacyPrescription, int(line.prescription_id))
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Rx lines can only be edited while prescription is DRAFT.")

    if data.requested_qty is not None:
        new_req = _d(data.requested_qty)
        if _d(line.dispensed_qty) and new_req < _d(line.dispensed_qty):
            raise HTTPException(status_code=400, detail="requested_qty cannot be less than dispensed_qty.")
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
            raise HTTPException(status_code=400, detail="Line status can only be WAITING or CANCELLED.")
        if _d(line.dispensed_qty) and data.status == "CANCELLED":
            raise HTTPException(status_code=400, detail="Cannot cancel a line that has already been dispensed.")
        line.status = data.status

    db.commit()
    db.refresh(rx)
    return rx


def delete_rx_line(db: Session, line_id: int, current_user: User) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, int(line_id))
    if not line:
        raise HTTPException(status_code=404, detail="Rx line not found.")

    rx = db.get(PharmacyPrescription, int(line.prescription_id))
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Rx lines can only be deleted while prescription is DRAFT.")

    db.delete(line)
    db.commit()
    db.refresh(rx)
    return rx


def sign_prescription(db: Session, rx_id: int, current_user: User) -> PharmacyPrescription:
    rx = (
        db.query(PharmacyPrescription)
        .options(selectinload(PharmacyPrescription.lines))
        .filter(PharmacyPrescription.id == rx_id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if rx.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Only DRAFT prescriptions can be signed.")

    if not rx.lines:
        raise HTTPException(status_code=400, detail="Cannot sign an empty prescription.")

    assign_batches_on_send(db, rx)

    rx.status = "ISSUED"
    rx.signed_at = datetime.utcnow()
    rx.signed_by_id = current_user.id

    db.commit()
    db.refresh(rx)
    return rx


def cancel_prescription(db: Session, rx_id: int, reason: str, current_user: User) -> PharmacyPrescription:
    rx = (
        db.query(PharmacyPrescription)
        .options(selectinload(PharmacyPrescription.lines))
        .filter(PharmacyPrescription.id == rx_id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    if any(_d(l.dispensed_qty) > 0 for l in (rx.lines or [])):
        raise HTTPException(status_code=400, detail="Cannot cancel prescription with dispensed lines.")

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
    total_paid = (
        db.query(func.coalesce(func.sum(PharmacyPayment.amount), 0))
        .filter(PharmacyPayment.sale_id == sale.id)
        .scalar()
    )
    total_paid = _d(total_paid)

    if total_paid <= 0:
        sale.payment_status = "UNPAID"
    elif total_paid < _d(sale.net_amount):
        sale.payment_status = "PARTIALLY_PAID"
    else:
        sale.payment_status = "PAID"


# ============================================================
# ✅ BILLING INTEGRATION (NEW MODELS)
# - Create/Reuse BillingCase based on encounter
# - Create/Sync Pharmacy invoice (module=PHARM, invoice_type=PHARMACY)
# - Use idempotent BillingInvoiceLine (source_module/source_ref_id/source_line_key)
# ============================================================
def _map_encounter_for_sale(db: Session, sale: PharmacySale) -> tuple[EncounterType, int]:
    """
    BillingCase requires encounter_type + encounter_id.
    Priority:
      1) IPD -> EncounterType.IP, encounter_id = ipd_admission_id
      2) OPD -> EncounterType.OP, encounter_id = visit_id
      3) Else (COUNTER/unknown) -> EncounterType.OP with a negative pseudo-id (-sale.id)
         (negative avoids collision with real encounter IDs)
    """
    if getattr(sale, "ipd_admission_id", None):
        return EncounterType.IP, int(sale.ipd_admission_id)

    if getattr(sale, "visit_id", None):
        return EncounterType.OP, int(sale.visit_id)

    # pseudo encounter
    return EncounterType.ER, -int(sale.id)


def _ensure_case_link(db: Session, *, case_id: int, entity_type: str, entity_id: int) -> None:
    existing = (
        db.query(BillingCaseLink)
        .filter(
            BillingCaseLink.billing_case_id == int(case_id),
            BillingCaseLink.entity_type == entity_type,
            BillingCaseLink.entity_id == int(entity_id),
        )
        .first()
    )
    if existing:
        return
    db.add(BillingCaseLink(billing_case_id=int(case_id), entity_type=entity_type, entity_id=int(entity_id)))


def _ensure_billing_case_for_sale(db: Session, sale: PharmacySale, current_user: User) -> BillingCase:
    if not getattr(sale, "patient_id", None):
        raise HTTPException(status_code=400, detail="patient_id is required to create billing case/invoice for pharmacy sale.")

    enc_type, enc_id = _map_encounter_for_sale(db, sale)

    case_row = (
        db.query(BillingCase)
        .filter(BillingCase.encounter_type == enc_type, BillingCase.encounter_id == int(enc_id))
        .first()
    )
    if case_row:
        if int(case_row.patient_id) != int(sale.patient_id):
            raise HTTPException(status_code=400, detail="BillingCase patient mismatch for this encounter.")
        return case_row

    # create new case
    case_number = _next_series_number(
        db,
        doc_type=NumberDocType.CASE,
        prefix=f"CASE-{enc_type.value}-",
        reset_period=NumberResetPeriod.YEAR,
        padding=6,
    )

    case_row = BillingCase(
        patient_id=int(sale.patient_id),
        encounter_type=enc_type,
        encounter_id=int(enc_id),
        case_number=case_number,
        status=BillingCaseStatus.OPEN,
        payer_mode=PayerMode.SELF,
        created_by=getattr(current_user, "id", None),
        updated_by=getattr(current_user, "id", None),
    )
    db.add(case_row)
    db.flush()
    return case_row


def _compute_invoice_totals_from_lines(lines: List[BillingInvoiceLine]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    sub_total = Decimal("0")
    discount_total = Decimal("0")
    tax_total = Decimal("0")

    for ln in lines:
        sub_total += _d(ln.line_total)
        discount_total += _d(ln.discount_amount)
        tax_total += _d(ln.tax_amount)

    unrounded = sub_total - discount_total + tax_total
    grand = _round_money(unrounded)
    round_off = _round_money(grand - unrounded)
    return (_round_money(sub_total), _round_money(discount_total), _round_money(tax_total), round_off, grand)


def _find_invoice_for_sale(db: Session, sale: PharmacySale) -> BillingInvoice | None:
    if getattr(sale, "billing_invoice_id", None):
        inv = db.get(BillingInvoice, int(sale.billing_invoice_id))
        if inv:
            return inv

    # fallback: try find via meta_json pharmacy_sale_id (if you used it before)
    inv = (
        db.query(BillingInvoice)
        .filter(
            BillingInvoice.module == "PHARM",
            BillingInvoice.meta_json.isnot(None),
        )
        .order_by(BillingInvoice.id.desc())
        .first()
    )
    # (We avoid JSON query dependency here; the FK is the primary method.)
    return None if inv is None else None


def _upsert_invoice_lines_from_sale(
    db: Session,
    *,
    billing_case_id: int,
    invoice: BillingInvoice,
    sale: PharmacySale,
    current_user: User,
) -> None:
    """
    Idempotency key:
      (billing_case_id, source_module='PHARM', source_ref_id=sale.id, source_line_key='SALEITEM:<sale_item_id>')
    """
    if invoice.status != DocStatus.DRAFT:
        # Safety: do not mutate posted/approved/void docs from pharmacy module
        raise HTTPException(status_code=400, detail=f"Invoice is {invoice.status} and cannot be modified from Pharmacy.")

    sale_items: List[PharmacySaleItem] = (
        db.query(PharmacySaleItem).filter(PharmacySaleItem.sale_id == sale.id).all()
    )

    existing_lines: List[BillingInvoiceLine] = (
        db.query(BillingInvoiceLine)
        .filter(
            BillingInvoiceLine.invoice_id == invoice.id,
            BillingInvoiceLine.billing_case_id == int(billing_case_id),
            BillingInvoiceLine.source_module == "PHM",
            BillingInvoiceLine.source_ref_id == int(sale.id),
        )
        .all()
    )
    existing_by_key = {str(l.source_line_key): l for l in existing_lines if l.source_line_key}

    seen_keys: set[str] = set()

    for it in sale_items:
        key = f"SALEITEM:{int(it.id)}"
        seen_keys.add(key)

        item: InventoryItem | None = db.get(InventoryItem, int(it.item_id)) if getattr(it, "item_id", None) else None
        item_type = _item_type_str(item) if item else "DRUG"

        qty = _d(it.quantity)
        unit_price = _d(it.unit_price)
        discount_amount = _round_money(_d(getattr(it, "discount_amount", 0)))
        gst_rate = _d(getattr(it, "tax_percent", 0))

        line_total = _round_money(qty * unit_price)  # gross
        taxable = _round_money(line_total - discount_amount)
        tax_amount = _compute_tax(taxable, gst_rate)
        net_amount = _round_money(taxable + tax_amount)

        ln = existing_by_key.get(key)
        if ln is None:
            ln = BillingInvoiceLine(
                billing_case_id=int(billing_case_id),
                invoice_id=int(invoice.id),
                service_group="PHM",
                item_type=item_type,
                item_id=int(it.item_id) if getattr(it, "item_id", None) else None,
                item_code=getattr(item, "code", None) if item else None,
                description=(getattr(it, "item_name", None) or (item.name if item else "Pharmacy Item")),
                qty=qty,
                unit_price=unit_price,
                discount_percent=Decimal("0.00"),
                discount_amount=discount_amount,
                gst_rate=gst_rate,
                tax_amount=tax_amount,
                line_total=line_total,
                net_amount=net_amount,
                source_module="PHM",
                source_ref_id=int(sale.id),
                source_line_key=key,
                is_manual=False,
                created_by=getattr(current_user, "id", None),
            )
            db.add(ln)
        else:
            ln.service_group = "PHM"
            ln.item_type = item_type
            ln.item_id = int(it.item_id) if getattr(it, "item_id", None) else None
            ln.item_code = getattr(item, "code", None) if item else None
            ln.description = (getattr(it, "item_name", None) or (item.name if item else ln.description))
            ln.qty = qty
            ln.unit_price = unit_price
            ln.discount_percent = Decimal("0.00")
            ln.discount_amount = discount_amount
            ln.gst_rate = gst_rate
            ln.tax_amount = tax_amount
            ln.line_total = line_total
            ln.net_amount = net_amount

    # remove stale lines (if any) for this sale
    for ln in existing_lines:
        if ln.source_line_key and str(ln.source_line_key) not in seen_keys:
            db.delete(ln)

    db.flush()


def _ensure_billing_invoice_for_sale(db: Session, sale: PharmacySale, current_user: User) -> BillingInvoice:
    """
    Creates OR syncs a billing invoice for a PharmacySale using your NEW billing models.
    """
    billing_case = _ensure_billing_case_for_sale(db, sale, current_user)

    # links (optional but recommended)
    if getattr(sale, "prescription_id", None):
        _ensure_case_link(db, case_id=billing_case.id, entity_type="PHARM_RX", entity_id=int(sale.prescription_id))
    _ensure_case_link(db, case_id=billing_case.id, entity_type="PHARM_SALE", entity_id=int(sale.id))

    inv: BillingInvoice | None = None
    if getattr(sale, "billing_invoice_id", None):
        inv = db.get(BillingInvoice, int(sale.billing_invoice_id))

    if inv is None:
        invoice_number = _next_series_number(
            db,
            doc_type=NumberDocType.INVOICE,
            prefix="INV-PHARM-",
            reset_period=NumberResetPeriod.YEAR,
            padding=6,
        )

        inv = BillingInvoice(
            billing_case_id=billing_case.id,
            invoice_number=invoice_number,
            module="PHM",
            invoice_type=InvoiceType.PHARMACY,
            status=DocStatus.DRAFT,
            payer_type=PayerType.PATIENT,
            payer_id=int(sale.patient_id) if getattr(sale, "patient_id", None) else None,
            currency="INR",
            service_date=getattr(sale, "bill_datetime", None),
            meta_json={
                "pharmacy_sale_id": int(sale.id),
                "bill_number": getattr(sale, "bill_number", None),
                "context_type": getattr(sale, "context_type", None),
                "prescription_id": getattr(sale, "prescription_id", None),
            },
            created_by=getattr(current_user, "id", None),
            updated_by=getattr(current_user, "id", None),
        )
        db.add(inv)
        db.flush()

        if hasattr(sale, "billing_invoice_id"):
            sale.billing_invoice_id = inv.id

    # sync invoice lines
    _upsert_invoice_lines_from_sale(
        db,
        billing_case_id=int(billing_case.id),
        invoice=inv,
        sale=sale,
        current_user=current_user,
    )

    # totals from lines
    inv_lines = (
        db.query(BillingInvoiceLine)
        .filter(BillingInvoiceLine.invoice_id == inv.id)
        .all()
    )
    sub_total, discount_total, tax_total, round_off, grand_total = _compute_invoice_totals_from_lines(inv_lines)

    inv.sub_total = sub_total
    inv.discount_total = discount_total
    inv.tax_total = tax_total
    inv.round_off = round_off
    inv.grand_total = grand_total
    inv.updated_by = getattr(current_user, "id", None)

    db.flush()
    return inv


def _map_paymode(mode: str | None) -> PayMode:
    m = (mode or "").upper().strip()
    if m in ("CASH",):
        return PayMode.CASH
    if m in ("CARD", "DEBIT", "CREDIT"):
        return PayMode.CARD
    if m in ("UPI",):
        return PayMode.UPI
    if m in ("BANK", "NEFT", "RTGS", "IMPS"):
        return PayMode.BANK
    if m in ("WALLET",):
        return PayMode.WALLET
    return PayMode.CASH


# ============================================================
# Dispense schedule enforcement
# ============================================================
def _enforce_item_schedule_for_dispense(*, item: InventoryItem, rx: PharmacyPrescription) -> None:
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
        if sch_code in ("H",):
            if not doctor_id:
                raise HTTPException(status_code=400, detail=f"Item '{item.name}' is Schedule {sch_code} and requires a doctor prescription.")
        elif sch_code in ("H1", "X"):
            if not doctor_id:
                raise HTTPException(status_code=400, detail=f"Item '{item.name}' is Schedule {sch_code} and requires a doctor prescription.")
            if not patient_id:
                raise HTTPException(status_code=400, detail=f"Item '{item.name}' is Schedule {sch_code} and requires a patient record (register compliance).")
        return

    if sch_system == "US_CSA":
        if sch_code == "I":
            raise HTTPException(status_code=400, detail=f"Item '{item.name}' is US Schedule I and cannot be dispensed.")

        requires_prescription = bool(meta.get("requires_prescription", False))
        requires_register = bool(meta.get("requires_register", False))

        if requires_prescription and not doctor_id:
            raise HTTPException(status_code=400, detail=f"Item '{item.name}' is US Schedule {sch_code} and requires a doctor prescription.")
        if requires_register and not patient_id:
            raise HTTPException(status_code=400, detail=f"Item '{item.name}' is US Schedule {sch_code} and requires a patient record (register compliance).")
        return


# ============================================================
# Dispense from Rx (Batch-wise MRP strict)
# ============================================================
def dispense_from_rx(
    db: Session,
    rx_id: int,
    payload: DispenseFromRxIn,
    current_user: User,
) -> tuple[PharmacyPrescription, PharmacySale | None]:
    rx = (
        db.query(PharmacyPrescription)
        .options(selectinload(PharmacyPrescription.lines))
        .filter(PharmacyPrescription.id == rx_id)
        .first()
    )
    if not rx:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    allowed_statuses = {"DRAFT", "ISSUED", "PARTIALLY_DISPENSED"}
    if rx.status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Only DRAFT, ISSUED or PARTIALLY_DISPENSED prescriptions can be dispensed.",
        )

    if rx.status == "DRAFT":
        rx.status = "ISSUED"

    location_id = getattr(payload, "location_id", None) or getattr(rx, "location_id", None)

    line_map = {int(l.id): l for l in (rx.lines or [])}
    lines_to_process: List[tuple[PharmacyPrescriptionLine, Decimal, Optional[int]]] = []

    for entry in (payload.lines or []):
        line_id = getattr(entry, "line_id", None) if not isinstance(entry, dict) else entry.get("line_id")
        disp_qty = getattr(entry, "dispense_qty", None) if not isinstance(entry, dict) else entry.get("dispense_qty")
        batch_id = getattr(entry, "batch_id", None) if not isinstance(entry, dict) else entry.get("batch_id")

        line = line_map.get(int(line_id or 0))
        if not line:
            raise HTTPException(status_code=400, detail=f"Rx line {line_id} not found in prescription.")

        if line.status in ("DISPENSED", "CANCELLED"):
            raise HTTPException(status_code=400, detail=f"Rx line {line.id} is {line.status} and cannot be dispensed.")

        remaining = _d(line.requested_qty) - _d(line.dispensed_qty)
        disp_qty_dec = _d(disp_qty)

        if disp_qty_dec > remaining:
            raise HTTPException(
                status_code=400,
                detail=f"Dispense quantity {disp_qty_dec} exceeds remaining {remaining} for line {line.id}.",
            )
        if disp_qty_dec <= 0:
            continue

        chosen_batch_id = int(batch_id) if batch_id else (int(getattr(line, "batch_id", 0) or 0) or None)
        lines_to_process.append((line, disp_qty_dec, chosen_batch_id))

    if not lines_to_process:
        raise HTTPException(status_code=400, detail="No valid lines to dispense.")

    sale: PharmacySale | None = None
    context_type = (getattr(payload, "context_type", None) or getattr(rx, "type", None) or "COUNTER").upper()

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
            raise HTTPException(status_code=404, detail=f"Inventory item {line.item_id} not found.")

        _enforce_item_schedule_for_dispense(item=item, rx=rx)

        if not location_id:
            raise HTTPException(
                status_code=400,
                detail="location_id is required to dispense with batch-wise MRP accuracy.",
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
        sale = (
            db.query(PharmacySale)
            .options(selectinload(PharmacySale.items))
            .filter(PharmacySale.id == sale.id)
            .first()
        )
        _recalc_sale_totals(sale)
        db.flush()

        # ✅ NEW billing invoice creation/sync
        _ensure_billing_invoice_for_sale(db, sale, current_user)

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
            )
            for item in (payload.items or [])
        ],
    )

    rx = create_prescription(db, rx_data, current_user)
    rx = sign_prescription(db, rx.id, current_user)

    lines = [
        {"line_id": l.id, "dispense_qty": l.requested_qty, "batch_id": getattr(l, "batch_id", None)}
        for l in (rx.lines or [])
        if l.status != "CANCELLED"
    ]

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
def finalize_sale(db: Session, sale_id: int, current_user: User) -> PharmacySale:
    sale = (
        db.query(PharmacySale)
        .options(selectinload(PharmacySale.items))
        .filter(PharmacySale.id == sale_id)
        .first()
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Cancelled sale cannot be finalized.")

    if sale.invoice_status == "FINALIZED":
        return sale

    _recalc_sale_totals(sale)
    sale.invoice_status = "FINALIZED"

    # keep billing in sync (create if missing)
    db.flush()
    _ensure_billing_invoice_for_sale(db, sale, current_user)

    db.commit()
    db.refresh(sale)
    return sale


def cancel_sale(db: Session, sale_id: int, reason: str, current_user: User) -> PharmacySale:
    sale = (
        db.query(PharmacySale)
        .options(selectinload(PharmacySale.items))
        .filter(PharmacySale.id == sale_id)
        .first()
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status == "CANCELLED":
        return sale

    _recalc_sale_totals(sale)
    _update_payment_status(db, sale)

    if sale.payment_status == "PAID":
        raise HTTPException(status_code=400, detail="Cannot cancel fully paid sale without supervisor override.")

    sale.invoice_status = "CANCELLED"
    sale.cancel_reason = reason
    sale.cancelled_at = datetime.utcnow()
    sale.cancelled_by_id = current_user.id

    # ✅ VOID billing invoice (new model uses DocStatus.VOID, not CANCELLED)
    if getattr(sale, "billing_invoice_id", None):
        inv = db.get(BillingInvoice, int(sale.billing_invoice_id))
        if inv and inv.status in (DocStatus.DRAFT, DocStatus.APPROVED):
            inv.status = DocStatus.VOID
            inv.voided_by = getattr(current_user, "id", None)
            inv.voided_at = datetime.utcnow()
            inv.void_reason = reason
            inv.updated_by = getattr(current_user, "id", None)

    db.commit()
    db.refresh(sale)
    return sale


def add_payment_to_sale(db: Session, sale_id: int, payload: PaymentCreate, current_user: User) -> PharmacyPayment:
    sale = db.get(PharmacySale, int(sale_id))
    if not sale:
        raise HTTPException(status_code=404, detail="Pharmacy sale not found.")

    if sale.invoice_status != "FINALIZED":
        raise HTTPException(status_code=400, detail="Payments can only be added to FINALIZED sales.")

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
    db.flush()

    # update sale payment status
    _update_payment_status(db, sale)

    # ✅ also create BillingPayment (so Billing module sees receipts)
    if getattr(sale, "billing_invoice_id", None):
        inv = db.get(BillingInvoice, int(sale.billing_invoice_id))
        if inv:
            bp = BillingPayment(
                billing_case_id=int(inv.billing_case_id),
                invoice_id=int(inv.id),
                payer_type=PayerType.PATIENT,
                payer_id=int(sale.patient_id) if getattr(sale, "patient_id", None) else None,
                mode=_map_paymode(getattr(payload, "mode", None)),
                amount=_round_money(_d(payload.amount)),
                txn_ref=getattr(payload, "reference", None),
                received_at=paid_on,
                received_by=getattr(current_user, "id", None),
                notes=getattr(payload, "note", None),
            )
            db.add(bp)

    db.commit()
    db.refresh(payment)
    db.refresh(sale)
    return payment
