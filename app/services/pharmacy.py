# FILE: app/services/pharmacy.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func

from app.models.user import User
from app.models.pharmacy_prescription import (
    PharmacyPrescription,
    PharmacyPrescriptionLine,
    PharmacySale,
    PharmacySaleItem,
    PharmacyPayment,
)
from app.services.inventory import create_stock_transaction

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
from app.models.billing import Invoice, InvoiceItem
from sqlalchemy import case, or_
from datetime import date

MONEY = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


# ---------- Number generators ----------


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
            next_seq = int(last_number[0].split("-")[-1]) + 1
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
            next_seq = int(last_number[0].split("-")[-1]) + 1
        except Exception:
            next_seq = 1
    return f"{prefix}-{next_seq:04d}"


# ---------- Stock helpers ----------


def _snapshot_available_stock(
    db: Session,
    location_id: int | None,
    item_id: int,
) -> Decimal | None:
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
                                        ItemBatch.is_active == True,
                                        ItemBatch.is_saleable == True,
                                    ).scalar())
    return Decimal(total or 0)


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
        self.qty = qty
        self.mrp = mrp
        self.tax_percent = tax_percent
        self.stock_txn = stock_txn


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

    Uses:
      - ItemBatch.current_qty, mrp, tax_percent, unit_cost, is_saleable, status
      - InventoryItem.default_mrp, default_tax_percent
      - StockTransaction.quantity_change, unit_cost, mrp

    MySQL-safe: emulates NULLS LAST using CASE instead of .nulls_last()
    and skips EXPIRED batches.
    """
    qty = Decimal(qty)
    if qty <= 0:
        return []

    today = date.today()

    # Base query: only active, saleable, non-expired (or no expiry) batches
    q = (
        db.query(ItemBatch).filter(
            ItemBatch.item_id == item.id,
            ItemBatch.location_id == location_id,
            ItemBatch.current_qty > 0,
            ItemBatch.is_active == True,  # noqa: E712
            ItemBatch.is_saleable == True,  # noqa: E712
            or_(
                ItemBatch.expiry_date.is_(None),
                ItemBatch.expiry_date >= today,
            ),
        ))

    # MySQL-safe NULLS LAST emulation:
    # CASE WHEN expiry_date IS NULL THEN 1 ELSE 0 END ASC
    nulls_last_expr = case(
        (ItemBatch.expiry_date.is_(None), 1),
        else_=0,
    )

    q = (
        q.order_by(
            nulls_last_expr.asc(),  # non-NULL first, NULL last
            ItemBatch.expiry_date.asc(),  # earliest expiry first
            ItemBatch.id.asc(),  # tie-breaker
        ).with_for_update())

    batches: List[ItemBatch] = q.all()

    remaining = qty
    allocations: List[AllocatedBatch] = []

    for batch in batches:
        if remaining <= 0:
            break

        available = Decimal(batch.current_qty or 0)
        if available <= 0:
            continue

        use_qty = min(available, remaining)
        if use_qty <= 0:
            continue

        # Reduce batch stock
        batch.current_qty = available - use_qty

        # Pricing: use batch.mrp/tax_percent if present, else item defaults
        mrp = Decimal(batch.mrp or item.default_mrp or 0)
        tax_percent = Decimal(batch.tax_percent or item.default_tax_percent
                              or 0)
        unit_cost = Decimal(batch.unit_cost or 0)
        
        
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
            patient_id=patient_id,  # will be ignored if column not present
            visit_id=visit_id,      # will be ignored if column not present
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
        # Not enough stock overall
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Insufficient stock for item {item.id}. "
                    f"Required {qty}, allocated {qty - remaining}."),
        )

    return allocations


# ---------- Rx CRUD ----------


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

    for line_data in data.lines:
        _add_rx_line_internal(db, rx, line_data)

    db.commit()
    db.refresh(rx)
    return rx


def _add_rx_line_internal(
    db: Session,
    rx: PharmacyPrescription,
    line_data: RxLineCreate,
) -> PharmacyPrescriptionLine:
    item: InventoryItem | None = db.get(InventoryItem, line_data.item_id)
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
        requested_qty=line_data.requested_qty,
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
        item_form=item.form,
        item_strength=item.strength,
        item_type="consumable" if item.is_consumable else "drug",
    )
    db.add(line)
    return line


def update_prescription(
    db: Session,
    rx_id: int,
    data: PrescriptionUpdate,
    current_user: User,
) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).get(rx_id))
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

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


def add_rx_line(
    db: Session,
    rx_id: int,
    line_data: RxLineCreate,
    current_user: User,
) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).get(rx_id))
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lines can only be added while prescription is DRAFT.",
        )

    _add_rx_line_internal(db, rx, line_data)
    db.commit()
    db.refresh(rx)
    return rx


def update_rx_line(
    db: Session,
    line_id: int,
    data: RxLineUpdate,
    current_user: User,
) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, line_id)
    if not line:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rx line not found.",
        )
    rx = db.get(PharmacyPrescription, line.prescription_id)
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rx lines can only be edited while prescription is DRAFT.",
        )

    if data.requested_qty is not None:
        if line.dispensed_qty and data.requested_qty < line.dispensed_qty:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="requested_qty cannot be less than dispensed_qty.",
            )
        line.requested_qty = data.requested_qty

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
        value = getattr(data, field)
        if value is not None:
            setattr(line, field, value)

    if data.status is not None:
        if data.status not in ("WAITING", "CANCELLED"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Line status can only be WAITING or CANCELLED.",
            )
        if line.dispensed_qty and data.status == "CANCELLED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot cancel a line that has already been dispensed.",
            )
        line.status = data.status

    db.commit()
    db.refresh(rx)
    return rx


def delete_rx_line(
    db: Session,
    line_id: int,
    current_user: User,
) -> PharmacyPrescription:
    line = db.get(PharmacyPrescriptionLine, line_id)
    if not line:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rx line not found.",
        )
    rx = db.get(PharmacyPrescription, line.prescription_id)
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    if rx.status != "DRAFT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rx lines can only be deleted while prescription is DRAFT.",
        )

    db.delete(line)
    db.commit()
    db.refresh(rx)
    return rx


def sign_prescription(
    db: Session,
    rx_id: int,
    current_user: User,
) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).get(rx_id))
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

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

    rx.status = "ISSUED"
    rx.signed_at = datetime.utcnow()
    rx.signed_by_id = current_user.id

    db.commit()
    db.refresh(rx)
    return rx


def cancel_prescription(
    db: Session,
    rx_id: int,
    reason: str,
    current_user: User,
) -> PharmacyPrescription:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).get(rx_id))
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    if any(Decimal(l.dispensed_qty or 0) > 0 for l in rx.lines):
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


# ---------- Sale helpers ----------


def _recalc_sale_totals(sale: PharmacySale) -> None:
    gross = Decimal("0")
    tax_total = Decimal("0")
    discount_total = Decimal("0")

    for item in sale.items:
        discount_total += Decimal(item.discount_amount or 0)
        gross += Decimal(item.line_amount or 0)
        tax_total += Decimal(item.tax_amount or 0)

    sale.gross_amount = _round_money(gross)
    sale.total_tax = _round_money(tax_total)
    sale.discount_amount_total = _round_money(discount_total)
    sale.net_amount = _round_money(gross + tax_total - discount_total)
    sale.rounding_adjustment = _round_money(sale.net_amount) - sale.net_amount
    sale.net_amount = _round_money(sale.net_amount + sale.rounding_adjustment)


def _update_payment_status(db: Session, sale: PharmacySale) -> None:
    total_paid = (db.query(
        func.coalesce(func.sum(PharmacyPayment.amount),
                      0)).filter(PharmacyPayment.sale_id == sale.id).scalar())
    total_paid = Decimal(total_paid or 0)
    if total_paid <= 0:
        sale.payment_status = "UNPAID"
    elif total_paid < sale.net_amount:
        sale.payment_status = "PARTIALLY_PAID"
    else:
        sale.payment_status = "PAID"


def _generate_invoice_number(db: Session):
    """
    Generate an invoice_number like INV-YYYYMMDD-0001
    Only if Invoice has invoice_number column.
    """
    col = getattr(Invoice, "invoice_number", None)
    if col is None:
        return None  # model doesn't have invoice_number

    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"INV-{today_str}"

    last = (db.query(col).filter(col.like(f"{prefix}-%")).order_by(
        col.desc()).first())
    seq = 1
    if last and last[0]:
        try:
            seq = int(str(last[0]).split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"{prefix}-{seq:04d}"


def _create_billing_invoice_for_sale(
    db: Session,
    sale: PharmacySale,
    current_user: User,
) -> Invoice:
    """
    Create (or reuse) a Billing.Invoice for a PharmacySale.

    - 1 PharmacySale -> 1 Invoice (no duplicates)
    - Invoice status starts as 'draft' (NO auto-finalize)
    - billing_type='pharmacy' so Billing console can filter by Pharmacy
    - context_type='pharmacy_sale', context_id = sale.id
    """

    # 1) Try reuse via FK on sale
    if hasattr(sale, "billing_invoice_id") and sale.billing_invoice_id:
        existing = db.query(Invoice).get(sale.billing_invoice_id)
        if existing:
            return existing

    # 2) Try reuse via context_type/context_id (older data before FK existed)
    existing = (db.query(Invoice).filter(
        Invoice.context_type == "pharmacy_sale",
        Invoice.context_id == sale.id,
    ).first())
    if existing:
        if hasattr(sale, "billing_invoice_id"):
            sale.billing_invoice_id = existing.id
        return existing

    # 3) Create fresh invoice
    gross = Decimal(str(getattr(sale, "gross_amount", 0) or 0))
    # some older schemas use total_amount instead of gross_amount
    if not gross:
        gross = Decimal(str(getattr(sale, "total_amount", 0) or 0))

    tax = Decimal(str(getattr(sale, "total_tax", 0) or 0))
    discount_total = Decimal(
        str(getattr(sale, "discount_amount_total", 0) or 0))
    net = Decimal(str(getattr(sale, "net_amount", 0) or 0))
    if not net:
        net = gross + tax - discount_total

    inv_kwargs: dict[str, Any] = dict(
        patient_id=sale.patient_id,
        context_type="pharmacy_sale",
        context_id=sale.id,
        status="draft",  # 👈 DRAFT; finalize from Billing module
        gross_total=float(gross),
        tax_total=float(tax),
        discount_total=float(discount_total),
        net_total=float(net),
        amount_paid=0.0,
        balance_due=float(net),
    )

    if hasattr(Invoice, "billing_type"):
        inv_kwargs["billing_type"] = "pharmacy"

    inv = Invoice(**inv_kwargs)

    if hasattr(inv, "created_by"):
        inv.created_by = getattr(current_user, "id", None)

    inv_no = _generate_invoice_number(db)
    if inv_no and hasattr(inv, "invoice_number"):
        inv.invoice_number = inv_no

    db.add(inv)
    db.flush()  # get inv.id

    # Link back to sale
    if hasattr(sale, "billing_invoice_id"):
        sale.billing_invoice_id = inv.id

    # 4) Create InvoiceItems from PharmacySaleItems
    sale_items: List[PharmacySaleItem] = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())
    seq = 1
    for it in sale_items:
        qty = Decimal(str(it.quantity or 0))
        unit_price = Decimal(str(it.unit_price or 0))
        tax_percent = Decimal(str(it.tax_percent or 0))

        line_amount = Decimal(str(it.line_amount or (qty * unit_price)))
        tax_amount = Decimal(
            str(it.tax_amount or (line_amount * tax_percent / Decimal("100"))))
        total_line = Decimal(str(it.total_amount
                                 or (line_amount + tax_amount)))
        discount_amt = Decimal(str(it.discount_amount or 0))

        inv_item = InvoiceItem(
            invoice_id=inv.id,
            seq=seq,
            service_type="pharmacy",
            service_ref_id=it.id,  # link back to sale item
            description=it.item_name or "",
            quantity=float(qty),
            unit_price=float(unit_price),
            tax_rate=float(tax_percent),
            discount_percent=0.0,  # discount handled via discount_amount
            discount_amount=float(discount_amt),
            tax_amount=float(tax_amount),
            line_total=float(total_line),
            is_voided=False,
        )

        if hasattr(inv_item, "created_by"):
            inv_item.created_by = getattr(current_user, "id", None)

        db.add(inv_item)
        seq += 1

    return inv


# ---------- Dispense from Rx (with optional sale) ----------

# FILE: app/services/pharmacy.py


def dispense_from_rx(
    db: Session,
    rx_id: int,
    payload: DispenseFromRxIn,
    current_user: User,
) -> tuple[PharmacyPrescription, PharmacySale | None]:
    rx = (db.query(PharmacyPrescription).options(
        selectinload(PharmacyPrescription.lines)).get(rx_id))
    if not rx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prescription not found.",
        )

    # allow DRAFT, ISSUED, PARTIALLY_DISPENSED
    allowed_statuses = {"DRAFT", "ISSUED", "PARTIALLY_DISPENSED"}
    if rx.status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Only DRAFT, ISSUED or PARTIALLY_DISPENSED "
                    "prescriptions can be dispensed."),
        )

    # auto-upgrade DRAFT to ISSUED on first dispense
    if rx.status == "DRAFT":
        rx.status = "ISSUED"

    # location is optional – if missing, dispense will NOT touch inventory,
    # but billing will still work using item default MRP/tax.
    location_id = payload.location_id or rx.location_id

    line_map = {l.id: l for l in rx.lines}
    lines_to_process: List[tuple[PharmacyPrescriptionLine, Decimal]] = []

    for entry in payload.lines:
        line = line_map.get(entry.line_id)
        if not line:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Rx line {entry.line_id} not found in prescription.",
            )
        if line.status in ("DISPENSED", "CANCELLED"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Rx line {line.id} is {line.status} "
                        "and cannot be dispensed."),
            )

        remaining = Decimal(line.requested_qty or 0) - Decimal(
            line.dispensed_qty or 0)
        if entry.dispense_qty > remaining:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Dispense quantity {entry.dispense_qty} "
                        f"exceeds remaining {remaining} for line {line.id}."),
            )
        if entry.dispense_qty <= 0:
            continue

        lines_to_process.append((line, entry.dispense_qty))

    if not lines_to_process:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid lines to dispense.",
        )

    # Create sale header if required
    sale: PharmacySale | None = None
    context_type = (payload.context_type or rx.type or "COUNTER").upper()

    if payload.create_sale:
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
        db.flush()  # get sale.id

    # Dispense each line + build sale items
    for line, disp_qty in lines_to_process:
        item: InventoryItem | None = db.get(InventoryItem, line.item_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Inventory item {line.item_id} not found.",
            )

        # --- STOCK ALLOCATION ---
        if location_id:
            # Linked to inventory: FEFO allocation & StockTransaction
            allocations = _allocate_stock_fefo(
                db=db,
                location_id=location_id,
                item=item,
                qty=disp_qty,
                patient_id=rx.patient_id,
                visit_id=rx.visit_id,
                ipd_admission_id=rx.ipd_admission_id,
                ref_type="PHARMACY_RX",
                ref_id=line.id,
                user=current_user,
            )
        else:
            # Not linked to inventory – no stock deduction
            # but still need pricing for billing, so use defaults.
            mrp = Decimal(item.default_mrp or 0)
            tax_percent = Decimal(item.default_tax_percent or 0)
            allocations = [
                AllocatedBatch(
                    batch=None,
                    qty=disp_qty,
                    mrp=mrp,
                    tax_percent=tax_percent,
                    stock_txn=None,
                )
            ]

        # Update dispensed qty & line status
        line.dispensed_qty = Decimal(line.dispensed_qty or 0) + disp_qty
        if line.dispensed_qty >= line.requested_qty:
            line.status = "DISPENSED"
        else:
            line.status = "PARTIAL"

        # Build sale items from allocations
        if sale:
            for alloc in allocations:
                line_amount = _round_money(alloc.qty * alloc.mrp)
                tax_amount = _round_money(line_amount * alloc.tax_percent /
                                          Decimal(100))
                total_amount = line_amount + tax_amount

                batch = alloc.batch
                stock_txn = alloc.stock_txn

                sale_item = PharmacySaleItem(
                    sale_id=sale.id,
                    rx_line_id=line.id,
                    item_id=item.id,
                    batch_id=batch.id if batch else None,
                    item_name=item.name,
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

    # ---------- After processing all lines ----------

    # Header Rx status
    if all(l.status in ("DISPENSED", "CANCELLED") for l in rx.lines):
        rx.status = "DISPENSED"
    else:
        rx.status = "PARTIALLY_DISPENSED"

    # Recalc sale totals and push to Billing as a DRAFT invoice
    if sale:
        _recalc_sale_totals(sale)
        db.flush()  # ensure sale/items IDs
        _create_billing_invoice_for_sale(db, sale, current_user)

    db.commit()
    db.refresh(rx)
    if sale:
        db.refresh(sale)

    return rx, sale


# ---------- Counter sale (Counter Rx + invoice in one shot) ----------


def create_counter_sale(
    db: Session,
    payload: CounterSaleCreateIn,
    current_user: User,
) -> tuple[PharmacyPrescription, PharmacySale]:
    """
    Creates a COUNTER prescription internally, signs it, dispenses full qty,
    and generates a PharmacySale in one go.
    """
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
            ) for item in payload.items
        ],
    )
    rx = create_prescription(db, rx_data, current_user)
    rx = sign_prescription(db, rx.id, current_user)

    lines = [{
        "line_id": l.id,
        "dispense_qty": l.requested_qty
    } for l in rx.lines if l.status != "CANCELLED"]
    dispense_payload = DispenseFromRxIn(
        location_id=payload.location_id,
        lines=lines,
        create_sale=True,
        context_type="COUNTER",
    )
    rx, sale = dispense_from_rx(db, rx.id, dispense_payload, current_user)
    assert sale is not None
    return rx, sale


# ---------- Sale operations ----------


def finalize_sale(
    db: Session,
    sale_id: int,
    current_user: User,
) -> PharmacySale:
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale_id))
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pharmacy sale not found.",
        )

    if sale.invoice_status == "CANCELLED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cancelled sale cannot be finalized.",
        )

    if sale.invoice_status == "FINALIZED":
        return sale

    _recalc_sale_totals(sale)
    sale.invoice_status = "FINALIZED"

    db.commit()
    db.refresh(sale)
    return sale


def cancel_sale(
    db: Session,
    sale_id: int,
    reason: str,
    current_user: User,
) -> PharmacySale:
    sale = (db.query(PharmacySale).options(selectinload(
        PharmacySale.items)).get(sale_id))
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pharmacy sale not found.",
        )

    if sale.invoice_status == "CANCELLED":
        return sale

    _recalc_sale_totals(sale)
    _update_payment_status(db, sale)
    if sale.payment_status == "PAID":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel fully paid sale without supervisor override.",
        )

    sale.invoice_status = "CANCELLED"
    sale.cancel_reason = reason
    sale.cancelled_at = datetime.utcnow()
    sale.cancelled_by_id = current_user.id

    db.commit()
    db.refresh(sale)
    return sale


def add_payment_to_sale(
    db: Session,
    sale_id: int,
    payload: PaymentCreate,
    current_user: User,
) -> PharmacyPayment:
    sale = db.get(PharmacySale, sale_id)
    if not sale:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pharmacy sale not found.",
        )

    if sale.invoice_status != "FINALIZED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payments can only be added to FINALIZED sales.",
        )

    paid_on = payload.paid_on or datetime.utcnow()
    payment = PharmacyPayment(
        sale_id=sale.id,
        amount=_round_money(payload.amount),
        mode=payload.mode,
        reference=payload.reference,
        paid_on=paid_on,
        note=payload.note,
        created_by_id=current_user.id,
    )
    db.add(payment)

    _update_payment_status(db, sale)

    db.commit()
    db.refresh(payment)
    db.refresh(sale)
    return payment
