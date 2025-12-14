# FILE: app/services/ot_billing.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.models.billing import Invoice, InvoiceItem
from app.models.ot import OtCase, OtSchedule, OtScheduleProcedure, OtProcedure


def _new_invoice_uid() -> str:
    return str(uuid.uuid4())


def _new_invoice_number() -> str:
    return f"INV-{uuid.uuid4().hex[:8].upper()}"


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")


def _next_seq(db: Session, invoice_id: int) -> int:
    from sqlalchemy import func
    m = db.query(func.max(InvoiceItem.seq)).filter(
        InvoiceItem.invoice_id == invoice_id).scalar()
    return (m or 0) + 1


def _case_completed(case: OtCase) -> bool:
    # Your schema uses outcome
    o = (case.outcome or "").lower()
    return o in ("completed", "converted", "success", "successful", "done",
                 "closed")


def ensure_ot_invoice_and_items(db: Session,
                                case_id: int,
                                created_by: int | None = None) -> Invoice:
    """
    ✅ Idempotent OT billing:
    - Requires: case.schedule.patient_id
    - Requires: case.actual_start_time + case.actual_end_time
    - Requires: outcome 'completed' style
    - Creates invoice if missing (context_type='ot', context_id=case_id)
    - Adds one item per schedule procedure link (service_ref_id = link.id)
    """
    case: OtCase | None = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure)).get(case_id))
    if not case:
        raise HTTPException(404, "OT case not found")

    if not _case_completed(case):
        raise HTTPException(400, "OT case not completed; cannot auto-bill")

    if not case.schedule or not case.schedule.patient_id:
        raise HTTPException(400, "OT case not linked to schedule/patient")

    if not case.actual_start_time or not case.actual_end_time or case.actual_end_time <= case.actual_start_time:
        raise HTTPException(400, "Invalid OT timings; cannot auto-bill")

    hours = Decimal(
        str((case.actual_end_time - case.actual_start_time).total_seconds() /
            3600.0)).quantize(Decimal("0.01"))
    if hours <= 0:
        raise HTTPException(400, "OT duration is zero; cannot auto-bill")

    # Find or create invoice for this OT case
    inv = (db.query(Invoice).filter(
        Invoice.context_type == "ot", Invoice.context_id == case_id,
        Invoice.status != "cancelled").order_by(Invoice.id.desc()).first())

    if not inv:
        inv = Invoice(
            invoice_uid=_new_invoice_uid(),
            invoice_number=_new_invoice_number(),
            patient_id=case.schedule.patient_id,
            context_type="ot",
            context_id=case_id,
            billing_type="ot",
            status="draft",
            created_by=created_by,
        )
        db.add(inv)
        db.flush()  # get inv.id

    # Existing OT items (by link id)
    existing = {
        int(it.service_ref_id): it
        for it in db.query(InvoiceItem).filter(
            InvoiceItem.invoice_id == inv.id,
            InvoiceItem.service_type == "ot_procedure",
            InvoiceItem.is_voided.is_(False),
        ).all() if it.service_ref_id
    }

    created_any = False
    for link in (case.schedule.procedures or []):
        if link.id in existing:
            continue

        proc: OtProcedure | None = link.procedure
        if not proc:
            continue

        rate = _d(proc.rate_per_hour)
        if rate <= 0:
            continue

        qty = hours
        base = (qty * rate).quantize(Decimal("0.01"))

        seq = _next_seq(db, inv.id)
        item = InvoiceItem(
            invoice_id=inv.id,
            seq=seq,
            service_type="ot_procedure",
            service_ref_id=link.id,
            description=
            f"OT charges - {proc.name} ({float(hours):.2f} hr, Case #{case.id})",
            quantity=qty,
            unit_price=rate,
            tax_rate=Decimal("0"),
            discount_percent=Decimal("0"),
            discount_amount=Decimal("0"),
            tax_amount=Decimal("0"),
            line_total=base,
            is_voided=False,
            created_by=created_by,
        )
        db.add(item)
        created_any = True

    db.flush()
    return inv
