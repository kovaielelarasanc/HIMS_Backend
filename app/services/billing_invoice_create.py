from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingCase,
    BillingInvoice,
    InvoiceType,
    PayerType,
    DocStatus,
    NumberDocType,
    NumberResetPeriod,
)
from app.services.billing_service import BillingError
from app.services.billing_numbers import next_billing_number  # you will add below


def create_new_invoice_for_case(
    db: Session,
    *,
    case: BillingCase,
    user,
    module: str,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: int | None = None,
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    allow_duplicate_draft: bool = False,
) -> BillingInvoice:
    if not allow_duplicate_draft:
        existing = (db.query(BillingInvoice.id).filter(
            BillingInvoice.billing_case_id == int(case.id),
            BillingInvoice.module == module,
            BillingInvoice.invoice_type == invoice_type,
            BillingInvoice.payer_type == payer_type,
            BillingInvoice.payer_id == payer_id,
            BillingInvoice.status == DocStatus.DRAFT,
        ).first())
        if existing:
            raise BillingError(
                "Draft invoice already exists for this module/payer/type",
                status_code=409,
                extra={"invoice_id": int(existing[0])},
            )

    invoice_no = next_billing_number(
        db,
        tenant_id=getattr(case, "tenant_id", None),
        doc_type=NumberDocType.INVOICE,
        reset_period=reset_period,
        prefix=f"{module}-",
        padding=6,
    )

    inv = BillingInvoice(
        billing_case_id=int(case.id),
        invoice_number=invoice_no,
        module=module,
        invoice_type=invoice_type,
        payer_type=payer_type,
        payer_id=payer_id,
        status=DocStatus.DRAFT,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
        service_date=getattr(case, "created_at", None) or datetime.utcnow(),
        meta_json={"created_from": "manual_create_invoice"},
    )
    db.add(inv)
    db.flush()
    return inv
