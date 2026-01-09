from __future__ import annotations

from decimal import Decimal
from typing import Optional, Dict, Any, Tuple

from sqlalchemy.orm import Session

from app.models.billing import (
    BillingCase,
    BillingInvoiceLine,
    EncounterType,
    InvoiceType,
    PayerType,
    DocStatus,
    ServiceGroup,
    PayMode,
)
from app.services.billing_service import (
    BillingError,
    get_or_create_case_for_op_visit,
    get_or_create_case_for_ip_admission,
    get_or_create_active_module_invoice,
    add_auto_line_idempotent,
    add_payment_for_invoice,
    get_tariff_rate,
)

from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder
from app.models.pharmacy_prescription import PharmacySale, PharmacySaleItem


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


# ============================================================
# Helpers: Pharmacy item kind (MEDICINE vs CONSUMABLE)
# ============================================================
def _pharmacy_kind_for_item(db: Session, sale_item: PharmacySaleItem) -> str:
    """
    Returns:
      - "PHM" for medicines
      - "PHC" for consumables
    This is written to work even if your inventory item model differs.
    """
    # 1) if sale_item already has a hint
    for attr in ("is_consumable", "consumable", "is_medicine", "medicine"):
        if hasattr(sale_item, attr):
            v = getattr(sale_item, attr)
            if attr in ("is_consumable", "consumable") and bool(v) is True:
                return "PHC"
            if attr in ("is_medicine", "medicine") and bool(v) is True:
                return "PHM"

    # 2) try inventory item master if available
    item_id = getattr(sale_item, "item_id", None)
    if item_id:
        try:
            # adjust names if your inventory master differs
            from app.models.pharmacy_inventory import PharmacyItem  # type: ignore
            it = db.get(PharmacyItem, int(item_id))
            if it:
                # common fields
                if getattr(it, "is_consumable", False):
                    return "PHC"
                cat = (getattr(it, "category", None)
                       or getattr(it, "item_type", None) or "").lower()
                if "consum" in cat:
                    return "PHC"
                if "medicine" in cat or "drug" in cat:
                    return "PHM"
        except Exception:
            pass

    # 3) fallback: name-based heuristics (safe)
    name = (getattr(sale_item, "item_name", "") or "").lower()
    if any(k in name for k in [
            "syringe", "glove", "gauze", "cotton", "bandage", "mask", "tube",
            "needle"
    ]):
        return "PHC"
    return "PHM"


def _service_group_for_pharmacy(kind: str) -> ServiceGroup:
    """
    If your ServiceGroup has CONSUMABLE, use it.
    Otherwise keep PHARM and store kind in meta_json.
    """
    if kind == "PHC":
        if hasattr(ServiceGroup, "CONSUMABLE"):
            return getattr(ServiceGroup, "CONSUMABLE")
        if hasattr(ServiceGroup, "CONSUMABLES"):
            return getattr(ServiceGroup, "CONSUMABLES")
    return ServiceGroup.PHARM


# ============================================================
# OPD consultation (module = OPD)
# ============================================================
def autobill_opd_consultation(
    db: Session,
    *,
    visit_id: int,
    appointment_id: Optional[int],
    user,
    fee_amount: Optional[Decimal] = None,
    gst_rate: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """
    Creates:
      - BillingCase: (OP, visit_id)
      - Invoice: module="OPD"
      - Line: source_module="OPD", source_ref_id=visit_id, source_line_key="CONSULT"
    """
    case = get_or_create_case_for_op_visit(db,
                                           visit_id=int(visit_id),
                                           user=user)

    inv = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="OPD",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
    )

    # idempotent
    exists = (db.query(BillingInvoiceLine.id).filter(
        BillingInvoiceLine.billing_case_id == int(case.id),
        BillingInvoiceLine.source_module == "OPD",
        BillingInvoiceLine.source_ref_id == int(visit_id),
        BillingInvoiceLine.source_line_key == "CONSULT",
    ).first())
    if exists:
        return {
            "ok": True,
            "case_id": int(case.id),
            "invoice_id": int(inv.id),
            "added": False
        }

    if fee_amount is None:
        fee_amount = Decimal("0")
    if gst_rate is None:
        gst_rate = Decimal("0")

    created = add_auto_line_idempotent(
        db,
        invoice_id=int(inv.id),
        billing_case_id=int(case.id),
        user=user,
        service_group=ServiceGroup.CONSULT
        if hasattr(ServiceGroup, "CONSULT") else ServiceGroup.OTHER,
        item_type="OPD_CONSULT",
        item_id=None,
        item_code=None,
        description="OPD Consultation Fee",
        qty=Decimal("1"),
        unit_price=_d(fee_amount),
        gst_rate=_d(gst_rate),
        source_module="OPD",
        source_ref_id=int(visit_id),
        source_line_key="CONSULT",
        doctor_id=None,
        meta_patch={"opd": {
            "appointment_id": appointment_id
        }},
    )

    return {
        "ok": True,
        "case_id": int(case.id),
        "invoice_id": int(inv.id),
        "added": bool(created)
    }


# ============================================================
# LIS -> module="LAB"
# ============================================================
def autobill_lis_order(db: Session, *, lis_order_id: int,
                       user) -> Dict[str, Any]:
    o = db.get(LisOrder, int(lis_order_id))
    if not o:
        raise BillingError("LIS Order not found")

    # context: opd/ipd
    ct = (getattr(o, "context_type", None) or "").lower()
    cid = getattr(o, "context_id", None)

    if ct == "opd" and cid:
        case = get_or_create_case_for_op_visit(db,
                                               visit_id=int(cid),
                                               user=user)
    elif ct == "ipd" and cid:
        case = get_or_create_case_for_ip_admission(db,
                                                   admission_id=int(cid),
                                                   user=user)
    else:
        raise BillingError(f"Unsupported LIS context: {ct} / {cid}")

    inv = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="LAB",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
    )

    # items
    items = getattr(o, "items", None)
    if items is None:
        items = db.query(LisOrderItem).filter(
            LisOrderItem.order_id == int(o.id)).all()

    added = 0
    skipped = 0

    for it in items:
        test_id = int(getattr(it, "test_id", 0) or 0)
        test_name = getattr(it, "test_name", None) or getattr(
            it, "name", None) or "Lab Test"

        rate, gst = get_tariff_rate(
            db,
            tariff_plan_id=getattr(case, "tariff_plan_id", None),
            item_type="LAB_TEST",
            item_id=test_id,
        )

        created = add_auto_line_idempotent(
            db,
            invoice_id=int(inv.id),
            billing_case_id=int(case.id),
            user=user,
            service_group=ServiceGroup.LAB
            if hasattr(ServiceGroup, "LAB") else ServiceGroup.OTHER,
            item_type="LAB_TEST",
            item_id=test_id,
            item_code=None,
            description=str(test_name),
            qty=Decimal("1"),
            unit_price=rate,
            gst_rate=gst,
            source_module="LAB",
            source_ref_id=int(o.id),
            source_line_key=f"TEST:{test_id}",
            doctor_id=getattr(o, "ordering_user_id", None),
        )
        if created is None:
            skipped += 1
        else:
            added += 1

    # pointer optional
    if hasattr(o, "billing_invoice_id"):
        o.billing_invoice_id = int(inv.id)
    if hasattr(o, "billing_status"):
        o.billing_status = "billed"
    db.flush()

    return {
        "ok": True,
        "case_id": int(case.id),
        "invoice_id": int(inv.id),
        "added": added,
        "skipped": skipped
    }


# ============================================================
# RIS -> module="RIS"
# ============================================================
def autobill_ris_order(db: Session, *, ris_order_id: int,
                       user) -> Dict[str, Any]:
    o = db.get(RisOrder, int(ris_order_id))
    if not o:
        raise BillingError("RIS Order not found")

    ct = (getattr(o, "context_type", None) or "").lower()
    cid = getattr(o, "context_id", None)

    if ct == "opd" and cid:
        case = get_or_create_case_for_op_visit(db,
                                               visit_id=int(cid),
                                               user=user)
    elif ct == "ipd" and cid:
        case = get_or_create_case_for_ip_admission(db,
                                                   admission_id=int(cid),
                                                   user=user)
    else:
        raise BillingError(f"Unsupported RIS context: {ct} / {cid}")

    inv = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="RIS",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
    )

    test_id = int(getattr(o, "test_id", 0) or 0)
    test_name = getattr(o, "test_name", None) or getattr(
        o, "name", None) or "Radiology Test"

    rate, gst = get_tariff_rate(
        db,
        tariff_plan_id=getattr(case, "tariff_plan_id", None),
        item_type="RAD_TEST",
        item_id=test_id,
    )

    created = add_auto_line_idempotent(
        db,
        invoice_id=int(inv.id),
        billing_case_id=int(case.id),
        user=user,
        service_group=ServiceGroup.RAD
        if hasattr(ServiceGroup, "RAD") else ServiceGroup.OTHER,
        item_type="RAD_TEST",
        item_id=test_id,
        item_code=None,
        description=str(test_name),
        qty=Decimal("1"),
        unit_price=rate,
        gst_rate=gst,
        source_module="RIS",
        source_ref_id=int(o.id),
        source_line_key=f"TEST:{test_id}",
        doctor_id=getattr(o, "ordering_user_id", None),
    )

    if hasattr(o, "billing_invoice_id"):
        o.billing_invoice_id = int(inv.id)
    if hasattr(o, "billing_status"):
        o.billing_status = "billed"
    db.flush()

    return {
        "ok": True,
        "case_id": int(case.id),
        "invoice_id": int(inv.id),
        "added": 0 if created is None else 1,
        "skipped": 1 if created is None else 0
    }


# ============================================================
# PHARMACY: split into PHM (meds) + PHC (consumables)
# ============================================================
def autobill_pharmacy_sale(db: Session, *, sale_id: int,
                           user) -> Dict[str, Any]:
    sale = db.get(PharmacySale, int(sale_id))
    if not sale:
        raise BillingError("PharmacySale not found")

    visit_id = getattr(sale, "visit_id", None)
    admission_id = getattr(sale, "ipd_admission_id", None) or getattr(
        sale, "admission_id", None)

    if admission_id:
        case = get_or_create_case_for_ip_admission(
            db, admission_id=int(admission_id), user=user)
    elif visit_id:
        case = get_or_create_case_for_op_visit(db,
                                               visit_id=int(visit_id),
                                               user=user)
    else:
        raise BillingError(
            "Sale has no visit_id/admission_id to map encounter (counter sale)"
        )

    # module invoices
    inv_phm = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="PHM",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None)
    inv_phc = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="PHC",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None)

    items = db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == int(sale.id)).all()

    added_phm = 0
    skipped_phm = 0
    added_phc = 0
    skipped_phc = 0

    for it in items:
        kind = _pharmacy_kind_for_item(db, it)  # "PHM" or "PHC"
        target_inv = inv_phm if kind == "PHM" else inv_phc

        item_id = int(getattr(it, "item_id", 0) or 0)
        name = getattr(it, "item_name",
                       None) or ("Consumable" if kind == "PHC" else "Medicine")
        qty = _d(getattr(it, "quantity", 0) or 0)
        unit_price = _d(getattr(it, "unit_price", 0) or 0)

        # tariff preferred
        rate, gst = get_tariff_rate(
            db,
            tariff_plan_id=getattr(case, "tariff_plan_id", None),
            item_type="DRUG" if kind == "PHM" else "CONSUMABLE",
            item_id=item_id,
        )
        if rate <= 0 and unit_price > 0:
            rate = unit_price
        if gst <= 0:
            gst = _d(getattr(it, "tax_percent", 0) or 0)

        created = add_auto_line_idempotent(
            db,
            invoice_id=int(target_inv.id),
            billing_case_id=int(case.id),
            user=user,
            service_group=_service_group_for_pharmacy(kind),
            item_type="DRUG" if kind == "PHM" else "CONSUMABLE",
            item_id=item_id if item_id > 0 else None,
            item_code=None,
            description=str(name),
            qty=qty if qty != 0 else Decimal("1"),
            unit_price=rate,
            gst_rate=gst,
            source_module=
            kind,  # ✅ IMPORTANT: idempotency won’t clash (PHM vs PHC)
            source_ref_id=int(sale.id),
            source_line_key=f"SALE_ITEM:{getattr(it, 'id', item_id)}",
            doctor_id=None,
            meta_patch={"pharmacy": {
                "kind": kind,
                "sale_id": int(sale.id)
            }},
        )

        if kind == "PHM":
            if created is None:
                skipped_phm += 1
            else:
                added_phm += 1
        else:
            if created is None:
                skipped_phc += 1
            else:
                added_phc += 1

    db.flush()
    return {
        "ok": True,
        "case_id": int(case.id),
        "invoice_id_phm": int(inv_phm.id),
        "invoice_id_phc": int(inv_phc.id),
        "added_phm": added_phm,
        "skipped_phm": skipped_phm,
        "added_phc": added_phc,
        "skipped_phc": skipped_phc,
    }


def add_pharmacy_payment_for_sale(
    db: Session,
    *,
    sale_id: int,
    paid_amount: Decimal,
    user,
    mode: PayMode = PayMode.CASH,
) -> Dict[str, Any]:
    """
    Records payment with txn_ref "PHARM:<bill_number>".
    We attach it to PHM invoice by default; case-level reporting uses txn_ref anyway.
    """
    sale = db.get(PharmacySale, int(sale_id))
    if not sale:
        raise BillingError("PharmacySale not found")

    bill_res = autobill_pharmacy_sale(db, sale_id=int(sale_id), user=user)

    case_id = int(bill_res["case_id"])
    invoice_id = int(
        bill_res["invoice_id_phm"])  # attach payment to PHM invoice

    p = add_payment_for_invoice(
        db,
        billing_case_id=case_id,
        invoice_id=invoice_id,
        amount=_d(paid_amount),
        user=user,
        txn_ref=f"PHARM:{getattr(sale, 'bill_number', sale.id)}",
        mode=mode,
        notes="Payment captured from Pharmacy module",
    )
    db.flush()
    return {
        "ok": True,
        "payment_id": int(p.id),
        "case_id": case_id,
        "invoice_id": invoice_id
    }
