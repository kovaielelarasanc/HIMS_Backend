# FILE: app/api/routes_billing.py
from __future__ import annotations

from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pydantic import ValidationError
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.billing import (
    Invoice,
    InvoiceItem,
    Payment,
    Advance,
    BillingProvider,
)
from app.schemas.billing import (
    InvoiceCreate,
    AddServiceIn,
    ManualItemIn,
    UpdateItemIn,
    VoidItemIn,
    PaymentIn,
    BulkAddFromUnbilledIn,
    InvoiceOut,
    InvoiceItemOut,
    PaymentOut,
)
import logging
import io
from app.models.ipd import IpdPackage
from app.models.payer import Payer, Tpa, CreditPlan
from app.models.payer import CreditProvider  # optional / legacy

router = APIRouter()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permissions helper
# ---------------------------------------------------------------------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []):
        for p in getattr(r, "permissions", []):
            if p.code == code:
                return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def recalc_totals(inv: Invoice) -> None:
    """Recalculate invoice totals based on non-voided items & payments."""
    gross = 0.0
    tax_total = 0.0
    discount_total = 0.0

    for it in inv.items:
        if it.is_voided:
            continue
        # line_total is after discount + tax
        gross += float(it.line_total or 0)
        tax_total += float(it.tax_amount or 0)
        discount_total += float(it.discount_amount or 0)

    inv.gross_total = gross
    inv.tax_total = tax_total
    inv.discount_total = discount_total

    # header net_total = sum of item net
    inv.net_total = gross

    paid = 0.0
    for pay in inv.payments:
        paid += float(pay.amount or 0)
    inv.amount_paid = paid
    inv.balance_due = float(inv.net_total or 0) - paid


def serialize_invoice(inv: Invoice) -> InvoiceOut:
    """Convert ORM invoice to Pydantic InvoiceOut, including items & payments."""
    items_out: List[InvoiceItemOut] = [
        InvoiceItemOut.model_validate(it, from_attributes=True)
        for it in inv.items
    ]
    pays_out: List[PaymentOut] = [
        PaymentOut.model_validate(pay, from_attributes=True)
        for pay in inv.payments
    ]

    data = InvoiceOut.model_validate(inv, from_attributes=True)
    data.items = items_out
    data.payments = pays_out
    return data


def _apply_unbilled_services(
    invoice_id: int,
    payload: Optional[BulkAddFromUnbilledIn],
    db: Session,
    user: User,
) -> InvoiceOut:
    """
    Shared logic for adding unbilled services to an invoice.
    Currently placeholder so FE doesn't break.
    """
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # TODO: Implement actual fetch using payload.uids from OPD/LIS/RIS modules
    _ = (payload.uids if payload else None) or []

    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


def _money(x: Any) -> str:
    return f"{float(x or 0):.2f}"


def _next_seq_for_invoice(db: Session, invoice_id: int) -> int:
    """
    Next line sequence number for given invoice.
    Uses MAX(seq) + 1, scoped per invoice_id.
    """
    max_seq = (db.query(func.max(InvoiceItem.seq)).filter(
        InvoiceItem.invoice_id == invoice_id).scalar())
    return (max_seq or 0) + 1


def _manual_ref_for_invoice(invoice_id: int, seq: int) -> int:
    """
    Generate a synthetic unique service_ref_id for manual items.

    This ensures (service_type='manual', service_ref_id, is_voided=0)
    is always unique globally.
    """
    return invoice_id * 1_000_000 + seq


def _generate_invoice_number(db: Session) -> str:
    """
    Optional helper: generate invoice_number if Invoice model has that column.

    Format: INV-YYYYMMDD-XXXX
    """
    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"INV-{today_str}"
    last = (db.query(Invoice.invoice_number).filter(
        Invoice.invoice_number.like(f"{prefix}-%")).order_by(
            Invoice.invoice_number.desc()).first())
    seq = 1
    if last and last[0]:
        try:
            seq = int(last[0].split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"{prefix}-{seq:04d}"


# ---------------------------------------------------------------------------
# Core Invoices
# ---------------------------------------------------------------------------
@router.post("/invoices", response_model=InvoiceOut)
def create_invoice(
        payload: InvoiceCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create a billing invoice (OP/IP/Pharmacy/Lab/Radiology/General).
    """
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = Invoice(
        patient_id=payload.patient_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
    )

    payload_data = payload.model_dump(exclude_unset=True)

    # Direct fields present on Invoice model
    for key in (
            "billing_type",
            "consultant_id",
            "remarks",
            "provider_id",
            "visit_no",
    ):
        if hasattr(inv, key) and key in payload_data:
            setattr(inv, key, payload_data[key])

    # Optional mapping: visit_id -> visit_no (string) if FE sends visit_id
    if "visit_id" in payload_data and hasattr(inv, "visit_no"):
        inv.visit_no = str(payload_data["visit_id"])

    inv.status = "draft"
    inv.gross_total = 0
    inv.tax_total = 0
    inv.discount_total = 0
    inv.net_total = 0
    inv.amount_paid = 0
    inv.balance_due = 0
    inv.created_by = user.id

    # Optional invoice_number auto-generate if model has that column
    if hasattr(inv,
               "invoice_number") and not getattr(inv, "invoice_number", None):
        try:
            inv.invoice_number = _generate_invoice_number(db)
        except Exception:
            # if anything fails, keep it None / default
            pass

    db.add(inv)
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize_invoice(inv)


@router.get("/invoices", response_model=List[InvoiceOut])
def list_invoices(
        patient_id: Optional[int] = None,
        billing_type: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    List invoices with optional filters.
    Supports: patient_id, billing_type, status, date range.
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(Invoice)
    if patient_id:
        q = q.filter(Invoice.patient_id == patient_id)
    if billing_type and hasattr(Invoice, "billing_type"):
        q = q.filter(Invoice.billing_type == billing_type)
    if status:
        q = q.filter(Invoice.status == status)
    if from_date:
        q = q.filter(Invoice.created_at >= datetime.combine(
            from_date, datetime.min.time()))
    if to_date:
        q = q.filter(Invoice.created_at <= datetime.combine(
            to_date, datetime.max.time()))

    q = q.order_by(Invoice.id.desc()).limit(500)
    invs = q.all()
    return [serialize_invoice(inv) for inv in invs]


@router.put("/invoices/{invoice_id}", response_model=InvoiceOut)
def update_invoice(
        invoice_id: int,
        payload: dict = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Update header fields of invoice (consultant, credit, remarks, etc.).
    Uses loose dict body so you can send extended fields safely.
    """
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")

    for k, v in payload.items():
        if hasattr(inv, k):
            setattr(inv, k, v)

    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/finalize", response_model=InvoiceOut)
def finalize_invoice(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Lock invoice; no structural edits allowed after this (only payments).
    """
    if not has_perm(user, "billing.finalize"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")

    recalc_totals(inv)
    inv.status = "finalized"
    inv.finalized_at = datetime.utcnow()
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/cancel")
def cancel_invoice(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.finalize"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")
    inv.status = "cancelled"
    inv.cancelled_at = datetime.utcnow()
    inv.updated_by = user.id
    db.commit()
    return {"message": "Cancelled"}


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
@router.post("/invoices/{invoice_id}/items/manual", response_model=InvoiceOut)
def add_manual_item(
        invoice_id: int,
        payload: ManualItemIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ---- IMPORTANT: make service_ref_id unique for manual lines ----
    service_type = (payload.service_type or "manual").strip() or "manual"

    if payload.service_ref_id and payload.service_ref_id > 0:
        service_ref_id = payload.service_ref_id
    else:
        # For manual items, generate a unique synthetic ref id
        max_ref = (db.query(func.max(InvoiceItem.service_ref_id)).filter(
            InvoiceItem.service_type == "manual").scalar()) or 0
        service_ref_id = int(max_ref) + 1

    it = InvoiceItem(
        invoice_id=invoice_id,
        service_type=service_type,
        service_ref_id=service_ref_id,
        description=(payload.description or "").strip(),
        quantity=payload.quantity or 1,
        unit_price=payload.unit_price,
        tax_rate=payload.tax_rate or 0,
        discount_percent=payload.discount_percent or 0,
        discount_amount=payload.discount_amount or 0,
        created_by=user.id,
    )

    qty = float(it.quantity or 0)
    price = float(it.unit_price or 0)
    discount_amount = float(it.discount_amount or 0)
    tax_rate = float(it.tax_rate or 0)

    base = (qty * price) - discount_amount
    tax_amt = base * (tax_rate / 100.0)
    it.tax_amount = tax_amt
    it.line_total = base + tax_amt

    db.add(it)
    db.flush()  # service_ref_id uniqueness prevents duplicate constraint

    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/items/service", response_model=InvoiceOut)
def add_service_item(
        invoice_id: int,
        payload: AddServiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Add service-based item (OPD visit, Lab test, Radiology, OT, etc).
    Actual price lookup you can hook from other modules.
    """
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    seq = _next_seq_for_invoice(db, invoice_id)

    desc = (payload.description
            or f"{payload.service_type.upper()} #{payload.service_ref_id}")

    qty = float(payload.quantity or 1)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be > 0")

    unit_price = float((getattr(payload, "unit_price", None) or 0))
    tax_rate = float(payload.tax_rate or 0)
    discount_percent = float(payload.discount_percent or 0)
    discount_amount = float(payload.discount_amount or 0)

    base = qty * unit_price

    # Same discount handling as manual
    if discount_percent and not discount_amount:
        discount_amount = round(base * discount_percent / 100.0, 2)
    elif discount_amount and not discount_percent and base:
        discount_percent = round((discount_amount / base) * 100.0, 2)

    taxable = base - discount_amount
    tax_amt = round(taxable * (tax_rate / 100.0), 2)
    line_total = round(taxable + tax_amt, 2)

    it = InvoiceItem(
        invoice_id=invoice_id,
        seq=seq,
        service_type=payload.service_type,
        service_ref_id=payload.service_ref_id,
        description=desc,
        quantity=int(qty),
        unit_price=unit_price,
        tax_rate=tax_rate,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        tax_amount=tax_amt,
        line_total=line_total,
        is_voided=False,
        created_by=user.id,
    )

    db.add(it)
    db.flush()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.put(
    "/invoices/{invoice_id}/items/{item_id}",
    response_model=InvoiceOut,
)
def update_item(
        invoice_id: int,
        item_id: int,
        payload: UpdateItemIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Item not found")

    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(it, k, v)

    qty = float(it.quantity or 0)
    price = float(it.unit_price or 0)
    tax_rate = float(it.tax_rate or 0)
    discount_percent = float(it.discount_percent or 0)
    discount_amount = float(it.discount_amount or 0)

    base = qty * price

    # Re-sync discount fields if only one changed
    if discount_percent and not discount_amount:
        discount_amount = round(base * discount_percent / 100.0, 2)
        it.discount_amount = discount_amount
    elif discount_amount and not discount_percent and base:
        discount_percent = round((discount_amount / base) * 100.0, 2)
        it.discount_percent = discount_percent

    taxable = base - discount_amount
    tax_amt = round(taxable * (tax_rate / 100.0), 2)
    it.tax_amount = tax_amt
    it.line_total = taxable + tax_amt
    it.updated_by = user.id

    db.commit()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post(
    "/invoices/{invoice_id}/items/{item_id}/void",
    response_model=InvoiceOut,
)
def void_item(
        invoice_id: int,
        item_id: int,
        payload: VoidItemIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Item not found")

    it.is_voided = True
    it.void_reason = payload.reason
    it.voided_by = user.id
    it.voided_at = datetime.utcnow()

    db.commit()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


# ---- UNBILLED SERVICES ----
@router.post(
    "/invoices/{invoice_id}/items/bulk-from-unbilled",
    response_model=InvoiceOut,
)
def bulk_add_from_unbilled(
        invoice_id: int,
        payload: BulkAddFromUnbilledIn = Body(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Old path kept for compatibility.
    """
    return _apply_unbilled_services(invoice_id, payload, db, user)


@router.get("/invoices/{invoice_id}/unbilled", response_model=List[dict])
def fetch_unbilled_services(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    New FE expects this path. For now, return empty list so screen won't crash.
    Later you can plug in OPD/LIS/RIS unbilled services.
    """
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # TODO: implement real lookup
    return []


@router.post(
    "/invoices/{invoice_id}/unbilled/bulk-add",
    response_model=InvoiceOut,
)
def bulk_add_from_unbilled_alias(
        invoice_id: int,
        payload: BulkAddFromUnbilledIn = Body(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Alias for FE: POST /billing/invoices/{id}/unbilled/bulk-add
    """
    return _apply_unbilled_services(invoice_id, payload, db, user)


# ---------------------------------------------------------------------------
# Payments & Advances
# ---------------------------------------------------------------------------
@router.post("/invoices/{invoice_id}/payments/bulk", response_model=InvoiceOut)
def add_payments_bulk(
        invoice_id: int,
        payload: dict,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Add one or more payments to invoice.

    Body (any ONE of these is OK):
      { "payments": [ {amount, mode, reference_no?}, ... ] }
      [ {amount, mode, reference_no?}, ... ]    # (just array, no wrapper)
    """
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    raw = payload
    # Allow plain list body also
    if isinstance(raw, list):
        raw_payments = raw
    else:
        raw_payments = raw.get("payments") or []

    if not raw_payments:
        raise HTTPException(status_code=400, detail="No payments provided")

    parsed: list[PaymentIn] = []
    for p in raw_payments:
        if isinstance(p, PaymentIn):
            parsed.append(p)
        else:
            try:
                parsed.append(PaymentIn(**p))
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors())

    for p in parsed:
        pay = Payment(
            invoice_id=invoice_id,
            amount=p.amount,
            mode=p.mode,
            reference_no=p.reference_no,
            notes=p.notes,
            created_by=user.id,
        )
        db.add(pay)

    db.flush()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/payments", response_model=InvoiceOut)
def add_payment(
        invoice_id: int,
        payload: dict = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    FE helper: add a single payment.

    Accepts BOTH:
      - { "amount": 500, "mode": "cash", "reference_no": "REC-001" }
      - { "payments": [ {amount, mode, reference_no?}, ... ] }  --> forwarded to bulk handler

    For refunds, send NEGATIVE amount:
      { "amount": -500, "mode": "cash_refund", "reference_no": "REF-001" }
    """
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # If caller accidentally sends bulk shape to this URL, just forward:
    if isinstance(payload, dict) and "payments" in payload:
        return add_payments_bulk(invoice_id, payload, db, user)

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    try:
        p = PaymentIn(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    pay = Payment(
        invoice_id=invoice_id,
        amount=p.amount,
        mode=p.mode,
        reference_no=p.reference_no,
        notes=p.notes,
        created_by=user.id,
    )

    db.add(pay)
    db.flush()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.delete(
    "/invoices/{invoice_id}/payments/{payment_id}",
    response_model=InvoiceOut,
)
def delete_payment(
        invoice_id: int,
        payment_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    FE helper: delete a payment row.
    """
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    pay = db.query(Payment).get(payment_id)
    if not pay or pay.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    db.delete(pay)
    db.commit()

    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/advances", response_model=dict)
def create_advance(
        payload: dict = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create a patient advance (IP/OP advance).
    Expected keys: patient_id, amount, mode, reference_no?, remarks?
    """
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient_id = payload.get("patient_id")
    amount = payload.get("amount")
    mode = payload.get("mode")
    if not patient_id or not amount or not mode:
        raise HTTPException(status_code=400, detail="Missing required fields")

    adv = Advance(
        patient_id=patient_id,
        amount=amount,
        balance_remaining=amount,
        mode=mode,
        reference_no=payload.get("reference_no"),
        remarks=payload.get("remarks"),
        created_by=user.id,
    )
    db.add(adv)
    db.commit()
    db.refresh(adv)
    return {
        "id": adv.id,
        "patient_id": adv.patient_id,
        "amount": float(adv.amount or 0),
        "balance_remaining": float(adv.balance_remaining or 0),
        "mode": adv.mode,
    }


@router.get("/advances", response_model=List[dict])
def list_advances(
        patient_id: Optional[int] = None,
        only_with_balance: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(Advance)
    if patient_id:
        q = q.filter(Advance.patient_id == patient_id)
    if only_with_balance:
        q = q.filter(Advance.balance_remaining > 0)

    q = q.order_by(Advance.id.desc())
    res = []
    for adv in q.all():
        res.append({
            "id":
            adv.id,
            "patient_id":
            adv.patient_id,
            "amount":
            float(adv.amount or 0),
            "balance_remaining":
            float(adv.balance_remaining or 0),
            "mode":
            adv.mode,
            "reference_no":
            adv.reference_no,
            "remarks":
            adv.remarks,
            "created_at":
            adv.created_at.isoformat()
            if getattr(adv, "created_at", None) else None,
        })
    return res


@router.post("/invoices/{invoice_id}/apply-advances",
             response_model=InvoiceOut)
def apply_advances_to_invoice(
        invoice_id: int,
        payload: dict = Body(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Apply available patient advances to reduce invoice balance.
    If payload has "advance_ids", use only those; else auto-apply oldest first.
    """
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if inv.balance_due is None:
        recalc_totals(inv)

    remaining = float(inv.balance_due or 0)
    if remaining <= 0:
        return serialize_invoice(inv)

    patient_id = inv.patient_id
    advance_ids = (payload or {}).get("advance_ids")

    q = db.query(Advance).filter(
        Advance.patient_id == patient_id,
        Advance.balance_remaining > 0,
    )
    if advance_ids:
        q = q.filter(Advance.id.in_(advance_ids))

    advances = q.order_by(Advance.created_at.asc()).all()
    if not advances:
        return serialize_invoice(inv)

    for adv in advances:
        if remaining <= 0:
            break
        avail = float(adv.balance_remaining or 0)
        if avail <= 0:
            continue
        use = min(avail, remaining)

        pay = Payment(
            invoice_id=invoice_id,
            amount=use,
            mode="advance",
            reference_no=f"ADV-{adv.id}",
            created_by=user.id,
        )
        db.add(pay)

        adv.balance_remaining = avail - use
        remaining -= use

    db.flush()
    db.refresh(inv)
    recalc_totals(inv)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


# ---------------------------------------------------------------------------
# Billing masters: doctors + credit/TPA providers + packages
# ---------------------------------------------------------------------------
@router.get("/masters")
def billing_masters(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Used by Billing screens:
      - list of doctors (consultant)
      - list of credit providers (TPA/Insurance/Corporate)
      - list of payers, TPAs, credit plans
      - IP packages (for package billing)
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # Avoid name clash with the type-hint User
    from app.models.user import User as UserModel

    # ----- Doctors -----
    doctors_q = db.query(UserModel).filter(UserModel.is_active.is_(True))
    if hasattr(UserModel, "is_doctor"):
        doctors_q = doctors_q.filter(UserModel.is_doctor.is_(True))
    doctors_q = doctors_q.order_by(UserModel.name.asc())

    doctors = [{
        "id": d.id,
        "name": d.name,
        "email": d.email,
    } for d in doctors_q.all()]

    # ----- Credit providers (legacy BillingProvider) -----
    providers_q = (db.query(BillingProvider).filter(
        BillingProvider.is_active.is_(True)).order_by(
            BillingProvider.name.asc()))
    credit_providers = [{
        "id": p.id,
        "name": p.name,
        "code": p.code,
        "provider_type": p.provider_type,
    } for p in providers_q.all()]

    # ----- Payers, TPAs, Credit Plans -----
    payers = [{
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "payer_type": p.payer_type,
    } for p in db.query(Payer).order_by(Payer.name.asc()).all()]

    tpas = [{
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "payer_id": t.payer_id,
    } for t in db.query(Tpa).order_by(Tpa.name.asc()).all()]

    credit_plans = [{
        "id": c.id,
        "code": c.code,
        "name": c.name,
        "payer_id": c.payer_id,
        "tpa_id": c.tpa_id,
    } for c in db.query(CreditPlan).order_by(CreditPlan.name.asc()).all()]

    # ----- IP Packages (for package billing) -----
    packages = [{
        "id": pkg.id,
        "name": pkg.name,
        "charges": float(pkg.charges or 0),
    } for pkg in db.query(IpdPackage).order_by(IpdPackage.name.asc()).all()]

    return {
        "doctors": doctors,
        "credit_providers": credit_providers,
        "payers": payers,
        "tpas": tpas,
        "credit_plans": credit_plans,
        "packages": packages,
    }


# ---------------------------------------------------------------------------
# Patient Billing Summary (JSON API for FE)
# ---------------------------------------------------------------------------
@router.get("/patients/{patient_id}/summary", response_model=dict)
def patient_billing_summary(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    JSON summary of patient's complete billing history, including:
    - All invoices
    - Totals
    - AR ageing buckets (0–30 / 31–60 / 61–90 / >90)
    - Revenue by billing_type (OP/IP/Lab/Pharmacy/etc.)
    - Payment mode breakup (cash/card/upi/etc.)
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    invs: List[Invoice] = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id).order_by(
            Invoice.created_at.asc()).all())

    invoices_out: List[Dict[str, Any]] = []
    total_net = total_paid = total_balance = 0.0
    today = datetime.utcnow().date()

    aging = {
        "bucket_0_30": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_31_60": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_61_90": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_90_plus": {
            "count": 0,
            "amount": 0.0
        },
    }

    by_type: Dict[str, Dict[str, float]] = {}

    for inv in invs:
        created = inv.created_at.date() if inv.created_at else today
        net = float(inv.net_total or 0)
        paid = float(inv.amount_paid or 0)
        bal = float(inv.balance_due or 0)

        total_net += net
        total_paid += paid
        total_balance += bal

        billing_type = getattr(inv, "billing_type", None) or "general"
        if billing_type not in by_type:
            by_type[billing_type] = {
                "net_total": 0.0,
                "amount_paid": 0.0,
                "balance_due": 0.0,
            }
        by_type[billing_type]["net_total"] += net
        by_type[billing_type]["amount_paid"] += paid
        by_type[billing_type]["balance_due"] += bal

        if bal > 0:
            days = (today - created).days
            if days <= 30:
                bucket_key = "bucket_0_30"
            elif days <= 60:
                bucket_key = "bucket_31_60"
            elif days <= 90:
                bucket_key = "bucket_61_90"
            else:
                bucket_key = "bucket_90_plus"

            aging[bucket_key]["count"] += 1
            aging[bucket_key]["amount"] += bal

        invoices_out.append({
            "id":
            inv.id,
            "invoice_number":
            getattr(inv, "invoice_number", inv.id),
            "billing_type":
            billing_type,
            "context_type":
            inv.context_type,
            "context_id":
            inv.context_id,
            "status":
            inv.status,
            "net_total":
            net,
            "amount_paid":
            paid,
            "balance_due":
            bal,
            "created_at":
            inv.created_at.isoformat() if inv.created_at else None,
            "finalized_at":
            inv.finalized_at.isoformat() if inv.finalized_at else None,
        })

    pay_rows = (db.query(Payment.mode, func.sum(Payment.amount)).join(
        Invoice, Invoice.id == Payment.invoice_id).filter(
            Invoice.patient_id == patient_id).group_by(Payment.mode).all())
    payment_modes = {mode: float(amount or 0) for mode, amount in pay_rows}

    return {
        "patient": {
            "id": patient.id,
            "uhid": getattr(patient, "uhid", None),
            "name":
            f"{patient.first_name or ''} {patient.last_name or ''}".strip(),
            "phone": patient.phone,
        },
        "invoices": invoices_out,
        "totals": {
            "net_total": total_net,
            "amount_paid": total_paid,
            "balance_due": total_balance,
        },
        "by_billing_type": by_type,
        "ar_aging": aging,
        "payment_modes": payment_modes,
    }


# Alias for FE path: /billing/patient/{id}/summary
@router.get("/patient/{patient_id}/summary", response_model=dict)
def patient_billing_summary_alias(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return patient_billing_summary(patient_id=patient_id, db=db, user=user)


# ---------------------------------------------------------------------------
# Printing: Single Invoice & Patient Billing Summary (PDF/HTML)
# ---------------------------------------------------------------------------
@router.get("/invoices/{invoice_id}/print")
def print_invoice(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Printable view / PDF for a single invoice with items + payments.

    - If WeasyPrint + system deps are OK -> returns PDF
    - If anything fails -> returns HTML (browser can still print)
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv: Invoice | None = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    patient: Patient | None = db.query(Patient).get(inv.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    items = [it for it in inv.items if not it.is_voided]
    payments = list(inv.payments or [])

    created_at = inv.created_at or datetime.utcnow()
    bill_date = created_at.strftime("%d-%m-%Y %H:%M")

    html_items = ""
    for idx, it in enumerate(items, start=1):
        html_items += (
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{(it.description or '').replace('<', '&lt;').replace('>', '&gt;')}</td>"
            f"<td class='money'>{it.quantity}</td>"
            f"<td class='money'>{_money(it.unit_price)}</td>"
            f"<td class='money'>{_money(it.tax_rate)}%</td>"
            f"<td class='money'>{_money(it.tax_amount)}</td>"
            f"<td class='money'>{_money(it.line_total)}</td>"
            "</tr>")
    if not html_items:
        html_items = (
            "<tr><td colspan='7' style='text-align:center;'>No items</td></tr>"
        )

    html_pay = ""
    for idx, pay in enumerate(payments, start=1):
        dt = (pay.paid_at.strftime("%d-%m-%Y %H:%M") if getattr(
            pay, "paid_at", None) else "—")
        html_pay += ("<tr>"
                     f"<td>{idx}</td>"
                     f"<td>{pay.mode}</td>"
                     f"<td>{(pay.reference_no or '')}</td>"
                     f"<td>{dt}</td>"
                     f"<td class='money'>{_money(pay.amount)}</td>"
                     "</tr>")
    if not html_pay:
        html_pay = (
            "<tr><td colspan='5' style='text-align:center;'>No payments</td></tr>"
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Invoice #{inv.id}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      margin: 16px;
      color: #111827;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 12px;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .title {{
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .muted {{
      color: #6b7280;
      font-size: 11px;
    }}
    .section {{
      margin-top: 10px;
      margin-bottom: 8px;
    }}
    .section-title {{
      font-weight: 600;
      font-size: 13px;
      margin-bottom: 4px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 4px;
    }}
    th, td {{
      border: 1px solid #e5e7eb;
      padding: 4px 6px;
      text-align: left;
    }}
    th {{
      background: #f3f4f6;
      font-weight: 600;
      font-size: 11px;
    }}
    td.money {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .totals {{
      margin-top: 6px;
      width: 100%;
      max-width: 280px;
      margin-left: auto;
      border: 1px solid #e5e7eb;
      border-radius: 4px;
    }}
    .totals-row {{
      display: flex;
      justify-content: space-between;
      padding: 4px 8px;
      font-size: 11px;
    }}
    .totals-row.label {{
      background: #f9fafb;
      font-weight: 600;
    }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">Tax Invoice</div>
      <div class="muted">Invoice ID: {inv.id}</div>
      <div class="muted">Invoice No: {getattr(inv, 'invoice_number', inv.id)}</div>
      <div class="muted">Date: {bill_date}</div>
      <div class="muted">Status: {inv.status}</div>
    </div>
    <div style="text-align:right;">
      <div class="section-title">Patient</div>
      <div class="muted">UHID: {getattr(patient, 'uhid', '')}</div>
      <div>{(patient.first_name or "")} {(patient.last_name or "")}</div>
      <div class="muted">ID: {patient.id}</div>
      <div class="muted">Phone: {patient.phone or "—"}</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Bill Details</div>
    <table>
      <thead>
        <tr>
          <th style="width:40px;">S.No</th>
          <th>Particulars</th>
          <th style="width:60px;">Qty</th>
          <th style="width:80px;">Price</th>
          <th style="width:60px;">GST%</th>
          <th style="width:80px;">GST Amt</th>
          <th style="width:90px;">Line Total</th>
        </tr>
      </thead>
      <tbody>
        {html_items}
      </tbody>
    </table>

    <div class="totals">
      <div class="totals-row">
        <span>Gross Amount</span>
        <span class="money">{_money(inv.gross_total)}</span>
      </div>
      <div class="totals-row">
        <span>Tax Total</span>
        <span class="money">{_money(inv.tax_total)}</span>
      </div>
      <div class="totals-row label">
        <span>Net Amount</span>
        <span class="money">{_money(inv.net_total)}</span>
      </div>
      <div class="totals-row">
        <span>Amount Received</span>
        <span class="money">{_money(inv.amount_paid)}</span>
      </div>
      <div class="totals-row">
        <span>Balance Amount</span>
        <span class="money">{_money(inv.balance_due)}</span>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Payments</div>
    <table>
      <thead>
        <tr>
          <th style="width:40px;">#</th>
          <th>Mode</th>
          <th>Reference</th>
          <th style="width:110px;">Paid At</th>
          <th style="width:90px;">Amount</th>
        </tr>
      </thead>
      <tbody>
        {html_pay}
      </tbody>
    </table>
  </div>
</body>
</html>
    """.strip()

    # ---------- PDF (WeasyPrint) with safe fallback ----------
    try:
        from weasyprint import HTML as _HTML  # type: ignore
        HTML = _HTML
    except Exception:
        HTML = None

    if HTML is not None:
        try:
            pdf_bytes = HTML(string=html).write_pdf()
            filename = f"invoice-{invoice_id}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"'
                },
            )
        except Exception as e:
            logger.exception(
                "WeasyPrint PDF generation failed for invoice %s, falling back to HTML: %s",
                invoice_id,
                e,
            )

    # Fallback: return HTML so browser can still show/print
    return StreamingResponse(
        io.BytesIO(html.encode("utf-8")),
        media_type="text/html; charset=utf-8",
    )


@router.get("/patients/{patient_id}/print-summary")
def print_patient_billing_summary(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Printable patient billing history with AR ageing + revenue breakdown.
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    summary = patient_billing_summary(patient_id, db=db, user=user)

    invoices = summary["invoices"]
    totals = summary["totals"]
    by_type = summary["by_billing_type"]
    aging = summary["ar_aging"]
    payment_modes = summary["payment_modes"]

    rows_html = ""
    for idx, inv in enumerate(invoices, start=1):
        rows_html += ("<tr>"
                      f"<td>{idx}</td>"
                      f"<td>{inv['id']}</td>"
                      f"<td>{inv['invoice_number']}</td>"
                      f"<td>{inv['billing_type']}</td>"
                      f"<td>{inv['context_type'] or ''}</td>"
                      f"<td>{(inv['created_at'] or '')[:10]}</td>"
                      f"<td class='money'>{_money(inv['net_total'])}</td>"
                      f"<td class='money'>{_money(inv['amount_paid'])}</td>"
                      f"<td class='money'>{_money(inv['balance_due'])}</td>"
                      f"<td>{inv['status']}</td>"
                      "</tr>")
    if not rows_html:
        rows_html = (
            "<tr><td colspan='10' style='text-align:center;'>No invoices</td></tr>"
        )

    type_rows = ""
    for btype, agg in by_type.items():
        type_rows += ("<tr>"
                      f"<td>{btype}</td>"
                      f"<td class='money'>{_money(agg['net_total'])}</td>"
                      f"<td class='money'>{_money(agg['amount_paid'])}</td>"
                      f"<td class='money'>{_money(agg['balance_due'])}</td>"
                      "</tr>")
    if not type_rows:
        type_rows = (
            "<tr><td colspan='4' style='text-align:center;'>No data</td></tr>")

    aging_rows = ""
    labels = {
        "bucket_0_30": "0–30 days",
        "bucket_31_60": "31–60 days",
        "bucket_61_90": "61–90 days",
        "bucket_90_plus": "> 90 days",
    }
    for key, label in labels.items():
        row = aging.get(key) or {"count": 0, "amount": 0}
        aging_rows += ("<tr>"
                       f"<td>{label}</td>"
                       f"<td class='money'>{row['count']}</td>"
                       f"<td class='money'>{_money(row['amount'])}</td>"
                       "</tr>")

    pay_rows_html = ""
    for mode, amt in payment_modes.items():
        pay_rows_html += ("<tr>"
                          f"<td>{mode}</td>"
                          f"<td class='money'>{_money(amt)}</td>"
                          "</tr>")
    if not pay_rows_html:
        pay_rows_html = (
            "<tr><td colspan='2' style='text-align:center;'>No payments</td></tr>"
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Billing Summary - {summary['patient']['uhid'] or summary['patient']['id']}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      margin: 16px;
      color: #111827;
    }}
    .title {{
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .muted {{
      color: #6b7280;
      font-size: 11px;
    }}
    .section {{
      margin-top: 12px;
      margin-bottom: 8px;
    }}
    .section-title {{
      font-weight: 600;
      font-size: 13px;
      margin-bottom: 4px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 4px;
    }}
    th, td {{
      border: 1px solid #e5e7eb;
      padding: 4px 6px;
      text-align: left;
    }}
    th {{
      background: #f3f4f6;
      font-weight: 600;
      font-size: 11px;
    }}
    td.money {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .totals {{
      display: flex;
      gap: 16px;
      margin-top: 4px;
      font-size: 11px;
    }}
    .pill {{
      padding: 4px 8px;
      border-radius: 999px;
      background: #f3f4f6;
    }}
  </style>
</head>
<body>
  <div class="title">Patient Billing Summary</div>
  <div class="muted">
    UHID: {summary['patient']['uhid'] or '—'} &nbsp;|
    Patient ID: {summary['patient']['id']} &nbsp;|
    Name: {summary['patient']['name']} &nbsp;|
    Phone: {summary['patient']['phone'] or '—'}
  </div>

  <div class="section">
    <div class="section-title">Overall Totals</div>
    <div class="totals">
      <span class="pill">Net Total: {_money(totals['net_total'])}</span>
      <span class="pill">Amount Received: {_money(totals['amount_paid'])}</span>
      <span class="pill">Balance Due: {_money(totals['balance_due'])}</span>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Invoice-wise Details</div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Inv ID</th>
          <th>Invoice No</th>
          <th>Billing Type</th>
          <th>Context</th>
          <th>Date</th>
          <th>Net</th>
          <th>Paid</th>
          <th>Balance</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">Revenue by Billing Type</div>
    <table>
      <thead>
        <tr>
          <th>Billing Type</th>
          <th>Net Total</th>
          <th>Amount Received</th>
          <th>Balance</th>
        </tr>
      </thead>
      <tbody>
        {type_rows}
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">Accounts Receivable Ageing</div>
    <table>
      <thead>
        <tr>
          <th>Bucket</th>
          <th>Invoice Count</th>
          <th>Outstanding Amount</th>
        </tr>
      </thead>
      <tbody>
        {aging_rows}
      </tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">Payment Mode Breakup</div>
    <table>
      <thead>
        <tr>
          <th>Mode</th>
          <th>Total Amount</th>
        </tr>
      </thead>
      <tbody>
        {pay_rows_html}
      </tbody>
    </table>
  </div>
</body>
</html>
    """.strip()

    try:
        from weasyprint import HTML as _HTML  # type: ignore
        HTML = _HTML
    except Exception:
        HTML = None

    if HTML is not None:
        try:
            pdf_bytes = HTML(string=html).write_pdf()
            filename = f"billing-summary-{patient_id}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"'
                },
            )
        except Exception as e:
            logger.exception(
                "WeasyPrint PDF generation failed for billing summary %s, falling back to HTML: %s",
                patient_id,
                e,
            )

    return StreamingResponse(
        io.BytesIO(html.encode("utf-8")),
        media_type="text/html; charset=utf-8",
    )


# Alias for FE path: /billing/patient/{id}/summary/print
@router.get("/patient/{patient_id}/summary/print")
def print_patient_billing_summary_alias(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return print_patient_billing_summary(patient_id=patient_id,
                                         db=db,
                                         user=user)
