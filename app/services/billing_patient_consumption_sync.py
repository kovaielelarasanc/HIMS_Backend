from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingNumberSeries,
    BillingTariffRate,
    BillingCaseStatus,
    DocStatus,
    EncounterType,
    InvoiceType,
    NumberDocType,
    NumberResetPeriod,
    PayerType,
    PayerMode,
    ServiceGroup,
)

from app.models.pharmacy_inventory import InventoryItem, ItemBatch


def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _period_key(reset: NumberResetPeriod, now: datetime) -> Optional[str]:
    if reset == NumberResetPeriod.NONE:
        return None
    if reset == NumberResetPeriod.YEAR:
        return f"{now.year}"
    if reset == NumberResetPeriod.MONTH:
        return f"{now.year}-{now.month:02d}"
    return None


def _next_billing_number(
    db: Session,
    *,
    doc_type: NumberDocType,
    prefix: str,
    reset_period: NumberResetPeriod = NumberResetPeriod.YEAR,
    padding: int = 6,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.utcnow()
    pk = _period_key(reset_period, now)

    row = db.execute(
        select(BillingNumberSeries)
        .where(
            and_(
                BillingNumberSeries.doc_type == doc_type,
                BillingNumberSeries.reset_period == reset_period,
                BillingNumberSeries.prefix == prefix,
                BillingNumberSeries.is_active == True,
            )
        )
        .with_for_update()
    ).scalar_one_or_none()

    if not row:
        row = BillingNumberSeries(
            doc_type=doc_type,
            prefix=prefix,
            reset_period=reset_period,
            padding=padding,
            next_number=1,
            last_period_key=pk,
            is_active=True,
        )
        db.add(row)
        db.flush()

    if reset_period != NumberResetPeriod.NONE and row.last_period_key != pk:
        row.last_period_key = pk
        row.next_number = 1

    seq = int(row.next_number)
    row.next_number = seq + 1
    db.flush()

    return f"{prefix}{seq:0{int(row.padding)}d}"


def _module_for_item(item: InventoryItem) -> str:
    t = (item.item_type or "").upper()
    if t in {"DRUG", "MED", "MEDICINE"}:
        return "PHM"
    return "PHC"


def _get_tariff_rate(
    db: Session,
    *,
    tariff_plan_id: Optional[int],
    item_id: int,
    fallback_price: Decimal,
    fallback_gst: Decimal,
) -> Tuple[Decimal, Decimal]:
    if tariff_plan_id:
        tr = db.execute(
            select(BillingTariffRate)
            .where(
                and_(
                    BillingTariffRate.tariff_plan_id == tariff_plan_id,
                    BillingTariffRate.item_type == "INV_ITEM",
                    BillingTariffRate.item_id == item_id,
                    BillingTariffRate.is_active == True,
                )
            )
        ).scalar_one_or_none()
        if tr:
            return (_d(tr.rate), _d(tr.gst_rate))
    return (fallback_price, fallback_gst)


def _recalc_invoice(db: Session, invoice_id: int) -> None:
    sums = db.execute(
        select(
            func.coalesce(func.sum(BillingInvoiceLine.line_total), 0),
            func.coalesce(func.sum(BillingInvoiceLine.discount_amount), 0),
            func.coalesce(func.sum(BillingInvoiceLine.tax_amount), 0),
            func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0),
        ).where(BillingInvoiceLine.invoice_id == invoice_id)
    ).one()

    sub_total = _d(sums[0])
    discount_total = _d(sums[1])
    tax_total = _d(sums[2])
    grand_total = _d(sums[3])

    inv = db.get(BillingInvoice, invoice_id)
    inv.sub_total = sub_total
    inv.discount_total = discount_total
    inv.tax_total = tax_total
    inv.round_off = Decimal("0")
    inv.grand_total = grand_total
    db.flush()


def ensure_billing_case(
    db: Session,
    *,
    patient_id: int,
    encounter_type: str,
    encounter_id: int,
    created_by: Optional[int],
) -> BillingCase:
    et = EncounterType(encounter_type)

    case = db.execute(
        select(BillingCase).where(
            and_(
                BillingCase.encounter_type == et,
                BillingCase.encounter_id == int(encounter_id),
            )
        )
    ).scalar_one_or_none()

    if case:
        return case

    case_no = _next_billing_number(
        db,
        doc_type=NumberDocType.CASE,
        prefix=f"CASE-{encounter_type}-",
        reset_period=NumberResetPeriod.YEAR,
        padding=6,
    )

    case = BillingCase(
        patient_id=patient_id,
        encounter_type=et,
        encounter_id=int(encounter_id),
        case_number=case_no,
        status=BillingCaseStatus.OPEN,
        payer_mode=PayerMode.SELF,
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(case)
    db.flush()
    return case


def get_or_create_draft_invoice(
    db: Session,
    *,
    billing_case_id: int,
    module: str,
    created_by: Optional[int],
) -> BillingInvoice:
    inv = db.execute(
        select(BillingInvoice).where(
            and_(
                BillingInvoice.billing_case_id == billing_case_id,
                BillingInvoice.module == module,
                BillingInvoice.status == DocStatus.DRAFT,
                BillingInvoice.invoice_type == InvoiceType.PATIENT,
            )
        )
    ).scalar_one_or_none()

    if inv:
        return inv

    inv_no = _next_billing_number(
        db,
        doc_type=NumberDocType.INVOICE,
        prefix=f"INV-{module}-",
        reset_period=NumberResetPeriod.YEAR,
        padding=6,
    )

    inv = BillingInvoice(
        billing_case_id=billing_case_id,
        invoice_number=inv_no,
        module=module,
        invoice_type=InvoiceType.PATIENT,
        status=DocStatus.DRAFT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
        created_by=created_by,
        updated_by=created_by,
        service_date=datetime.utcnow(),
    )
    db.add(inv)
    db.flush()
    return inv


def sync_consumption_to_billing(
    db: Session,
    *,
    consumption_id: int,
    patient_id: int,
    encounter_type: str,
    encounter_id: int,
    doctor_id: Optional[int],
    created_by: Optional[int],
    lines: List[dict],
    tariff_plan_id: Optional[int] = None,
) -> Tuple[int, List[int]]:
    """
    lines: [{line_id, item_id, qty, batch_id(optional), item_code, item_name, item_type}]
    """
    case = ensure_billing_case(
        db,
        patient_id=patient_id,
        encounter_type=encounter_type,
        encounter_id=encounter_id,
        created_by=created_by,
    )

    invoice_ids: List[int] = []
    invoice_by_module: Dict[str, BillingInvoice] = {}

    for ln in lines:
        item_id = int(ln["item_id"])
        qty = _d(ln["qty"])
        if qty <= 0:
            continue

        item = db.get(InventoryItem, item_id)
        if not item:
            continue

        module = _module_for_item(item)
        if module not in invoice_by_module:
            invoice_by_module[module] = get_or_create_draft_invoice(
                db,
                billing_case_id=case.id,
                module=module,
                created_by=created_by,
            )
            invoice_ids.append(invoice_by_module[module].id)

        inv = invoice_by_module[module]

        # fallback pricing from batch MRP + tax
        fallback_price = Decimal("0")
        fallback_gst = Decimal("0")

        batch_id = ln.get("batch_id")
        if batch_id:
            b = db.get(ItemBatch, int(batch_id))
            if b:
                fallback_price = _d(b.mrp)
                # your batch model uses tax_percent
                fallback_gst = _d(getattr(b, "tax_percent", 0))

        unit_price, gst_rate = _get_tariff_rate(
            db,
            tariff_plan_id=tariff_plan_id,
            item_id=item_id,
            fallback_price=fallback_price,
            fallback_gst=fallback_gst,
        )

        line_total = qty * unit_price
        tax_amount = (line_total * gst_rate) / Decimal("100")
        net_amount = line_total + tax_amount

        # idempotent line key
        source_module = "INV_CONS"
        source_ref_id = int(consumption_id)
        source_line_key = str(ln["line_id"])

        existing = db.execute(
            select(BillingInvoiceLine).where(
                and_(
                    BillingInvoiceLine.billing_case_id == case.id,
                    BillingInvoiceLine.source_module == source_module,
                    BillingInvoiceLine.source_ref_id == source_ref_id,
                    BillingInvoiceLine.source_line_key == source_line_key,
                )
            )
        ).scalar_one_or_none()

        desc = f"{item.name}"
        item_code = getattr(item, "code", None)

        if existing:
            existing.invoice_id = inv.id
            existing.service_group = ServiceGroup.PHARM
            existing.item_type = "INV_ITEM"
            existing.item_id = item_id
            existing.item_code = item_code
            existing.description = desc
            existing.qty = qty
            existing.unit_price = unit_price
            existing.gst_rate = gst_rate
            existing.tax_amount = tax_amount
            existing.line_total = line_total
            existing.net_amount = net_amount
            existing.doctor_id = doctor_id
        else:
            db.add(
                BillingInvoiceLine(
                    billing_case_id=case.id,
                    invoice_id=inv.id,
                    service_group=ServiceGroup.PHARM,
                    item_type="INV_ITEM",
                    item_id=item_id,
                    item_code=item_code,
                    description=desc,
                    qty=qty,
                    unit_price=unit_price,
                    discount_percent=Decimal("0"),
                    discount_amount=Decimal("0"),
                    gst_rate=gst_rate,
                    tax_amount=tax_amount,
                    line_total=line_total,
                    net_amount=net_amount,
                    doctor_id=doctor_id,
                    source_module=source_module,
                    source_ref_id=source_ref_id,
                    source_line_key=source_line_key,
                    is_manual=False,
                    created_by=created_by,
                )
            )

    db.flush()

    for inv in invoice_by_module.values():
        _recalc_invoice(db, inv.id)

    return case.id, invoice_ids
