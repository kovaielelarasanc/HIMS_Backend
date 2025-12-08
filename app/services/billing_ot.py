# FILE: app/services/billing_ot.pys
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.models.billing import Invoice, InvoiceItem
from app.models.ot import OtCase, OtSchedule, OtProcedure  

OT_SERVICE_TYPE = "ot"


def _get_or_create_ipd_invoice_for_case(db: Session, case: OtCase) -> Invoice:
    """
    For IPD OT cases: reuse existing running IP invoice if present,
    otherwise create a new draft invoice.
    """
    inv: Optional[Invoice] = None

    if case.admission_id:
        inv = (db.execute(
            select(Invoice).where(
                Invoice.context_type == "ipd",
                Invoice.context_id == case.admission_id,
                Invoice.status != "cancelled",
            ).order_by(Invoice.id.desc())).scalars().first())

    # No IP admission or no invoice found -> create a fresh one
    context_type = "ipd" if case.admission_id else "ot"
    context_id = case.admission_id or case.id

    if inv is None:
        inv = Invoice(
            patient_id=case.patient_id,
            context_type=context_type,
            context_id=context_id,
            billing_type="ip_billing" if case.admission_id else "other",
            status="draft",
            remarks=f"Auto OT billing for case #{case.id}",
        )
        db.add(inv)
        db.flush()  # to get inv.id

    return inv


def _recalculate_invoice_totals(db: Session, invoice: Invoice) -> None:
    """
    Simple recalculation of invoice totals from its items.
    Replace with your central billing helper if you already have one.
    """
    items = (
        db.execute(
            select(InvoiceItem).where(
                InvoiceItem.invoice_id == invoice.id,
                InvoiceItem.is_voided == False)  # noqa: E712
        ).scalars().all())

    gross = Decimal("0.00")
    disc = Decimal("0.00")
    tax = Decimal("0.00")
    net = Decimal("0.00")

    for it in items:
        line_gross = (it.unit_price or 0) * (it.quantity or 0)
        gross += line_gross
        disc += it.discount_amount or 0
        tax += it.tax_amount or 0
        net += it.line_total or 0

    invoice.gross_total = gross
    invoice.discount_total = disc
    invoice.tax_total = tax
    invoice.net_total = net

    # If you want auto-balance = net_total for draft invoices:
    invoice.balance_due = net
    # amount_paid stays 0 until billing posts payment


def create_ot_invoice_items_for_case(
    db: Session,
    case: OtCase,
    schedule: Optional[OtSchedule] = None,
) -> Invoice:
    """
    Called when OT case is closed with outcome 'Completed'.

    - get or create an invoice
    - create OT line item(s)
    - recalc totals
    """
    invoice = _get_or_create_ipd_invoice_for_case(db, case)

    # Example: one line for primary procedure using your OT tariff.
    # Adjust according to your real tariff model.
    primary_proc: Optional[OtProcedure] = (
        case.primary_procedure  # if relationship exists
        if hasattr(case, "primary_procedure") else None)

    description_parts = []
    if primary_proc:
        description_parts.append(primary_proc.name or "")
    elif schedule and schedule.procedure_name:
        description_parts.append(schedule.procedure_name)
    else:
        description_parts.append(f"OT charges for case #{case.id}")

    if schedule and schedule.date:
        description_parts.append(f"on {schedule.date.isoformat()}")

    description = " ".join(p for p in description_parts if p).strip()

    # ---- Get tariff / price ----
    # For now, a fixed amount; replace with your OT tariff lookup.
    default_amount = Decimal(
        "0.00")  # 👈 put your default or tariff amount here

    # (Optional) if you store tariff on primary_proc, use that:
    if primary_proc and getattr(primary_proc, "tariff_amount",
                                None) is not None:
        default_amount = Decimal(str(primary_proc.tariff_amount))

    # Prevent duplicate OT posting for same case into same invoice:
    existing_ot_item = (
        db.execute(
            select(InvoiceItem).where(
                InvoiceItem.invoice_id == invoice.id,
                InvoiceItem.service_type == OT_SERVICE_TYPE,
                InvoiceItem.service_ref_id == case.id,
                InvoiceItem.is_voided == False,  # noqa: E712
            )).scalars().first())
    if existing_ot_item:
        # Already billed OT for this case – do nothing
        return invoice

    item = InvoiceItem(
        invoice_id=invoice.id,
        seq=len(invoice.items) + 1,
        service_type=OT_SERVICE_TYPE,
        service_ref_id=case.id,
        description=description[:300],
        quantity=1,
        unit_price=default_amount,
        tax_rate=0,
        discount_percent=0,
        discount_amount=0,
        tax_amount=0,
        line_total=default_amount,
    )
    db.add(item)
    db.flush()

    _recalculate_invoice_totals(db, invoice)

    return invoice
