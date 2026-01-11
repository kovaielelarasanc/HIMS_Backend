# FILE: app/services/billing_hooks.py
from __future__ import annotations
from datetime import datetime, time
from decimal import Decimal
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from app.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    InvoiceType,
    PayerType,
    ServiceGroup,
    PayMode,
    DocStatus,
    NumberResetPeriod,
)
from app.services.billing_service import (
    BillingError,
    get_or_create_case_for_op_visit,
    get_or_create_case_for_ip_admission,
    get_or_create_active_module_invoice,
    add_auto_line_idempotent,
    add_payment_for_invoice,
    get_tariff_rate,
    add_manual_line,
)
from app.models.opd import Appointment, Visit, DoctorFee
from app.models.user import User
from app.models.department import Department
from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder
from app.models.pharmacy_prescription import PharmacySale, PharmacySaleItem
from app.services.billing_invoice_create import create_new_invoice_for_case

IST = ZoneInfo("Asia/Kolkata")


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def _sg_doctor_fee() -> ServiceGroup:
    # use your existing ServiceGroup for doctor fees
    for k in ("DOC", "DOCTOR", "CONSULTATION"):
        if k in ServiceGroup.__members__:
            return ServiceGroup[k]
    return list(ServiceGroup)[0]


def _safe_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    return dt.replace(tzinfo=None)


def _pick_user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    for k in ["full_name", "display_name", "name", "username", "email"]:
        v = getattr(u, k, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    fn = getattr(u, "first_name", None)
    ln = getattr(u, "last_name", None)
    if (isinstance(fn, str) and fn.strip()) or (isinstance(ln, str)
                                                and ln.strip()):
        return f"{(fn or '').strip()} {(ln or '').strip()}".strip() or None
    uid = getattr(u, "id", None)
    return f"User #{uid}" if uid else None


def _pick_amount_from_appointment(appt: Appointment) -> Optional[Decimal]:
    # try common fields if your appointment already stores manual amount
    for k in ("consult_amount", "consultation_amount", "consult_fee",
              "fee_amount", "amount"):
        v = getattr(appt, k, None)
        if v is not None and str(v) != "":
            return Decimal(str(v))
    return None


# ============================================================
# Helpers: Pharmacy item kind (MEDICINE vs CONSUMABLE)
# ============================================================
def _pharmacy_kind_for_item(db: Session, sale_item: PharmacySaleItem) -> str:
    """
    Returns:
      - "PHM" for medicines
      - "PHC" for consumables
    """
    # 1) if sale_item already has a hint
    for attr in ("is_consumable", "consumable", "is_medicine", "medicine"):
        if hasattr(sale_item, attr):
            v = getattr(sale_item, attr)
            if attr in ("is_consumable", "consumable") and bool(v) is True:
                return "PHC"
            if attr in ("is_medicine", "medicine") and bool(v) is True:
                return "PHM"

    # 2) try inventory master (best-effort)
    item_id = getattr(sale_item, "item_id", None)
    if item_id:
        try:
            from app.models.pharmacy_inventory import PharmacyItem  # type: ignore
            it = db.get(PharmacyItem, int(item_id))
            if it:
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

    # 3) fallback: name heuristics
    name = (getattr(sale_item, "item_name", "") or "").lower()
    if any(k in name for k in [
            "syringe", "glove", "gauze", "cotton", "bandage", "mask", "tube",
            "needle"
    ]):
        return "PHC"
    return "PHM"


def _service_group_for_pharmacy(kind: str) -> ServiceGroup:
    if kind == "PHC":
        if hasattr(ServiceGroup, "CONSUMABLE"):
            return getattr(ServiceGroup, "CONSUMABLE")
        if hasattr(ServiceGroup, "CONSUMABLES"):
            return getattr(ServiceGroup, "CONSUMABLES")
    return ServiceGroup.PHARM


# ============================================================
# OPD consultation (module = OPD)  ✅ SIGNATURE FIXED
# ============================================================
def autobill_opd_consultation(
        db: Session,
        *,
        appointment: Appointment,
        visit: Visit,
        user: User,
        amount: Optional[Decimal] = None,  # ✅ manual amount
) -> Optional[int]:
    if not appointment or not visit:
        return None

    # -----------------------------
    # 1) Billing Case (OP visit)
    # -----------------------------
    try:
        case = get_or_create_case_for_op_visit(
            db,
            visit_id=int(getattr(visit, "id")),
            user=user,
            tariff_plan_id=None,
            reset_period=NumberResetPeriod.NONE,
        )
    except TypeError:
        case = get_or_create_case_for_op_visit(
            db,
            visit_id=int(getattr(visit, "id")),
            user=user,
            tariff_plan_id=None,
        )

    # -----------------------------
    # 2) Ensure DOC invoice (draft)
    # -----------------------------
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case.id)).filter(
            BillingInvoice.status == DocStatus.DRAFT).filter(
                func.upper(func.coalesce(BillingInvoice.module, "MISC")) ==
                "DOC").filter(
                    BillingInvoice.invoice_type == InvoiceType.PATIENT).filter(
                        BillingInvoice.payer_type == PayerType.PATIENT).filter(
                            BillingInvoice.payer_id.is_(None)).order_by(
                                desc(BillingInvoice.id)).first())

    if not inv:
        inv = create_new_invoice_for_case(
            db,
            case=case,
            user=user,
            module="DOC",  # ✅ store under Doctor Fees module
            invoice_type=InvoiceType.PATIENT,
            payer_type=PayerType.PATIENT,
            payer_id=None,
            reset_period=NumberResetPeriod.YEAR,
            allow_duplicate_draft=False,
        )
        db.flush()

    # -----------------------------
    # 3) Idempotency
    # -----------------------------
    src_module = "OPD"
    src_ref_id = int(getattr(appointment, "id"))
    src_line_key = "CONSULT_FEE"

    exists = (db.query(BillingInvoiceLine.id).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).filter(
            BillingInvoiceLine.source_module == src_module).
              filter(BillingInvoiceLine.source_ref_id == src_ref_id).filter(
                  BillingInvoiceLine.source_line_key == src_line_key).first())
    if exists:
        return int(inv.id)

    # -----------------------------
    # 4) Service date
    # -----------------------------
    service_dt = _safe_naive(getattr(visit, "visit_at", None))
    if not service_dt:
        ap_date = getattr(appointment, "date", None)
        ap_slot = getattr(appointment, "slot_start", None)
        if ap_date:
            service_dt = datetime.combine(ap_date, ap_slot or time.min)

    # -----------------------------
    # 5) Doctor + Department (NO DoctorFee master)
    # -----------------------------
    doctor_user_id = (getattr(appointment, "doctor_user_id", None)
                      or getattr(appointment, "doctor_id", None))
    doctor_user_id = int(doctor_user_id) if doctor_user_id else None

    doctor = db.get(User, doctor_user_id) if doctor_user_id else None
    dept = None
    if doctor and getattr(doctor, "department_id", None):
        dept = db.get(Department, int(doctor.department_id))

    doctor_name = _pick_user_display_name(
        doctor) or f"Doctor #{doctor_user_id}" if doctor_user_id else None
    department_name = getattr(dept, "name", None) if dept else None
    department_id = getattr(dept, "id", None) if dept else None

    # -----------------------------
    # 6) Amount (manual)
    # -----------------------------
    if amount is None:
        amount = _pick_amount_from_appointment(appointment)

    if amount is None:
        # choose one behavior:
        # A) default to 0 (editable later)
        amount = Decimal("0")
        # B) OR force it:
        # raise Exception("Consultation amount missing. Pass amount=... or store it on Appointment.")

    desc_txt = "OPD Consultation"
    if doctor_name:
        desc_txt += f" - {doctor_name}"
    if department_name:
        desc_txt += f" ({department_name})"

    # -----------------------------
    # 7) Add line
    # -----------------------------
    ln = add_manual_line(
        db,
        invoice_id=int(inv.id),
        user=user,
        service_group=_sg_doctor_fee(),
        description=desc_txt,
        qty=Decimal("1"),
        unit_price=Decimal(str(amount)),
        gst_rate=Decimal("0"),
        discount_percent=Decimal("0"),
        discount_amount=Decimal("0"),
        item_type="OPD_CONSULT",
        item_id=int(getattr(appointment, "id")),
        item_code=f"OPD-CONSULT-{doctor_user_id or 'NA'}",
        doctor_id=doctor_user_id,
        manual_reason="Auto OPD consultation",
    )

    # mark as auto if you have is_manual
    if hasattr(ln, "is_manual"):
        ln.is_manual = False

    # set service_date
    if hasattr(ln, "service_date") and service_dt:
        ln.service_date = service_dt

    # source fields
    if hasattr(ln, "source_module"):
        ln.source_module = src_module
    if hasattr(ln, "source_ref_id"):
        ln.source_ref_id = src_ref_id
    if hasattr(ln, "source_line_key"):
        ln.source_line_key = src_line_key

    # ✅ save doctor/dept into meta_json so API can always show it
    if hasattr(ln, "meta_json"):
        ln.meta_json = {
            "appointment_id": int(getattr(appointment, "id")),
            "visit_id": int(getattr(visit, "id")),
            "doctor_id": doctor_user_id,
            "doctor_name": doctor_name,
            "department_id": department_id,
            "department_name": department_name,
        }

    db.add(ln)
    db.flush()

    return int(inv.id)


# ============================================================
# LIS -> module="LAB"
# ============================================================
def autobill_lis_order(db: Session, *, lis_order_id: int,
                       user: Any) -> Dict[str, Any]:
    o = db.get(LisOrder, int(lis_order_id))
    if not o:
        raise BillingError("LIS Order not found")

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
            is_manual=False,
            manual_reason=None,
        )
        if created is None:
            skipped += 1
        else:
            added += 1

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
                       user: Any) -> Dict[str, Any]:
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
        is_manual=False,
        manual_reason=None,
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
                           user: Any) -> Dict[str, Any]:
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

    inv_phm = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="PHM",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
    )
    inv_phc = get_or_create_active_module_invoice(
        db,
        billing_case_id=int(case.id),
        user=user,
        module="PHC",
        invoice_type=InvoiceType.PATIENT,
        payer_type=PayerType.PATIENT,
        payer_id=None,
    )

    items = db.query(PharmacySaleItem).filter(
        PharmacySaleItem.sale_id == int(sale.id)).all()

    added_phm = skipped_phm = added_phc = skipped_phc = 0

    for it in items:
        kind = _pharmacy_kind_for_item(db, it)  # "PHM" or "PHC"
        target_inv = inv_phm if kind == "PHM" else inv_phc

        item_id = int(getattr(it, "item_id", 0) or 0)
        name = getattr(it, "item_name",
                       None) or ("Consumable" if kind == "PHC" else "Medicine")
        qty = _d(getattr(it, "quantity", 0) or 0)
        unit_price = _d(getattr(it, "unit_price", 0) or 0)

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
            source_module=kind,  # ✅ idempotency separated: PHM vs PHC
            source_ref_id=int(sale.id),
            source_line_key=f"SALE_ITEM:{getattr(it, 'id', item_id)}",
            doctor_id=None,
            meta_patch={"pharmacy": {
                "kind": kind,
                "sale_id": int(sale.id)
            }},
            is_manual=False,
            manual_reason=None,
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
    user: Any,
    mode: PayMode = PayMode.CASH,
) -> Dict[str, Any]:
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
