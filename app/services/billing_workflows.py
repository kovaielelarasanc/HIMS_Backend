# FILE: app/services/billing_workflows.py
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models.billing import (BillingCase, BillingInvoice,
                                BillingInvoiceLine, EncounterType,
                                BillingCaseStatus, InvoiceType, DocStatus,
                                PayerType, ServiceGroup, CoverageFlag)
from app.services.id_gen import next_billing_case_number, next_invoice_number
from app.services.billing_service import create_invoice, BillingError


def _tenant_id(user) -> Optional[int]:
    return getattr(user, "tenant_id", None) or getattr(user, "hospital_id",
                                                       None)


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


# ------------------------------------------------------------
# OT: Create case
# ------------------------------------------------------------
def get_or_create_case_for_ot_case(
        db: Session,
        ot_case_id: int,
        *,
        user,
        reset_period=None,  # kept for future (id_gen handles period)
) -> BillingCase:
    # adjust import path if your OT models differ
    from app.models.ot import OtCase  # ✅ change if needed

    ot = db.query(OtCase).get(int(ot_case_id))
    if not ot:
        raise BillingError("OT case not found")

    case = db.query(BillingCase).filter(
        BillingCase.encounter_type == EncounterType.OT,
        BillingCase.encounter_id == int(ot_case_id),
    ).first()
    if case:
        return case

    tenant_id = _tenant_id(user)
    patient_id = int(getattr(ot, "patient_id", 0) or 0)
    if not patient_id:
        raise BillingError("OT case has no patient_id")

    case = BillingCase(
        tenant_id=tenant_id,
        patient_id=patient_id,
        encounter_type=EncounterType.OT,
        encounter_id=int(ot_case_id),
        case_number="TEMP",
        status=BillingCaseStatus.OPEN,
        payer_mode=getattr(case, "payer_mode", None) or None,
        created_by=getattr(user, "id", None),
        updated_by=getattr(user, "id", None),
    )
    db.add(case)
    db.flush()

    # generate case number
    case.case_number = next_billing_case_number(db,
                                                tenant_id=tenant_id,
                                                encounter_type="OT")
    db.flush()
    return case


def _get_or_create_patient_invoice(db: Session, *, billing_case_id: int, user,
                                   module: str) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(billing_case_id),
        BillingInvoice.invoice_type == InvoiceType.PATIENT,
        BillingInvoice.payer_type == PayerType.PATIENT,
        BillingInvoice.status.in_([DocStatus.DRAFT, DocStatus.APPROVED]),
    ).order_by(BillingInvoice.id.desc()).first()
    if inv:
        # set module if available
        if hasattr(inv, "module") and module:
            inv.module = module
        return inv

    inv = create_invoice(
        db,
        billing_case_id=int(billing_case_id),
        user=user,
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
        module=module,
        encounter_type="OT",
    )
    return inv


# ------------------------------------------------------------
# OT: Autobill procedure (idempotent)
# ------------------------------------------------------------
def autobill_ot_case(db: Session, *, ot_case_id: int, user) -> Dict[str, Any]:
    from app.models.ot import OtCase, OtProcedure  # ✅ adjust if needed

    ot = db.query(OtCase).get(int(ot_case_id))
    if not ot:
        raise BillingError("OT case not found")

    case = get_or_create_case_for_ot_case(db,
                                          ot_case_id=int(ot_case_id),
                                          user=user)
    inv = _get_or_create_patient_invoice(db,
                                         billing_case_id=int(case.id),
                                         user=user,
                                         module="OT")

    # Resolve procedure + price
    proc_id = getattr(ot, "procedure_id", None) or getattr(
        ot, "ot_procedure_id", None)
    proc = db.query(OtProcedure).get(int(proc_id)) if proc_id else None

    desc = "OT Charges"
    rate = Decimal("0")
    if proc is not None:
        desc = getattr(proc, "name", None) or "OT Procedure"
        rate = _d(
            getattr(proc, "total_fixed_cost", None)
            or getattr(proc, "rate", None) or 0)

    # idempotent key
    source_module = "OT"
    source_ref_id = int(ot_case_id)
    source_line_key = f"OT_CASE:{int(ot_case_id)}"

    exists = db.query(BillingInvoiceLine.id).filter(
        BillingInvoiceLine.billing_case_id == int(case.id),
        BillingInvoiceLine.source_module == source_module,
        BillingInvoiceLine.source_ref_id == source_ref_id,
        BillingInvoiceLine.source_line_key == source_line_key,
    ).first()
    if exists:
        return {
            "ok": True,
            "skipped": True,
            "case_id": case.id,
            "invoice_id": inv.id
        }

    qty = Decimal("1")
    line_total = qty * rate
    disc = Decimal("0")
    gst = Decimal("0")
    tax = Decimal("0")
    net = line_total

    ln = BillingInvoiceLine(
        billing_case_id=int(case.id),
        invoice_id=int(inv.id),
        service_group=ServiceGroup.OT,
        item_type="OT_PROC",
        item_id=int(proc_id) if proc_id else None,
        item_code=None,
        description=str(desc),
        qty=qty,
        unit_price=rate,
        discount_percent=Decimal("0"),
        discount_amount=disc,
        gst_rate=gst,
        tax_amount=tax,
        line_total=line_total,
        net_amount=net,
        source_module=source_module,
        source_ref_id=source_ref_id,
        source_line_key=source_line_key,
        is_covered=CoverageFlag.NO,
        approved_amount=Decimal("0"),
        patient_pay_amount=net,
        requires_preauth=False,
        is_manual=False,
        created_by=getattr(user, "id", None),
    )
    db.add(ln)
    db.flush()

    # optional: set case ready
    try:
        case.status = BillingCaseStatus.READY_FOR_POST
        db.flush()
    except Exception:
        pass

    return {
        "ok": True,
        "skipped": False,
        "case_id": case.id,
        "invoice_id": inv.id,
        "line_id": ln.id
    }


# ------------------------------------------------------------
# ER: Create case (optional)
# ------------------------------------------------------------
def get_or_create_case_for_er_visit(db: Session,
                                    er_visit_id: int,
                                    *,
                                    user,
                                    reset_period=None) -> BillingCase:
    # If you don’t have ER model now, implement later.
    # For now raise a clear message.
    raise BillingError(
        "ER workflow not wired yet. Create ERVisit model + implement this function."
    )
