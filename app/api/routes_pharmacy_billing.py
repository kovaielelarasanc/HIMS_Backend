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
from sqlalchemy import or_, func

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient

# ✅ Your existing pharmacy models (keep as-is from your project)
from app.models.pharmacy_prescription import (
    PharmacySale,
    PharmacySaleItem,
    PharmacyPrescription,
)

from app.models.pharmacy_inventory import ItemBatch
from app.services.inventory import adjust_batch_qty, create_stock_transaction
from app.core.emailer import send_email

# ✅ NEW Billing models (NO old Invoice/InvoiceItem/Payment imports)
from app.models.billing import BillingInvoice, BillingInvoiceLine, BillingPayment, PayMode

# ✅ NEW Billing hooks (encounter-based billing)
from app.services.billing_hooks import autobill_pharmacy_sale, add_pharmacy_payment_for_sale

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

    # Linked Billing.Invoice summary (NEW billing)
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


# ============================================================
# NEW BILLING INTEGRATION (NO old Invoice/InvoiceItem/Payment)
# ============================================================
def _billing_invoice_id_for_sale(db: Session,
                                 sale: PharmacySale) -> Optional[int]:
    row = (db.query(BillingInvoiceLine.invoice_id).filter(
        BillingInvoiceLine.source_module.in_(["PHM", "PHC"]),
        BillingInvoiceLine.source_ref_id == int(sale.id),
    ).order_by(BillingInvoiceLine.invoice_id.asc()).first())
    return int(row[0]) if row else None


def _sale_paid_amount_from_billing(db: Session, sale: PharmacySale) -> float:
    """
    We record payments from this pharmacy bill with txn_ref:
      txn_ref = f"PHARM:{sale.bill_number}"

    This avoids mixing payments from other modules in the same encounter invoice.
    """
    inv_id = _billing_invoice_id_for_sale(db, sale)
    if not inv_id:
        return 0.0

    ref = f"PHARM:{getattr(sale, 'bill_number', sale.id)}"
    inv = db.query(BillingInvoice).get(inv_id)
    case_id = int(inv.billing_case_id) if inv else None
    amt = (db.query(func.coalesce(func.sum(BillingPayment.amount), 0)).filter(
        BillingPayment.billing_case_id == case_id,
        BillingPayment.txn_ref == ref).scalar()) if case_id else 0

    return float(amt or 0)


def _ensure_billing_lines_for_sale(db: Session, sale: PharmacySale,
                                   user: User) -> Optional[int]:
    """
    Lazily ensure the sale is mapped into the NEW billing system.
    Safe: never breaks listing if billing cannot be mapped.
    """
    inv_id = _billing_invoice_id_for_sale(db, sale)
    if inv_id:
        return inv_id

    # Try to auto-bill into encounter invoice (OP/IP)
    try:
        res = autobill_pharmacy_sale(db, sale_id=int(sale.id), user=user)
        return int(
            res.get("invoice_id")) if res and res.get("invoice_id") else None
    except Exception:
        # Counter sales (no visit/admission), missing patient, etc. should not break UI
        return None


def _sale_summary_from_row(
    db: Session,
    sale: PharmacySale,
    patient: Patient,
    user: User,
    admission_id: Optional[int] = None,
) -> PharmacySaleSummaryOut:
    """
    Summary row enriched with NEW billing:
      - invoice_id: encounter invoice id that contains PHARM lines
      - paid_amount/balance_amount: sale-level via txn_ref PHARM:<bill_number>
    """
    created_at = _sale_bill_datetime_value(sale)

    gross_amount = getattr(sale, "gross_amount", None)
    if gross_amount is None:
        gross_amount = getattr(sale, "total_amount", None)

    total_tax = getattr(sale, "total_tax", None)
    net_amount = getattr(sale, "net_amount", None)

    # Ensure billing lines exist when possible
    invoice_id = _ensure_billing_lines_for_sale(db, sale, user)

    paid_amount = _sale_paid_amount_from_billing(db,
                                                 sale) if invoice_id else 0.0
    balance_amount = _safe_float(net_amount) - paid_amount

    # ---- status: derive primarily from sale flags, fallback to payment math ----
    sale_payment_status = (getattr(sale, "payment_status", None) or "").upper()
    sale_invoice_status = (getattr(sale, "invoice_status", None) or "").upper()

    if sale_invoice_status == "CANCELLED":
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

        # fallback if billing payment isn't used
        if sale_payment_status in ("UNPAID", "PARTIALLY_PAID",
                                   "PAID") and invoice_id is None:
            status_val = "PARTIAL" if sale_payment_status == "PARTIALLY_PAID" else sale_payment_status

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
        payment_mode=None,
        created_at=created_at,
        context_type=getattr(sale, "context_type", None),
        visit_id=getattr(sale, "visit_id", None),
        admission_id=admission_id,
        invoice_id=invoice_id,
        paid_amount=paid_amount,
        balance_amount=balance_amount,
    )


def _sale_detail(db: Session, sale: PharmacySale, patient: Patient,
                 user: User) -> PharmacySaleDetailOut:
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

    # Ensure billing lines exist (best-effort)
    _ensure_billing_lines_for_sale(db, sale, user)

    summary = _sale_summary_from_row(
        db,
        sale,
        patient,
        user,
        admission_id=getattr(sale, "ipd_admission_id", None),
    )
    return PharmacySaleDetailOut(**summary.model_dump(), items=out_items)


def _build_sale_pdf(sale: PharmacySale, patient: Patient,
                    items: List[PharmacySaleItem]) -> bytes:
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
                getattr(it, "tax_amount",
                        (line_amount * tax_percent / Decimal("100")))))
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
        status: Optional[str] = Query(
            None,
            description=(
                "Filter by status: UNPAID | PARTIAL | PAID | CANCELLED"),
        ),
        limit: int = Query(100, ge=1, le=300),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Pharmacy Billing Console:
    - Shows PharmacySale bills created by the pharmacy module.
    - Lazily maps each sale into NEW encounter-based Billing (OP/IP) when possible.
    """
    _need_any(user, ["pharmacy.billing.view", "pharmacy.sales.view"])

    bill_dt_col = _sale_bill_datetime_column()

    q_sales = (db.query(PharmacySale, Patient, PharmacyPrescription).join(
        Patient, PharmacySale.patient_id == Patient.id).outerjoin(
            PharmacyPrescription,
            PharmacySale.prescription_id == PharmacyPrescription.id))

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
        if status_upper in ("UNPAID", "PARTIAL", "PAID"):
            # Use PharmacySale flags for initial filtering
            if status_upper == "PARTIAL":
                q_sales = q_sales.filter(
                    getattr(PharmacySale, "payment_status") ==
                    "PARTIALLY_PAID")
            else:
                q_sales = q_sales.filter(
                    getattr(PharmacySale, "payment_status") == status_upper)
        elif status_upper == "CANCELLED":
            q_sales = q_sales.filter(
                getattr(PharmacySale, "invoice_status") == "CANCELLED")

    rows = (q_sales.order_by(bill_dt_col.desc(),
                             PharmacySale.id.desc()).limit(limit).all())

    out: List[PharmacySaleSummaryOut] = []
    for sale, patient, rx in rows:
        admission_id = None
        if rx and (getattr(rx, "type", "") or "").upper() == "IPD":
            admission_id = getattr(rx, "ipd_admission_id", None)

        out.append(
            _sale_summary_from_row(db,
                                   sale,
                                   patient,
                                   user,
                                   admission_id=admission_id))

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
        raise HTTPException(status_code=400,
                            detail="Patient not found for this bill")

    # Best-effort NEW billing mapping
    _ensure_billing_lines_for_sale(db, sale, user)
    db.commit()

    return _sale_detail(db, sale, patient, user)


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
      - PARTIAL
      - PAID
      - CANCELLED

    Also records payment into NEW Billing engine using:
      add_pharmacy_payment_for_sale(..., txn_ref="PHARM:<bill_number>")
    """
    _need_any(user, ["pharmacy.billing.manage", "pharmacy.sales.manage"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="PharmacySale not found")

    key = payload.payment_status.strip().upper()
    if key not in {"UNPAID", "PAID", "PARTIAL", "CANCELLED"}:
        raise HTTPException(
            status_code=400,
            detail=
            "Invalid payment_status (use: unpaid / partial / paid / cancelled)",
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

    # ✅ Record payment into NEW Billing (sale-scoped via txn_ref)
    if key != "CANCELLED" and payload.paid_amount and float(
            payload.paid_amount) > 0:
        try:
            add_pharmacy_payment_for_sale(
                db,
                sale_id=int(sale.id),
                paid_amount=Decimal(str(payload.paid_amount)),
                user=user,
                mode=PayMode.CASH,  # map UI -> PayMode later if needed
            )
        except Exception:
            # Never break API due to billing payments
            pass

    # Ensure billing lines exist (best-effort)
    _ensure_billing_lines_for_sale(db, sale, user)

    db.commit()
    db.refresh(sale)

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    return _sale_detail(db, sale, patient, user)


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

    items = db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all()

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

    items = db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == sale.id).all()
    pdf_bytes = _build_sale_pdf(sale, patient, items)
    filename = f"PHARM_{sale.bill_number}.pdf"

    body_text = (
        f"Dear {_full_name(patient)},\n\n"
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

    return _sale_detail(db, sale, patient, user)


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
    - Also maps into NEW Billing as negative lines (best-effort).
    """
    _need_any(user, ["pharmacy.returns.view", "pharmacy.sales.return"])

    bill_dt_col = _sale_bill_datetime_column()

    q_sales = (db.query(PharmacySale, Patient, PharmacyPrescription).join(
        Patient, PharmacySale.patient_id == Patient.id).outerjoin(
            PharmacyPrescription,
            PharmacySale.prescription_id == PharmacyPrescription.id).filter(
                PharmacySale.net_amount < 0))

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

    rows = (q_sales.order_by(bill_dt_col.desc(),
                             PharmacySale.id.desc()).limit(200).all())

    out: List[PharmacySaleSummaryOut] = []
    for sale, patient, rx in rows:
        admission_id = None
        if rx and (getattr(rx, "type", "") or "").upper() == "IPD":
            admission_id = getattr(rx, "ipd_admission_id", None)

        out.append(
            _sale_summary_from_row(db,
                                   sale,
                                   patient,
                                   user,
                                   admission_id=admission_id))

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
        PharmacySale.prescription_id == PharmacyPrescription.id).filter(
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
            detail=
            "No unpaid IPD pharmacy bills found for this patient/admission",
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

        sis = db.query(PharmacySaleItem).filter(
            PharmacySaleItem.sale_id == sale.id).all()
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
            ConsolidatedLineOut(medicine_name=name,
                                qty=vals["qty"],
                                amount=vals["amount"]))

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


# ---------------- API: Create pharmacy return (negative sale + stock adjust) ----------------
@router.post("/returns", response_model=PharmacySaleDetailOut)
def create_pharmacy_return(
        payload: PharmacyReturnCreateIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Create a negative PharmacySale as a return against an earlier invoice.
    - Adjusts stock back to the relevant batch.
    - Uses txn_type = RETURN_FROM_CUSTOMER in StockTransaction.
    - Also maps into NEW Billing as negative invoice lines (best-effort).
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
            detail="Cannot create a return from a negative/zero invoice")

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
                detail=
                f"Source bill line {line.bill_line_id} not found for this invoice",
            )

        qty_ret = Decimal(str(line.qty_to_return))
        if qty_ret <= 0:
            continue

        sold_qty = Decimal(str(getattr(src, "quantity", None) or 0))
        if qty_ret > sold_qty:
            raise HTTPException(
                status_code=400,
                detail=
                f"Return qty {qty_ret} exceeds sold qty {sold_qty} for item {getattr(src, 'item_name', None)}",
            )

        batch = db.query(ItemBatch).get(src.batch_id) if getattr(
            src, "batch_id", None) else None
        if batch:
            if getattr(batch, "expiry_date",
                       None) and batch.expiry_date < today:
                raise HTTPException(
                    status_code=400,
                    detail=
                    f"Batch {batch.batch_no} is already expired and cannot be returned to active stock.",
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

    # ✅ NEW billing mapping (negative lines, best-effort)
    _ensure_billing_lines_for_sale(db, return_sale, user)

    db.commit()
    db.refresh(return_sale)

    return _sale_detail(db, return_sale, patient, user)


# ---------------- API: Get return invoice detail ----------------
@router.get("/returns/{sale_id}", response_model=PharmacySaleDetailOut)
def get_pharmacy_return(
        sale_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Returns are PharmacySale rows with net_amount < 0.
    """
    _need_any(user, ["pharmacy.returns.view", "pharmacy.sales.return"])

    sale: Optional[PharmacySale] = db.query(PharmacySale).get(sale_id)
    if not sale or getattr(sale, "net_amount", 0) >= 0:
        raise HTTPException(status_code=404, detail="Return invoice not found")

    patient: Optional[Patient] = db.query(Patient).get(sale.patient_id)
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    _ensure_billing_lines_for_sale(db, sale, user)
    db.commit()

    return _sale_detail(db, sale, patient, user)
