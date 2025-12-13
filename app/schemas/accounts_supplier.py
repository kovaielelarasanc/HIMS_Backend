from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


Money = Decimal


class SupplierInvoiceOut(BaseModel):
    id: int
    grn_id: int
    grn_number: str

    supplier_id: int
    location_id: int | None

    invoice_number: str
    invoice_date: date | None
    due_date: date | None

    invoice_amount: Money
    paid_amount: Money
    outstanding_amount: Money
    status: str

    is_overdue: bool
    last_payment_date: date | None

    notes: str | None = ""

    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SupplierPaymentAllocIn(BaseModel):
    invoice_id: int
    amount: Money = Field(..., gt=0)


class SupplierPaymentCreate(BaseModel):
    supplier_id: int
    payment_date: date | None = None

    payment_method: str = "CASH"
    reference_no: str | None = None

    amount: Money = Field(..., gt=0)

    # if allocations provided: allocate exactly these
    allocations: List[SupplierPaymentAllocIn] = Field(default_factory=list)

    # if True and allocations empty: auto allocate oldest unpaid first
    auto_allocate: bool = True

    remarks: str | None = ""


class SupplierPaymentOut(BaseModel):
    id: int
    supplier_id: int
    payment_date: date
    payment_method: str
    reference_no: str | None
    amount: Money
    allocated_amount: Money
    advance_amount: Money
    remarks: str | None
    created_at: datetime
    created_by_id: int | None

    model_config = ConfigDict(from_attributes=True)


class SupplierPaymentAllocationOut(BaseModel):
    id: int
    payment_id: int
    invoice_id: int
    amount: Money
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SupplierLedgerRowOut(BaseModel):
    invoice: SupplierInvoiceOut
    allocations: List[SupplierPaymentAllocationOut] = Field(default_factory=list)


class SupplierLedgerFilter(BaseModel):
    supplier_id: int | None = None
    from_date: date | None = None
    to_date: date | None = None

    status: str | None = None  # UNPAID/PARTIAL/PAID
    min_amount: Money | None = None
    max_amount: Money | None = None

    overdue_only: bool = False


class SupplierMonthlySummaryRow(BaseModel):
    supplier_id: int
    month: str  # YYYY-MM
    total_purchase: Money
    total_paid: Money
    pending_amount: Money
    overdue_invoices: int
    last_payment_date: date | None


class SupplierMonthlySummaryOut(BaseModel):
    month: str
    rows: List[SupplierMonthlySummaryRow]
