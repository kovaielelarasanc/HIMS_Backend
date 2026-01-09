from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.billing import (BillingCase, BillingCaseLink, BillingInvoice,
                                BillingInvoiceLine, BillingPayment,
                                BillingAdvance, BillingAuditLog, EncounterType,
                                BillingCaseStatus, InvoiceType, DocStatus,
                                PayerType, ServiceGroup, CoverageFlag, PayMode,
                                AdvanceType)
from app.services.id_gen import (
    next_billing_case_number,
    next_invoice_number,
    next_receipt_number,
)
from app.models.opd import Visit, Appointment
from app.models.ipd import IpdAdmission
from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder
from app.models.ot import OtProcedure, OtSchedule, OtCase  # adjust import paths if different

# Masters used for manual-add search (your tables exist)
from app.models.lab_master import LabTest  # table: lab_tests
from app.models.radiology_master import RadiologyTest  # table: radiology_tests


# ============================================================
# Helpers
# ============================================================
def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _round2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def _line_calc(qty: Decimal, rate: Decimal, disc_amt: Decimal,
               gst_rate: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
    line_total = _round2(qty * rate)
    taxable = _round2(line_total - disc_amt)
    if taxable < 0:
        taxable = Decimal("0.00")
    tax_amt = _round2((taxable * gst_rate) / Decimal("100.0"))
    net = _round2(taxable + tax_amt)
    return line_total, tax_amt, net


def _audit(db: Session,
           entity_type: str,
           entity_id: int,
           action: str,
           *,
           user_id: Optional[int],
           old: Any = None,
           new: Any = None,
           reason: Optional[str] = None):
    db.add(
        BillingAuditLog(entity_type=entity_type,
                        entity_id=entity_id,
                        action=action,
                        old_json=old,
                        new_json=new,
                        reason=reason,
                        user_id=user_id))


def _ensure_invoice_open(invoice: BillingInvoice):
    if invoice.status in (DocStatus.POSTED, DocStatus.VOID):
        raise ValueError("Invoice is locked (POSTED/VOID).")


# ============================================================
# Case / Invoice Creation
# ============================================================
def get_or_create_case_for_op_visit(
    db: Session,
    visit_id: int,
    *,
    tenant_id: Optional[int],
    user_id: Optional[int],
) -> BillingCase:
    v = db.query(Visit).filter(Visit.id == visit_id).first()
    if not v:
        raise ValueError("OPD Visit not found")

    case = db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.OP,
        BillingCase.encounter_id == visit_id).first()
    if case:
        return case

    # Create case
    with db.begin_nested():
        case = BillingCase(
            tenant_id=tenant_id,
            patient_id=v.patient_id,
            encounter_type=EncounterType.OP,
            encounter_id=visit_id,
            case_number=next_billing_case_number(db,
                                                 tenant_id=tenant_id,
                                                 encounter_type="OP"),
            status=BillingCaseStatus.OPEN,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(case)
        db.flush()

        # link visit
        db.add(
            BillingCaseLink(billing_case_id=case.id,
                            entity_type="VISIT",
                            entity_id=visit_id))

        # link appointment if exists
        if getattr(v, "appointment_id", None):
            db.add(
                BillingCaseLink(billing_case_id=case.id,
                                entity_type="APPOINTMENT",
                                entity_id=int(v.appointment_id)))

        _audit(db,
               "case",
               case.id,
               "create",
               user_id=user_id,
               new={
                   "encounter": "OP",
                   "visit_id": visit_id
               })

    return case


def get_or_create_case_for_ip_admission(
    db: Session,
    admission_id: int,
    *,
    tenant_id: Optional[int],
    user_id: Optional[int],
) -> BillingCase:
    adm = db.query(IpdAdmission).filter(
        IpdAdmission.id == admission_id).first()
    if not adm:
        raise ValueError("IPD Admission not found")

    case = db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.IP,
        BillingCase.encounter_id == admission_id).first()
    if case:
        return case

    with db.begin_nested():
        case = BillingCase(
            tenant_id=tenant_id,
            patient_id=adm.patient_id,
            encounter_type=EncounterType.IP,
            encounter_id=admission_id,
            case_number=next_billing_case_number(db,
                                                 tenant_id=tenant_id,
                                                 encounter_type="IP"),
            status=BillingCaseStatus.OPEN,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(case)
        db.flush()

        db.add(
            BillingCaseLink(billing_case_id=case.id,
                            entity_type="ADMISSION",
                            entity_id=admission_id))

        _audit(db,
               "case",
               case.id,
               "create",
               user_id=user_id,
               new={
                   "encounter": "IP",
                   "admission_id": admission_id
               })

    return case


def get_or_create_module_invoice(
        db: Session,
        case_id: int,
        *,
        invoice_type: InvoiceType = InvoiceType.PATIENT,
        payer_type: PayerType = PayerType.PATIENT,
        payer_id: Optional[int] = None,
        user_id: Optional[int],
        tenant_id: Optional[int],
        encounter_type: str,
        module: str,  # "LAB"|"RAD"|"PHARM"|"OT"|"ROOM"|"MISC"|...
) -> BillingInvoice:
    """
    Creates ONE DRAFT invoice per module per case (patient invoice).
    If you want multiple invoices per module, remove the unique logic here.
    """
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case_id,
        BillingInvoice.invoice_type == invoice_type,
        BillingInvoice.status == DocStatus.DRAFT,
        BillingInvoice.payer_type == payer_type,
        BillingInvoice.payer_id == payer_id,
        BillingInvoice.currency == "INR",
    ).order_by(BillingInvoice.id.desc()).first()

    if inv:
        return inv

    with db.begin_nested():
        inv = BillingInvoice(
            billing_case_id=case_id,
            invoice_number=next_invoice_number(db,
                                               tenant_id=tenant_id,
                                               encounter_type=encounter_type),
            invoice_type=invoice_type,
            status=DocStatus.DRAFT,
            payer_type=payer_type,
            payer_id=payer_id,
            currency="INR",
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(inv)
        db.flush()
        _audit(db,
               "invoice",
               inv.id,
               "create",
               user_id=user_id,
               new={
                   "module": module,
                   "case_id": case_id
               })
    return inv


# ============================================================
# Totals recompute
# ============================================================
def recompute_invoice_totals(db: Session, invoice_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == invoice_id).first()
    if not inv:
        raise ValueError("Invoice not found")
    _ensure_invoice_open(inv)

    lines = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == invoice_id).all()

    sub = Decimal("0.00")
    disc = Decimal("0.00")
    tax = Decimal("0.00")
    net = Decimal("0.00")

    for ln in lines:
        sub += _d(ln.line_total)
        disc += _d(ln.discount_amount)
        tax += _d(ln.tax_amount)
        net += _d(ln.net_amount)

    inv.sub_total = _round2(sub)
    inv.discount_total = _round2(disc)
    inv.tax_total = _round2(tax)
    inv.round_off = Decimal("0.00")  # you can implement round-off rule
    inv.grand_total = _round2(net)

    db.flush()
    return inv


# ============================================================
# Manual add item (your "master search -> pick -> add")
# ============================================================
def resolve_master_item_snapshot(db: Session, item_type: str,
                                 item_id: int) -> Dict[str, Any]:
    """
    For manual add, we fetch name/rate/code from masters.
    Supported now:
      - LAB_TEST -> lab_tests
      - RAD_TEST -> radiology_tests
      - OT_PROC  -> ot_procedures
    Extend similarly for drugs/room packages/procedure masters.
    """
    t = (item_type or "").upper().strip()

    if t == "LAB_TEST":
        x = db.query(LabTest).filter(LabTest.id == item_id).first()
        if not x:
            raise ValueError("Lab test not found")
        return {
            "code": getattr(x, "code", None) or str(item_id),
            "name": getattr(x, "name", None) or "Lab Test",
            "rate":
            _d(getattr(x, "price", None) or getattr(x, "rate", None) or 0),
            "gst": _d(getattr(x, "gst_rate", None) or 0),
            "service_group": ServiceGroup.LAB,
            "source_module": "LAB",
            "source_line_key": f"LAB_TEST:{item_id}",
        }

    if t == "RAD_TEST":
        x = db.query(RadiologyTest).filter(RadiologyTest.id == item_id).first()
        if not x:
            raise ValueError("Radiology test not found")
        return {
            "code": getattr(x, "code", None) or str(item_id),
            "name": getattr(x, "name", None) or "Radiology Test",
            "rate":
            _d(getattr(x, "price", None) or getattr(x, "rate", None) or 0),
            "gst": _d(getattr(x, "gst_rate", None) or 0),
            "service_group": ServiceGroup.RAD,
            "source_module": "RIS",
            "source_line_key": f"RAD_TEST:{item_id}",
        }

    if t == "OT_PROC":
        x = db.query(OtProcedure).filter(OtProcedure.id == item_id).first()
        if not x:
            raise ValueError("OT procedure not found")
        # Use total fixed cost by default
        total = _d(getattr(x, "total_fixed_cost", None) or 0)
        return {
            "code": getattr(x, "code", None) or str(item_id),
            "name": getattr(x, "name", None) or "OT Procedure",
            "rate": total,
            "gst": Decimal("0.00"),
            "service_group": ServiceGroup.OT,
            "source_module": "OT",
            "source_line_key": f"OT_PROC:{item_id}",
        }

    raise ValueError(f"Unsupported item_type: {item_type}")


def add_manual_line(
    db: Session,
    *,
    invoice_id: int,
    billing_case_id: int,
    item_type: Optional[str],
    item_id: Optional[int],
    description: Optional[str],
    qty: Decimal,
    unit_price: Decimal,
    gst_rate: Decimal,
    discount_amount: Decimal,
    doctor_id: Optional[int],
    revenue_head_id: Optional[int],
    cost_center_id: Optional[int],
    user_id: Optional[int],
    manual_reason: Optional[str] = None,
) -> BillingInvoiceLine:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == invoice_id).first()
    if not inv:
        raise ValueError("Invoice not found")
    _ensure_invoice_open(inv)

    if qty <= 0:
        raise ValueError("qty must be > 0")

    # if item_type/item_id provided, prefer master snapshot for description/rate if user passed 0
    if item_type and item_id:
        snap = resolve_master_item_snapshot(db, item_type, int(item_id))
        if not description:
            description = snap["name"]
        if unit_price is None or _d(unit_price) <= 0:
            unit_price = snap["rate"]
        if gst_rate is None:
            gst_rate = snap["gst"]

        service_group = snap["service_group"]
        source_module = snap["source_module"]
        source_ref_id = None
        source_line_key = snap["source_line_key"]
        item_code = snap["code"]
    else:
        service_group = ServiceGroup.MISC
        source_module = "MANUAL"
        source_ref_id = None
        source_line_key = None
        item_code = None

    qty_d = _d(qty)
    rate_d = _d(unit_price)
    disc_d = _d(discount_amount)
    gst_d = _d(gst_rate)

    line_total, tax_amt, net_amt = _line_calc(qty_d, rate_d, disc_d, gst_d)

    with db.begin_nested():
        ln = BillingInvoiceLine(
            billing_case_id=billing_case_id,
            invoice_id=invoice_id,
            service_group=service_group,
            item_type=(item_type.upper() if item_type else None),
            item_id=int(item_id) if item_id else None,
            item_code=item_code,
            description=description or "Manual Item",
            qty=qty_d,
            unit_price=rate_d,
            discount_percent=Decimal("0.00"),
            discount_amount=disc_d,
            gst_rate=gst_d,
            tax_amount=tax_amt,
            line_total=line_total,
            net_amount=net_amt,
            revenue_head_id=revenue_head_id,
            cost_center_id=cost_center_id,
            doctor_id=doctor_id,
            source_module=source_module,
            source_ref_id=source_ref_id,
            source_line_key=source_line_key,
            is_covered=CoverageFlag.NO,
            approved_amount=Decimal("0.00"),
            patient_pay_amount=net_amt,
            requires_preauth=False,
            is_manual=True,
            manual_reason=manual_reason or "Manual Add",
            created_by=user_id,
        )
        db.add(ln)
        db.flush()

        recompute_invoice_totals(db, invoice_id)
        _audit(db,
               "invoice_line",
               ln.id,
               "manual_add",
               user_id=user_id,
               new={
                   "invoice_id": invoice_id,
                   "desc": ln.description
               })

    return ln


# ============================================================
# Auto billing sync (LAB / RIS)
# ============================================================
def sync_lab_orders_to_invoice(
    db: Session,
    *,
    case: BillingCase,
    invoice: BillingInvoice,
    user_id: Optional[int],
) -> Dict[str, Any]:
    """
    Pulls LIS orders for the same context (OP/IP) that are not billed.
    Adds idempotent invoice lines from lis_order_items.
    Marks lis_orders as billed + links billing_invoice_id.
    """
    _ensure_invoice_open(invoice)

    ctx_type = "opd" if case.encounter_type == EncounterType.OP else "ipd" if case.encounter_type == EncounterType.IP else None
    if not ctx_type:
        return {"added": 0, "orders": 0}

    orders = db.query(LisOrder).filter(
        LisOrder.patient_id == case.patient_id,
        LisOrder.context_type == ctx_type,
        LisOrder.context_id == case.encounter_id,
        LisOrder.billing_status == "not_billed",
    ).all()

    added = 0
    with db.begin_nested():
        for o in orders:
            # link order to case
            db.add(
                BillingCaseLink(billing_case_id=case.id,
                                entity_type="LIS_ORDER",
                                entity_id=o.id))

            items = db.query(LisOrderItem).filter(
                LisOrderItem.order_id == o.id).all()
            for it in items:
                # idempotent key: LIS:order_id:test_id
                source_module = "LAB"
                source_ref_id = int(o.id)
                source_line_key = f"TEST:{int(it.test_id)}"

                exists = db.query(BillingInvoiceLine).filter(
                    BillingInvoiceLine.billing_case_id == case.id,
                    BillingInvoiceLine.source_module == source_module,
                    BillingInvoiceLine.source_ref_id == source_ref_id,
                    BillingInvoiceLine.source_line_key ==
                    source_line_key).first()
                if exists:
                    continue

                # rate: take from it? else set 0 and let manual/tariff apply later
                rate = Decimal("0.00")
                gst = Decimal("0.00")

                # If you have tariff rates, you can resolve here (optional)
                desc = getattr(it, "test_name", None) or "Lab Test"

                line_total, tax_amt, net_amt = _line_calc(
                    Decimal("1"), rate, Decimal("0.00"), gst)

                ln = BillingInvoiceLine(
                    billing_case_id=case.id,
                    invoice_id=invoice.id,
                    service_group=ServiceGroup.LAB,
                    item_type="LAB_TEST",
                    item_id=int(it.test_id),
                    item_code=getattr(it, "test_code", None),
                    description=desc,
                    qty=Decimal("1.0000"),
                    unit_price=rate,
                    discount_percent=Decimal("0.00"),
                    discount_amount=Decimal("0.00"),
                    gst_rate=gst,
                    tax_amount=tax_amt,
                    line_total=line_total,
                    net_amount=net_amt,
                    source_module=source_module,
                    source_ref_id=source_ref_id,
                    source_line_key=source_line_key,
                    is_manual=False,
                    created_by=user_id,
                )
                db.add(ln)
                added += 1

            # mark order billed
            o.billing_status = "billed"
            o.billing_invoice_id = invoice.id

        recompute_invoice_totals(db, invoice.id)
        _audit(db,
               "invoice",
               invoice.id,
               "sync_lab",
               user_id=user_id,
               new={
                   "orders": len(orders),
                   "lines_added": added
               })

    return {"added": added, "orders": len(orders)}


def sync_ris_orders_to_invoice(
    db: Session,
    *,
    case: BillingCase,
    invoice: BillingInvoice,
    user_id: Optional[int],
) -> Dict[str, Any]:
    """
    Pulls RIS orders for same context not billed.
    Adds one invoice line per ris_order (idempotent).
    """
    _ensure_invoice_open(invoice)

    ctx_type = "opd" if case.encounter_type == EncounterType.OP else "ipd" if case.encounter_type == EncounterType.IP else None
    if not ctx_type:
        return {"added": 0, "orders": 0}

    orders = db.query(RisOrder).filter(
        RisOrder.patient_id == case.patient_id,
        RisOrder.context_type == ctx_type,
        RisOrder.context_id == case.encounter_id,
        RisOrder.billing_status == "not_billed",
    ).all()

    added = 0
    with db.begin_nested():
        for o in orders:
            db.add(
                BillingCaseLink(billing_case_id=case.id,
                                entity_type="RIS_ORDER",
                                entity_id=o.id))

            source_module = "RIS"
            source_ref_id = int(o.id)
            source_line_key = f"TEST:{int(o.test_id)}"

            exists = db.query(BillingInvoiceLine).filter(
                BillingInvoiceLine.billing_case_id == case.id,
                BillingInvoiceLine.source_module == source_module,
                BillingInvoiceLine.source_ref_id == source_ref_id,
                BillingInvoiceLine.source_line_key == source_line_key).first()
            if exists:
                continue

            rate = Decimal("0.00")
            gst = Decimal("0.00")
            desc = getattr(o, "test_name", None) or "Radiology Test"

            line_total, tax_amt, net_amt = _line_calc(Decimal("1"), rate,
                                                      Decimal("0.00"), gst)

            ln = BillingInvoiceLine(
                billing_case_id=case.id,
                invoice_id=invoice.id,
                service_group=ServiceGroup.RAD,
                item_type="RAD_TEST",
                item_id=int(o.test_id),
                item_code=getattr(o, "test_code", None),
                description=desc,
                qty=Decimal("1.0000"),
                unit_price=rate,
                discount_percent=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                gst_rate=gst,
                tax_amount=tax_amt,
                line_total=line_total,
                net_amount=net_amt,
                source_module=source_module,
                source_ref_id=source_ref_id,
                source_line_key=source_line_key,
                is_manual=False,
                created_by=user_id,
            )
            db.add(ln)
            added += 1

            o.billing_status = "billed"
            o.billing_invoice_id = invoice.id

        recompute_invoice_totals(db, invoice.id)
        _audit(db,
               "invoice",
               invoice.id,
               "sync_ris",
               user_id=user_id,
               new={
                   "orders": len(orders),
                   "lines_added": added
               })

    return {"added": added, "orders": len(orders)}


# ============================================================
# Approve / Post / Void
# ============================================================
def approve_invoice(db: Session, invoice_id: int, *,
                    user_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == invoice_id).first()
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status != DocStatus.DRAFT:
        raise ValueError("Only DRAFT can be approved")

    with db.begin_nested():
        inv.status = DocStatus.APPROVED
        inv.approved_by = user_id
        inv.approved_at = func.now()
        inv.updated_by = user_id
        _audit(db, "invoice", inv.id, "approve", user_id=user_id)
    return inv


def post_invoice(db: Session, invoice_id: int, *,
                 user_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == invoice_id).first()
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status != DocStatus.APPROVED:
        raise ValueError("Only APPROVED can be posted")

    with db.begin_nested():
        inv.status = DocStatus.POSTED
        inv.posted_by = user_id
        inv.posted_at = func.now()
        inv.updated_by = user_id
        _audit(db, "invoice", inv.id, "post", user_id=user_id)
    return inv


def void_invoice(db: Session, invoice_id: int, *, user_id: int,
                 reason: str) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == invoice_id).first()
    if not inv:
        raise ValueError("Invoice not found")
    if inv.status == DocStatus.VOID:
        return inv
    if inv.status == DocStatus.POSTED:
        raise ValueError(
            "Posted invoices cannot be voided. Use credit/debit notes.")

    with db.begin_nested():
        inv.status = DocStatus.VOID
        inv.voided_by = user_id
        inv.voided_at = func.now()
        inv.void_reason = reason or "Void"
        inv.updated_by = user_id
        _audit(db, "invoice", inv.id, "void", user_id=user_id, reason=reason)
    return inv


# ============================================================
# Payments / Advances
# ============================================================
def add_payment(
    db: Session,
    *,
    case_id: int,
    invoice_id: Optional[int],
    payer_type: PayerType,
    payer_id: Optional[int],
    mode: PayMode,
    amount: Decimal,
    txn_ref: Optional[str],
    user_id: int,
    tenant_id: Optional[int],
    encounter_type: str,
) -> BillingPayment:
    if _d(amount) <= 0:
        raise ValueError("amount must be > 0")

    with db.begin_nested():
        p = BillingPayment(
            billing_case_id=case_id,
            invoice_id=invoice_id,
            payer_type=payer_type,
            payer_id=payer_id,
            mode=mode,
            amount=_round2(_d(amount)),
            txn_ref=txn_ref,
            received_by=user_id,
            notes=
            f"RCPT:{next_receipt_number(db, tenant_id=tenant_id, encounter_type=encounter_type)}",
        )
        db.add(p)
        db.flush()
        _audit(db,
               "payment",
               p.id,
               "create",
               user_id=user_id,
               new={
                   "amount": str(p.amount),
                   "mode": str(mode)
               })
    return p


def add_advance(
    db: Session,
    *,
    case_id: int,
    entry_type: AdvanceType,
    mode: PayMode,
    amount: Decimal,
    txn_ref: Optional[str],
    remarks: Optional[str],
    user_id: int,
) -> BillingAdvance:
    if _d(amount) <= 0:
        raise ValueError("amount must be > 0")

    with db.begin_nested():
        a = BillingAdvance(
            billing_case_id=case_id,
            entry_type=entry_type,
            mode=mode,
            amount=_round2(_d(amount)),
            txn_ref=txn_ref,
            entry_by=user_id,
            remarks=remarks,
        )
        db.add(a)
        db.flush()
        _audit(db,
               "advance",
               a.id,
               "create",
               user_id=user_id,
               new={
                   "amount": str(a.amount),
                   "type": str(entry_type)
               })
    return a


# ============================================================
# Dashboard summary
# ============================================================
def case_dashboard(db: Session, case_id: int) -> Dict[str, Any]:
    c = db.query(BillingCase).filter(BillingCase.id == case_id).first()
    if not c:
        raise ValueError("Case not found")

    invoices = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case_id).order_by(
            BillingInvoice.id.desc()).all()
    payments = db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case_id).order_by(
            BillingPayment.id.desc()).all()
    advances = db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case_id).order_by(
            BillingAdvance.id.desc()).all()

    total_invoiced = sum(
        [_d(x.grand_total) for x in invoices if x.status != DocStatus.VOID],
        Decimal("0.00"))
    total_paid = sum([_d(x.amount) for x in payments], Decimal("0.00"))
    total_adv = sum([
        _d(x.amount) for x in advances if x.entry_type == AdvanceType.ADVANCE
    ], Decimal("0.00"))

    due = _round2(total_invoiced - total_paid - total_adv)

    return {
        "case": {
            "id": c.id,
            "case_number": c.case_number,
            "patient_id": c.patient_id,
            "encounter_type": str(c.encounter_type),
            "encounter_id": c.encounter_id,
            "status": str(c.status),
        },
        "totals": {
            "invoiced": str(_round2(total_invoiced)),
            "paid": str(_round2(total_paid)),
            "advance": str(_round2(total_adv)),
            "due": str(due),
        },
        "invoices": [{
            "id": i.id,
            "invoice_number": i.invoice_number,
            "invoice_type": str(i.invoice_type),
            "status": str(i.status),
            "grand_total": str(i.grand_total),
        } for i in invoices]
    }
