from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.accounts_supplier import SupplierInvoice, SupplierPayment
from app.schemas.accounts_supplier import (
    SupplierInvoiceOut,
    SupplierPaymentCreate,
    SupplierPaymentOut,
    SupplierMonthlySummaryOut,
    SupplierMonthlySummaryRow,
)
from app.services.supplier_ledger import (
    _d,
    compute_invoice_status,
    allocate_payment_to_invoices,
    auto_allocate_oldest_first,
)
from app.services.excel_export import (
    build_supplier_ledger_excel,
    build_supplier_monthly_summary_excel,
)

router = APIRouter(prefix="/pharmacy/accounts", tags=["Pharmacy Accounts"])


# ---------- Debug helper ----------
DEBUG_LEDGER = True

def dbg(*args):
    if DEBUG_LEDGER:
        print("[PHARM-ACCOUNTS]", *args)


# ---------- Permissions ----------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False


def _month_range(month: str) -> Tuple[date, date]:
    try:
        y, m = month.split("-")
        y = int(y)
        m = int(m)
        start = date(y, m, 1)
        end = date(y + (1 if m == 12 else 0), (1 if m == 12 else m + 1), 1)
        return start, end
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month. Use YYYY-MM")


# -------------------- Invoices --------------------

@router.get("/supplier-invoices", response_model=List[SupplierInvoiceOut])
def list_supplier_invoices(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),

    supplier_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    status: Optional[str] = Query(None),  # UNPAID / PARTIAL / PAID / CANCELLED
    overdue_only: bool = Query(False),
    min_amount: Optional[Decimal] = Query(None),
    max_amount: Optional[Decimal] = Query(None),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    dbg("LIST INVOICES", "supplier_id=", supplier_id)

    q = db.query(SupplierInvoice).options(joinedload(SupplierInvoice.supplier))

    if supplier_id:
        q = q.filter(SupplierInvoice.supplier_id == supplier_id)
    if from_date:
        q = q.filter(SupplierInvoice.invoice_date >= from_date)
    if to_date:
        q = q.filter(SupplierInvoice.invoice_date <= to_date)
    if status:
        q = q.filter(SupplierInvoice.status == status)
    if overdue_only:
        q = q.filter(SupplierInvoice.is_overdue.is_(True))
    if min_amount is not None:
        q = q.filter(SupplierInvoice.invoice_amount >= min_amount)
    if max_amount is not None:
        q = q.filter(SupplierInvoice.invoice_amount <= max_amount)

    # ✅ MySQL-safe NULLs last + latest first
    q = q.order_by(
        SupplierInvoice.invoice_date.is_(None),   # NULL last
        SupplierInvoice.invoice_date.desc(),
        SupplierInvoice.id.desc(),
    )

    invoices = q.all()

    # Keep derived fields consistent
    for inv in invoices:
        compute_invoice_status(inv)

    return invoices


@router.get("/supplier-invoices/{invoice_id}", response_model=SupplierInvoiceOut)
def get_supplier_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    inv = (
        db.query(SupplierInvoice)
        .options(joinedload(SupplierInvoice.supplier))
        .filter(SupplierInvoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    compute_invoice_status(inv)
    return inv


# -------------------- Payments --------------------

@router.post("/supplier-payments", response_model=SupplierPaymentOut)
def create_supplier_payment(
    payload: SupplierPaymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.manage"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    dbg("CREATE PAYMENT", "supplier_id=", payload.supplier_id, "amount=", payload.amount)

    try:
        pay = SupplierPayment(
            supplier_id=payload.supplier_id,
            payment_date=payload.payment_date or date.today(),
            payment_method=(payload.payment_method or "CASH").upper(),
            reference_no=(payload.reference_no or None),
            amount=_d(payload.amount),
            allocated_amount=Decimal("0.00"),
            advance_amount=Decimal("0.00"),
            remarks=payload.remarks or "",
            created_by_id=current_user.id,
        )
        db.add(pay)
        db.flush()

        # allocation pairs: [(invoice_id, amount), ...]
        alloc_pairs: List[Tuple[int, Decimal]] = []

        if payload.allocations and len(payload.allocations) > 0:
            alloc_pairs = [(a.invoice_id, _d(a.amount)) for a in payload.allocations]
        elif payload.auto_allocate:
            alloc_pairs = auto_allocate_oldest_first(
                db=db,
                supplier_id=payload.supplier_id,
                amount=_d(payload.amount),
            )

        dbg("ALLOC PAIRS", alloc_pairs)

        # ✅ tenant-free signature
        allocate_payment_to_invoices(db=db, payment=pay, allocations=alloc_pairs)

        db.commit()
        db.refresh(pay)
        return pay

    except ValueError as e:
        db.rollback()
        dbg("PAYMENT FAILED (ValueError)", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        dbg("PAYMENT FAILED (Exception)", repr(e))
        raise HTTPException(status_code=500, detail=f"Failed to create payment: {e}")


@router.get("/supplier-payments", response_model=List[SupplierPaymentOut])
def list_supplier_payments(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    supplier_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = db.query(SupplierPayment)

    if supplier_id:
        q = q.filter(SupplierPayment.supplier_id == supplier_id)
    if from_date:
        q = q.filter(SupplierPayment.payment_date >= from_date)
    if to_date:
        q = q.filter(SupplierPayment.payment_date <= to_date)

    q = q.order_by(SupplierPayment.payment_date.desc(), SupplierPayment.id.desc())
    return q.all()


# -------------------- Monthly Summary --------------------

@router.get("/supplier-ledger/monthly-summary", response_model=SupplierMonthlySummaryOut)
def supplier_monthly_summary(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    start, end = _month_range(month)

    invq = (
        db.query(
            SupplierInvoice.supplier_id.label("supplier_id"),
            func.coalesce(func.sum(SupplierInvoice.invoice_amount), 0).label("total_purchase"),
            func.coalesce(func.sum(SupplierInvoice.paid_amount), 0).label("total_paid"),
            func.coalesce(func.sum(SupplierInvoice.outstanding_amount), 0).label("pending_amount"),
            func.coalesce(func.sum(case((SupplierInvoice.is_overdue.is_(True), 1), else_=0)), 0).label("overdue_invoices"),
            func.max(SupplierInvoice.last_payment_date).label("last_payment_date"),
        )
        .filter(
            SupplierInvoice.invoice_date >= start,
            SupplierInvoice.invoice_date < end,
        )
        .group_by(SupplierInvoice.supplier_id)
        .all()
    )

    rows = [
        SupplierMonthlySummaryRow(
            supplier_id=int(r.supplier_id),
            month=month,
            total_purchase=_d(r.total_purchase),
            total_paid=_d(r.total_paid),
            pending_amount=_d(r.pending_amount),
            overdue_invoices=int(r.overdue_invoices or 0),
            last_payment_date=r.last_payment_date,
        )
        for r in invq
    ]

    return SupplierMonthlySummaryOut(month=month, rows=rows)


# -------------------- Excel Exports --------------------

@router.get("/supplier-ledger/export.xlsx")
def export_supplier_ledger_excel(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
    supplier_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    status: Optional[str] = Query(None),
    overdue_only: bool = Query(False),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    q = (
        db.query(SupplierInvoice)
        .options(joinedload(SupplierInvoice.supplier))
    )

    if supplier_id:
        q = q.filter(SupplierInvoice.supplier_id == supplier_id)
    if from_date:
        q = q.filter(SupplierInvoice.invoice_date >= from_date)
    if to_date:
        q = q.filter(SupplierInvoice.invoice_date <= to_date)
    if status:
        q = q.filter(SupplierInvoice.status == status)
    if overdue_only:
        q = q.filter(SupplierInvoice.is_overdue.is_(True))

    q = q.order_by(
        SupplierInvoice.invoice_date.is_(None),
        SupplierInvoice.invoice_date.desc(),
        SupplierInvoice.id.desc(),
    )

    invoices = q.all()

    bio = BytesIO()
    build_supplier_ledger_excel(bio, invoices)
    bio.seek(0)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="supplier_ledger.xlsx"'},
    )


@router.get("/supplier-ledger/monthly-summary/export.xlsx")
def export_supplier_monthly_summary_excel(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth_current_user),
):
    if not has_perm(current_user, "pharmacy.accounts.supplier_ledger.view"):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    summary = supplier_monthly_summary(month=month, db=db, current_user=current_user)

    bio = BytesIO()
    build_supplier_monthly_summary_excel(bio, summary)
    bio.seek(0)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="supplier_monthly_summary_{month}.xlsx"'},
    )
