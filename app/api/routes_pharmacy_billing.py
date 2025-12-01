# FILE: app/api/routes_pharmacy_billing.py
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from io import BytesIO
from typing import List, Optional, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.pharmacy_prescription import (
    PharmacySale,
    PharmacySaleItem,
    PharmacyPrescription,
)
from app.models.pharmacy_inventory import ItemBatch
from app.services.inventory import adjust_batch_qty, create_stock_transaction
from app.core.emailer import send_email
from app.models.billing import Invoice, InvoiceItem, Payment

router = APIRouter()


# ---------------- RBAC helper ----------------
def _need_any(user: User, codes: List[str]) -> None:
    """
    Require that the user has any one of the given permission codes.
    Admin bypasses this.
    """
    if getattr(user, "is_admin", False):
        return
    roles = getattr(user, "roles", []) or []
    have = {p.code for r in roles for p in (r.permissions or [])}
    if have.intersection(set(codes)):
        return
    raise HTTPException(status_code=403, detail="Not permitted")


def _safe_float(x) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def _full_name(p: Patient) -> str:
    parts = [getattr(p, "first_name", None), getattr(p, "last_name", None)]
    name = " ".join([x for x in parts if x]).strip()
    if name:
        return name
    return (getattr(p, "first_name", "") or getattr(p, "name", "")
            or f"Patient #{p.id}")


def _user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    full_name = getattr(user, "full_name", None)
    if full_name:
        return full_name
    name = getattr(user, "name", None)
    if name:
        return name
    username = getattr(user, "username", None)
    if username:
        return username
    email = getattr(user, "email", None)
    if email:
        return email
    return f"User #{getattr(user, 'id', 'unknown')}"


# ---------------- Pydantic models ----------------
class PharmacySaleSummaryOut(BaseModel):
    id: int
    bill_number: str
    status: str

    patient_id: int
    patient_uhid: Optional[str] = None
    patient_name: str

    total_amount: float
    total_tax: float
    net_amount: float

    payment_mode: Optional[str] = None  # kept for compatibility
    created_at: datetime

    context_type: Optional[str] = None
    visit_id: Optional[int] = None
    admission_id: Optional[int] = None

    # Linked Billing.Invoice + payment summary
    invoice_id: Optional[int] = None
    paid_amount: float = 0.0
    balance_amount: float = 0.0


class PharmacySaleItemOut(BaseModel):
    medicine_id: Optional[int] = None
    medicine_name: Optional[str] = None
    qty: float
    unit_price: float
    tax_percent: float
    amount: float


class PharmacySaleDetailOut(PharmacySaleSummaryOut):
    items: List[PharmacySaleItemOut]


class PharmacyBillStatusUpdateIn(BaseModel):
    payment_status: str  # "paid" | "unpaid" | "partial" | "cancelled"
    paid_amount: Optional[float] = None  # incremental payment for this update
    note: Optional[str] = None


class ConsolidatedLineOut(BaseModel):
    medicine_name: Optional[str] = None
    qty: float
    amount: float


class ConsolidatedIpdInvoiceIn(BaseModel):
    patient_id: int
    admission_id: Optional[int] = None


class ConsolidatedIpdInvoiceOut(BaseModel):
    patient_id: int
    admission_id: Optional[int] = None
    patient_uhid: Optional[str] = None
    patient_name: str

    sale_ids: List[int]

    total_amount: float
    total_tax: float
    net_amount: float

    items: List[ConsolidatedLineOut]


class ReturnLineIn(BaseModel):
    bill_line_id: int
    qty_to_return: float


class PharmacyReturnCreateIn(BaseModel):
    source_invoice_id: int
    lines: List[ReturnLineIn]
    reason: Optional[str] = None


# ---------------- core helpers ----------------
def _sale_bill_datetime_column():
    """
    Column to use for filtering/ordering by bill date.
    Compatible with both old `bill_date` and new `bill_datetime` schemas.
    """
    col = getattr(PharmacySale, "bill_datetime", None)
    if col is None:
        col = getattr(PharmacySale, "bill_date", None)
    if col is None:
        col = PharmacySale.created_at
    return col


def _sale_bill_datetime_value(sale: PharmacySale) -> datetime:
    """
    Concrete datetime for a PharmacySale's bill timestamp.
    """
    dt = getattr(sale, "bill_datetime", None)
    if dt is None:
        dt = getattr(sale, "bill_date", None)
    if dt is None:
        dt = getattr(sale, "created_at", None)
    return dt or datetime.utcnow()


def _sale_summary_from_row(
    sale: PharmacySale,
    patient: Patient,
    admission_id: Optional[int] = None,
    invoice: Optional[Invoice] = None,
) -> PharmacySaleSummaryOut:
    """
    Build a summary row for PharmacySale, enriched with Billing.Invoice data
    (invoice_id, paid_amount, balance_amount) when available.
    """
    created_at = _sale_bill_datetime_value(sale)

    gross_amount = getattr(sale, "gross_amount", None)
    if gross_amount is None:
        gross_amount = getattr(sale, "total_amount", None)

    total_tax = getattr(sale, "total_tax", None)
    net_amount = getattr(sale, "net_amount", None)

    # ---- invoice-derived fields ----
    invoice_id: Optional[int] = None
    paid_amount: float = 0.0
    balance_amount: float = 0.0

    if invoice is not None:
        invoice_id = getattr(invoice, "id", None)
        paid_amount = _safe_float(getattr(invoice, "amount_paid", None))

        if getattr(invoice, "balance_due", None) is not None:
            balance_amount = _safe_float(getattr(invoice, "balance_due", None))
        else:
            balance_amount = _safe_float(net_amount) - paid_amount
    else:
        balance_amount = _safe_float(net_amount) - paid_amount

    # ---- status: derive from invoice if possible ----
    sale_payment_status = (getattr(sale, "payment_status", None) or "").upper()
    sale_invoice_status = (getattr(sale, "invoice_status", None) or "").upper()
    inv_status = (getattr(invoice, "status", None)
                  or "").upper() if invoice else ""

    if sale_invoice_status == "CANCELLED" or inv_status == "CANCELLED":
        status_val = "CANCELLED"
    else:
        n = _safe_float(net_amount)
        p = paid_amount
        if n <= 0:
            status_val = "PAID"
        elif p <= 0:
            status_val = "UNPAID"
        elif 0 < p < n - 0.01:
            status_val = "PARTIAL"
        else:
            status_val = "PAID"

        # fallback if everything is zero
        if not status_val and sale_payment_status:
            status_val = sale_payment_status

    return PharmacySaleSummaryOut(
        id=sale.id,
        bill_number=sale.bill_number,
        status=status_val or "UNPAID",
        patient_id=patient.id,
        patient_uhid=getattr(patient, "uhid", None),
        patient_name=_full_name(patient),
        total_amount=_safe_float(gross_amount),
        total_tax=_safe_float(total_tax),
        net_amount=_safe_float(net_amount),
        payment_mode=None,  # not tracked on PharmacySale model
        created_at=created_at,
        context_type=getattr(sale, "context_type", None),
        visit_id=getattr(sale, "visit_id", None),
        admission_id=admission_id,
        invoice_id=invoice_id,
        paid_amount=paid_amount,
        balance_amount=balance_amount,
    )


def _sale_detail(
    db: Session,
    sale: PharmacySale,
    patient: Patient,
) -> PharmacySaleDetailOut:
    items = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())

    out_items: List[PharmacySaleItemOut] = []
    for it in items:
        out_items.append(
            PharmacySaleItemOut(
                medicine_id=getattr(it, "item_id", None),
                medicine_name=getattr(it, "item_name", None),
                qty=_safe_float(getattr(it, "quantity", None)),
                unit_price=_safe_float(getattr(it, "unit_price", None)),
                tax_percent=_safe_float(getattr(it, "tax_percent", None)),
                amount=_safe_float(
                    getattr(it, "total_amount", None)
                    or getattr(it, "line_amount", None)),
            ))

    # Ensure linked Billing.Invoice exists so we have invoice_id / paid / balance
    inv = (db.query(Invoice).filter(
        Invoice.context_type == "pharmacy_sale",
        Invoice.context_id == sale.id,
    ).first())
    if inv is None:
        inv = _ensure_billing_invoice_for_sale(db, sale)
        db.flush()
        db.refresh(inv)

    summary = _sale_summary_from_row(
        sale,
        patient,
        admission_id=getattr(sale, "ipd_admission_id", None),
        invoice=inv,
    )
    return PharmacySaleDetailOut(**summary.model_dump(), items=out_items)


def _build_sale_pdf(
    sale: PharmacySale,
    patient: Patient,
    items: List[PharmacySaleItem],
) -> bytes:
    """
    Basic A4 pharmacy invoice for printing/email.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Pharmacy Invoice: {sale.bill_number}")
    y -= 20

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Patient: {_full_name(patient)}")
    y -= 15
    if getattr(patient, "uhid", None):
        c.drawString(40, y, f"UHID: {patient.uhid}")
        y -= 15

    bill_dt = _sale_bill_datetime_value(sale)
    c.drawString(40, y, f"Bill date: {bill_dt.strftime('%Y-%m-%d %H:%M')}")
    y -= 15

    status_text = (getattr(sale, "payment_status", None)
                   or getattr(sale, "invoice_status", None) or "")
    c.drawString(40, y, f"Status: {status_text}")
    y -= 25

    # Table header
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Code")
    c.drawString(100, y, "Item")
    c.drawString(320, y, "Qty")
    c.drawString(370, y, "Rate")
    c.drawString(430, y, "Tax%")
    c.drawString(480, y, "Amount")
    y -= 15
    c.setFont("Helvetica", 9)

    total_amount = Decimal("0")
    total_tax = Decimal("0")
    net_amount = Decimal("0")

    for it in items:
        if y < 80:
            c.showPage()
            y = height - 40
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, "Code")
            c.drawString(100, y, "Item")
            c.drawString(320, y, "Qty")
            c.drawString(370, y, "Rate")
            c.drawString(430, y, "Tax%")
            c.drawString(480, y, "Amount")
            y -= 15
            c.setFont("Helvetica", 9)

        qty = Decimal(str(getattr(it, "quantity", 0) or 0))
        unit_price = Decimal(str(getattr(it, "unit_price", 0) or 0))
        tax_percent = Decimal(str(getattr(it, "tax_percent", 0) or 0))
        line_amount = Decimal(str(getattr(it, "line_amount",
                                          qty * unit_price)))
        tax_amount = Decimal(
            str(
                getattr(
                    it,
                    "tax_amount",
                    (line_amount * tax_percent / Decimal("100")),
                )))
        total_line = Decimal(
            str(getattr(it, "total_amount", line_amount + tax_amount)))

        code = getattr(it, "item_id", None) or ""
        name = getattr(it, "item_name", "") or ""

        c.drawString(40, y, str(code))
        c.drawString(100, y, name[:30])
        c.drawRightString(350, y, f"{qty}")
        c.drawRightString(410, y, f"{unit_price:.2f}")
        c.drawRightString(460, y, f"{tax_percent:.2f}")
        c.drawRightString(550, y, f"{total_line:.2f}")
        y -= 14

        total_amount += line_amount
        total_tax += tax_amount
        net_amount += total_line

    y -= 20
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(550, y, f"Total: {total_amount:.2f}")
    y -= 14
    c.drawRightString(550, y, f"Tax:   {total_tax:.2f}")
    y -= 14
    c.drawRightString(550, y, f"Net:   {net_amount:.2f}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ---------------- Billing integration helpers ----------------
def _ensure_billing_invoice_for_sale(
    db: Session,
    sale: PharmacySale,
) -> Invoice:
    """
    Ensure there is a Billing.Invoice + InvoiceItems representing this PharmacySale.

    Idempotent behaviour:
    - If an Invoice already exists with context_type='pharmacy_sale' and context_id = sale.id,
      reuse it.
    - If not, but there are existing InvoiceItems with service_type='pharmacy' pointing to
      this sale's PharmacySaleItem rows, reuse that Invoice (older data style) and, if needed,
      stamp context_type/context_id for future lookups.
    - Only if nothing exists, create a new Invoice and corresponding InvoiceItems.
    """

    # 1) New-style lookup via context_type/context_id
    inv = (db.query(Invoice).filter(
        Invoice.context_type == "pharmacy_sale",
        Invoice.context_id == sale.id,
    ).first())
    if inv:
        return inv

    # 2) Fallback: older invoices found via InvoiceItem.service_ref_id
    sale_item_ids = [
        row[0] for row in db.query(PharmacySaleItem.id).filter(
            PharmacySaleItem.sale_id == sale.id).all()
    ]

    if sale_item_ids:
        existing_item = (db.query(InvoiceItem).filter(
            InvoiceItem.service_type == "pharmacy",
            InvoiceItem.service_ref_id.in_(sale_item_ids),
        ).first())

        if existing_item:
            inv = db.query(Invoice).get(existing_item.invoice_id)
            if inv:
                if not getattr(inv, "context_type", None):
                    inv.context_type = "pharmacy_sale"
                    inv.context_id = sale.id
                elif (inv.context_type == "pharmacy"
                      and getattr(inv, "context_id", None) is None):
                    inv.context_type = "pharmacy_sale"
                    inv.context_id = sale.id
                return inv

    # 3) No existing invoice/items found -> create fresh Invoice + items
    rx: Optional[PharmacyPrescription] = None
    if getattr(sale, "prescription_id", None):
        rx = db.query(PharmacyPrescription).get(sale.prescription_id)

    billing_type = "pharmacy"
    context_type = "pharmacy_sale"
    context_id: Optional[int] = sale.id

    if rx is not None:
        rxtype = (getattr(rx, "type", "") or "").upper()
        if rxtype == "OPD":
            context_type = "OPD"
            context_id = getattr(sale, "visit_id", None) or sale.id
        elif rxtype == "IPD":
            context_type = "IPD"
            context_id = (getattr(rx, "ipd_admission_id", None)
                          or getattr(sale, "visit_id", None) or sale.id)
        elif rxtype == "COUNTER":
            context_type = "PHARM_COUNTER"

    invoice_status_upper = (getattr(sale, "invoice_status", None)
                            or "").upper()
    if invoice_status_upper == "CANCELLED":
        inv_status = "cancelled"
    else:
        inv_status = "finalized"

    net = _safe_float(getattr(sale, "net_amount", None))
    gross = _safe_float(
        getattr(sale, "gross_amount", None)
        or getattr(sale, "total_amount", None))
    tax = _safe_float(getattr(sale, "total_tax", None))

    inv = Invoice(
        patient_id=sale.patient_id,
        context_type=context_type,
        context_id=context_id,
        status=inv_status,
        gross_total=gross,
        tax_total=tax,
        discount_total=0.0,
        net_total=net,
        amount_paid=0.0,
        balance_due=net,
    )

    if hasattr(inv, "billing_type"):
        setattr(inv, "billing_type", billing_type)
    if hasattr(inv, "created_by"):
        setattr(inv, "created_by", getattr(sale, "created_by_id", None))

    db.add(inv)
    db.flush()
    db.refresh(inv)

    items = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())
    seq_no = 1
    for it in items:
        qty = _safe_float(getattr(it, "quantity", None))
        unit_price = _safe_float(getattr(it, "unit_price", None))
        tax_percent = _safe_float(getattr(it, "tax_percent", None))
        line_amount = _safe_float(
            getattr(it, "line_amount", None) or (qty * unit_price))
        tax_amount = _safe_float(
            getattr(it, "tax_amount", None)
            or (line_amount * tax_percent / 100.0))
        total_line = _safe_float(
            getattr(it, "total_amount", None) or (line_amount + tax_amount))

        inv_item = InvoiceItem(
            invoice_id=inv.id,
            seq=seq_no,
            service_type="pharmacy",
            service_ref_id=it.id,
            description=getattr(it, "item_name", "") or "",
            quantity=qty,
            unit_price=unit_price,
            tax_rate=tax_percent,
            discount_percent=0.0,
            discount_amount=0.0,
            tax_amount=tax_amount,
            line_total=total_line,
            is_voided=False,
            created_by=getattr(sale, "created_by_id", None),
        )
        db.add(inv_item)
        seq_no += 1

    return inv


def _sync_billing_invoice_with_sale(
    db: Session,
    sale: PharmacySale,
    new_paid_amount: Optional[float] = None,
) -> Invoice:
    """
    Keep Billing.Invoice in sync with PharmacySale:
    - Align totals from sale.gross_amount / total_tax / net_amount
    - Optionally add a Payment row when status update carries paid_amount
    - If sale.payment_status=PAID, ensure balance_due hits 0 (creates top-up payment if needed)
    """
    inv = (db.query(Invoice).filter(
        Invoice.context_type == "pharmacy_sale",
        Invoice.context_id == sale.id,
    ).first())
    if inv is None:
        inv = _ensure_billing_invoice_for_sale(db, sale)

    net = _safe_float(getattr(sale, "net_amount", None))
    gross = _safe_float(
        getattr(sale, "gross_amount", None)
        or getattr(sale, "total_amount", None))
    tax = _safe_float(getattr(sale, "total_tax", None))

    inv.gross_total = gross
    inv.tax_total = tax
    inv.net_total = net

    existing_paid = 0.0
    for p in getattr(inv, "payments", []) or []:
        existing_paid += _safe_float(getattr(p, "amount", None))

    if new_paid_amount and new_paid_amount != 0 and net > 0:
        pay = Payment(
            invoice_id=inv.id,
            amount=float(new_paid_amount),
            mode="pharmacy_rx",
            reference_no=f"PHARM_{sale.bill_number}",
            notes=None,
            created_by=getattr(sale, "created_by_id", None),
        )
        db.add(pay)
        existing_paid += float(new_paid_amount)

    payment_status_upper = (getattr(sale, "payment_status", None)
                            or "").upper()
    invoice_status_upper = (getattr(sale, "invoice_status", None)
                            or "").upper()

    if net >= 0:
        if payment_status_upper == "PAID" and existing_paid < net:
            delta = net - existing_paid
            if delta > 0.01:
                pay = Payment(
                    invoice_id=inv.id,
                    amount=delta,
                    mode="pharmacy_rx",
                    reference_no=f"PHARM_{sale.bill_number}",
                    notes="Auto-sync full payment from pharmacy sale",
                    created_by=getattr(sale, "created_by_id", None),
                )
                db.add(pay)
                existing_paid += delta

        inv.amount_paid = existing_paid
        inv.balance_due = max(net - existing_paid, 0.0)
    else:
        inv.amount_paid = existing_paid
        inv.balance_due = net - existing_paid

    if invoice_status_upper == "CANCELLED":
        inv.status = "cancelled"
    else:
        inv.status = "finalized"

    if getattr(inv, "finalized_at", None) is None:
        inv.finalized_at = _sale_bill_datetime_value(sale)

    return inv


# ---------------- API: List Pharmacy bills ----------------
@router.get("", response_model=List[PharmacySaleSummaryOut])
@router.get("/", response_model=List[PharmacySaleSummaryOut])
def list_pharmacy_bills(
        q: Optional[str] = Query(None,
                                 description="Search UHID / name / phone"),
        bill_type: Optional[str] = Query(
            None,
            alias="type",
            description="Filter by Rx type: OPD | IPD | COUNTER | ALL",
        ),
        date_from: Optional[str] = Query(
            None, description="YYYY-MM-DD on bill date/time"),
        date_to: Optional[str] = Query(
            None, description="YYYY-MM-DD on bill date/time"),
        status:
    Optional[str] = Query(
        None,
        description=
        ("Filter by status: UNPAID | PARTIAL | PAID | CANCELLED | DRAFT | FINALIZED"
         ),
    ),
        limit: int = Query(100, ge=1, le=300),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Pharmacy Billing Console:
    - Shows PharmacySale bills created by the inventory-based module.
    - Lazily ensures each sale has a linked Billing.Invoice.
    """
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    bill_dt_col = _sale_bill_datetime_column()

    q_sales = (db.query(PharmacySale, Patient, PharmacyPrescription).join(
        Patient, PharmacySale.patient_id == Patient.id).outerjoin(
            PharmacyPrescription,
            PharmacySale.prescription_id == PharmacyPrescription.id,
        ))

    if date_from:
        df = datetime.fromisoformat(date_from + "T00:00:00")
        q_sales = q_sales.filter(bill_dt_col >= df)
    if date_to:
        dt = datetime.fromisoformat(date_to + "T23:59:59")
        q_sales = q_sales.filter(bill_dt_col <= dt)

    if q:
        ql = f"%{q.strip()}%"
        q_sales = q_sales.filter(
            or_(
                Patient.uhid.ilike(ql),
                Patient.first_name.ilike(ql),
                Patient.last_name.ilike(ql),
                Patient.phone.ilike(ql),
            ))

    if bill_type:
        bt = bill_type.strip().upper()
        if bt != "ALL":
            q_sales = q_sales.filter(
                or_(
                    PharmacyPrescription.type == bt,
                    PharmacySale.context_type == bt,
                ))

    if status:
        status_upper = status.strip().upper()
        if status_upper in ("UNPAID", "PARTIAL", "PARTIALLY_PAID", "PAID"):
            mapped = "PARTIALLY_PAID" if status_upper == "PARTIAL" else status_upper
            q_sales = q_sales.filter(
                getattr(PharmacySale, "payment_status") == mapped)
        elif status_upper == "CANCELLED":
            q_sales = q_sales.filter(
                getattr(PharmacySale, "invoice_status") == "CANCELLED")
        else:
            q_sales = q_sales.filter(
                getattr(PharmacySale, "invoice_status") == status_upper)

    rows = (q_sales.order_by(
        bill_dt_col.desc(),
        PharmacySale.id.desc(),
    ).limit(limit).all())

    out: List[PharmacySaleSummaryOut] = []
    for sale, patient, rx in rows:
        inv = _ensure_billing_invoice_for_sale(db, sale)

        admission_id = None
        if rx and rx.type == "IPD":
            admission_id = getattr(rx, "ipd_admission_id", None)

        out.append(
            _sale_summary_from_row(
                sale,
                patient,
                admission_id=admission_id,
                invoice=inv,
            ))

    db.commit()
    return out


# ---------------- API: Bill detail ----------------
@router.get("/{sale_id}", response_model=PharmacySaleDetailOut)
def get_pharmacy_bill(
        sale_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="PharmacySale not found")

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(
            status_code=400,
            detail="Patient not found for this bill",
        )

    _ensure_billing_invoice_for_sale(db, sale)
    db.commit()

    return _sale_detail(db, sale, patient)


# ---------------- API: Update bill status ----------------
@router.post("/{sale_id}/status", response_model=PharmacySaleDetailOut)
def update_pharmacy_bill_status(
        sale_id: int,
        payload: PharmacyBillStatusUpdateIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Update PharmacySale payment/invoice status:

    payment_status:
        - UNPAID
        - PARTIALLY_PAID
        - PAID

    Special keyword:
        - CANCELLED -> sets invoice_status = CANCELLED

    Also keeps Billing.Invoice in sync.
    - paid_amount is treated as *incremental* payment for this update.
    """
    _need_any(user, ["pharmacy.billing.manage", "pharmacy.sales.manage"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="PharmacySale not found")

    key = payload.payment_status.strip().upper()
    if key not in {"UNPAID", "PAID", "PARTIAL", "CANCELLED"}:
        raise HTTPException(
            status_code=400,
            detail=("Invalid payment_status "
                    "(use: unpaid / partial / paid / cancelled)"),
        )

    if key == "CANCELLED":
        setattr(sale, "invoice_status", "CANCELLED")
        if getattr(sale, "payment_status", None) is None:
            setattr(sale, "payment_status", "UNPAID")
    else:
        mapped_payment = "PARTIALLY_PAID" if key == "PARTIAL" else key
        setattr(sale, "payment_status", mapped_payment)
        if getattr(sale, "invoice_status", None) != "CANCELLED":
            setattr(sale, "invoice_status", "FINALIZED")

    sale.updated_at = datetime.utcnow()

    _sync_billing_invoice_with_sale(db, sale, payload.paid_amount)

    db.commit()
    db.refresh(sale)

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")
    return _sale_detail(db, sale, patient)


# ---------------- API: Bill PDF download ----------------
@router.get("/{sale_id}/pdf")
def download_pharmacy_bill_pdf(
        sale_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Download PharmacySale invoice as PDF.
    """
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="PharmacySale not found")

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    items = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())

    pdf_bytes = _build_sale_pdf(sale, patient, items)
    filename = f"PHARM_{sale.bill_number}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------- API: Email bill PDF ----------------
@router.post("/{sale_id}/email", response_model=PharmacySaleDetailOut)
def email_pharmacy_bill_pdf(
        sale_id: int,
        email: str = Query(..., description="Recipient email"),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="PharmacySale not found")

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    items = (db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all())
    pdf_bytes = _build_sale_pdf(sale, patient, items)
    filename = f"PHARM_{sale.bill_number}.pdf"

    body_text = (
        f"Dear { _full_name(patient) },\n\n"
        f"Please find attached your pharmacy invoice {sale.bill_number}.\n\n"
        "Regards,\n"
        f"{_user_display_name(user)}")

    try:
        send_email(
            email,
            f"Pharmacy Invoice {sale.bill_number}",
            body_text,
            attachments=[(filename, pdf_bytes, "application/pdf")],
        )
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Failed to send email: {e}")

    sale.email_sent_to = email
    sale.email_sent_at = datetime.utcnow()
    db.commit()
    db.refresh(sale)

    return _sale_detail(db, sale, patient)


# ---------------- API: Returns view ----------------
@router.get("/returns", response_model=List[PharmacySaleSummaryOut])
def list_pharmacy_returns(
        q: Optional[str] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Returns view:
    - Any PharmacySale with net_amount < 0 is considered a return.
    - Also ensures a negative Billing.Invoice exists (credit note effect).
    """
    _need_any(user, ["pharmacy.returns.view", "pharmacy.sales.return"])

    bill_dt_col = _sale_bill_datetime_column()

    q_sales = (db.query(PharmacySale, Patient, PharmacyPrescription).join(
        Patient, PharmacySale.patient_id == Patient.id).outerjoin(
            PharmacyPrescription,
            PharmacySale.prescription_id == PharmacyPrescription.id,
        ).filter(PharmacySale.net_amount < 0))

    if date_from:
        df = datetime.fromisoformat(date_from + "T00:00:00")
        q_sales = q_sales.filter(bill_dt_col >= df)
    if date_to:
        dt = datetime.fromisoformat(date_to + "T23:59:59")
        q_sales = q_sales.filter(bill_dt_col <= dt)

    if q:
        ql = f"%{q.strip()}%"
        q_sales = q_sales.filter(
            or_(
                Patient.uhid.ilike(ql),
                Patient.first_name.ilike(ql),
                Patient.last_name.ilike(ql),
                Patient.phone.ilike(ql),
            ))

    rows = (q_sales.order_by(
        bill_dt_col.desc(),
        PharmacySale.id.desc(),
    ).limit(200).all())

    out: List[PharmacySaleSummaryOut] = []
    for sale, patient, rx in rows:
        inv = _ensure_billing_invoice_for_sale(db, sale)

        admission_id = None
        if rx and rx.type == "IPD":
            admission_id = getattr(rx, "ipd_admission_id", None)
        out.append(
            _sale_summary_from_row(
                sale,
                patient,
                admission_id=admission_id,
                invoice=inv,
            ))

    db.commit()
    return out


# ---------------- API: IPD consolidated invoice summary ----------------
@router.post("/ipd/consolidated", response_model=ConsolidatedIpdInvoiceOut)
def create_consolidated_ipd_invoice(
        payload: ConsolidatedIpdInvoiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Summarize all UNPAID/PARTIALLY_PAID IPD pharmacy bills for a patient/admission
    into a single consolidated structure (for discharge-time payment).
    """
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    patient: Optional[Patient] = db.query(Patient).get(payload.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    q_sales = (db.query(PharmacySale, PharmacyPrescription).join(
        PharmacyPrescription,
        PharmacySale.prescription_id == PharmacyPrescription.id,
    ).filter(
        PharmacySale.patient_id == payload.patient_id,
        PharmacyPrescription.type == "IPD",
        getattr(PharmacySale,
                "payment_status").in_(["UNPAID", "PARTIALLY_PAID"]),
    ))

    if payload.admission_id is not None:
        q_sales = q_sales.filter(
            PharmacyPrescription.ipd_admission_id == payload.admission_id)

    rows = q_sales.all()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                "No unpaid IPD pharmacy bills found for this patient/admission"
            ),
        )

    sale_ids: List[int] = []
    total_amount = Decimal("0")
    total_tax = Decimal("0")
    net_amount = Decimal("0")
    items_map: Dict[str, Dict[str, float]] = {}

    for sale, _rx in rows:
        sale_ids.append(sale.id)
        total_amount += Decimal(
            str(
                getattr(sale, "gross_amount", None)
                or getattr(sale, "total_amount", None) or 0))
        total_tax += Decimal(str(getattr(sale, "total_tax", None) or 0))
        net_amount += Decimal(str(getattr(sale, "net_amount", None) or 0))

        sis = (db.query(PharmacySaleItem).filter(
            PharmacySaleItem.sale_id == sale.id).all())
        for it in sis:
            key = getattr(it, "item_name", "") or f"Item#{it.item_id}"
            rec = items_map.setdefault(key, {"qty": 0.0, "amount": 0.0})
            rec["qty"] += _safe_float(getattr(it, "quantity", None))
            rec["amount"] += _safe_float(
                getattr(it, "total_amount", None)
                or getattr(it, "line_amount", None))

    lines: List[ConsolidatedLineOut] = []
    for name, vals in items_map.items():
        lines.append(
            ConsolidatedLineOut(
                medicine_name=name,
                qty=vals["qty"],
                amount=vals["amount"],
            ))

    return ConsolidatedIpdInvoiceOut(
        patient_id=payload.patient_id,
        admission_id=payload.admission_id,
        patient_uhid=getattr(patient, "uhid", None),
        patient_name=_full_name(patient),
        sale_ids=sale_ids,
        total_amount=_safe_float(total_amount),
        total_tax=_safe_float(total_tax),
        net_amount=_safe_float(net_amount),
        items=lines,
    )


# ---------------- API: Create pharmacy return (negative invoice) ----------------
@router.post("/returns", response_model=PharmacySaleDetailOut)
def create_pharmacy_return(
        payload: PharmacyReturnCreateIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create a negative pharmacy invoice as a return against an earlier invoice.
    - Adjusts stock back to the relevant batch.
    - Uses txn_type = RETURN_FROM_CUSTOMER in StockTransaction.
    - Also creates a negative Billing.Invoice (credit note style).
    """
    _need_any(user, ["pharmacy.returns.manage", "pharmacy.sales.return"])

    source_sale: Optional[PharmacySale] = db.query(PharmacySale).get(
        payload.source_invoice_id)
    if not source_sale:
        raise HTTPException(status_code=404,
                            detail="Source PharmacySale not found")

    if getattr(source_sale, "net_amount", 0) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot create a return from a negative/zero invoice",
        )

    patient: Optional[Patient] = db.query(Patient).get(source_sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    source_items = {
        it.id: it
        for it in db.query(PharmacySaleItem).filter(
            PharmacySaleItem.sale_id == source_sale.id).all()
    }
    if not source_items:
        raise HTTPException(status_code=400,
                            detail="Source invoice has no items")

    today = date.today()

    suffix = int(datetime.utcnow().timestamp()) % 1000
    return_bill_number = f"{source_sale.bill_number}-R{suffix:03d}"

    return_sale = PharmacySale(
        bill_number=return_bill_number,
        prescription_id=getattr(source_sale, "prescription_id", None),
        patient_id=source_sale.patient_id,
        visit_id=getattr(source_sale, "visit_id", None),
        ipd_admission_id=getattr(source_sale, "ipd_admission_id", None),
        location_id=getattr(source_sale, "location_id", None),
        context_type=getattr(source_sale, "context_type", "COUNTER"),
        bill_datetime=datetime.utcnow(),
        gross_amount=Decimal("0"),
        total_tax=Decimal("0"),
        discount_amount_total=Decimal("0"),
        net_amount=Decimal("0"),
        rounding_adjustment=Decimal("0"),
        invoice_status="FINALIZED",
        payment_status="PAID",
        created_by_id=getattr(user, "id", None),
    )
    db.add(return_sale)
    db.flush()

    total_amount = Decimal("0")
    total_tax = Decimal("0")

    for line in payload.lines:
        src = source_items.get(line.bill_line_id)
        if not src:
            raise HTTPException(
                status_code=400,
                detail=(f"Source bill line {line.bill_line_id} "
                        "not found for this invoice"),
            )

        qty_ret = Decimal(str(line.qty_to_return))
        if qty_ret <= 0:
            continue

        sold_qty = Decimal(str(getattr(src, "quantity", None) or 0))
        if qty_ret > sold_qty:
            raise HTTPException(
                status_code=400,
                detail=(f"Return qty {qty_ret} exceeds sold qty {sold_qty} "
                        f"for item {getattr(src, 'item_name', None)}"),
            )

        batch = (db.query(ItemBatch).get(src.batch_id) if getattr(
            src, "batch_id", None) else None)
        if batch:
            if getattr(batch, "expiry_date",
                       None) and batch.expiry_date < today:
                raise HTTPException(
                    status_code=400,
                    detail=(f"Batch {batch.batch_no} is already expired "
                            "and cannot be returned to active stock."),
                )

            adjust_batch_qty(batch=batch, delta=qty_ret)
            create_stock_transaction(
                db,
                user=user,
                location_id=batch.location_id,
                item_id=batch.item_id,
                batch_id=batch.id,
                qty_delta=qty_ret,
                txn_type="RETURN_FROM_CUSTOMER",
                ref_type="PHARMACY_RETURN",
                ref_id=return_sale.id,
                unit_cost=batch.unit_cost,
                mrp=batch.mrp,
                remark=payload.reason
                or f"Return against {source_sale.bill_number}",
                patient_id=source_sale.patient_id,
                visit_id=getattr(source_sale, "visit_id", None),
            )

        unit_price = Decimal(str(getattr(src, "unit_price", None) or 0))
        tax_percent = Decimal(str(getattr(src, "tax_percent", None) or 0))

        line_amount = -(qty_ret * unit_price)
        tax_amount = line_amount * tax_percent / Decimal("100")
        total_line = line_amount + tax_amount

        ret_item = PharmacySaleItem(
            sale_id=return_sale.id,
            rx_line_id=getattr(src, "rx_line_id", None),
            item_id=getattr(src, "item_id", None),
            item_name=getattr(src, "item_name", None),
            batch_id=getattr(src, "batch_id", None),
            batch_no=getattr(src, "batch_no", None),
            quantity=-qty_ret,
            unit_price=unit_price,
            tax_percent=tax_percent,
            line_amount=line_amount,
            tax_amount=tax_amount,
            total_amount=total_line,
        )
        db.add(ret_item)

        total_amount += line_amount
        total_tax += tax_amount

    return_sale.gross_amount = total_amount
    return_sale.total_tax = total_tax
    return_sale.net_amount = total_amount + total_tax
    return_sale.updated_at = datetime.utcnow()

    _ensure_billing_invoice_for_sale(db, return_sale)

    db.commit()
    db.refresh(return_sale)

    return _sale_detail(db, return_sale, patient)


# ---------------- API: Get return invoice detail ----------------
@router.get("/returns/{sale_id}", response_model=PharmacySaleDetailOut)
def get_pharmacy_return(
        sale_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Returns are just PharmacySale rows with net_amount < 0.
    """
    _need_any(user, ["pharmacy.returns.view", "pharmacy.sales.return"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale or getattr(sale, "net_amount", 0) >= 0:
        raise HTTPException(status_code=404, detail="Return invoice not found")

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    _ensure_billing_invoice_for_sale(db, sale)
    db.commit()

    return _sale_detail(db, sale, patient)
