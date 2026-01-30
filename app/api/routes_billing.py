# FILE: app/api/routes_billing.py
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from typing import Optional, Any, Dict, List
from fastapi.encoders import jsonable_encoder
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc, exists, and_
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from app.models.charge_item_master import ChargeItemMaster
from enum import Enum
from fastapi.responses import StreamingResponse
from io import BytesIO

from app.models.ui_branding import UiBranding
from app.services.pdfs.billing_case_export import build_full_case_pdf

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.payer import Payer, Tpa, CreditPlan
from app.models.department import Department
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPayment,
    BillingAdvance,
    BillingInsuranceCase,
    BillingPreauthRequest,
    BillingClaim,
    EncounterType,
    BillingCaseStatus,
    DocStatus,
    PayerMode,
    ServiceGroup,
    PayMode,
    AdvanceType,
    InvoiceType,
    PayerType,
    NumberResetPeriod,
    InsurancePayerKind,
    InsuranceStatus,
    PreauthStatus,
    ClaimStatus,
    PaymentKind,
    ReceiptStatus,
    BillingCaseLink,
)

from app.services.billing_service import (
    get_or_create_case_for_op_visit,
    get_or_create_case_for_ip_admission,
    create_invoice,
    add_manual_line,
    add_lines_from_lis_order,
    add_line_from_ris_order,
    approve_invoice,
    post_invoice,
    void_invoice,
    record_payment,
    record_payment_full,
    record_advance,
    BillingError,
    BillingStateError,
)
from app.services.billing_invoice_create import create_new_invoice_for_case
from app.services.billing_particulars import (
    list_particulars_meta,
    get_particular_options,
    add_particular_lines,
)
from app.schemas.charge_item_master import AddChargeItemLineIn, AddChargeItemLineOut
from app.services.billing_charge_item_service import add_charge_item_line_to_invoice, fetch_idempotent_existing_line
from app.services.billing_claims_service import (
    upsert_draft_claim_from_invoice,
    claim_submit,
    claim_acknowledge,
    claim_approve,
    claim_settle,
    claim_to_dict,
    get_claim,
)
# import service functions you will add below
from app.services.billing_finance import (
    apply_advances_to_selected_invoices,
    list_case_invoice_outstanding,
)
from app.services.billing_service import update_invoice_line, delete_invoice_line

try:
    from app.services.billing_finance import apply_advances_to_case, case_financials as case_financials_v2
except Exception:
    try:
        from app.services.billing_service import apply_advances_to_case, case_financials_v2 as case_financials_v2  # type: ignore
    except Exception:
        apply_advances_to_case = None  # type: ignore
        case_financials_v2 = None  # type: ignore

import io
import csv
try:
    from app.models.billing import BillingPaymentAllocation, BillingAdvanceApplication
except Exception:
    BillingPaymentAllocation = None  # type: ignore
    BillingAdvanceApplication = None  # type: ignore
from fastapi.responses import StreamingResponse, Response
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

try:
    from app.services.billing_ot import create_ot_invoice_items_for_case  # type: ignore
except Exception:
    create_ot_invoice_items_for_case = None  # type: ignore



# optional (safe)
try:
    from app.models.billing import PaymentDirection
except Exception:
    PaymentDirection = None  # type: ignore

router = APIRouter(prefix="/billing", tags=["Billing"])

import logging

logger = logging.getLogger(__name__)

from typing import Any, Dict, Optional, Tuple, List, Union

MODULES = {
    "ADM": "Admission Charges",
    "ROOM": "Observation / Room Charges",
    "BLOOD": "Blood Bank Charges",
    "LAB": "Clinical Lab Charges",
    "DIET": "Dietary Charges",
    "DOC": "Doctor Fees",
    "PHM": "Pharmacy Charges (Medicines)",
    "PHC": "Pharmacy Charges (Consumables)",
    "PROC": "Procedure Charges",
    "SCAN": "Scan Charges",
    "SURG": "Surgery Charges",
    "XRAY": "X-Ray Charges",
    "MISC": "Misc Charges",
    "OTT": "OT Theater Charges",
    "OTI": "OT Instrument Charges",
    "OTD": "OT Device Charges",
}

MODULE_COLUMNS = {
    "DEFAULT": [
        {
            "key": "service_date",
            "label": "Date"
        },
        {
            "key": "item_code",
            "label": "Code"
        },
        {
            "key": "description",
            "label": "Item Name"
        },
        {
            "key": "qty",
            "label": "Qty"
        },
        {
            "key": "unit_price",
            "label": "Unit Price"
        },
        {
            "key": "discount_amount",
            "label": "Discount"
        },
        {
            "key": "gst_rate",
            "label": "GST %"
        },
        {
            "key": "tax_amount",
            "label": "Tax"
        },
        {
            "key": "net_amount",
            "label": "Total"
        },
    ],
    "PHARMACY": [
        {
            "key": "service_date",
            "label": "Bill Date"
        },
        {
            "key": "item_code",
            "label": "Code"
        },
        {
            "key": "description",
            "label": "Item Name"
        },
        {
            "key": "meta.batch_id",
            "label": "Batch"
        },
        {
            "key": "meta.expiry_date",
            "label": "Expiry"
        },
        {
            "key": "qty",
            "label": "Qty"
        },
        {
            "key": "unit_price",
            "label": "Item Amount"
        },
        {
            "key": "meta.hsn_sac",
            "label": "HSN/SAC"
        },
        {
            "key": "meta.cgst_pct",
            "label": "CGST %"
        },
        {
            "key": "meta.sgst_pct",
            "label": "SGST %"
        },
        {
            "key": "tax_amount",
            "label": "Tax"
        },
        {
            "key": "net_amount",
            "label": "Total"
        },
    ],
}


def _now_utc():
    return datetime.utcnow()


def _norm_payer_bucket(
        payer_type: PayerType,
        payer_id: Optional[int]) -> tuple[PayerType, Optional[int]]:
    """
    PATIENT bucket must always have payer_id=None
    """
    pt = payer_type
    pid = int(payer_id) if payer_id is not None else None
    if str(_enum_value(pt)).upper() == "PATIENT":
        pid = None
    return pt, pid




def _d(x) -> Decimal:
    return Decimal(str(x or 0))


def _apply_active_payment_filters(q):
    # payment.status == ACTIVE
    if ReceiptStatus is not None and hasattr(BillingPayment, "status"):
        try:
            q = q.filter(BillingPayment.status == ReceiptStatus.ACTIVE)
        except Exception:
            pass

    # payment.direction == IN (ignore refunds/outgoing)
    if PaymentDirection is not None and hasattr(BillingPayment, "direction"):
        try:
            q = q.filter(BillingPayment.direction == PaymentDirection.IN)
        except Exception:
            pass

    return q


def _apply_active_alloc_filters(q):
    # allocation.status == ACTIVE
    if ReceiptStatus is not None and hasattr(BillingPaymentAllocation, "status"):
        try:
            q = q.filter(BillingPaymentAllocation.status == ReceiptStatus.ACTIVE)
        except Exception:
            pass
    return q


def _sum_allocated_for_invoice(db: Session, invoice_id: int) -> Decimal:
    """
    ✅ Correct source-of-truth:
    - Sum ACTIVE allocations for invoice (joined to ACTIVE payments).
    - PLUS legacy direct payments (payment.invoice_id == invoice_id) ONLY IF that payment has NO allocations.
    - Prevents double counting.
    """
    invoice_id = int(invoice_id)

    # If allocation model not wired -> legacy only
    if BillingPaymentAllocation is None:
        q = db.query(func.coalesce(func.sum(BillingPayment.amount), 0)).filter(
            BillingPayment.invoice_id == invoice_id
        )
        q = _apply_active_payment_filters(q)
        return _d(q.scalar())

    # 1) allocations sum
    aq = (
        db.query(func.coalesce(func.sum(BillingPaymentAllocation.amount), 0))
        .join(BillingPayment, BillingPayment.id == BillingPaymentAllocation.payment_id)
        .filter(BillingPaymentAllocation.invoice_id == invoice_id)
    )
    aq = _apply_active_alloc_filters(aq)
    aq = _apply_active_payment_filters(aq)
    alloc_sum = _d(aq.scalar())

    # 2) legacy direct payments WITHOUT allocations (anti-join)
    alloc_exists = exists().where(
        and_(
            BillingPaymentAllocation.payment_id == BillingPayment.id,
            # only consider active allocations as "exists"
            (BillingPaymentAllocation.status == ReceiptStatus.ACTIVE)
            if (ReceiptStatus is not None and hasattr(BillingPaymentAllocation, "status"))
            else True,
        )
    )

    dq = (
        db.query(func.coalesce(func.sum(BillingPayment.amount), 0))
        .filter(BillingPayment.invoice_id == invoice_id)
        .filter(~alloc_exists)
    )
    dq = _apply_active_payment_filters(dq)
    direct_sum = _d(dq.scalar())

    return alloc_sum + direct_sum


def _invoice_outstanding(db: Session, inv: BillingInvoice) -> Decimal:
    st = str(_enum_value(getattr(inv, "status", "")) or "").upper()
    if st == "VOID":
        return Decimal("0")

    gt = _d(getattr(inv, "grand_total", 0))
    paid = _sum_allocated_for_invoice(db, int(inv.id))
    out = gt - paid
    return out if out > 0 else Decimal("0")


def _invoice_outstanding(db: Session, inv: BillingInvoice) -> Decimal:
    gt = Decimal(str(getattr(inv, "grand_total", 0) or 0))
    paid = _sum_allocated_for_invoice(db, int(inv.id))
    out = gt - paid
    if out < 0:
        out = Decimal("0")
    return out


def _create_payment_row(
    db: Session,
    *,
    billing_case_id: int,
    invoice_id: int,
    user: User,
    amount: Decimal,
    mode: PayMode,
    txn_ref: Optional[str],
    notes: Optional[str],
    payer_type: PayerType,
    payer_id: Optional[int],
) -> BillingPayment:
    p = BillingPayment()

    # required core fields
    if hasattr(p, "billing_case_id"):
        p.billing_case_id = int(billing_case_id)
    if hasattr(p, "invoice_id"):
        p.invoice_id = int(invoice_id)

    p.amount = Decimal(str(amount))
    if hasattr(p, "mode"):
        p.mode = mode
    if hasattr(p, "txn_ref"):
        p.txn_ref = (txn_ref or "").strip() or None
    if hasattr(p, "notes"):
        p.notes = (notes or "").strip() or None

    if hasattr(p, "payer_type"):
        p.payer_type = payer_type
    if hasattr(p, "payer_id"):
        p.payer_id = int(payer_id) if payer_id is not None else None

    # status/kind (optional, handle safely)
    if hasattr(p, "status") and ReceiptStatus is not None:
        try:
            p.status = ReceiptStatus.ACTIVE
        except Exception:
            pass

    if hasattr(p, "kind") and PaymentKind is not None:
        # choose a "normal payment" kind if you have it, else keep None
        try:
            if "PAYMENT" in getattr(PaymentKind, "__members__", {}):
                p.kind = PaymentKind["PAYMENT"]
        except Exception:
            pass

    # timestamps / user refs
    if hasattr(p, "received_at") and getattr(p, "received_at", None) is None:
        p.received_at = _now_utc()
    if hasattr(p, "received_by") and getattr(p, "received_by", None) is None:
        p.received_by = getattr(user, "id", None)

    db.add(p)
    db.flush()
    return p


def _allocate_payment_to_invoice(
    db: Session,
    *,
    payment: BillingPayment,
    invoice_id: int,
    amount: Decimal,
):
    """
    If allocation table exists -> insert allocation row.
    Otherwise invoice_id on payment already links it (legacy).
    """
    if BillingPaymentAllocation is None:
        return None

    a = BillingPaymentAllocation()
    if hasattr(a, "tenant_id"):
        setattr(a, "tenant_id", getattr(payment, "tenant_id", None))

    if hasattr(a, "billing_case_id") and hasattr(payment, "billing_case_id"):
        setattr(a, "billing_case_id", int(getattr(payment, "billing_case_id")))

    setattr(a, "payment_id", int(getattr(payment, "id")))
    setattr(a, "invoice_id", int(invoice_id))
    setattr(a, "amount", Decimal(str(amount)))

    if hasattr(a, "status") and ReceiptStatus is not None:
        try:
            a.status = ReceiptStatus.ACTIVE
        except Exception:
            pass

    if hasattr(a, "allocated_at") and getattr(a, "allocated_at", None) is None:
        setattr(a, "allocated_at", _now_utc())

    db.add(a)
    db.flush()
    return a


def _find_payment_row_in_obj(db: Session,
                             obj: Any) -> Optional[BillingPayment]:
    """
    Try very hard to locate a BillingPayment ORM row inside an unknown return payload.
    - If we find an ORM row -> return it
    - If we find an id/payment_id -> db.get and return
    - Recurses into dict/list/tuple
    """
    if obj is None:
        return None

    # ORM row directly
    if isinstance(obj, BillingPayment):
        return obj

    # common "id carriers"
    def _try_id(v: Any) -> Optional[BillingPayment]:
        if v is None:
            return None
        try:
            pid = int(v)
        except Exception:
            return None
        row = db.get(BillingPayment, pid)
        return row

    # dict payload
    if isinstance(obj, dict):
        # If a nested "payment" exists
        if "payment" in obj:
            row = _find_payment_row_in_obj(db, obj.get("payment"))
            if row:
                return row

        # Try common id keys
        for k in ("payment_id", "billing_payment_id", "receipt_id", "id"):
            if k in obj and obj.get(k) is not None:
                row = _try_id(obj.get(k))
                if row:
                    return row

        # Recurse values
        for v in obj.values():
            row = _find_payment_row_in_obj(db, v)
            if row:
                return row

    # list/tuple/set payload
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            row = _find_payment_row_in_obj(db, v)
            if row:
                return row

    return None


def _find_payment_dict_in_obj(obj: Any) -> Optional[Dict[str, Any]]:
    """
    If no ORM row is discoverable, try to find a serialized payment dict
    (one that at least looks like a payment).
    """
    if obj is None:
        return None

    # Already a payment-like dict
    if isinstance(obj, dict):
        # common wrapper keys
        if "payment" in obj and isinstance(obj["payment"], dict):
            return obj["payment"]

        # heuristic: looks like payment
        keys = set(obj.keys())
        if {"billing_case_id", "amount"
            }.issubset(keys) and ("mode" in keys or "payer_type" in keys):
            return obj

        # search nested
        for v in obj.values():
            found = _find_payment_dict_in_obj(v)
            if found:
                return found

    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            found = _find_payment_dict_in_obj(v)
            if found:
                return found

    return None


def _extract_allocations(obj: Any) -> Optional[Any]:
    """
    Pull allocations if present. Handles different key names.
    """
    if isinstance(obj, dict):
        for k in ("allocations", "allocation", "allocs",
                  "payment_allocations"):
            if k in obj:
                return obj.get(k)
        # also allow nested
        if "data" in obj:
            return _extract_allocations(obj.get("data"))
    return None


def _pick_status(enum_cls, *preferred_names: str):
    """Return enum member if exists else None."""
    for n in preferred_names:
        if n and n in getattr(enum_cls, "__members__", {}):
            return enum_cls[n]
    return None


def _require_perm_code(user: User, code: str):
    # uses your existing deps.require_perm if present
    if getattr(user, "is_admin", False):
        return
    from app.api.deps import require_perm
    require_perm(user, code)


def _money(x) -> str:
    try:
        return str(Decimal(str(x or 0)))
    except Exception:
        return "0"


def _ensure_draft_misc_invoice_for_case(
    db: Session,
    *,
    case: BillingCase,
    user: User,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: Optional[int],
    allow_duplicate_draft: bool,
    reset_period: NumberResetPeriod,
) -> BillingInvoice:
    inv = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case.id)).filter(
            BillingInvoice.status == DocStatus.DRAFT).filter(
                (BillingInvoice.module == None)
                | (BillingInvoice.module == "")
                | (BillingInvoice.module == "MISC")).filter(
                    BillingInvoice.invoice_type == invoice_type).filter(
                        BillingInvoice.payer_type == payer_type).filter(
                            BillingInvoice.payer_id == payer_id).order_by(
                                desc(BillingInvoice.id)).first())

    if inv:
        if (getattr(inv, "module", None) or "").strip() == "":
            inv.module = "MISC"
        return inv

    inv = create_new_invoice_for_case(
        db,
        case=case,
        user=user,
        module="MISC",
        invoice_type=invoice_type,
        payer_type=payer_type,
        payer_id=payer_id,
        reset_period=reset_period,
        allow_duplicate_draft=allow_duplicate_draft,
    )
    db.flush()
    return inv


def _ensure_draft_charge_item_invoice_for_case(
    db: Session,
    *,
    case: BillingCase,
    user: User,
    module: str,
    invoice_type: InvoiceType,
    payer_type: PayerType,
    payer_id: Optional[int],
    allow_duplicate_draft: bool,
    reset_period: NumberResetPeriod,
) -> BillingInvoice:
    mod = _normalize_module(module)

    qry = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case.id)).filter(
            BillingInvoice.status == DocStatus.DRAFT).filter(
                BillingInvoice.invoice_type == invoice_type).filter(
                    BillingInvoice.payer_type == payer_type).filter(
                        BillingInvoice.payer_id == payer_id))

    # Treat NULL/"" as MISC only when expected module is MISC
    if mod == "MISC":
        qry = qry.filter(
            (BillingInvoice.module == None)
            | (BillingInvoice.module == "")
            | (func.upper(func.coalesce(BillingInvoice.module, "MISC")) ==
               "MISC"))
    else:
        qry = qry.filter(
            func.upper(func.coalesce(BillingInvoice.module, "")) == mod)

    inv = qry.order_by(desc(BillingInvoice.id)).first()

    if inv:
        # normalize blank module
        if (getattr(inv, "module", None) or "").strip() == "":
            inv.module = mod
        return inv

    inv = create_new_invoice_for_case(
        db,
        case=case,
        user=user,
        module=mod,
        invoice_type=invoice_type,
        payer_type=payer_type,
        payer_id=payer_id,
        reset_period=reset_period,
        allow_duplicate_draft=allow_duplicate_draft,
    )
    db.flush()
    return inv


@router.post("/cases/{case_id}/charge-items/add",
             response_model=AddChargeItemLineOut)
def add_charge_item_to_case_misc_invoice(
        case_id: int,
        inp: AddChargeItemLineIn,
        invoice_type: InvoiceType = Query(InvoiceType.PATIENT),
        payer_type: PayerType = Query(PayerType.PATIENT),
        payer_id: Optional[int] = Query(None),
        reset_period: NumberResetPeriod = Query(NumberResetPeriod.YEAR),
        allow_duplicate_draft: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv: Optional[BillingInvoice] = None  # ✅ local, NOT a FastAPI param
    case = _get_case_or_404(db, user, int(case_id))

    # ✅ Determine correct module based on ChargeItemMaster
    ci = db.get(ChargeItemMaster, int(inp.charge_item_id))
    if not ci or not getattr(ci, "is_active", False):
        raise HTTPException(status_code=404,
                            detail="Charge item not found / inactive")

    cat = (getattr(ci, "category", None) or "").strip().upper()

    # category-first routing (your requirement)
    if cat in {"ADM", "DIET", "BLOOD"}:
        target_module = cat
    else:
        # category=MISC (or unknown): try module_header only if it matches predefined MODULES
        mh = (getattr(ci, "module_header", None) or "").strip().upper()
        target_module = mh if mh in MODULES else "MISC"

    try:
        inv = _ensure_draft_charge_item_invoice_for_case(
            db,
            case=case,
            user=user,
            module=target_module,
            invoice_type=invoice_type,
            payer_type=payer_type,
            payer_id=payer_id,
            allow_duplicate_draft=allow_duplicate_draft,
            reset_period=reset_period,
        )

        inv2, line = add_charge_item_line_to_invoice(
            db,
            invoice_id=int(inv.id),
            charge_item_id=int(inp.charge_item_id),
            qty=inp.qty,
            unit_price=inp.unit_price,
            gst_rate=inp.gst_rate,
            discount_percent=inp.discount_percent,
            discount_amount=inp.discount_amount,
            idempotency_key=inp.idempotency_key,
            revenue_head_id=inp.revenue_head_id,
            cost_center_id=inp.cost_center_id,
            doctor_id=inp.doctor_id,
            manual_reason=inp.manual_reason,
            created_by=getattr(user, "id", None),
        )

        db.commit()
        db.refresh(inv2)
        db.refresh(line)
        return {"invoice": inv2, "line": line}

    except IntegrityError:
        db.rollback()
        if inp.idempotency_key and inv is not None:
            existing = fetch_idempotent_existing_line(
                db,
                billing_case_id=int(case.id),
                invoice_id=int(inv.id),
                idempotency_key=str(inp.idempotency_key),
            )
            if existing:
                inv3 = db.get(BillingInvoice, int(inv.id))
                return {"invoice": inv3, "line": existing}
        raise HTTPException(
            status_code=409,
            detail="Could not add line (duplicate or constraint error)")

    except Exception as e:
        db.rollback()
        _err(e)


def _require_draft_invoice(inv: BillingInvoice):
    st = _enum_value(inv.status)
    if st != "DRAFT":
        raise HTTPException(status_code=409,
                            detail="Invoice locked. Reopen to edit.")


# ============================================================
# ✅ Modules (your “Main Particulars” mapping)
# ============================================================
@router.get("/meta/particulars")
def billing_particulars_meta(user: User = Depends(current_user)):
    return list_particulars_meta()


@router.get("/cases/{case_id}/particulars/{code}/options")
def billing_particular_options(
        case_id: int,
        code: str,

        # BED filters
        ward_id: Optional[int] = Query(None),
        room_id: Optional[int] = Query(None),

        # DOCTOR filters
        department_id: Optional[int] = Query(None),
        dept_id: Optional[int] = Query(None),  # alias support

        # common filters
    search: str = Query(""),
        q: Optional[str] = Query(None),
        modality: str = Query(""),
        service_date: Optional[str] = Query(None),
        limit: int = Query(80),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    c = _get_case_or_404(db, user, case_id)
    s = (search or "").strip() or (q or "").strip()
    dep = department_id or dept_id

    return get_particular_options(
        db,
        case=c,
        code=(code or "").strip().upper(),
        ward_id=ward_id,
        room_id=room_id,
        department_id=dep,
        search=s,
        modality=modality,
        limit=limit,
        service_date_str=service_date,
    )


class ParticularLineIn(BaseModel):
    # ✅ Pydantic v2
    model_config = {"populate_by_name": True, "extra": "ignore"}

    line_key: Optional[str] = Field(None, alias="lineKey")

    item_id: Optional[int] = Field(None, alias="itemId")

    doctor_id: Optional[int] = Field(None, alias="doctorId")
    service_date: Optional[str] = Field(None, alias="serviceDate")

    qty: Decimal = Field(Decimal("1"))

    gst_rate: Decimal = Field(Decimal("0"), alias="gstRate")
    discount_percent: Decimal = Field(Decimal("0"), alias="discountPercent")
    discount_amount: Decimal = Field(Decimal("0"), alias="discountAmount")

    description: Optional[str] = None
    unit_price: Optional[Decimal] = Field(None, alias="unitPrice")

    ward_id: Optional[int] = Field(None, alias="wardId")
    room_id: Optional[int] = Field(None, alias="roomId")

    modality: Optional[str] = None
    duration_min: Optional[int] = Field(None, alias="durationMin")
    split_costs: Optional[bool] = Field(None, alias="splitCosts")
    hours: Optional[Decimal] = None

    # ✅ Pydantic v1


class ParticularAddIn(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    payer_type: PayerType = Field(PayerType.PATIENT, alias="payerType")
    payer_id: Optional[int] = Field(None, alias="payerId")
    invoice_type: Optional[InvoiceType] = Field(None, alias="invoiceType")

    service_date: Optional[str] = Field(None, alias="serviceDate")
    qty: Decimal = Decimal("1")

    gst_rate: Decimal = Field(Decimal("0"), alias="gstRate")
    discount_percent: Decimal = Field(Decimal("0"), alias="discountPercent")
    discount_amount: Decimal = Field(Decimal("0"), alias="discountAmount")

    description: Optional[str] = None
    unit_price: Optional[Decimal] = Field(None, alias="unitPrice")

    item_ids: Optional[List[int]] = Field(None, alias="itemIds")
    doctor_id: Optional[int] = Field(None, alias="doctorId")

    ward_id: Optional[int] = Field(None, alias="wardId")
    room_id: Optional[int] = Field(None, alias="roomId")

    modality: Optional[str] = None
    duration_min: Optional[int] = Field(None, alias="durationMin")
    split_costs: bool = Field(False, alias="splitCosts")
    hours: Optional[Decimal] = None

    lines: Optional[List[ParticularLineIn]] = None


@router.post("/cases/{case_id}/particulars/{code}/add")
def billing_particular_add(
        case_id: int,
        code: str,
        inp: ParticularAddIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)

        raw_lines = None
        if inp.lines:
            raw_lines = []
            for ln in inp.lines:
                if hasattr(ln, "model_dump"):
                    raw_lines.append(ln.model_dump())
                else:
                    raw_lines.append(ln.dict())

        res = add_particular_lines(
            db,
            case=c,
            user=user,
            code=(code or "").strip().upper(),
            payer_type=inp.payer_type,
            payer_id=inp.payer_id,
            invoice_type=inp.invoice_type,
            service_date_str=inp.service_date,
            qty=inp.qty,
            gst_rate=inp.gst_rate,
            discount_percent=inp.discount_percent,
            discount_amount=inp.discount_amount,
            description=inp.description,
            unit_price=inp.unit_price,
            item_ids=inp.item_ids,
            doctor_id=inp.doctor_id,
            ward_id=inp.ward_id,
            room_id=inp.room_id,
            modality=inp.modality,
            duration_min=inp.duration_min,
            split_costs=inp.split_costs,
            hours=inp.hours,
            lines=raw_lines,
        )

        db.commit()
        return res
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# Helpers
# ============================================================
def _err(e: Exception):
    if isinstance(e, BillingStateError):
        raise HTTPException(status_code=409, detail=str(e))
    if isinstance(e, BillingError):
        raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=500, detail=str(e))


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def _col(model, names: List[str]):
    for n in names:
        if hasattr(model, n):
            return getattr(model, n)
    return None


def _lc_like(col, text: str):
    t = (text or "").strip().lower()
    if not t:
        return None
    return func.lower(func.coalesce(col, "")).like(f"%{t}%")


def _patient_uhid_col():
    return _col(
        Patient,
        ["uhid", "mrn", "mrn_no", "patient_code", "reg_no", "registration_no"])


def _patient_phone_col():
    return _col(
        Patient,
        [
            "phone", "mobile", "mobile_no", "phone_number", "contact_no",
            "contact_number", "primary_phone", "whatsapp_no"
        ],
    )


def _patient_name_cols():
    first = _col(Patient, ["first_name", "fname"])
    last = _col(Patient, ["last_name", "lname"])
    full = _col(Patient, ["full_name", "name"])
    return first, last, full


# ============================================================
# Meta: payers / referrers
# ============================================================
@router.get("/meta/payers")
def meta_payers(
        q: str = Query(""),
        payer_type: str = Query(""),  # insurance|corporate|govt|...
        include_tpas: bool = Query(True),
        include_plans: bool = Query(True),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    t = (q or "").strip().lower()
    pt = (payer_type or "").strip().lower()

    pq = db.query(Payer).filter(Payer.is_active.is_(True))
    if pt:
        pq = pq.filter(func.lower(Payer.payer_type) == pt)
    if t:
        pq = pq.filter(or_(_lc_like(Payer.name, t), _lc_like(Payer.code, t)))

    payers = pq.order_by(Payer.name.asc()).limit(200).all()

    tpas = []
    if include_tpas:
        tq = db.query(Tpa).filter(Tpa.is_active.is_(True))
        if t:
            tq = tq.filter(or_(_lc_like(Tpa.name, t), _lc_like(Tpa.code, t)))
        tpas = tq.order_by(Tpa.name.asc()).limit(200).all()

    plans = []
    if include_plans:
        cq = db.query(CreditPlan).filter(CreditPlan.is_active.is_(True))
        if t:
            cq = cq.filter(
                or_(_lc_like(CreditPlan.name, t), _lc_like(CreditPlan.code,
                                                           t)))
        plans = cq.order_by(CreditPlan.name.asc()).limit(200).all()

    return {
        "payers": [{
            "id": x.id,
            "code": x.code,
            "name": x.name,
            "payer_type": x.payer_type
        } for x in payers],
        "tpas": [{
            "id": x.id,
            "code": x.code,
            "name": x.name,
            "payer_id": x.payer_id
        } for x in tpas],
        "credit_plans": [{
            "id": x.id,
            "code": x.code,
            "name": x.name,
            "payer_id": x.payer_id,
            "tpa_id": x.tpa_id
        } for x in plans],
    }


@router.get("/meta/referrers")
def meta_referrers(
        q: str = Query(""),
        limit: int = Query(50),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    t = (q or "").strip().lower()
    limit = min(max(int(limit or 50), 1), 200)

    uq = db.query(User)
    if t:
        name_col = _col(User,
                        ["name", "full_name", "display_name", "username"])
        email_col = _col(User, ["email"])
        phone_col = _col(User, ["phone", "mobile", "mobile_no"])
        conds = []
        if name_col is not None:
            conds.append(_lc_like(name_col, t))
        if email_col is not None:
            conds.append(_lc_like(email_col, t))
        if phone_col is not None:
            conds.append(_lc_like(phone_col, t))
        conds = [c for c in conds if c is not None]
        if conds:
            uq = uq.filter(or_(*conds))

    rows = uq.order_by(desc(getattr(User, "id"))).limit(limit).all()

    def _pick_name(u: User) -> str:
        for k in ["full_name", "display_name", "name", "username", "email"]:
            v = getattr(u, k, None)
            if v:
                return str(v)
        return f"User #{u.id}"

    return {
        "items": [{
            "id": int(u.id),
            "name": _pick_name(u),
            "email": getattr(u, "email", None)
        } for u in rows]
    }


# ============================================================
# Case Settings (Bill types + Referral)
# ============================================================
class CaseSettingsIn(BaseModel):
    payer_mode: Optional[PayerMode] = None

    default_payer_type: Optional[str] = None  # PATIENT|PAYER|TPA|CREDIT_PLAN
    default_payer_id: Optional[int] = None
    default_tpa_id: Optional[int] = None
    default_credit_plan_id: Optional[int] = None

    referral_user_id: Optional[int] = None
    referral_notes: Optional[str] = None


_ALLOWED_PAYER_TYPES = {"PATIENT", "PAYER", "TPA", "CREDIT_PLAN", ""}


@router.get("/cases/{case_id}/settings")
def get_case_settings(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    c = _get_case_or_404(db, user, case_id)
    return {
        "payer_mode": _enum_value(c.payer_mode),
        "default_payer_type": getattr(c, "default_payer_type", None),
        "default_payer_id": getattr(c, "default_payer_id", None),
        "default_tpa_id": getattr(c, "default_tpa_id", None),
        "default_credit_plan_id": getattr(c, "default_credit_plan_id", None),
        "referral_user_id": getattr(c, "referral_user_id", None),
        "referral_notes": getattr(c, "referral_notes", None),
    }


@router.put("/cases/{case_id}/settings")
def update_case_settings(
        case_id: int,
        inp: CaseSettingsIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)

        if inp.payer_mode is not None:
            c.payer_mode = inp.payer_mode

        if inp.default_payer_type is not None:
            pt = (inp.default_payer_type or "").strip().upper()
            if pt not in _ALLOWED_PAYER_TYPES:
                raise HTTPException(400, "Invalid default_payer_type")
            setattr(c, "default_payer_type", pt or None)

        if inp.default_payer_id is not None:
            setattr(
                c, "default_payer_id",
                int(inp.default_payer_id) if inp.default_payer_id else None)

        if inp.default_tpa_id is not None:
            setattr(c, "default_tpa_id",
                    int(inp.default_tpa_id) if inp.default_tpa_id else None)

        if inp.default_credit_plan_id is not None:
            setattr(
                c, "default_credit_plan_id",
                int(inp.default_credit_plan_id)
                if inp.default_credit_plan_id else None)

        if inp.referral_user_id is not None:
            setattr(
                c, "referral_user_id",
                int(inp.referral_user_id) if inp.referral_user_id else None)

        if inp.referral_notes is not None:
            setattr(c, "referral_notes", (inp.referral_notes or "").strip()
                    or None)

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"ok": True, "case": _case_to_dict(c, None)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# Invoice Summary (group-wise)
# ============================================================
@router.get("/cases/{case_id}/invoice-summary")
def case_invoice_summary(
        case_id: int,
        group_by: str = Query("module",
                              description="module|service_group|invoice"),
        module: str = Query(""),
        status: str = Query(""),  # DRAFT|APPROVED|POSTED|VOID
        service_group: str = Query(""),  # enum name
        q: str = Query(""),
        from_date: str = Query(""),  # YYYY-MM-DD
        to_date: str = Query(""),
        is_manual: Optional[bool] = Query(None),
        min_net: Optional[Decimal] = Query(None),
        max_net: Optional[Decimal] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    c = _get_case_or_404(db, user, case_id)

    gb = (group_by or "module").strip().lower()
    if gb not in {"module", "service_group", "invoice"}:
        gb = "module"

    t = (q or "").strip().lower()
    mod = (module or "").strip().upper()
    st = (status or "").strip().upper()
    sg = (service_group or "").strip().upper()

    qry = (db.query(BillingInvoice, BillingInvoiceLine, User, Department).join(
        BillingInvoiceLine,
        BillingInvoiceLine.invoice_id == BillingInvoice.id).outerjoin(
            User, User.id == BillingInvoiceLine.doctor_id).outerjoin(
                Department, Department.id == User.department_id).filter(
                    BillingInvoice.billing_case_id == int(c.id)))

    if mod:
        qry = qry.filter(
            func.upper(func.coalesce(BillingInvoice.module, "MISC")) == mod)
    if st and st in DocStatus.__members__:
        qry = qry.filter(BillingInvoice.status == DocStatus[st])
    if sg and sg in ServiceGroup.__members__:
        qry = qry.filter(BillingInvoiceLine.service_group == ServiceGroup[sg])
    if t:
        qry = qry.filter(
            or_(_lc_like(BillingInvoiceLine.description, t),
                _lc_like(BillingInvoiceLine.item_code, t)))
    if is_manual is not None and hasattr(BillingInvoiceLine, "is_manual"):
        qry = qry.filter(BillingInvoiceLine.is_manual.is_(bool(is_manual)))

    if (from_date or to_date) and hasattr(BillingInvoiceLine, "service_date"):
        if from_date:
            qry = qry.filter(
                func.date(BillingInvoiceLine.service_date) >= from_date)
        if to_date:
            qry = qry.filter(
                func.date(BillingInvoiceLine.service_date) <= to_date)

    if min_net is not None:
        qry = qry.filter(
            BillingInvoiceLine.net_amount >= Decimal(str(min_net)))
    if max_net is not None:
        qry = qry.filter(
            BillingInvoiceLine.net_amount <= Decimal(str(max_net)))

    rows = qry.order_by(desc(BillingInvoice.id),
                        BillingInvoiceLine.id.asc()).limit(5000).all()

    def group_key(inv: BillingInvoice, ln: BillingInvoiceLine) -> str:
        if gb == "invoice":
            return f"INV:{inv.id}"
        if gb == "service_group":
            return str(_enum_value(ln.service_group) or "OTHER")
        m = (getattr(inv, "module", None) or "MISC").strip().upper()
        return m

    def group_label(key: str) -> str:
        if key.startswith("INV:"):
            iid = int(key.split(":")[1])
            inv_obj = next(
                (inv for (inv, ln, doc, dept) in rows if int(inv.id) == iid),
                None)
            if inv_obj:
                mod2 = (inv_obj.module or "MISC").upper()
                num = inv_obj.invoice_number or f"#{inv_obj.id}"
                return f"{mod2} · {num}"
            return f"Invoice #{iid}"
        if gb == "service_group":
            return key
        return MODULES.get(key, key)

    groups: Dict[str, Dict[str, Any]] = {}
    total_net = Decimal("0")

    for inv, ln, doc, dept in rows:
        k = group_key(inv, ln)
        if k not in groups:
            groups[k] = {
                "key": k,
                "label": group_label(k),
                "total": Decimal("0"),
                "items": []
            }

        item = _line_to_dict(ln, inv=inv, doctor=doc, department=dept)
        item["invoice_id"] = int(inv.id)
        item["invoice_number"] = inv.invoice_number
        item["invoice_status"] = _enum_value(inv.status)
        item["module"] = (inv.module or "MISC").upper()

        net = Decimal(str(getattr(ln, "net_amount", 0) or 0))
        groups[k]["total"] += net
        total_net += net
        groups[k]["items"].append(item)

    out = list(groups.values())
    out.sort(key=lambda x: float(x["total"]), reverse=True)

    return {
        "case_id":
        int(c.id),
        "group_by":
        gb,
        "filters": {
            "module": mod or None,
            "status": st or None,
            "service_group": sg or None,
            "q": t or None,
            "from_date": from_date or None,
            "to_date": to_date or None,
            "is_manual": is_manual,
            "min_net": str(min_net) if min_net is not None else None,
            "max_net": str(max_net) if max_net is not None else None,
        },
        "totals": {
            "net_total": str(total_net)
        },
        "groups": [{
            "key": g["key"],
            "label": g["label"],
            "total": str(g["total"]),
            "count": len(g["items"]),
            "items": g["items"]
        } for g in out],
    }


def _normalize_module(module: Optional[str]) -> str:
    m = (module or "").strip().upper()
    if not m:
        return "MISC"
    if m not in MODULES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid module '{m}'. Allowed: {', '.join(MODULES.keys())}"
        )
    return m


def _case_to_dict(c: BillingCase,
                  p: Optional[Patient] = None) -> Dict[str, Any]:
    uhid_col = _patient_uhid_col()
    phone_col = _patient_phone_col()
    first_col, last_col, full_col = _patient_name_cols()

    patient_name = None
    uhid = None
    phone = None

    if p is not None:
        if full_col is not None:
            patient_name = (getattr(p, full_col.key, None)
                            or "").strip() or None
        else:
            fn = (getattr(p, first_col.key, "")
                  if first_col is not None else "") or ""
            ln = (getattr(p, last_col.key, "")
                  if last_col is not None else "") or ""
            patient_name = f"{fn} {ln}".strip() or None

        if uhid_col is not None:
            uhid = getattr(p, uhid_col.key, None)
        if phone_col is not None:
            phone = getattr(p, phone_col.key, None)

    return {
        "id": int(c.id),
        "case_number": c.case_number,
        "status": _enum_value(c.status),
        "payer_mode": _enum_value(c.payer_mode),
        "tariff_plan_id": c.tariff_plan_id,
        "notes": c.notes,
        "encounter_type": _enum_value(c.encounter_type),
        "encounter_id": int(c.encounter_id),
        "patient_id": int(c.patient_id),
        "patient_name": patient_name,
        "uhid": uhid,
        "phone": phone,

        # settings
        "default_payer_type": getattr(c, "default_payer_type", None),
        "default_payer_id": getattr(c, "default_payer_id", None),
        "default_tpa_id": getattr(c, "default_tpa_id", None),
        "default_credit_plan_id": getattr(c, "default_credit_plan_id", None),
        "referral_user_id": getattr(c, "referral_user_id", None),
        "referral_notes": getattr(c, "referral_notes", None),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _invoice_to_dict(inv: BillingInvoice) -> Dict[str, Any]:
    mod = (getattr(inv, "module", None) or "").strip().upper()
    return {
        "id":
        int(inv.id),
        "billing_case_id":
        int(inv.billing_case_id),
        "invoice_number":
        inv.invoice_number,
        "module":
        getattr(inv, "module", None),
        "module_label":
        MODULES.get(mod, None) if mod else None,
        "invoice_type":
        _enum_value(inv.invoice_type),
        "payer_type":
        _enum_value(inv.payer_type),
        "payer_id":
        inv.payer_id,
        "status":
        _enum_value(inv.status),
        "currency":
        getattr(inv, "currency", "INR"),
        "sub_total":
        str(getattr(inv, "sub_total", 0) or 0),
        "discount_total":
        str(getattr(inv, "discount_total", 0) or 0),
        "tax_total":
        str(getattr(inv, "tax_total", 0) or 0),
        "round_off":
        str(getattr(inv, "round_off", 0) or 0),
        "grand_total":
        str(getattr(inv, "grand_total", 0) or 0),
        "approved_at":
        inv.approved_at.isoformat()
        if getattr(inv, "approved_at", None) else None,
        "posted_at":
        inv.posted_at.isoformat() if getattr(inv, "posted_at", None) else None,
        "voided_at":
        inv.voided_at.isoformat() if getattr(inv, "voided_at", None) else None,
        "created_at":
        inv.created_at.isoformat()
        if getattr(inv, "created_at", None) else None,
        "updated_at":
        inv.updated_at.isoformat()
        if getattr(inv, "updated_at", None) else None,
    }


def _pick_user_display_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None

    # common fields
    for k in ["full_name", "display_name", "name", "username", "email"]:
        v = getattr(u, k, None)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # fallback: first_name + last_name
    fn = getattr(u, "first_name", None)
    ln = getattr(u, "last_name", None)
    if (isinstance(fn, str) and fn.strip()) or (isinstance(ln, str)
                                                and ln.strip()):
        return f"{(fn or '').strip()} {(ln or '').strip()}".strip() or None

    uid = getattr(u, "id", None)
    return f"User #{uid}" if uid else None


def _line_to_dict(
    ln: BillingInvoiceLine,
    *,
    inv: Optional[BillingInvoice] = None,
    doctor: Optional[User] = None,
    department: Optional[Department] = None,
) -> Dict[str, Any]:
    meta = getattr(ln, "meta_json", None)
    meta = meta if isinstance(meta, dict) else {}

    dt = (getattr(ln, "service_date", None) or getattr(ln, "created_at", None)
          or getattr(inv, "created_at", None))
    service_date_out = dt.isoformat() if dt else None

    # ✅ doctor name: join first, else meta fallback
    doc_name = _pick_user_display_name(doctor) or meta.get(
        "doctor_name") or meta.get("consultant_name")

    # ✅ dept: join first, else meta fallback
    dept_name = (getattr(department, "name", None) if department else
                 None) or meta.get("department_name") or meta.get("dept_name")
    dept_id = (getattr(department, "id", None)
               if department else None) or meta.get("department_id")

    return {
        "id": int(ln.id),
        "billing_case_id": int(ln.billing_case_id),
        "invoice_id": int(ln.invoice_id),
        "service_group": _enum_value(ln.service_group),
        "item_type": ln.item_type,
        "item_id": ln.item_id,
        "item_code": ln.item_code,
        "description": ln.description,
        "qty": str(getattr(ln, "qty", 0) or 0),
        "unit_price": str(getattr(ln, "unit_price", 0) or 0),
        "discount_percent": str(getattr(ln, "discount_percent", 0) or 0),
        "discount_amount": str(getattr(ln, "discount_amount", 0) or 0),
        "gst_rate": str(getattr(ln, "gst_rate", 0) or 0),
        "tax_amount": str(getattr(ln, "tax_amount", 0) or 0),
        "line_total": str(getattr(ln, "line_total", 0) or 0),
        "net_amount": str(getattr(ln, "net_amount", 0) or 0),
        "service_date": service_date_out,
        "meta_json": meta,
        "meta": meta,
        "revenue_head_id": ln.revenue_head_id,
        "cost_center_id": ln.cost_center_id,
        "doctor_id": ln.doctor_id,
        "doctor_name": doc_name,
        "department_id": dept_id,
        "department_name": dept_name,
        "source_module": ln.source_module,
        "source_ref_id": ln.source_ref_id,
        "source_line_key": ln.source_line_key,
        "is_manual": getattr(ln, "is_manual", False),
        "manual_reason": getattr(ln, "manual_reason", None),
        "created_at": ln.created_at.isoformat() if ln.created_at else None,
        "updated_at": ln.updated_at.isoformat() if ln.updated_at else None,
    }


# def _payment_to_dict(p: BillingPayment) -> Dict[str, Any]:
#     return {
#         "id":
#         int(p.id),
#         "billing_case_id":
#         int(p.billing_case_id),
#         "invoice_id":
#         int(p.invoice_id) if p.invoice_id else None,
#         "payer_type":
#         _enum_value(getattr(p, "payer_type", None)),
#         "payer_id":
#         getattr(p, "payer_id", None),
#         "mode":
#         _enum_value(getattr(p, "mode", None)),
#         "amount":
#         str(getattr(p, "amount", 0) or 0),
#         "txn_ref":
#         getattr(p, "txn_ref", None),
#         "notes":
#         getattr(p, "notes", None),
#         "received_at":
#         p.received_at.isoformat() if getattr(p, "received_at", None) else None,
#         "created_at":
#         p.created_at.isoformat() if getattr(p, "created_at", None) else None,
#         "received_by":
#         getattr(p, "received_by", None),
#     }


def _advance_to_dict(a: BillingAdvance) -> Dict[str, Any]:
    return {
        "id": int(a.id),
        "billing_case_id": int(a.billing_case_id),
        "entry_type": _enum_value(getattr(a, "entry_type", None)),
        "mode": _enum_value(getattr(a, "mode", None)),
        "amount": str(getattr(a, "amount", 0) or 0),
        "txn_ref": getattr(a, "txn_ref", None),
        "remarks": getattr(a, "remarks", None),
        "entry_at":
        a.entry_at.isoformat() if getattr(a, "entry_at", None) else None,
        "entry_by": getattr(a, "entry_by", None),
    }


def _get_case_or_404(db: Session, user: User, case_id: int) -> BillingCase:
    c = db.query(BillingCase).filter(BillingCase.id == int(case_id)).first()
    if not c:
        raise HTTPException(status_code=404, detail="Billing case not found")
    return c


def _get_invoice_or_404(db: Session, user: User,
                        invoice_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


# ============================================================
# ✅ Meta endpoints for frontend dynamic UI
# ============================================================
@router.get("/meta/modules")
def billing_modules_meta(user: User = Depends(current_user)):
    return {
        "modules": [{
            "code": k,
            "label": v
        } for k, v in MODULES.items()],
        "columns": MODULE_COLUMNS,
    }


# ============================================================
# Manual Case Create: Schemas + Encounter loaders
# ============================================================
class ManualCaseCreateIn(BaseModel):
    patient_id: int = Field(..., gt=0)
    encounter_type: EncounterType
    encounter_id: int = Field(..., gt=0)
    tariff_plan_id: Optional[int] = None
    reset_period: NumberResetPeriod = NumberResetPeriod.NONE


def _pick_dt_value(obj, candidate_fields: List[str]) -> Optional[datetime]:
    for f in candidate_fields:
        if hasattr(obj, f):
            v = getattr(obj, f, None)
            if isinstance(v, datetime):
                return v
    return None


def _load_op_encounters(db: Session, patient_id: int, limit: int = 100):
    items = []
    try:
        from app.models.opd import Visit
        q = db.query(Visit).filter(
            getattr(Visit, "patient_id") == int(patient_id))
        q = q.order_by(desc(getattr(Visit, "created_at",
                                    Visit.id))).limit(int(limit))
        rows = q.all()
        for r in rows:
            dt = _pick_dt_value(r, [
                "visit_dt", "visited_at", "start_time", "checkin_at",
                "created_at", "updated_at"
            ])
            items.append({
                "encounter_id": int(getattr(r, "id")),
                "encounter_at": dt.isoformat() if dt else None
            })
        return items
    except Exception:
        pass

    try:
        from app.models.opd import Appointment
        q = db.query(Appointment).filter(
            getattr(Appointment, "patient_id") == int(patient_id))
        q = q.order_by(desc(getattr(Appointment, "created_at",
                                    Appointment.id))).limit(int(limit))
        rows = q.all()
        for r in rows:
            dt = _pick_dt_value(r, [
                "appointment_dt", "scheduled_at", "start_at", "created_at",
                "updated_at"
            ])
            items.append({
                "encounter_id": int(getattr(r, "id")),
                "encounter_at": dt.isoformat() if dt else None
            })
        return items
    except Exception:
        return []


def _load_ip_encounters(db: Session, patient_id: int, limit: int = 100):
    candidates = [
        ("app.models.ipd", "IpdAdmission"),
        ("app.models.ipd", "Admission"),
        ("app.models.ipd_admission", "IpdAdmission"),
        ("app.models.ipd_admissions", "IpdAdmission"),
    ]
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            Model = getattr(m, cls)
            q = db.query(Model).filter(
                getattr(Model, "patient_id") == int(patient_id))
            q = q.order_by(
                desc(
                    getattr(Model, "admission_at",
                            getattr(Model, "created_at",
                                    Model.id)))).limit(int(limit))
            rows = q.all()
            out = []
            for r in rows:
                dt = _pick_dt_value(r, [
                    "admission_at", "admitted_at", "admission_dt",
                    "created_at", "updated_at"
                ])
                out.append({
                    "encounter_id": int(getattr(r, "id")),
                    "encounter_at": dt.isoformat() if dt else None
                })
            return out
        except Exception:
            continue
    return []


def _load_ot_encounters(db: Session, patient_id: int, limit: int = 100):
    candidates = [
        ("app.models.ot", "OtCase"),
        ("app.models.ot", "OTCase"),
        ("app.models.ot_case", "OtCase"),
    ]
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            Model = getattr(m, cls)
            q = db.query(Model).filter(
                getattr(Model, "patient_id") == int(patient_id))
            q = q.order_by(
                desc(
                    getattr(Model, "scheduled_at",
                            getattr(Model, "created_at",
                                    Model.id)))).limit(int(limit))
            rows = q.all()
            out = []
            for r in rows:
                dt = _pick_dt_value(r, [
                    "scheduled_at", "surgery_at", "procedure_at", "created_at",
                    "updated_at"
                ])
                out.append({
                    "encounter_id": int(getattr(r, "id")),
                    "encounter_at": dt.isoformat() if dt else None
                })
            return out
        except Exception:
            continue
    return []


def _load_er_encounters(db: Session, patient_id: int, limit: int = 100):
    candidates = [
        ("app.models.er", "ErVisit"),
        ("app.models.er", "ERVisit"),
        ("app.models.er_visit", "ErVisit"),
    ]
    for mod, cls in candidates:
        try:
            m = __import__(mod, fromlist=[cls])
            Model = getattr(m, cls)
            q = db.query(Model).filter(
                getattr(Model, "patient_id") == int(patient_id))
            q = q.order_by(
                desc(
                    getattr(Model, "arrived_at",
                            getattr(Model, "created_at",
                                    Model.id)))).limit(int(limit))
            rows = q.all()
            out = []
            for r in rows:
                dt = _pick_dt_value(
                    r, ["arrived_at", "visit_at", "created_at", "updated_at"])
                out.append({
                    "encounter_id": int(getattr(r, "id")),
                    "encounter_at": dt.isoformat() if dt else None
                })
            return out
        except Exception:
            continue
    return []


# ============================================================
# Patients search + encounter list (manual case create)
# ============================================================
@router.get("/patients/search")
def search_patients(
        q: str = Query(default="", min_length=0),
        limit: int = 20,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        limit = min(max(int(limit or 20), 1), 50)
        t = (q or "").strip().lower()

        qry = db.query(Patient)

        if t:
            conds = []
            uhid_col = _patient_uhid_col()
            phone_col = _patient_phone_col()
            first_col, last_col, full_col = _patient_name_cols()

            if t.isdigit():
                conds.append(Patient.id == int(t))

            if uhid_col is not None:
                c = _lc_like(uhid_col, t)
                if c is not None:
                    conds.append(c)

            if phone_col is not None:
                c = _lc_like(phone_col, t)
                if c is not None:
                    conds.append(c)

            if full_col is not None:
                c = _lc_like(full_col, t)
                if c is not None:
                    conds.append(c)
            else:
                if first_col is not None:
                    c = _lc_like(first_col, t)
                    if c is not None:
                        conds.append(c)
                if last_col is not None:
                    c = _lc_like(last_col, t)
                    if c is not None:
                        conds.append(c)

            if conds:
                qry = qry.filter(or_(*conds))

        rows = qry.order_by(desc(getattr(Patient, "id"))).limit(limit).all()

        uhid_col = _patient_uhid_col()
        phone_col = _patient_phone_col()
        first_col, last_col, full_col = _patient_name_cols()

        items = []
        for p in rows:
            if full_col is not None:
                name = (getattr(p, full_col.key, None) or "").strip() or None
            else:
                fn = (getattr(p, first_col.key, "")
                      if first_col is not None else "") or ""
                ln = (getattr(p, last_col.key, "")
                      if last_col is not None else "") or ""
                name = f"{fn} {ln}".strip() or None

            uhid = getattr(p, uhid_col.key,
                           None) if uhid_col is not None else None
            phone = getattr(p, phone_col.key,
                            None) if phone_col is not None else None

            items.append({
                "id": int(p.id),
                "name": name,
                "uhid": uhid,
                "phone": phone
            })

        return {"items": items}
    except Exception as e:
        _err(e)


@router.get("/patients/{patient_id}/encounters")
def list_patient_encounters(
        patient_id: int,
        encounter_type: str = Query(..., description="OP/IP/OT/ER"),
        limit: int = 100,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        p = db.query(Patient).filter(Patient.id == int(patient_id)).first()
        if not p:
            raise HTTPException(status_code=404, detail="Patient not found")

        et = (encounter_type or "").strip().upper()
        limit = min(max(int(limit or 100), 1), 200)

        if et == "OP":
            items = _load_op_encounters(db,
                                        patient_id=int(patient_id),
                                        limit=limit)
        elif et == "IP":
            items = _load_ip_encounters(db,
                                        patient_id=int(patient_id),
                                        limit=limit)
        elif et == "OT":
            items = _load_ot_encounters(db,
                                        patient_id=int(patient_id),
                                        limit=limit)
        elif et == "ER":
            items = _load_er_encounters(db,
                                        patient_id=int(patient_id),
                                        limit=limit)
        else:
            items = []

        return {
            "items": [{
                "encounter_id": x["encounter_id"],
                "encounter_at": x.get("encounter_at")
            } for x in (items or [])]
        }
    except Exception as e:
        _err(e)


# ============================================================
# Manual create case
# ============================================================
@router.post("/cases/manual")
def create_case_manual(
        inp: ManualCaseCreateIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        p = db.query(Patient).filter(Patient.id == int(inp.patient_id)).first()
        if not p:
            raise HTTPException(status_code=404, detail="Patient not found")

        existing = (db.query(BillingCase).filter(
            BillingCase.encounter_type == inp.encounter_type,
            BillingCase.encounter_id == int(inp.encounter_id),
        ).first())
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message":
                    "The selected patient and encounter id based Case Already available.",
                    "case_id": int(existing.id),
                    "case_number": existing.case_number,
                },
            )

        if inp.encounter_type == EncounterType.OP:
            created_case = get_or_create_case_for_op_visit(
                db,
                visit_id=int(inp.encounter_id),
                user=user,
                tariff_plan_id=inp.tariff_plan_id,
                reset_period=inp.reset_period,
            )
        elif inp.encounter_type == EncounterType.IP:
            created_case = get_or_create_case_for_ip_admission(
                db,
                admission_id=int(inp.encounter_id),
                user=user,
                tariff_plan_id=inp.tariff_plan_id,
                reset_period=inp.reset_period,
            )
        elif inp.encounter_type == EncounterType.OT:
            from app.services.billing_workflows import get_or_create_case_for_ot_case
            created_case = get_or_create_case_for_ot_case(
                db,
                ot_case_id=int(inp.encounter_id),
                user=user,
                reset_period=inp.reset_period,
            )
        elif inp.encounter_type == EncounterType.ER:
            from app.services.billing_workflows import get_or_create_case_for_er_visit
            created_case = get_or_create_case_for_er_visit(
                db,
                er_visit_id=int(inp.encounter_id),
                user=user,
                reset_period=inp.reset_period,
            )
        else:
            raise HTTPException(status_code=400,
                                detail="Unsupported encounter_type")

        # Safety check: created case should match chosen patient (frontend should ensure, but backend must protect)
        if int(created_case.patient_id) != int(inp.patient_id):
            raise HTTPException(
                status_code=400,
                detail="Encounter does not belong to selected patient")

        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            ex2 = (db.query(BillingCase).filter(
                BillingCase.encounter_type == inp.encounter_type,
                BillingCase.encounter_id == int(inp.encounter_id),
            ).first())
            if ex2:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message":
                        "The selected patient and encounter id based Case Already available.",
                        "case_id": int(ex2.id),
                        "case_number": ex2.case_number,
                    },
                )
            raise

        db.commit()
        db.refresh(created_case)

        return {
            "id": int(created_case.id),
            "case_number": created_case.case_number,
            "encounter_type": _enum_value(created_case.encounter_type),
            "encounter_id": int(created_case.encounter_id),
            "patient_id": int(created_case.patient_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ GET: Cases (List / Detail)
# ============================================================
@router.get("/cases")
def list_cases(
        q: Optional[str] = None,
        encounter_type: Optional[str] = Query(default=None,
                                              description="OP/IP/OT/ER"),
        status: Optional[str] = Query(
            default=None, description="OPEN/READY_FOR_POST/CLOSED/CANCELLED"),
        payer_mode: Optional[str] = Query(
            default=None, description="SELF/INSURANCE/CORPORATE/MIXED"),
        date_from: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
        page: int = 1,
        page_size: int = 20,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 1), 100)

        qry = db.query(BillingCase,
                       Patient).join(Patient,
                                     BillingCase.patient_id == Patient.id)

        if encounter_type:
            et = encounter_type.strip().upper()
            if et in EncounterType.__members__:
                qry = qry.filter(
                    BillingCase.encounter_type == EncounterType[et])

        if status:
            st = status.strip().upper()
            if st in BillingCaseStatus.__members__:
                qry = qry.filter(BillingCase.status == BillingCaseStatus[st])

        if payer_mode:
            pm = payer_mode.strip().upper()
            if pm in PayerMode.__members__:
                qry = qry.filter(BillingCase.payer_mode == PayerMode[pm])

        # Date filtering
        if date_from:
            qry = qry.filter(func.date(BillingCase.created_at) >= date_from)
        if date_to:
            qry = qry.filter(func.date(BillingCase.created_at) <= date_to)

        if q and q.strip():
            t = q.strip().lower()
            conds = []

            if t.isdigit():
                n = int(t)
                conds.append(BillingCase.id == n)
                conds.append(BillingCase.encounter_id == n)
                conds.append(BillingCase.patient_id == n)

            c1 = _lc_like(BillingCase.case_number, t)
            if c1 is not None:
                conds.append(c1)

            uhid_col = _patient_uhid_col()
            phone_col = _patient_phone_col()
            first_col, last_col, full_col = _patient_name_cols()

            if uhid_col is not None:
                c = _lc_like(uhid_col, t)
                if c is not None:
                    conds.append(c)

            if phone_col is not None:
                c = _lc_like(phone_col, t)
                if c is not None:
                    conds.append(c)

            if full_col is not None:
                c = _lc_like(full_col, t)
                if c is not None:
                    conds.append(c)
            else:
                if first_col is not None:
                    c = _lc_like(first_col, t)
                    if c is not None:
                        conds.append(c)
                if last_col is not None:
                    c = _lc_like(last_col, t)
                    if c is not None:
                        conds.append(c)

            if conds:
                qry = qry.filter(or_(*conds))

        total = qry.with_entities(func.count(BillingCase.id)).scalar() or 0
        rows = qry.order_by(desc(BillingCase.created_at)).offset(
            (page - 1) * page_size).limit(page_size).all()

        return {
            "items": [_case_to_dict(c, p) for (c, p) in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size
        }
    except Exception as e:
        _err(e)


@router.get("/cases/{case_id}")
def get_case_detail(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        row = (db.query(BillingCase, Patient).join(
            Patient, BillingCase.patient_id == Patient.id).filter(
                BillingCase.id == int(case_id)).first())
        if not row:
            raise HTTPException(status_code=404,
                                detail="Billing case not found")

        c, p = row

        posted_total = (db.query(
            func.coalesce(func.sum(BillingInvoice.grand_total), 0)).filter(
                BillingInvoice.billing_case_id == c.id).filter(
                    BillingInvoice.status == DocStatus.POSTED).scalar()) or 0

        paid_total = (db.query(
            func.coalesce(func.sum(BillingPayment.amount), 0)).filter(
                BillingPayment.billing_case_id == c.id).scalar()) or 0

        balance = Decimal(str(posted_total)) - Decimal(str(paid_total))

        out = _case_to_dict(c, p)
        out["totals"] = {
            "posted_invoice_total": str(posted_total or 0),
            "paid_total": str(paid_total or 0),
            "balance": str(balance)
        }
        return out
    except Exception as e:
        _err(e)


# ============================================================
# ✅ Case Dashboard Endpoint (Extreme UI needs this)
# ============================================================
@router.get("/cases/{case_id}/dashboard")
def case_dashboard(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)
        p = db.query(Patient).filter(Patient.id == int(c.patient_id)).first()

        inv_rows = (db.query(BillingInvoice).filter(
            BillingInvoice.billing_case_id == int(c.id)).order_by(
                desc(BillingInvoice.created_at)).all())

        # totals by module (ignore VOID)
        mod_rows = (db.query(
            BillingInvoice.module,
            func.coalesce(func.sum(BillingInvoice.grand_total), 0),
        ).filter(BillingInvoice.billing_case_id == int(c.id)).filter(
            BillingInvoice.status != DocStatus.VOID).group_by(
                BillingInvoice.module).all())

        # ✅ show ALL modules always + amount order wise
        mod_amount: Dict[str, Decimal] = {
            k: Decimal("0")
            for k in MODULES.keys()
        }
        extra_amount: Dict[str, Decimal] = {}

        for mod, amt in mod_rows:
            m = (mod or "MISC").strip().upper() or "MISC"
            a = Decimal(str(amt or 0))
            if m in mod_amount:
                mod_amount[m] += a
            else:
                extra_amount[m] = extra_amount.get(m, Decimal("0")) + a

        particulars = []
        total_bill = Decimal("0")
        for code, label in MODULES.items():
            a = mod_amount.get(code, Decimal("0"))
            total_bill += a
            particulars.append({
                "module": code,
                "label": label,
                "amount": float(a)
            })

        for code, a in extra_amount.items():
            total_bill += a
            particulars.append({
                "module": code,
                "label": MODULES.get(code, code),
                "amount": float(a)
            })

        particulars.sort(key=lambda x: x["amount"], reverse=True)

        paid = (db.query(func.coalesce(
            func.sum(BillingPayment.amount),
            0)).filter(BillingPayment.billing_case_id == int(c.id)).scalar())
        paid = Decimal(str(paid or 0))

        adv = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == int(c.id)).filter(
                BillingAdvance.entry_type == AdvanceType.ADVANCE).scalar())
        adv = Decimal(str(adv or 0))

        refunds = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == int(c.id)).filter(
                BillingAdvance.entry_type == AdvanceType.REFUND).scalar())
        refunds = Decimal(str(refunds or 0))

        net_deposit = adv - refunds
        balance = total_bill - paid

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(c.id)).first()

        return {
            "case": _case_to_dict(c, p),
            "particulars": particulars,
            "totals": {
                "total_bill": float(total_bill),
                "payments_received": float(paid),
                "net_deposit": float(net_deposit),
                "balance": float(balance),
            },
            "insurance": _insurance_to_dict(ins) if ins else None,
            "invoices": [_invoice_to_dict(x) for x in inv_rows],
        }
    except Exception as e:
        _err(e)


def _insurance_to_dict(ins: BillingInsuranceCase) -> Dict[str, Any]:
    return {
        "id": int(ins.id),
        "billing_case_id": int(ins.billing_case_id),
        "payer_kind": _enum_value(ins.payer_kind),
        "insurance_company_id": ins.insurance_company_id,
        "tpa_id": ins.tpa_id,
        "corporate_id": ins.corporate_id,
        "policy_no": ins.policy_no,
        "member_id": ins.member_id,
        "plan_name": ins.plan_name,
        "status": _enum_value(ins.status),
        "approved_limit": str(getattr(ins, "approved_limit", 0) or 0),
        "approved_at":
        ins.approved_at.isoformat() if ins.approved_at else None,
        "created_at": ins.created_at.isoformat() if ins.created_at else None,
        "updated_at": ins.updated_at.isoformat() if ins.updated_at else None,
    }


# ============================================================
# ✅ Case sub-resources
# ============================================================
@router.get("/cases/{case_id}/invoices")
def list_case_invoices(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        rows = (db.query(BillingInvoice).filter(
            BillingInvoice.billing_case_id == int(case_id)).order_by(
                desc(BillingInvoice.created_at)).all())
        return {"items": [_invoice_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


@router.get("/cases/{case_id}/payments")
def list_case_payments(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        rows = (db.query(BillingPayment).filter(
            BillingPayment.billing_case_id == int(case_id)).order_by(
                desc(BillingPayment.received_at)).all())
        return {"items": [_payment_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


@router.get("/cases/{case_id}/advances")
def list_case_advances(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        rows = (db.query(BillingAdvance).filter(
            BillingAdvance.billing_case_id == int(case_id)).order_by(
                desc(BillingAdvance.entry_at)).all())
        return {"items": [_advance_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


# ============================================================
# ✅ Invoice detail / lines / payments
# ============================================================
@router.get("/invoices/{invoice_id}")
def get_invoice_detail(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        return _invoice_to_dict(inv)
    except Exception as e:
        _err(e)


@router.get("/invoices/{invoice_id}/lines")
def list_invoice_lines(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)

        rows = (db.query(BillingInvoiceLine, User, Department).outerjoin(
            User, User.id == BillingInvoiceLine.doctor_id).outerjoin(
                Department, Department.id == User.department_id).filter(
                    BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
                        BillingInvoiceLine.id.asc()).all())

        return {
            "items": [
                _line_to_dict(ln, inv=inv, doctor=doc, department=dept)
                for (ln, doc, dept) in rows
            ]
        }
    except Exception as e:
        _err(e)


class InvoiceLineUpdateIn(BaseModel):
    model_config = {"extra": "ignore"}

    qty: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    gst_rate: Optional[Decimal] = None
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    description: Optional[str] = None
    doctor_id: Optional[int] = None
    service_date: Optional[datetime] = None
    item_code: Optional[str] = None  # ✅ add (optional)
    meta_json: Optional[Dict[str, Any]] = None

    reason: str = Field(..., min_length=3, max_length=255)



@router.put("/lines/{line_id}")
def update_line(
        line_id: int,
        inp: InvoiceLineUpdateIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        ln = db.get(BillingInvoiceLine, int(line_id))
        if not ln:
            raise HTTPException(status_code=404,
                                detail="Invoice line not found")

        inv = _get_invoice_or_404(db, user, int(ln.invoice_id))

        # ✅ allow edit only in DRAFT/APPROVED (approved requires reopen flow usually)
        st = str(_enum_value(inv.status) or "").upper()
        if st not in {"DRAFT", "APPROVED"}:
            raise HTTPException(status_code=409, detail="Invoice locked")

        # ✅ Permission gate
        from app.api.deps import require_perm
        if not getattr(user, "is_admin", False):
            require_perm(user, "billing.invoice.lines.edit")

        updated = update_invoice_line(
            db,
            line_id=int(line_id),
            user=user,
            qty=inp.qty,
            unit_price=inp.unit_price,
            gst_rate=inp.gst_rate,
            discount_percent=inp.discount_percent,
            discount_amount=inp.discount_amount,
            description=inp.description,
            doctor_id=inp.doctor_id,
            service_date=inp.service_date,
            meta_json=(jsonable_encoder(inp.meta_json) if inp.meta_json is not None else None),
            reason=inp.reason,
        )


        db.commit()
        db.refresh(updated)
        return {"line": _line_to_dict(updated, inv=inv)}

    except Exception as e:
        db.rollback()
        _err(e)


@router.delete("/lines/{line_id}")
def delete_line(
        line_id: int,
        reason: str = Query(..., min_length=3, max_length=255),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        ln = db.get(BillingInvoiceLine, int(line_id))
        if not ln:
            return {"ok": True}  # ✅ idempotent delete

        inv = _get_invoice_or_404(db, user, int(ln.invoice_id))
        st = str(_enum_value(inv.status) or "").upper()
        if st not in {"DRAFT", "APPROVED"}:
            raise HTTPException(status_code=409, detail="Invoice locked")

        from app.api.deps import require_perm
        if not getattr(user, "is_admin", False):
            require_perm(user, "billing.invoice.lines.delete")

        delete_invoice_line(db, line_id=int(line_id), user=user, reason=reason)
        db.commit()
        return {"ok": True}

    except Exception as e:
        db.rollback()
        _err(e)


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _pick_dt(obj, *keys):
    for k in keys:
        if hasattr(obj, k):
            v = getattr(obj, k, None)
            if v is not None:
                return v
    return None


def _payment_to_dict(p):
    return {
        "id": p.id,
        "billing_case_id": getattr(p, "billing_case_id", None),
        "invoice_id": getattr(p, "invoice_id", None),
        "amount": float(getattr(p, "amount", 0) or 0),
        "mode": getattr(p, "mode", None),
        "txn_ref": getattr(p, "txn_ref", None),
        "notes": getattr(p, "notes", None),
        "status": getattr(p, "status", None),
        "kind": getattr(p, "kind", None),  # optional
        "direction": getattr(p, "direction", None),  # optional
        "created_at": _pick_dt(p, "created_at", "paid_at", "received_at"),
        "paid_at": _pick_dt(p, "paid_at", "received_at"),
    }


def _alloc_to_dict(a):
    return {
        "id": a.id,
        "payment_id": getattr(a, "payment_id", None),
        "invoice_id": getattr(a, "invoice_id", None),
        "amount": float(getattr(a, "amount", 0) or 0),
        "status": getattr(a, "status", None),
        "allocated_at": _pick_dt(a, "allocated_at", "created_at"),
    }


@router.get("/invoices/{invoice_id}/payments")
def list_invoice_payments(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ Returns BOTH:
    - Direct payments where payment.invoice_id == invoice_id
    - Advance/manual allocations where allocation.invoice_id == invoice_id
    So InvoiceEditor can compute paid/due correctly from allocations.
    """
    # Ensure invoice exists
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # 1) Load allocations for this invoice (ACTIVE only if enum exists)
    aq = db.query(BillingPaymentAllocation).filter(
        BillingPaymentAllocation.invoice_id == int(invoice_id))
    if ReceiptStatus is not None and hasattr(BillingPaymentAllocation,
                                            "status"):
        try:
            aq = aq.filter(
                BillingPaymentAllocation.status == ReceiptStatus.ACTIVE)
        except Exception:
            pass
    allocs = aq.all()

    allocs_by_payment = defaultdict(list)
    alloc_sum_by_payment = defaultdict(Decimal)
    pay_ids_from_alloc = set()

    for a in allocs:
        pid = getattr(a, "payment_id", None)
        if not pid:
            continue
        pay_ids_from_alloc.add(int(pid))
        allocs_by_payment[int(pid)].append(_alloc_to_dict(a))
        alloc_sum_by_payment[int(pid)] += _dec(getattr(a, "amount", 0))

    # 2) Load direct payments for this invoice
    pq_direct = db.query(
        BillingPayment.id).filter(BillingPayment.invoice_id == int(invoice_id))
    direct_ids = {int(x[0]) for x in pq_direct.all()}

    # 3) Union ids
    all_ids = sorted(list(pay_ids_from_alloc.union(direct_ids)))
    if not all_ids:
        return {"items": []}

    # 4) Fetch payments
    pq = db.query(BillingPayment).filter(
        BillingPayment.id.in_(all_ids)).order_by(
            desc(getattr(BillingPayment, "created_at", BillingPayment.id)))
    pays = pq.all()

    out = []
    for p in pays:
        d = _payment_to_dict(p)

        pid = int(p.id)
        d["allocations"] = allocs_by_payment.get(pid, [])
        d["allocated_amount"] = float(alloc_sum_by_payment.get(pid, Decimal("0")))

        # ✅ THIS is what Payment Tab should use for this invoice:
        # - if allocations exist -> allocated_amount
        # - else (legacy direct payment) -> full payment.amount
        applied = Decimal("0")
        if d["allocations"]:
            applied = alloc_sum_by_payment.get(pid, Decimal("0"))
        else:
            if getattr(p, "invoice_id", None) and int(getattr(p, "invoice_id")) == int(invoice_id):
                applied = _dec(getattr(p, "amount", 0))

        d["applied_amount"] = float(applied)
        out.append(d)

    return {"items": out}


# ============================================================
# ✅ Create invoice (module wise)
# ============================================================
@router.post("/cases/{case_id}/invoices")
def create_case_invoice(
        case_id: int,
        module: str = Query(..., min_length=2, max_length=16),
        invoice_type: InvoiceType = Query(InvoiceType.PATIENT),
        payer_type: PayerType = Query(PayerType.PATIENT),
        payer_id: Optional[int] = Query(None),
        reset_period: NumberResetPeriod = Query(NumberResetPeriod.YEAR),
        allow_duplicate_draft: bool = Query(False),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    case = _get_case_or_404(db, user, int(case_id))
    try:
        mod = _normalize_module(module)

        inv = create_new_invoice_for_case(
            db,
            case=case,
            user=user,
            module=mod,
            invoice_type=invoice_type,
            payer_type=payer_type,
            payer_id=payer_id,
            reset_period=reset_period,
            allow_duplicate_draft=allow_duplicate_draft,
        )
        db.commit()
        return {
            "invoice": {
                "id": int(inv.id),
                "invoice_number": inv.invoice_number,
                "module": inv.module,
                "invoice_type": inv.invoice_type,
                "status": inv.status,
                "payer_type": inv.payer_type,
                "payer_id": inv.payer_id,
                "service_date": getattr(inv, "service_date", None),
            }
        }
    except BillingError as e:
        db.rollback()
        raise HTTPException(status_code=getattr(e, "status_code", 400),
                            detail=getattr(e, "extra", None) or str(e))
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Lines: NEW BODY API (supports pharmacy meta headers)
# ============================================================
class ManualLineCreateIn(BaseModel):
    service_group: ServiceGroup
    description: str = Field(..., min_length=2, max_length=255)

    qty: Decimal = Field(default=Decimal("1"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)

    gst_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discount_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)

    item_type: Optional[str] = None
    item_id: Optional[int] = None
    item_code: Optional[str] = None

    doctor_id: Optional[int] = None
    manual_reason: Optional[str] = Field(default="Manual entry",
                                         max_length=255)

    service_date: Optional[datetime] = None
    meta_json: Optional[Dict[str, Any]] = None


@router.post("/invoices/{invoice_id}/lines")
def add_manual_line_v2(
        invoice_id: int,
        inp: ManualLineCreateIn = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        _require_draft_invoice(inv)

        # ✅ HARD BLOCK: no one can inject CHARGE_ITEM via manual API
        it = (inp.item_type or "").strip().upper()
        if it in {"CHARGE_ITEM", "CHARGEITEM"}:
            raise HTTPException(
                status_code=422,
                detail=
                ("item_type=CHARGE_ITEM is not allowed in manual line API. "
                 "Use /billing/cases/{case_id}/charge-items/add. "
                 "Invoice module will be auto-routed by charge item category (ADM/DIET/BLOOD/MISC)."
                 ),
            )

        ln = add_manual_line(
            db,
            invoice_id=int(inv.id),
            user=user,
            service_group=inp.service_group,
            description=inp.description,
            qty=inp.qty,
            unit_price=inp.unit_price,
            gst_rate=inp.gst_rate,
            discount_percent=inp.discount_percent,
            discount_amount=inp.discount_amount,
            item_type=inp.item_type,
            item_id=inp.item_id,
            item_code=inp.item_code,
            doctor_id=inp.doctor_id,
            manual_reason=inp.manual_reason,
        )

        if hasattr(ln, "service_date"):
            if inp.service_date:
                ln.service_date = inp.service_date
            elif getattr(ln, "service_date", None) is None:
                ln.service_date = getattr(inv, "created_at",
                                          None) or datetime.utcnow()
        if hasattr(ln, "meta_json") and inp.meta_json is not None:
            ln.meta_json = inp.meta_json

        db.commit()
        db.refresh(ln)
        return _line_to_dict(ln)
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/invoices/{invoice_id}/lines/manual")
def add_manual(
        invoice_id: int,
        service_group: ServiceGroup,
        description: str,
        qty: Decimal = Decimal("1"),
        unit_price: Decimal = Decimal("0"),
        gst_rate: Decimal = Decimal("0"),
        discount_percent: Decimal = Decimal("0"),
        discount_amount: Decimal = Decimal("0"),
        item_type: Optional[str] = None,
        item_id: Optional[int] = None,
        item_code: Optional[str] = None,
        doctor_id: Optional[int] = None,
        manual_reason: Optional[str] = "Manual entry",
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    payload = ManualLineCreateIn(
        service_group=service_group,
        description=description,
        qty=qty,
        unit_price=unit_price,
        gst_rate=gst_rate,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        item_type=item_type,
        item_id=item_id,
        item_code=item_code,
        doctor_id=doctor_id,
        manual_reason=manual_reason,
    )
    return add_manual_line_v2(invoice_id=invoice_id,
                              inp=payload,
                              db=db,
                              user=user)  # type: ignore


@router.post("/invoices/{invoice_id}/lines/from-lis/{order_id}")
def add_from_lis(
        invoice_id: int,
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        _require_draft_invoice(inv)
        res = add_lines_from_lis_order(db,
                                       invoice_id=invoice_id,
                                       lis_order_id=order_id,
                                       user=user)
        db.commit()
        return res
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/invoices/{invoice_id}/lines/from-ris/{order_id}")
def add_from_ris(
        invoice_id: int,
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        _require_draft_invoice(inv)
        res = add_line_from_ris_order(db,
                                      invoice_id=invoice_id,
                                      ris_order_id=order_id,
                                      user=user)
        db.commit()
        return res
    except Exception as e:
        db.rollback()
        _err(e)


class EditReasonIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


@router.post("/invoices/{invoice_id}/edit-request")
def request_edit(
        invoice_id: int,
        inp: EditReasonIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, user, invoice_id)
    if _enum_value(inv.status) != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail="Edit request allowed only for APPROVED invoices")

    # ✅ Minimal: store as BillingNote OR create your own table
    # If you already have BillingNote model, use it.
    # Otherwise, for now return ok and you can wire admin flow later.
    return {
        "ok": True,
        "message": "Edit request captured",
        "invoice_id": int(inv.id),
        "reason": inp.reason
    }


@router.post("/invoices/{invoice_id}/reopen")
def reopen_invoice(
        invoice_id: int,
        inp: EditReasonIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, user, invoice_id)
    if _enum_value(inv.status) != "APPROVED":
        raise HTTPException(status_code=409,
                            detail="Only APPROVED invoices can be reopened")

    # ✅ permission check (your deps.py already has require_perm)
    from app.api.deps import require_perm
    if not getattr(user, "is_admin", False):
        require_perm(user, "billing.invoice.reopen")

    # ✅ Reopen to DRAFT (simple & clean workflow)
    inv.status = DocStatus.DRAFT
    inv.approved_at = None
    db.add(inv)
    db.commit()
    db.refresh(inv)

    # ✅ AUDIT: here you should write BillingAuditLog (recommended)
    return {
        "ok": True,
        "invoice_id": int(inv.id),
        "status": _enum_value(inv.status),
        "reason": inp.reason
    }


# ============================================================
# ✅ Approve / Post / Void
# ============================================================
@router.post("/invoices/{invoice_id}/approve")
def approve(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_invoice_or_404(db, user, invoice_id)
        inv = approve_invoice(db, invoice_id=invoice_id, user=user)
        db.commit()
        return {"id": int(inv.id), "status": _enum_value(inv.status)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/invoices/{invoice_id}/post")
def post(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_invoice_or_404(db, user, invoice_id)

        # ✅ IMPORTANT: make billing_service.post_invoice return (inv, claim)
        inv, claim = post_invoice(db, invoice_id=invoice_id, user=user)

        db.commit()
        return {
            "id": int(inv.id),
            "status": _enum_value(inv.status),
            "claim": claim_to_dict(claim) if claim else None,
        }
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/invoices/{invoice_id}/void")
def void(
        invoice_id: int,
        reason: str,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_invoice_or_404(db, user, invoice_id)
        inv = void_invoice(db, invoice_id=invoice_id, user=user, reason=reason)
        db.commit()
        return {"id": int(inv.id), "status": _enum_value(inv.status)}
    except Exception as e:
        db.rollback()
        _err(e)


class PaymentIn(BaseModel):
    amount: Decimal = Field(..., gt=0)
    mode: PayMode = PayMode.CASH
    invoice_id: Optional[int] = None
    txn_ref: Optional[str] = None
    notes: Optional[str] = None
    payer_type: PayerType = PayerType.PATIENT
    payer_id: Optional[int] = None


# ✅ helper: record ONLY BillingAdvance row (no apply, no allocations, no payment creation)
def _record_advance_only(
    db: Session,
    *,
    billing_case_id: int,
    user: User,
    amount: Decimal,
    entry_type: AdvanceType,
    mode: PayMode,
    txn_ref: Optional[str] = None,
    remarks: Optional[str] = None,
) -> BillingAdvance:
    a = BillingAdvance()

    # tenant (optional)
    if hasattr(a, "tenant_id"):
        setattr(a, "tenant_id", getattr(user, "tenant_id", None))

    # required
    if hasattr(a, "billing_case_id"):
        setattr(a, "billing_case_id", int(billing_case_id))

    # amount
    if hasattr(a, "amount"):
        setattr(a, "amount", Decimal(str(amount)))

    # enum field name differs across codebases
    if hasattr(a, "entry_type"):
        setattr(a, "entry_type", entry_type)
    elif hasattr(a, "advance_type"):
        setattr(a, "advance_type", entry_type)

    # mode/refs
    if hasattr(a, "mode"):
        setattr(a, "mode", mode)
    if hasattr(a, "txn_ref"):
        setattr(a, "txn_ref", txn_ref)

    # remarks/notes field differs
    if hasattr(a, "remarks"):
        setattr(a, "remarks", (remarks or "").strip() or None)
    elif hasattr(a, "notes"):
        setattr(a, "notes", (remarks or "").strip() or None)

    # timestamps differ
    if hasattr(a, "entry_at") and getattr(a, "entry_at", None) is None:
        setattr(a, "entry_at", datetime.utcnow())

    # created_by fields (optional)
    if hasattr(a, "created_by_id") and getattr(a, "created_by_id",
                                               None) is None:
        setattr(a, "created_by_id", getattr(user, "id", None))
    if hasattr(a, "created_by") and getattr(a, "created_by", None) is None:
        setattr(a, "created_by", getattr(user, "id", None))

    db.add(a)
    db.flush()  # ensure a.id exists
    return a


@router.post("/cases/{case_id}/payments")
def pay(
        case_id: int,

        # JSON body (recommended)
        inp: Optional[PaymentIn] = Body(default=None),

        # backward-compatible query params
        amount: Optional[Decimal] = Query(default=None),
        mode: PayMode = Query(default=PayMode.CASH),
        invoice_id: Optional[int] = Query(default=None),
        txn_ref: Optional[str] = Query(default=None),
        notes: Optional[str] = Query(default=None),
        payer_type: PayerType = Query(default=PayerType.PATIENT),
        payer_id: Optional[int] = Query(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ Fixes:
    - Enforces correct payer bucket (must match invoice payer bucket)
    - Computes outstanding from allocations/direct payments
    - Prevents false 'No outstanding amount for this payer bucket'
    """
    try:
        _get_case_or_404(db, user, int(case_id))

        # merge body over query params
        if inp is not None:
            if inp.amount is not None: amount = inp.amount
            if inp.mode is not None: mode = inp.mode
            if inp.invoice_id is not None: invoice_id = inp.invoice_id
            if inp.txn_ref is not None: txn_ref = inp.txn_ref
            if inp.notes is not None: notes = inp.notes
            if inp.payer_type is not None: payer_type = inp.payer_type
            if inp.payer_id is not None: payer_id = inp.payer_id

        if amount is None:
            raise HTTPException(status_code=422, detail="amount is required")

        amount = Decimal(str(amount))
        if amount <= 0:
            raise HTTPException(status_code=422, detail="amount must be > 0")

        # choose invoice (POSTED preferred, else APPROVED)
        inv_id = invoice_id
        if inv_id is None:
            inv_id = (db.query(BillingInvoice.id).filter(
                BillingInvoice.billing_case_id == int(case_id)).filter(
                    BillingInvoice.status == DocStatus.POSTED).order_by(
                        desc(BillingInvoice.created_at)).limit(1).scalar())
            if inv_id is None:
                inv_id = (db.query(BillingInvoice.id).filter(
                    BillingInvoice.billing_case_id == int(case_id)).filter(
                        BillingInvoice.status == DocStatus.APPROVED).order_by(
                            desc(BillingInvoice.created_at)).limit(1).scalar())
            if inv_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="No APPROVED/POSTED invoice found for this case")

        inv = _get_invoice_or_404(db, user, int(inv_id))

        if int(inv.billing_case_id) != int(case_id):
            raise HTTPException(
                status_code=400,
                detail="Selected invoice does not belong to this case")

        if inv.status not in (DocStatus.APPROVED, DocStatus.POSTED):
            raise HTTPException(
                status_code=409,
                detail=
                f"Invoice must be APPROVED/POSTED to accept payments (current: {inv.status})",
            )

        # ✅ Enforce payer bucket = invoice payer bucket
        inv_pt, inv_pid = _norm_payer_bucket(inv.payer_type,
                                             getattr(inv, "payer_id", None))
        req_pt, req_pid = _norm_payer_bucket(payer_type, payer_id)

        # if caller didn't intend payer bucket, we force invoice bucket
        # (prevents front-end mistakes like payer_id sent with PATIENT)
        req_pt = inv_pt if req_pt is None else req_pt
        req_pid = inv_pid if (str(_enum_value(req_pt)).upper() != "PATIENT"
                              and req_pid is None) else req_pid
        req_pt, req_pid = _norm_payer_bucket(req_pt, req_pid)

        if str(_enum_value(req_pt)).upper() != str(
                _enum_value(inv_pt)).upper():
            raise HTTPException(
                status_code=409,
                detail="Payment payer_type must match invoice payer_type")
        if str(_enum_value(inv_pt)).upper() != "PATIENT":
            if (req_pid or None) != (inv_pid or None):
                raise HTTPException(
                    status_code=409,
                    detail="Payment payer_id must match invoice payer_id")

        # ✅ Use invoice outstanding (not 'bucket outstanding' that breaks)
        outstanding = _invoice_outstanding(db, inv)
        if outstanding <= 0:
            raise HTTPException(
                status_code=409,
                detail="Invoice already fully paid (no outstanding).")

        if amount > outstanding:
            raise HTTPException(
                status_code=409,
                detail=
                f"Payment exceeds outstanding. Outstanding={outstanding}, Payment={amount}",
            )

        # ✅ Create payment + allocation
        pay_row = _create_payment_row(
            db,
            billing_case_id=int(case_id),
            invoice_id=int(inv.id),
            user=user,
            amount=amount,
            mode=mode,
            txn_ref=txn_ref,
            notes=notes,
            payer_type=inv_pt,
            payer_id=inv_pid,
        )

        alloc_row = _allocate_payment_to_invoice(
            db,
            payment=pay_row,
            invoice_id=int(inv.id),
            amount=amount,
        )

        db.commit()
        try:
            db.refresh(pay_row)
        except Exception:
            pass

        out: Dict[str, Any] = {"payment": _payment_to_dict(pay_row)}
        if alloc_row is not None:
            out["allocations"] = [_alloc_to_dict(alloc_row)]
        out["invoice_outstanding_after"] = str(_invoice_outstanding(db, inv))
        return out

    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as e:
        db.rollback()
        logger.exception("Payment IntegrityError")
        raise HTTPException(
            status_code=409,
            detail="Payment could not be recorded due to a database constraint."
        ) from e
    except Exception as e:
        db.rollback()
        logger.exception("Unhandled error in /payments")
        raise HTTPException(status_code=500,
                            detail="Internal Server Error") from e


@router.post("/cases/{case_id}/advances/apply")
def apply_advance_to_invoices_DISABLED(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    🚫 Auto-apply is intentionally disabled.
    Use: POST /billing/cases/{case_id}/advances/apply-selected
    """
    logger.warning("AUTO-APPLY DISABLED endpoint hit: case_id=%s user_id=%s",
                   int(case_id), getattr(user, "id", None))
    raise HTTPException(
        status_code=410,
        detail=
        "Auto-apply is disabled. Use /billing/cases/{case_id}/advances/apply-selected with invoice_ids.",
    )


@router.get("/cases/{case_id}/finance")
def case_finance(case_id: int,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    _get_case_or_404(db, user, case_id)
    if case_financials_v2 is None:
        raise HTTPException(status_code=501,
                            detail="case_financials_v2 not wired")
    return {"ok": True, "finance": case_financials_v2(db, case_id=case_id)}


@router.post("/cases/{case_id}/advances")
def advance(
        case_id: int,
        amount: Decimal = Query(...),
        entry_type: AdvanceType = Query(default=AdvanceType.ADVANCE),
        mode: PayMode = Query(default=PayMode.CASH),
        txn_ref: Optional[str] = Query(default=None),
        remarks: Optional[str] = Query(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ HARD GUARANTEE:
    This endpoint ONLY inserts BillingAdvance row.
    ❌ No auto-apply
    ❌ No allocations
    ❌ No ADVANCE_ADJUSTMENT payment creation
    """
    try:
        _get_case_or_404(db, user, int(case_id))

        if amount is None or Decimal(str(amount)) <= 0:
            raise HTTPException(status_code=422, detail="amount must be > 0")

        a = _record_advance_only(
            db,
            billing_case_id=int(case_id),
            user=user,
            amount=Decimal(str(amount)),
            entry_type=entry_type,
            mode=mode,
            txn_ref=txn_ref,
            remarks=remarks,
        )

        db.commit()
        db.refresh(a)
        return {"ok": True, "advance": _advance_to_dict(a)}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Unhandled error in /advances")
        raise HTTPException(status_code=500,
                            detail="Internal Server Error") from e


# ============================================================
# ✅ Refunds (Deposit / Advance Refund)
# ============================================================
class RefundIn(BaseModel):
    amount: Decimal = Field(..., gt=0)
    mode: PayMode = PayMode.CASH
    txn_ref: Optional[str] = None
    remarks: Optional[str] = None
    # optional: if you want to restrict refund to "advance balance only"
    strict_deposit_refund: bool = True


@router.get("/cases/{case_id}/refunds")
def list_case_refunds(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        rows = (db.query(BillingAdvance).filter(
            BillingAdvance.billing_case_id == int(case_id)).filter(
                BillingAdvance.entry_type == AdvanceType.REFUND).order_by(
                    desc(BillingAdvance.entry_at)).all())
        return {"items": [_advance_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


@router.post("/cases/{case_id}/refunds")
def refund_deposit(
        case_id: int,
        inp: RefundIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, int(case_id))
        _require_perm_code(user, "billing.refunds.create")

        # refundable calc (your existing logic) ...
        adv = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == int(case_id)).filter(
                BillingAdvance.entry_type == AdvanceType.ADVANCE).scalar()
               ) or 0

        refunds = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == int(case_id)).filter(
                BillingAdvance.entry_type == AdvanceType.REFUND).scalar()) or 0

        adv = Decimal(str(adv or 0))
        refunds = Decimal(str(refunds or 0))
        refundable = adv - refunds

        if inp.strict_deposit_refund and inp.amount > refundable:
            raise HTTPException(
                status_code=409,
                detail=
                f"Refund exceeds refundable deposit. Refundable={refundable}")

        # ✅ IMPORTANT: use _record_advance_only (no auto apply)
        a = _record_advance_only(
            db,
            billing_case_id=int(case_id),
            user=user,
            amount=Decimal(str(inp.amount)),
            entry_type=AdvanceType.REFUND,
            mode=inp.mode,
            txn_ref=inp.txn_ref,
            remarks=(inp.remarks or "").strip() or None,
        )

        db.commit()
        db.refresh(a)
        return {
            "ok": True,
            "refund": _advance_to_dict(a),
            "refundable_balance": str(refundable - inp.amount)
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Unhandled error in /refunds")
        raise HTTPException(status_code=500,
                            detail="Internal Server Error") from e


# ============================================================
# ✅ Claims: Reject / Cancel / Reopen (Lifecycle controls)
# ============================================================
class ClaimDecisionIn(BaseModel):
    remarks: str = ""
    reason_code: Optional[str] = None
    amount: Optional[
        Decimal] = None  # optional: rejection may include allowed amount / deductions


def _claim_status_name(c: BillingClaim) -> str:
    return str(_enum_value(getattr(c, "status", "") or "")).upper()


@router.post("/claims/{claim_id}/reject")
def reject_claim(
        claim_id: int,
        inp: ClaimDecisionIn = Body(default=ClaimDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Reject/deny a claim:
    - allowed from SUBMITTED/ACKNOWLEDGED (typically)
    - sets status to REJECTED (or DENIED if your enum uses that)
    """
    try:
        _require_perm_code(user, "billing.claims.reject")
        c = get_claim(db, int(claim_id))

        cur = _claim_status_name(c)
        allowed = {"SUBMITTED", "ACKNOWLEDGED", "UNDER_REVIEW"}
        if cur not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Claim cannot be rejected from status={cur}")

        st_rejected = (_pick_status(ClaimStatus, "REJECTED")
                       or _pick_status(ClaimStatus, "DENIED")
                       or _pick_status(ClaimStatus, "CANCELLED"))
        if st_rejected is None:
            raise HTTPException(
                status_code=500,
                detail="ClaimStatus missing REJECTED/DENIED in enum")

        c.status = st_rejected
        if hasattr(c, "remarks"):
            c.remarks = (inp.remarks or "").strip() or None

        # optional fields if your model has them
        if inp.reason_code and hasattr(c, "reason_code"):
            setattr(c, "reason_code", inp.reason_code)

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"claim": claim_to_dict(c)}

    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/claims/{claim_id}/cancel")
def cancel_claim(
        claim_id: int,
        inp: ClaimDecisionIn = Body(default=ClaimDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Cancel claim (hospital cancels the submission):
    - allowed from DRAFT/SUBMITTED/ACKNOWLEDGED
    """
    try:
        _require_perm_code(user, "billing.claims.cancel")
        c = get_claim(db, int(claim_id))
        cur = _claim_status_name(c)

        allowed = {"DRAFT", "SUBMITTED", "ACKNOWLEDGED"}
        if cur not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Claim cannot be cancelled from status={cur}")

        st_cancel = _pick_status(ClaimStatus, "CANCELLED") or _pick_status(
            ClaimStatus, "VOID")
        if st_cancel is None:
            raise HTTPException(
                status_code=500,
                detail="ClaimStatus missing CANCELLED/VOID in enum")

        c.status = st_cancel
        if hasattr(c, "remarks"):
            c.remarks = (inp.remarks or "").strip() or None

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"claim": claim_to_dict(c)}

    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/claims/{claim_id}/reopen")
def reopen_claim(
        claim_id: int,
        inp: ClaimDecisionIn = Body(default=ClaimDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Reopen claim back to DRAFT:
    - allowed from REJECTED/DENIED/CANCELLED (depending on your enum)
    """
    try:
        _require_perm_code(user, "billing.claims.reopen")
        c = get_claim(db, int(claim_id))
        cur = _claim_status_name(c)

        allowed = {"REJECTED", "DENIED", "CANCELLED", "VOID"}
        if cur not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Claim cannot be reopened from status={cur}")

        st_draft = _pick_status(ClaimStatus, "DRAFT")
        if st_draft is None:
            raise HTTPException(status_code=500,
                                detail="ClaimStatus missing DRAFT in enum")

        c.status = st_draft
        if hasattr(c, "remarks") and inp.remarks:
            c.remarks = (inp.remarks or "").strip() or None

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"claim": claim_to_dict(c)}

    except Exception as e:
        db.rollback()
        _err(e)


# ------------------------------------------------------------
# ✅ Manual Apply (Selected invoices)
# ------------------------------------------------------------
class ApplyAdvanceSelectedIn(BaseModel):
    invoice_ids: List[int] = Field(..., min_length=1)
    apply_amount: Decimal = Field(..., gt=0)  # ✅ required now
    notes: Optional[str] = None


@router.get("/cases/{case_id}/invoices/outstanding")
def case_invoice_outstanding(
        case_id: int,
        statuses: str = Query(default="APPROVED,POSTED"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _get_case_or_404(db, user, int(case_id))

    st = [x.strip().upper() for x in (statuses or "").split(",") if x.strip()]
    if not st:
        st = ["APPROVED", "POSTED"]

    items = list_case_invoice_outstanding(db,
                                          billing_case_id=int(case_id),
                                          status_names=st)
    total_due = sum(Decimal(str(x["patient_outstanding"])) for x in items)

    return {"items": items, "totals": {"patient_outstanding": str(total_due)}}


@router.post("/cases/{case_id}/advances/apply-selected")
def apply_advance_selected(
        case_id: int,
        inp: ApplyAdvanceSelectedIn = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    ✅ Manual workflow:
    - user selects invoice_ids (checklist)
    - backend calculates each invoice outstanding (patient bucket)
    - apply_amount <= advance_balance and <= selected_total_due
    - creates a Payment(kind=ADVANCE_ADJUSTMENT) + allocations only for selected invoices
    - consumes advances into BillingAdvanceApplication
    - returns allocations + history refs
    """
    _get_case_or_404(db, user, int(case_id))

    try:
        res = apply_advances_to_selected_invoices(
            db,
            billing_case_id=int(case_id),
            user=user,
            invoice_ids=[int(x) for x in inp.invoice_ids],
            apply_amount=inp.apply_amount,
            notes=inp.notes,
        )
        db.commit()
        return {"ok": True, **res}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        _err(e)


@router.get("/cases/{case_id}/advances/applications")
def list_advance_apply_history(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _get_case_or_404(db, user, int(case_id))

    if BillingPaymentAllocation is None or BillingAdvanceApplication is None:
        raise HTTPException(
            status_code=501,
            detail=
            "Advance application history models not wired (BillingPaymentAllocation / BillingAdvanceApplication).",
        )

    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == int(case_id)).filter(
            BillingPayment.kind == PaymentKind.ADVANCE_ADJUSTMENT).filter(
                BillingPayment.status == ReceiptStatus.ACTIVE).order_by(
                    desc(BillingPayment.received_at),
                    desc(BillingPayment.id)).all())

    out = []
    for p in pays:
        allocs = (db.query(BillingPaymentAllocation, BillingInvoice).join(
            BillingInvoice,
            BillingInvoice.id == BillingPaymentAllocation.invoice_id).filter(
                BillingPaymentAllocation.payment_id == int(p.id)).order_by(
                    BillingPaymentAllocation.id.asc()).all())

        used = (db.query(BillingAdvanceApplication, BillingAdvance).join(
            BillingAdvance,
            BillingAdvance.id == BillingAdvanceApplication.advance_id).filter(
                BillingAdvanceApplication.payment_id == int(p.id)).order_by(
                    BillingAdvanceApplication.id.asc()).all())

        out.append({
            "payment":
            _payment_to_dict(p),
            "allocations": [{
                "invoice_id": int(inv.id),
                "invoice_number": inv.invoice_number,
                "module": inv.module,
                "status": str(inv.status),
                "amount": str(getattr(a, "amount", 0) or 0),
            } for (a, inv) in allocs],
            "consumed_advances": [{
                "advance_id":
                int(adv.id),
                "entry_at":
                getattr(adv, "entry_at", None),
                "mode":
                str(getattr(adv, "mode", "") or ""),
                "txn_ref":
                getattr(adv, "txn_ref", None),
                "amount":
                str(getattr(app, "amount", 0) or 0),
            } for (app, adv) in used],
        })

    return {"items": out}


# ============================================================
# ✅ Invoice Print / Export (PDF + CSV)
# ============================================================
def _render_invoice_pdf_bytes(db: Session, invoice_id: int) -> bytes:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    case = db.query(BillingCase).filter(
        BillingCase.id == int(inv.billing_case_id)).first()
    patient = db.query(Patient).filter(
        Patient.id == int(case.patient_id)).first() if case else None

    lines = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
            BillingInvoiceLine.id.asc()).all())

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    left = 15 * mm
    top = h - 15 * mm
    y = top

    # Header
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "TAX INVOICE")
    y -= 6 * mm

    c.setFont("Helvetica", 9)
    inv_no = inv.invoice_number or f"INV-{inv.id}"
    c.drawString(left, y, f"Invoice No: {inv_no}")
    c.drawRightString(
        w - left, y,
        f"Date: {(getattr(inv, 'created_at', None) or datetime.utcnow()).strftime('%d-%m-%Y')}"
    )
    y -= 6 * mm

    # Patient / Case
    pname = None
    if patient:
        first_col, last_col, full_col = _patient_name_cols()
        if full_col is not None:
            pname = (getattr(patient, full_col.key, None)
                     or "").strip() or None
        else:
            fn = (getattr(patient, first_col.key, "")
                  if first_col else "") or ""
            ln = (getattr(patient, last_col.key, "") if last_col else "") or ""
            pname = f"{fn} {ln}".strip() or None

    c.drawString(
        left, y,
        f"Patient: {pname or ('#'+str(case.patient_id) if case else '-')}")
    if case:
        c.drawRightString(w - left, y, f"Case: {case.case_number}")
    y -= 6 * mm

    if case:
        c.drawString(
            left, y,
            f"Encounter: {_enum_value(case.encounter_type)} / {case.encounter_id}"
        )
        c.drawRightString(w - left, y, f"Payer: {_enum_value(inv.payer_type)}")
        y -= 8 * mm

    # Table header
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, y, "S.No")
    c.drawString(left + 12 * mm, y, "Description")
    c.drawRightString(w - left - 45 * mm, y, "Qty")
    c.drawRightString(w - left - 25 * mm, y, "Rate")
    c.drawRightString(w - left, y, "Amount")
    y -= 4 * mm
    c.line(left, y, w - left, y)
    y -= 5 * mm

    c.setFont("Helvetica", 9)
    sn = 1
    for ln in lines:
        if y < 25 * mm:
            c.showPage()
            y = top
            c.setFont("Helvetica", 9)

        desc_txt = (ln.description or "")[:70]
        qty = _money(getattr(ln, "qty", 0))
        rate = _money(getattr(ln, "unit_price", 0))
        amt = _money(getattr(ln, "net_amount", 0))

        c.drawString(left, y, str(sn))
        c.drawString(left + 12 * mm, y, desc_txt)
        c.drawRightString(w - left - 45 * mm, y, qty)
        c.drawRightString(w - left - 25 * mm, y, rate)
        c.drawRightString(w - left, y, amt)
        y -= 6 * mm
        sn += 1

    # Totals
    if y < 45 * mm:
        c.showPage()
        y = top

    y -= 2 * mm
    c.line(left, y, w - left, y)
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(w - left - 30 * mm, y, "Grand Total:")
    c.drawRightString(w - left, y, _money(getattr(inv, "grand_total", 0)))
    y -= 10 * mm

    c.setFont("Helvetica", 8)
    c.drawString(left, y, "This is a computer generated invoice.")
    c.showPage()
    c.save()

    return buf.getvalue()


@router.get("/invoices/{invoice_id}/print")
def print_invoice_pdf(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.invoice.print")
        _get_invoice_or_404(db, user, invoice_id)

        pdf_bytes = _render_invoice_pdf_bytes(db, int(invoice_id))
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition":
                f'inline; filename="invoice_{invoice_id}.pdf"'
            },
        )
    except Exception as e:
        _err(e)


@router.get("/invoices/{invoice_id}/export.csv")
def export_invoice_csv(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.invoice.export")
        inv = _get_invoice_or_404(db, user, invoice_id)

        lines = (db.query(BillingInvoiceLine).filter(
            BillingInvoiceLine.invoice_id == int(inv.id)).order_by(
                BillingInvoiceLine.id.asc()).all())

        output = io.StringIO()
        wtr = csv.writer(output)
        wtr.writerow([
            "invoice_id", "invoice_number", "line_id", "service_group",
            "description", "qty", "unit_price", "gst_rate", "discount_amount",
            "tax_amount", "net_amount"
        ])
        for ln in lines:
            wtr.writerow([
                int(inv.id),
                inv.invoice_number,
                int(ln.id),
                _enum_value(ln.service_group),
                ln.description,
                _money(getattr(ln, "qty", 0)),
                _money(getattr(ln, "unit_price", 0)),
                _money(getattr(ln, "gst_rate", 0)),
                _money(getattr(ln, "discount_amount", 0)),
                _money(getattr(ln, "tax_amount", 0)),
                _money(getattr(ln, "net_amount", 0)),
            ])

        data = output.getvalue().encode("utf-8")
        return Response(
            content=data,
            media_type="text/csv",
            headers={
                "Content-Disposition":
                f'attachment; filename="invoice_{invoice_id}.csv"'
            },
        )
    except Exception as e:
        _err(e)


# ============================================================
# ✅ Case Statement Print (PDF)
# ============================================================
def _render_case_statement_pdf_bytes(db: Session, case_id: int) -> bytes:
    cse = db.query(BillingCase).filter(BillingCase.id == int(case_id)).first()
    if not cse:
        raise HTTPException(status_code=404, detail="Billing case not found")

    patient = db.query(Patient).filter(
        Patient.id == int(cse.patient_id)).first()

    invoices = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == int(case_id)).order_by(
            desc(BillingInvoice.created_at)).all())

    payments = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == int(case_id)).order_by(
            desc(BillingPayment.received_at)).all())

    advances = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == int(case_id)).order_by(
            desc(BillingAdvance.entry_at)).all())

    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    left = 15 * mm
    y = h - 15 * mm

    cv.setFont("Helvetica-Bold", 12)
    cv.drawString(left, y, "BILLING CASE STATEMENT")
    y -= 8 * mm

    cv.setFont("Helvetica", 9)
    cv.drawString(
        left, y,
        f"Case: {cse.case_number}   Encounter: {_enum_value(cse.encounter_type)} / {cse.encounter_id}"
    )
    y -= 5 * mm
    cv.drawString(left, y, f"Patient ID: {cse.patient_id}")
    y -= 8 * mm

    # Invoices
    cv.setFont("Helvetica-Bold", 10)
    cv.drawString(left, y, "Invoices")
    y -= 6 * mm

    cv.setFont("Helvetica-Bold", 9)
    cv.drawString(left, y, "No")
    cv.drawString(left + 20 * mm, y, "Invoice No")
    cv.drawString(left + 55 * mm, y, "Module")
    cv.drawString(left + 85 * mm, y, "Status")
    cv.drawRightString(w - left, y, "Amount")
    y -= 4 * mm
    cv.line(left, y, w - left, y)
    y -= 6 * mm

    cv.setFont("Helvetica", 9)
    total_invoice = Decimal("0")
    for i, inv in enumerate(invoices, start=1):
        if y < 25 * mm:
            cv.showPage()
            y = h - 15 * mm
            cv.setFont("Helvetica", 9)

        amt = Decimal(str(getattr(inv, "grand_total", 0) or 0))
        total_invoice += amt
        cv.drawString(left, y, str(i))
        cv.drawString(left + 20 * mm, y, inv.invoice_number or f"#{inv.id}")
        cv.drawString(left + 55 * mm, y, (inv.module or "MISC"))
        cv.drawString(left + 85 * mm, y, str(_enum_value(inv.status)))
        cv.drawRightString(w - left, y, str(amt))
        y -= 6 * mm

    y -= 4 * mm
    cv.setFont("Helvetica-Bold", 10)
    cv.drawRightString(w - left - 30 * mm, y, "Total Invoices:")
    cv.drawRightString(w - left, y, str(total_invoice))
    y -= 10 * mm

    # Payments
    cv.setFont("Helvetica-Bold", 10)
    cv.drawString(left, y, "Payments")
    y -= 6 * mm

    cv.setFont("Helvetica", 9)
    total_paid = Decimal("0")
    for p in payments:
        if y < 25 * mm:
            cv.showPage()
            y = h - 15 * mm
            cv.setFont("Helvetica", 9)
        amt = Decimal(str(getattr(p, "amount", 0) or 0))
        total_paid += amt
        cv.drawString(
            left, y,
            f"{(getattr(p, 'received_at', None) or getattr(p, 'created_at', None) or datetime.utcnow()).strftime('%d-%m-%Y')}  {_enum_value(p.mode)}  {amt}"
        )
        y -= 6 * mm

    # Advances/Refunds
    y -= 4 * mm
    cv.setFont("Helvetica-Bold", 10)
    cv.drawString(left, y, "Advances / Refunds")
    y -= 6 * mm

    cv.setFont("Helvetica", 9)
    adv_total = Decimal("0")
    ref_total = Decimal("0")
    for a in advances:
        if y < 25 * mm:
            cv.showPage()
            y = h - 15 * mm
            cv.setFont("Helvetica", 9)
        amt = Decimal(str(getattr(a, "amount", 0) or 0))
        et = str(_enum_value(getattr(a, "entry_type", None)) or "")
        if et.upper() == "ADVANCE":
            adv_total += amt
        elif et.upper() == "REFUND":
            ref_total += amt
        cv.drawString(
            left, y,
            f"{(getattr(a, 'entry_at', None) or datetime.utcnow()).strftime('%d-%m-%Y')}  {et}  {_enum_value(getattr(a,'mode',None))}  {amt}"
        )
        y -= 6 * mm

    y -= 6 * mm
    cv.setFont("Helvetica-Bold", 10)
    cv.drawRightString(w - left - 30 * mm, y, "Total Paid:")
    cv.drawRightString(w - left, y, str(total_paid))
    y -= 6 * mm

    cv.drawRightString(w - left - 30 * mm, y, "Net Deposit:")
    cv.drawRightString(w - left, y, str(adv_total - ref_total))
    y -= 6 * mm

    cv.drawRightString(w - left - 30 * mm, y, "Balance:")
    cv.drawRightString(w - left, y, str(total_invoice - total_paid))
    y -= 10 * mm

    cv.setFont("Helvetica", 8)
    cv.drawString(left, y, "Statement generated by system.")
    cv.showPage()
    cv.save()

    return buf.getvalue()


@router.get("/cases/{case_id}/statement/print")
def print_case_statement_pdf(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.case.statement.print")
        _get_case_or_404(db, user, int(case_id))

        pdf_bytes = _render_case_statement_pdf_bytes(db, int(case_id))
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition":
                f'inline; filename="case_statement_{case_id}.pdf"'
            },
        )
    except Exception as e:
        _err(e)


# ============================================================
# ✅ Summary endpoint
# ============================================================
@router.get("/cases/{case_id}/summary")
def billing_case_summary(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)

        inv_rows = (db.query(
            BillingInvoice.module,
            func.coalesce(
                func.sum(BillingInvoice.grand_total),
                0)).filter(BillingInvoice.billing_case_id == c.id).filter(
                    BillingInvoice.status != DocStatus.VOID).group_by(
                        BillingInvoice.module).all())

        particulars = []
        total_bill = Decimal("0.00")
        for mod, amt in inv_rows:
            mod = (mod or "MISC").strip().upper()
            a = Decimal(str(amt or 0))
            total_bill += a
            particulars.append({
                "module": mod,
                "label": MODULES.get(mod, mod),
                "amount": float(a)
            })

        paid = (db.query(func.coalesce(
            func.sum(BillingPayment.amount),
            0)).filter(BillingPayment.billing_case_id == c.id).scalar())
        paid = Decimal(str(paid or 0))

        adv = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == c.id).filter(
                BillingAdvance.entry_type == AdvanceType.ADVANCE).scalar())
        adv = Decimal(str(adv or 0))

        refunds = (db.query(func.coalesce(
            func.sum(BillingAdvance.amount),
            0)).filter(BillingAdvance.billing_case_id == c.id).filter(
                BillingAdvance.entry_type == AdvanceType.REFUND).scalar())
        refunds = Decimal(str(refunds or 0))

        net_deposit = adv - refunds
        balance = total_bill - paid

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == c.id).first()

        return {
            "case_id": int(c.id),
            "particulars": sorted(particulars, key=lambda x: x["label"]),
            "totals": {
                "total_bill": float(total_bill),
                "payments_received": float(paid),
                "net_deposit": float(net_deposit),
                "balance": float(balance),
            },
            "insurance": _insurance_to_dict(ins) if ins else None,
        }
    except Exception as e:
        _err(e)


# ============================================================
# ✅ Insurance Tab APIs
# ============================================================
class InsuranceUpsertIn(BaseModel):
    payer_kind: InsurancePayerKind = InsurancePayerKind.INSURANCE
    insurance_company_id: Optional[int] = None
    tpa_id: Optional[int] = None
    corporate_id: Optional[int] = None

    policy_no: Optional[str] = None
    member_id: Optional[str] = None
    plan_name: Optional[str] = None

    status: InsuranceStatus = InsuranceStatus.INITIATED
    approved_limit: Decimal = Decimal("0.00")
    approved_at: Optional[datetime] = None


@router.get("/cases/{case_id}/insurance")
def get_insurance_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(case_id)).first()
        return {"insurance": _insurance_to_dict(ins) if ins else None}
    except Exception as e:
        _err(e)


@router.put("/cases/{case_id}/insurance")
def upsert_insurance_case(
        case_id: int,
        inp: InsuranceUpsertIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)

        # ✅ REQUIRED validations based on payer_kind
        pk = _enum_value(inp.payer_kind)
        if pk == "INSURANCE" and not inp.insurance_company_id:
            raise HTTPException(
                status_code=422,
                detail=
                "insurance_company_id is required for payer_kind=INSURANCE")
        if pk == "TPA" and not inp.tpa_id:
            raise HTTPException(status_code=422,
                                detail="tpa_id is required for payer_kind=TPA")
        if pk == "CORPORATE" and not inp.corporate_id:
            raise HTTPException(
                status_code=422,
                detail="corporate_id is required for payer_kind=CORPORATE")

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(case_id)).first()
        if not ins:
            ins = BillingInsuranceCase(billing_case_id=int(case_id),
                                       created_by=getattr(user, "id", None))

        ins.payer_kind = inp.payer_kind
        ins.insurance_company_id = inp.insurance_company_id
        ins.tpa_id = inp.tpa_id
        ins.corporate_id = inp.corporate_id
        ins.policy_no = (inp.policy_no or "").strip() or None
        ins.member_id = (inp.member_id or "").strip() or None
        ins.plan_name = (inp.plan_name or "").strip() or None
        ins.status = inp.status
        ins.approved_limit = Decimal(str(inp.approved_limit or 0))
        ins.approved_at = inp.approved_at

        # ✅ Auto adjust case payer_mode (real-world)
        if pk in {"INSURANCE", "TPA"}:
            c.payer_mode = PayerMode.INSURANCE
        elif pk == "CORPORATE":
            c.payer_mode = PayerMode.CORPORATE
        else:
            # fallback
            c.payer_mode = c.payer_mode or PayerMode.SELF

        db.add(ins)
        db.add(c)
        db.commit()
        db.refresh(ins)
        return {"insurance": _insurance_to_dict(ins)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Claims APIs (Lifecycle)
# ============================================================
class ClaimFromInvoiceIn(BaseModel):
    invoice_id: int = Field(..., gt=0)


@router.post("/cases/{case_id}/insurance/claims/from-invoice")
def create_or_refresh_claim_from_invoice(
        case_id: int,
        inp: ClaimFromInvoiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)
        inv = _get_invoice_or_404(db, user, int(inp.invoice_id))
        if int(inv.billing_case_id) != int(c.id):
            raise HTTPException(status_code=400,
                                detail="Invoice does not belong to this case")

        claim = upsert_draft_claim_from_invoice(db,
                                                invoice_id=int(inv.id),
                                                user=user)
        db.commit()
        return {"claim": claim_to_dict(claim) if claim else None}
    except Exception as e:
        db.rollback()
        _err(e)


@router.get("/cases/{case_id}/insurance/claims")
def list_case_claims(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(case_id)).first()
        if not ins:
            return {"items": []}

        rows = (db.query(BillingClaim).filter(
            BillingClaim.insurance_case_id == int(ins.id)).order_by(
                desc(BillingClaim.created_at), desc(BillingClaim.id)).all())
        return {"items": [claim_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


@router.get("/claims/{claim_id}")
def get_claim_detail(
        claim_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = get_claim(db, int(claim_id))
        return {"claim": claim_to_dict(c)}
    except Exception as e:
        _err(e)


class ClaimRemarksIn(BaseModel):
    remarks: str = ""


@router.post("/claims/{claim_id}/submit")
def submit_claim(
        claim_id: int,
        inp: ClaimRemarksIn = Body(default=ClaimRemarksIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = claim_submit(db,
                         claim_id=int(claim_id),
                         user=user,
                         remarks=(inp.remarks or "").strip())
        db.commit()
        return {"claim": claim_to_dict(c)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/claims/{claim_id}/acknowledge")
def acknowledge_claim(
        claim_id: int,
        inp: ClaimRemarksIn = Body(default=ClaimRemarksIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = claim_acknowledge(db,
                              claim_id=int(claim_id),
                              user=user,
                              remarks=(inp.remarks or "").strip())
        db.commit()
        return {"claim": claim_to_dict(c)}
    except Exception as e:
        db.rollback()
        _err(e)


class ClaimApproveIn(BaseModel):
    approved_amount: Decimal = Field(..., gt=0)
    remarks: str = ""


@router.post("/claims/{claim_id}/approve")
def approve_claim(
        claim_id: int,
        inp: ClaimApproveIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = claim_approve(
            db,
            claim_id=int(claim_id),
            user=user,
            approved_amount=inp.approved_amount,
            remarks=(inp.remarks or "").strip(),
        )
        db.commit()
        return {"claim": claim_to_dict(c)}
    except Exception as e:
        db.rollback()
        _err(e)


class ClaimSettleIn(BaseModel):
    settled_amount: Decimal = Field(..., gt=0)
    remarks: str = ""


@router.post("/claims/{claim_id}/settle")
def settle_claim(
        claim_id: int,
        inp: ClaimSettleIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = claim_settle(
            db,
            claim_id=int(claim_id),
            user=user,
            settled_amount=inp.settled_amount,
            remarks=(inp.remarks or "").strip(),
        )
        db.commit()
        return {"claim": claim_to_dict(c)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ EXTRA HELPERS (Invoice totals, Insurance status mapping)
# ============================================================


def _invoice_status_name(inv: BillingInvoice) -> str:
    return str(_enum_value(getattr(inv, "status", "") or "")).upper()


def _case_status_name(c: BillingCase) -> str:
    return str(_enum_value(getattr(c, "status", "") or "")).upper()


def _require_invoice_editable(inv: BillingInvoice):
    st = _invoice_status_name(inv)
    if st not in {"DRAFT", "APPROVED"}:
        raise HTTPException(
            status_code=409,
            detail="Invoice locked (only DRAFT/APPROVED can be edited).")


def _require_invoice_not_void(inv: BillingInvoice):
    st = _invoice_status_name(inv)
    if st == "VOID":
        raise HTTPException(status_code=409, detail="Invoice is VOID.")


def _recalc_invoice_totals(db: Session, inv: BillingInvoice) -> BillingInvoice:
    # sums from lines -> updates invoice totals
    lines = db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.invoice_id == int(inv.id)).all()

    sub_total = Decimal("0")
    discount_total = Decimal("0")
    tax_total = Decimal("0")
    grand_total = Decimal("0")

    for ln in lines:
        sub_total += Decimal(str(getattr(ln, "line_total", 0) or 0))
        discount_total += Decimal(str(getattr(ln, "discount_amount", 0) or 0))
        tax_total += Decimal(str(getattr(ln, "tax_amount", 0) or 0))
        grand_total += Decimal(str(getattr(ln, "net_amount", 0) or 0))

    inv.sub_total = sub_total
    inv.discount_total = discount_total
    inv.tax_total = tax_total

    # Keep round_off simple; you can add rounding logic later
    inv.round_off = Decimal("0")
    inv.grand_total = grand_total

    db.add(inv)
    return inv


def _get_insurance_case_or_409(db: Session,
                               case_id: int) -> BillingInsuranceCase:
    ins = db.query(BillingInsuranceCase).filter(
        BillingInsuranceCase.billing_case_id == int(case_id)).first()
    if not ins:
        raise HTTPException(
            status_code=409,
            detail=
            "Insurance not configured for this case. Please fill Insurance tab first."
        )
    return ins


def _preauth_to_dict(p: BillingPreauthRequest) -> Dict[str, Any]:
    return {
        "id":
        int(p.id),
        "insurance_case_id":
        int(p.insurance_case_id),
        "requested_amount":
        str(getattr(p, "requested_amount", 0) or 0),
        "approved_amount":
        str(getattr(p, "approved_amount", 0) or 0),
        "status":
        _enum_value(p.status),
        "submitted_at":
        p.submitted_at.isoformat() if p.submitted_at else None,
        "approved_at":
        p.approved_at.isoformat() if p.approved_at else None,
        "remarks":
        getattr(p, "remarks", None),
        "attachments_json":
        getattr(p, "attachments_json", None),
        "created_at":
        p.created_at.isoformat() if p.created_at else None,
        "updated_at":
        p.updated_at.isoformat() if getattr(p, "updated_at", None) else None,
    }


def _sync_ins_status_from_preauth(ins: BillingInsuranceCase,
                                  preauth: BillingPreauthRequest):
    ps = str(_enum_value(preauth.status) or "").upper()
    if ps == "SUBMITTED":
        ins.status = InsuranceStatus.PREAUTH_SUBMITTED
    elif ps == "APPROVED":
        ins.status = InsuranceStatus.PREAUTH_APPROVED
        ins.approved_limit = Decimal(
            str(getattr(preauth, "approved_amount", 0) or 0))
        ins.approved_at = preauth.approved_at
    elif ps == "PARTIAL":
        ins.status = InsuranceStatus.PREAUTH_PARTIAL
        ins.approved_limit = Decimal(
            str(getattr(preauth, "approved_amount", 0) or 0))
        ins.approved_at = preauth.approved_at
    elif ps == "REJECTED":
        ins.status = InsuranceStatus.PREAUTH_REJECTED
    elif ps == "CANCELLED":
        # fallback: keep initiated if cancelled
        ins.status = InsuranceStatus.INITIATED


def _sync_ins_status_from_claim(ins: BillingInsuranceCase,
                                claim: BillingClaim):
    cs = str(_enum_value(claim.status) or "").upper()
    if cs == "SUBMITTED":
        ins.status = InsuranceStatus.CLAIM_SUBMITTED
    elif cs == "UNDER_QUERY":
        ins.status = InsuranceStatus.QUERY
    elif cs == "SETTLED":
        ins.status = InsuranceStatus.SETTLED
    elif cs == "DENIED":
        ins.status = InsuranceStatus.DENIED
    elif cs == "CLOSED":
        ins.status = InsuranceStatus.CLOSED


# ============================================================
# ✅ Meta: enums + masters (read-only)
# ============================================================


@router.get("/meta/enums")
def billing_meta_enums(user: User = Depends(current_user)):

    def _enum_list(e):
        return [{
            "name": k,
            "value": v.value
        } for k, v in e.__members__.items()]

    return {
        "EncounterType": _enum_list(EncounterType),
        "BillingCaseStatus": _enum_list(BillingCaseStatus),
        "PayerMode": _enum_list(PayerMode),
        "InvoiceType": _enum_list(InvoiceType),
        "DocStatus": _enum_list(DocStatus),
        "PayerType": _enum_list(PayerType),
        "ServiceGroup": _enum_list(ServiceGroup),
        "PayMode": _enum_list(PayMode),
        "AdvanceType": _enum_list(AdvanceType),
        "InsurancePayerKind": _enum_list(InsurancePayerKind),
        "InsuranceStatus": _enum_list(InsuranceStatus),
        "PreauthStatus": _enum_list(PreauthStatus),
        "ClaimStatus": _enum_list(ClaimStatus),
        "NumberResetPeriod": _enum_list(NumberResetPeriod),
    }


@router.get("/meta/tariff-plans")
def billing_meta_tariff_plans(
        q: str = Query(""),
        active_only: bool = Query(True),
        limit: int = Query(100),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # Imported lazily to avoid import issues
    from app.models.billing import BillingTariffPlan

    limit = min(max(int(limit or 100), 1), 200)
    t = (q or "").strip().lower()

    qry = db.query(BillingTariffPlan)
    if active_only and hasattr(BillingTariffPlan, "is_active"):
        qry = qry.filter(BillingTariffPlan.is_active.is_(True))
    if t:
        qry = qry.filter(
            or_(_lc_like(BillingTariffPlan.name, t),
                _lc_like(BillingTariffPlan.code, t)))

    rows = qry.order_by(BillingTariffPlan.name.asc()).limit(limit).all()
    return {
        "items": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name,
            "type": _enum_value(getattr(x, "type", None))
        } for x in rows]
    }


@router.get("/meta/revenue-heads")
def billing_meta_revenue_heads(
        q: str = Query(""),
        active_only: bool = Query(True),
        limit: int = Query(200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    from app.models.billing import BillingRevenueHead
    limit = min(max(int(limit or 200), 1), 500)
    t = (q or "").strip().lower()

    qry = db.query(BillingRevenueHead)
    if active_only and hasattr(BillingRevenueHead, "is_active"):
        qry = qry.filter(BillingRevenueHead.is_active.is_(True))
    if t:
        qry = qry.filter(
            or_(_lc_like(BillingRevenueHead.name, t),
                _lc_like(BillingRevenueHead.code, t)))

    rows = qry.order_by(BillingRevenueHead.name.asc()).limit(limit).all()
    return {
        "items": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name
        } for x in rows]
    }


@router.get("/meta/cost-centers")
def billing_meta_cost_centers(
        q: str = Query(""),
        active_only: bool = Query(True),
        limit: int = Query(200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    from app.models.billing import BillingCostCenter
    limit = min(max(int(limit or 200), 1), 500)
    t = (q or "").strip().lower()

    qry = db.query(BillingCostCenter)
    if active_only and hasattr(BillingCostCenter, "is_active"):
        qry = qry.filter(BillingCostCenter.is_active.is_(True))
    if t:
        qry = qry.filter(
            or_(_lc_like(BillingCostCenter.name, t),
                _lc_like(BillingCostCenter.code, t)))

    rows = qry.order_by(BillingCostCenter.name.asc()).limit(limit).all()
    return {
        "items": [{
            "id": int(x.id),
            "code": x.code,
            "name": x.name
        } for x in rows]
    }


# ============================================================
# ✅ Invoice: update header + recalc totals
# ============================================================


class InvoiceUpdateIn(BaseModel):
    model_config = {"extra": "ignore"}

    invoice_type: Optional[InvoiceType] = None
    payer_type: Optional[PayerType] = None
    payer_id: Optional[int] = None

    service_date: Optional[datetime] = None
    module: Optional[
        str] = None  # normally do NOT change; allowed only in DRAFT if you want
    meta_json: Optional[Dict[str, Any]] = None


@router.put("/invoices/{invoice_id}")
def update_invoice_header(
        invoice_id: int,
        inp: InvoiceUpdateIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        _require_invoice_not_void(inv)
        _require_invoice_editable(inv)

        # ✅ permission (optional)
        _require_perm_code(user, "billing.invoice.edit")

        if inp.module is not None:
            inv.module = _normalize_module(inp.module)

        if inp.invoice_type is not None:
            inv.invoice_type = inp.invoice_type

        if inp.payer_type is not None:
            inv.payer_type = inp.payer_type

        if inp.payer_id is not None:
            inv.payer_id = int(inp.payer_id) if inp.payer_id else None

        # ✅ payer validation
        pt = str(_enum_value(inv.payer_type) or "").upper()
        if pt != "PATIENT" and not inv.payer_id:
            raise HTTPException(
                status_code=422,
                detail="payer_id required when payer_type is not PATIENT")

        if inp.service_date is not None:
            inv.service_date = inp.service_date

        if inp.meta_json is not None and hasattr(inv, "meta_json"):
            inv.meta_json = inp.meta_json

        inv.updated_by = getattr(user, "id", None)

        # recalc after header update (safe)
        inv = _recalc_invoice_totals(db, inv)

        db.commit()
        db.refresh(inv)
        return {"invoice": _invoice_to_dict(inv)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/invoices/{invoice_id}/recalculate")
def recalc_invoice(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        _require_invoice_not_void(inv)
        _require_invoice_editable(inv)

        _require_perm_code(user, "billing.invoice.recalculate")

        inv = _recalc_invoice_totals(db, inv)
        db.commit()
        db.refresh(inv)
        return {"invoice": _invoice_to_dict(inv)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Case lifecycle: cancel / close / reopen
# ============================================================


class CaseActionIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)
    allow_close_with_balance: bool = False  # safety


@router.post("/cases/{case_id}/cancel")
def cancel_case(
        case_id: int,
        inp: CaseActionIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        c = _get_case_or_404(db, user, case_id)
        _require_perm_code(user, "billing.case.cancel")

        st = _case_status_name(c)
        if st == "CANCELLED":
            return {"ok": True, "status": "CANCELLED"}

        if st == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="Closed case cannot be cancelled. Reopen first.")

        c.status = BillingCaseStatus.CANCELLED
        c.notes = ((c.notes or "") + f"\n[CANCEL] {inp.reason}").strip()
        c.updated_by = getattr(user, "id", None)

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"ok": True, "case": _case_to_dict(c, None)}
    except Exception as e:
        db.rollback()
        _err(e)


def _safe_set(obj: Any, field_names: List[str], value: Any) -> bool:
    for f in field_names:
        if hasattr(obj, f):
            setattr(obj, f, value)
            return True
    return False


def _sum_case_invoices(db: Session, case_id: int) -> Decimal:
    # Sum non-VOID invoices (all statuses except VOID)
    q = db.query(func.coalesce(
        func.sum(BillingInvoice.grand_total),
        0)).filter(BillingInvoice.billing_case_id == int(case_id))
    # DocStatus may vary; handle safely
    try:
        q = q.filter(BillingInvoice.status != DocStatus.VOID)
    except Exception:
        pass
    v = q.scalar() or 0
    return Decimal(str(v or 0))


def _sum_case_payments(db: Session, case_id: int) -> Decimal:
    q = db.query(func.coalesce(
        func.sum(BillingPayment.amount),
        0)).filter(BillingPayment.billing_case_id == int(case_id))
    # exclude VOID/CANCELLED receipts if model has status
    if hasattr(BillingPayment, "status") and "ReceiptStatus" in globals():
        try:
            q = q.filter(BillingPayment.status == ReceiptStatus.ACTIVE)
        except Exception:
            pass
    v = q.scalar() or 0
    return Decimal(str(v or 0))


def _sum_case_net_deposit(db: Session, case_id: int) -> Decimal:
    adv = db.query(func.coalesce(func.sum(BillingAdvance.amount), 0)).filter(
        BillingAdvance.billing_case_id == int(case_id)).filter(
            BillingAdvance.entry_type == AdvanceType.ADVANCE).scalar() or 0

    ref = db.query(func.coalesce(func.sum(BillingAdvance.amount), 0)).filter(
        BillingAdvance.billing_case_id == int(case_id)).filter(
            BillingAdvance.entry_type == AdvanceType.REFUND).scalar() or 0

    return Decimal(str(adv or 0)) - Decimal(str(ref or 0))


def _case_has_open_invoices(db: Session, case_id: int) -> bool:
    # If any DRAFT/APPROVED invoices exist -> case is not “finalized”
    try:
        cnt = db.query(func.count(BillingInvoice.id)).filter(
            BillingInvoice.billing_case_id == int(case_id)).filter(
                BillingInvoice.status.in_(
                    [DocStatus.DRAFT, DocStatus.APPROVED])).scalar() or 0
        return int(cnt) > 0
    except Exception:
        return False


@router.post("/cases/{case_id}/close")
def close_case(
        case_id: int,
        inp: CaseActionIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Close case:
    - default: blocks closing if balance != 0 OR any DRAFT/APPROVED invoices exist
    - allow_close_with_balance=True bypasses balance block (still recommended to avoid)
    """
    try:
        c = _get_case_or_404(db, user, int(case_id))
        _require_perm_code(user, "billing.case.close")

        st = _case_status_name(c)
        if st == "CLOSED":
            return {
                "ok": True,
                "status": "CLOSED",
                "case": _case_to_dict(c, None)
            }

        if st == "CANCELLED":
            raise HTTPException(
                status_code=409,
                detail="Cancelled case cannot be closed. Reopen first.")

        if _case_has_open_invoices(db, int(case_id)):
            raise HTTPException(
                status_code=409,
                detail=
                "Cannot close case while invoices are in DRAFT/APPROVED. Post/Void them first.",
            )

        total_bill = _sum_case_invoices(db, int(case_id))
        total_paid = _sum_case_payments(db, int(case_id))
        balance = total_bill - total_paid

        if (not inp.allow_close_with_balance) and balance != Decimal("0"):
            raise HTTPException(
                status_code=409,
                detail=
                f"Cannot close case with non-zero balance. Balance={balance}. "
                f"Set allow_close_with_balance=true only if you REALLY want to force close.",
            )

        # set CLOSED
        if "CLOSED" in BillingCaseStatus.__members__:
            c.status = BillingCaseStatus.CLOSED
        else:
            # fallback if enum differs
            c.status = BillingCaseStatus.READY_FOR_POST if "READY_FOR_POST" in BillingCaseStatus.__members__ else c.status

        note = f"\n[CLOSE] {inp.reason}".strip()
        c.notes = ((c.notes or "") + note).strip()

        _safe_set(c, ["updated_by", "updated_by_id"],
                  getattr(user, "id", None))
        _safe_set(c, ["updated_at"], datetime.utcnow())

        db.add(c)
        db.commit()
        db.refresh(c)

        return {
            "ok": True,
            "case": _case_to_dict(c, None),
            "totals": {
                "total_bill": str(total_bill),
                "total_paid": str(total_paid),
                "balance": str(balance),
                "net_deposit": str(_sum_case_net_deposit(db, int(case_id))),
            },
        }
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/cases/{case_id}/reopen")
def reopen_case(
        case_id: int,
        inp: CaseActionIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Reopen case:
    - CLOSED -> READY_FOR_POST (or OPEN if READY_FOR_POST not present)
    - CANCELLED -> OPEN
    """
    try:
        c = _get_case_or_404(db, user, int(case_id))
        _require_perm_code(user, "billing.case.reopen")

        st = _case_status_name(c)
        if st not in {"CLOSED", "CANCELLED"}:
            return {"ok": True, "status": st, "case": _case_to_dict(c, None)}

        # choose reopen target
        if "READY_FOR_POST" in BillingCaseStatus.__members__:
            c.status = BillingCaseStatus.READY_FOR_POST
        elif "OPEN" in BillingCaseStatus.__members__:
            c.status = BillingCaseStatus.OPEN
        else:
            # fallback: keep existing
            c.status = c.status

        c.notes = ((c.notes or "") + f"\n[REOPEN] {inp.reason}").strip()
        _safe_set(c, ["updated_by", "updated_by_id"],
                  getattr(user, "id", None))
        _safe_set(c, ["updated_at"], datetime.utcnow())

        db.add(c)
        db.commit()
        db.refresh(c)
        return {"ok": True, "case": _case_to_dict(c, None)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Receipt void (Payment void)
# ============================================================


class ReceiptVoidIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


def _receipt_status_name(p: BillingPayment) -> str:
    return str(_enum_value(getattr(p, "status", "") or "")).upper()


@router.post("/payments/{payment_id}/void")
def void_payment_receipt(
        payment_id: int,
        inp: ReceiptVoidIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Voids a receipt/payment:
    - marks payment status VOID (if supported)
    - does NOT delete rows (audit-safe)
    """
    try:
        _require_perm_code(user, "billing.receipts.void")
        p = db.query(BillingPayment).filter(
            BillingPayment.id == int(payment_id)).first()
        if not p:
            return {"ok": True}

        # already void?
        if hasattr(p, "status") and _receipt_status_name(p) in {
                "VOID", "CANCELLED"
        }:
            return {"ok": True, "payment": _payment_to_dict(p)}

        if hasattr(p, "status"):
            st_void = _pick_status(ReceiptStatus, "VOID") or _pick_status(
                ReceiptStatus, "CANCELLED")
            if st_void is not None:
                p.status = st_void

        _safe_set(p, ["voided_at"], datetime.utcnow())
        _safe_set(p, ["voided_by", "voided_by_id"], getattr(user, "id", None))
        if hasattr(p, "void_reason"):
            p.void_reason = inp.reason
        elif hasattr(p, "notes"):
            p.notes = ((p.notes or "") + f"\n[VOID] {inp.reason}").strip()

        db.add(p)
        db.commit()
        db.refresh(p)
        return {"ok": True, "payment": _payment_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Insurance: PREAUTH endpoints (full workflow)
# ============================================================

# ============================================================
# ✅ Preauth Workflow APIs (create/submit/approve/reject/cancel)
# ============================================================


class PreauthCreateIn(BaseModel):
    requested_amount: Decimal = Field(..., gt=0)
    remarks: str = ""
    attachments_json: Optional[Dict[str, Any]] = None


class PreauthApproveIn(BaseModel):
    approved_amount: Decimal = Field(..., gt=0)
    is_partial: bool = False
    remarks: str = ""


class PreauthDecisionIn(BaseModel):
    remarks: str = ""


def _preauth_status_name(p: BillingPreauthRequest) -> str:
    return str(_enum_value(getattr(p, "status", "") or "")).upper()


def _get_preauth_or_404(db: Session, preauth_id: int) -> BillingPreauthRequest:
    row = db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.id == int(preauth_id)).first()
    if not row:
        raise HTTPException(status_code=404,
                            detail="Preauth request not found")
    return row


@router.get("/cases/{case_id}/insurance/preauths")
def list_case_preauth(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _get_case_or_404(db, user, int(case_id))
    _require_perm_code(user, "billing.preauth.view")

    ins = _get_insurance_case_or_409(db, int(case_id))
    rows = db.query(BillingPreauthRequest).filter(
        BillingPreauthRequest.insurance_case_id == int(ins.id)).order_by(
            desc(BillingPreauthRequest.created_at),
            desc(BillingPreauthRequest.id)).all()

    return {"items": [_preauth_to_dict(x) for x in rows]}


@router.post("/cases/{case_id}/insurance/preauths")
def create_preauth(
        case_id: int,
        inp: PreauthCreateIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, int(case_id))
        _require_perm_code(user, "billing.preauth.create")

        ins = _get_insurance_case_or_409(db, int(case_id))

        p = BillingPreauthRequest(
            insurance_case_id=int(ins.id),
            requested_amount=Decimal(str(inp.requested_amount)),
            status=_pick_status(PreauthStatus, "DRAFT")
            or list(PreauthStatus.__members__.values())[0],
        )

        if hasattr(p, "remarks"):
            p.remarks = (inp.remarks or "").strip() or None
        if hasattr(p, "attachments_json"):
            p.attachments_json = inp.attachments_json

        _safe_set(p, ["created_by", "created_by_id"],
                  getattr(user, "id", None))

        db.add(p)
        db.flush()

        # optional: sync insurance status
        try:
            _sync_ins_status_from_preauth(ins, p)
            db.add(ins)
        except Exception:
            pass

        db.commit()
        db.refresh(p)
        return {"preauth": _preauth_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/insurance/preauth/{preauth_id}/submit")
def submit_preauth(
        preauth_id: int,
        inp: PreauthDecisionIn = Body(default=PreauthDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.preauth.submit")
        p = _get_preauth_or_404(db, int(preauth_id))

        st_sub = _pick_status(PreauthStatus, "SUBMITTED")
        if st_sub is None:
            raise HTTPException(status_code=500,
                                detail="PreauthStatus missing SUBMITTED")

        p.status = st_sub
        _safe_set(p, ["submitted_at"], datetime.utcnow())
        if hasattr(p, "remarks") and inp.remarks:
            p.remarks = (inp.remarks or "").strip() or None

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(p.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_preauth(ins, p)
            db.add(ins)

        db.add(p)
        db.commit()
        db.refresh(p)
        return {"preauth": _preauth_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/insurance/preauth/{preauth_id}/approve")
def approve_preauth(
        preauth_id: int,
        inp: PreauthApproveIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.preauth.approve")
        p = _get_preauth_or_404(db, int(preauth_id))

        st_app = _pick_status(PreauthStatus,
                              "PARTIAL") if inp.is_partial else _pick_status(
                                  PreauthStatus, "APPROVED")
        if st_app is None:
            raise HTTPException(
                status_code=500,
                detail="PreauthStatus missing APPROVED/PARTIAL")

        p.status = st_app
        _safe_set(p, ["approved_at"], datetime.utcnow())
        if hasattr(p, "approved_amount"):
            p.approved_amount = Decimal(str(inp.approved_amount))
        if hasattr(p, "remarks"):
            p.remarks = (inp.remarks or "").strip() or None

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(p.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_preauth(ins, p)
            db.add(ins)

        db.add(p)
        db.commit()
        db.refresh(p)
        return {"preauth": _preauth_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/insurance/preauth/{preauth_id}/reject")
def reject_preauth(
        preauth_id: int,
        inp: PreauthDecisionIn = Body(default=PreauthDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.preauth.reject")
        p = _get_preauth_or_404(db, int(preauth_id))

        st_rej = _pick_status(PreauthStatus, "REJECTED") or _pick_status(
            PreauthStatus, "DENIED")
        if st_rej is None:
            raise HTTPException(status_code=500,
                                detail="PreauthStatus missing REJECTED/DENIED")

        p.status = st_rej
        if hasattr(p, "remarks"):
            p.remarks = (inp.remarks or "").strip() or None

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(p.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_preauth(ins, p)
            db.add(ins)

        db.add(p)
        db.commit()
        db.refresh(p)
        return {"preauth": _preauth_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/insurance/preauth/{preauth_id}/cancel")
def cancel_preauth(
        preauth_id: int,
        inp: PreauthDecisionIn = Body(default=PreauthDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.preauth.cancel")
        p = _get_preauth_or_404(db, int(preauth_id))

        st_can = _pick_status(PreauthStatus, "CANCELLED") or _pick_status(
            PreauthStatus, "VOID")
        if st_can is None:
            raise HTTPException(status_code=500,
                                detail="PreauthStatus missing CANCELLED/VOID")

        p.status = st_can
        if hasattr(p, "remarks"):
            p.remarks = (inp.remarks or "").strip() or None

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(p.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_preauth(ins, p)
            db.add(ins)

        db.add(p)
        db.commit()
        db.refresh(p)
        return {"preauth": _preauth_to_dict(p)}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Claims: set query + close (missing lifecycle actions)
# ============================================================


class ClaimQueryIn(BaseModel):
    remarks: str = ""


@router.post("/claims/{claim_id}/set-query")
def set_claim_under_query(
        claim_id: int,
        inp: ClaimQueryIn = Body(default=ClaimQueryIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.claims.set_query")
        c = get_claim(db, int(claim_id))

        cur = str(_enum_value(getattr(c, "status", "")) or "").upper()
        if cur not in {"SUBMITTED", "APPROVED"}:
            raise HTTPException(
                status_code=409,
                detail=f"Claim cannot be moved to UNDER_QUERY from status={cur}"
            )

        c.status = ClaimStatus.UNDER_QUERY
        if hasattr(c, "remarks"):
            c.remarks = (inp.remarks or "").strip() or c.remarks

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(c.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_claim(ins, c)
            db.add(ins)

        db.add(c)
        db.commit()
        db.refresh(c)
        return {
            "claim": claim_to_dict(c),
            "insurance": _insurance_to_dict(ins) if ins else None
        }
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/claims/{claim_id}/close")
def close_claim(
        claim_id: int,
        inp: ClaimQueryIn = Body(default=ClaimQueryIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.claims.close")
        c = get_claim(db, int(claim_id))

        cur = str(_enum_value(getattr(c, "status", "")) or "").upper()
        if cur not in {"SETTLED", "DENIED", "APPROVED", "UNDER_QUERY"}:
            raise HTTPException(
                status_code=409,
                detail=f"Claim cannot be CLOSED from status={cur}")

        c.status = ClaimStatus.CLOSED
        if hasattr(c, "remarks") and inp.remarks:
            c.remarks = (inp.remarks or "").strip() or c.remarks

        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.id == int(c.insurance_case_id)).first()
        if ins:
            _sync_ins_status_from_claim(ins, c)
            db.add(ins)

        db.add(c)
        db.commit()
        db.refresh(c)
        return {
            "claim": claim_to_dict(c),
            "insurance": _insurance_to_dict(ins) if ins else None
        }
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Claim creation validation improvement (optional endpoint)
# ============================================================


@router.post("/cases/{case_id}/insurance/claims/from-invoice/validated")
def create_or_refresh_claim_from_invoice_validated(
        case_id: int,
        inp: ClaimFromInvoiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    """
    Same as /from-invoice but with real-world validations:
    - invoice must belong to case
    - invoice should be POSTED (recommended) for claim creation
    - invoice payer_type must be INSURER/CORPORATE (not PATIENT)
    """
    try:
        cse = _get_case_or_404(db, user, case_id)
        _get_insurance_case_or_409(db, int(case_id))

        inv = _get_invoice_or_404(db, user, int(inp.invoice_id))
        if int(inv.billing_case_id) != int(cse.id):
            raise HTTPException(status_code=400,
                                detail="Invoice does not belong to this case")

        st = str(_enum_value(inv.status) or "").upper()
        if st != "POSTED":
            raise HTTPException(
                status_code=409,
                detail="Claim can be created only from POSTED invoices")

        pt = str(_enum_value(inv.payer_type) or "").upper()
        if pt not in {"INSURER", "CORPORATE"}:
            raise HTTPException(
                status_code=409,
                detail=
                "Claim can be created only for INSURER/CORPORATE payer invoices"
            )

        claim = upsert_draft_claim_from_invoice(db,
                                                invoice_id=int(inv.id),
                                                user=user)
        # sync insurance status from claim draft if needed
        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(case_id)).first()
        if ins and claim:
            _sync_ins_status_from_claim(ins, claim)
            db.add(ins)

        db.commit()
        return {"claim": claim_to_dict(claim) if claim else None}
    except Exception as e:
        db.rollback()
        _err(e)


# ============================================================
# ✅ Payments: receipt print + void receipt
# ============================================================


class PaymentVoidIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


def _render_receipt_pdf_bytes(db: Session, payment_id: int) -> bytes:
    pay = db.query(BillingPayment).filter(
        BillingPayment.id == int(payment_id)).first()
    if not pay:
        raise HTTPException(status_code=404, detail="Payment not found")

    case = db.query(BillingCase).filter(
        BillingCase.id == int(pay.billing_case_id)).first()
    patient = db.query(Patient).filter(
        Patient.id == int(case.patient_id)).first() if case else None
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == int(
        pay.invoice_id)).first() if pay.invoice_id else None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    left = 15 * mm
    top = h - 15 * mm
    y = top

    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "PAYMENT RECEIPT")
    y -= 7 * mm

    c.setFont("Helvetica", 9)
    rcpt = getattr(pay, "receipt_number", None) or f"RCPT-{pay.id}"
    c.drawString(left, y, f"Receipt No: {rcpt}")
    c.drawRightString(
        w - left, y,
        f"Date: {(getattr(pay, 'received_at', None) or getattr(pay, 'created_at', None) or datetime.utcnow()).strftime('%d-%m-%Y %H:%M')}"
    )
    y -= 6 * mm

    pname = None
    if patient:
        first_col, last_col, full_col = _patient_name_cols()
        if full_col is not None:
            pname = (getattr(patient, full_col.key, None)
                     or "").strip() or None
        else:
            fn = (getattr(patient, first_col.key, "")
                  if first_col else "") or ""
            ln = (getattr(patient, last_col.key, "") if last_col else "") or ""
            pname = f"{fn} {ln}".strip() or None

    c.drawString(
        left, y,
        f"Patient: {pname or (f'#{case.patient_id}' if case else '-')}")
    if case:
        c.drawRightString(w - left, y, f"Case: {case.case_number}")
    y -= 6 * mm

    if inv:
        c.drawString(
            left, y,
            f"Invoice: {inv.invoice_number or f'#{inv.id}'}  ({_enum_value(inv.status)})"
        )
        y -= 6 * mm

    c.line(left, y, w - left, y)
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, "Payment Details")
    y -= 8 * mm

    c.setFont("Helvetica", 10)
    c.drawString(left, y, f"Amount: {_money(getattr(pay, 'amount', 0))} INR")
    y -= 6 * mm
    c.drawString(left, y, f"Mode: {_enum_value(pay.mode)}")
    y -= 6 * mm
    if getattr(pay, "txn_ref", None):
        c.drawString(left, y, f"Txn Ref: {pay.txn_ref}")
        y -= 6 * mm
    if getattr(pay, "notes", None):
        c.drawString(left, y, f"Notes: {pay.notes}")
        y -= 6 * mm

    y -= 10 * mm
    c.setFont("Helvetica", 8)
    c.drawString(left, y, "This is a computer generated receipt.")
    y -= 18 * mm
    c.setFont("Helvetica", 9)
    c.drawRightString(w - left, y, "Authorised Signature")

    c.showPage()
    c.save()
    return buf.getvalue()


@router.get("/payments/{payment_id}/print")
def print_payment_receipt_pdf(
        payment_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _require_perm_code(user, "billing.receipt.print")
        pdf_bytes = _render_receipt_pdf_bytes(db, int(payment_id))
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition":
                f'inline; filename="receipt_{payment_id}.pdf"'
            },
        )
    except Exception as e:
        _err(e)


class BillingExportKind(str, Enum):
    FULL_CASE = "FULL_CASE"


def _need_any(user: User, perms: List[str]):
    up = set(getattr(user, "permissions", []) or [])
    if not any(p in up for p in perms):
        raise HTTPException(status_code=403, detail="Not permitted")


def _get_branding(db: Session) -> Optional[UiBranding]:
    q = db.query(UiBranding)
    if hasattr(UiBranding, "is_active"):
        q = q.filter(UiBranding.is_active == True)  # noqa
    return q.order_by(UiBranding.id.desc()).first()


def _to_dec(v) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")


def _pick_first_attr(obj: Any, fields: List[str]):
    for f in fields:
        if hasattr(obj, f):
            v = getattr(obj, f, None)
            if v not in (None, "", 0):
                return v
    return None


def _load_encounter_meta(db: Session, case: BillingCase) -> Dict[str, Any]:
    """
    ✅ Rule:
      - if OP => use OP Visit Number
      - if IP => use IP Admission Number

    We attempt:
      1) direct case.encounter_id -> Visit/Admission primary key
      2) BillingCaseLink (VISIT / ADMISSION)
      3) fallback to encounter_id as string (handled in PDF builder)
    """
    meta: Dict[str, Any] = {}

    et = str(getattr(case, "encounter_type", "") or "").upper()
    encounter_id = getattr(case, "encounter_id", None)

    # --------------------------
    # helper to load link id
    # --------------------------
    def _link_entity_id(entity_type: str) -> Optional[int]:
        try:
            row = (db.query(BillingCaseLink).filter(
                BillingCaseLink.billing_case_id == case.id,
                BillingCaseLink.entity_type == entity_type,
            ).order_by(BillingCaseLink.id.desc()).first())
            return int(row.entity_id) if row else None
        except Exception:
            return None

    # --------------------------
    # OP: Visit
    # --------------------------
    if et == "OP":
        visit_obj = None
        try:
            from app.models.opd import Visit  # noqa

            # try encounter_id
            if encounter_id:
                visit_obj = db.query(Visit).filter(
                    Visit.id == int(encounter_id)).first()

            # try link
            if not visit_obj:
                vid = _link_entity_id("VISIT")
                if vid:
                    visit_obj = db.query(Visit).filter(
                        Visit.id == int(vid)).first()

            if visit_obj:
                visit_no = _pick_first_attr(
                    visit_obj,
                    ["visit_number", "visit_no", "visit_code", "visit_id"])
                if visit_no:
                    meta["op_visit_no"] = str(visit_no)

                # doctor (best-effort)
                doc_name = None
                if hasattr(visit_obj, "doctor"):
                    doc = getattr(visit_obj, "doctor", None)
                    doc_name = getattr(doc, "name", None) if doc else None
                if not doc_name and hasattr(visit_obj, "doctor_name"):
                    doc_name = getattr(visit_obj, "doctor_name", None)
                if not doc_name and hasattr(visit_obj, "doctor_id"):
                    # optional lookup if doctor_id exists
                    try:
                        did = getattr(visit_obj, "doctor_id", None)
                        if did:
                            u = db.query(User).filter(
                                User.id == int(did)).first()
                            doc_name = getattr(u, "name", None) if u else None
                    except Exception:
                        pass
                if doc_name:
                    meta["doctor"] = doc_name

                # visit date
                vdt = _pick_first_attr(
                    visit_obj,
                    ["visit_date", "visited_at", "created_at", "updated_at"])
                if vdt:
                    meta[
                        "admitted_on"] = vdt  # for OP we still show as admitted_on slot
        except Exception:
            pass

        return meta

    # --------------------------
    # IP: Admission
    # --------------------------
    if et == "IP":
        adm_obj = None
        try:
            from app.models.ipd import IpdAdmission  # noqa

            # try encounter_id
            if encounter_id:
                adm_obj = db.query(IpdAdmission).filter(
                    IpdAdmission.id == int(encounter_id)).first()

            # try link
            if not adm_obj:
                aid = _link_entity_id("ADMISSION")
                if aid:
                    adm_obj = db.query(IpdAdmission).filter(
                        IpdAdmission.id == int(aid)).first()

            if adm_obj:
                adm_no = _pick_first_attr(
                    adm_obj,
                    ["admission_number", "admission_no", "ip_no", "ip_number"])
                if adm_no:
                    meta["ip_admission_no"] = str(adm_no)

                # ward/room (best-effort)
                ward = _pick_first_attr(adm_obj,
                                        ["ward_name", "ward", "ward_no"])
                room = _pick_first_attr(
                    adm_obj,
                    ["room_no", "room_number", "room", "bed_no", "bed_number"])
                if ward:
                    meta["ward"] = str(ward)
                if room:
                    meta["room"] = str(room)

                # doctor
                doc_name = None
                if hasattr(adm_obj, "doctor"):
                    doc = getattr(adm_obj, "doctor", None)
                    doc_name = getattr(doc, "name", None) if doc else None
                if not doc_name and hasattr(adm_obj, "doctor_name"):
                    doc_name = getattr(adm_obj, "doctor_name", None)
                if not doc_name and hasattr(adm_obj, "doctor_id"):
                    try:
                        did = getattr(adm_obj, "doctor_id", None)
                        if did:
                            u = db.query(User).filter(
                                User.id == int(did)).first()
                            doc_name = getattr(u, "name", None) if u else None
                    except Exception:
                        pass
                if doc_name:
                    meta["doctor"] = doc_name

                # dates
                adt = _pick_first_attr(adm_obj, [
                    "admitted_at", "admission_date", "admitted_on",
                    "created_at"
                ])
                ddt = _pick_first_attr(adm_obj, [
                    "discharged_at", "discharge_date", "discharged_on",
                    "updated_at"
                ])
                if adt:
                    meta["admitted_on"] = adt
                if ddt:
                    meta["discharged_on"] = ddt
        except Exception:
            pass

        return meta

    # OT/ER etc => leave empty (PDF falls back to encounter_id)
    return meta


def _load_payer_context(db: Session, case: BillingCase,
                        ins: Optional[BillingInsuranceCase]) -> Dict[str, Any]:
    """
    Attach payer/insurer/tpa display names for PDF.
    Best effort using your existing payer models.
    """
    ctx: Dict[str, Any] = {
        "payer": None,
        "insurer": None,
        "insurance_company": None
    }

    try:
        from app.models.payer import Payer, Tpa, CreditPlan  # noqa
    except Exception:
        Payer = None
        Tpa = None
        CreditPlan = None

    # If insurance case exists, derive insurer/TPA/corporate
    if ins:
        kind = str(getattr(ins, "payer_kind", "") or "").upper()
        if kind == "INSURANCE" and getattr(ins, "insurance_company_id",
                                           None) and Payer:
            p = db.query(Payer).filter(
                Payer.id == int(ins.insurance_company_id)).first()
            if p:
                ctx["insurer"] = {"name": getattr(p, "name", None)}
                ctx["insurance_company"] = {"name": getattr(p, "name", None)}

        if kind == "TPA" and getattr(ins, "tpa_id", None) and Tpa:
            t = db.query(Tpa).filter(Tpa.id == int(ins.tpa_id)).first()
            if t:
                ctx["payer"] = {"name": getattr(t, "name", None)}

        if kind == "CORPORATE" and getattr(ins, "corporate_id", None):
            # corporate might be stored in CreditPlan or Payer depending on your DB
            corp_name = None
            if CreditPlan:
                cp = db.query(CreditPlan).filter(
                    CreditPlan.id == int(ins.corporate_id)).first()
                if cp:
                    corp_name = getattr(cp, "name", None)
            if (not corp_name) and Payer:
                p = db.query(Payer).filter(
                    Payer.id == int(ins.corporate_id)).first()
                if p:
                    corp_name = getattr(p, "name", None)
            if corp_name:
                ctx["payer"] = {"name": corp_name}

    return ctx


def _load_case_export_payload(db: Session, user: User,
                              case_id: int) -> Dict[str, Any]:
    # ✅ Permission if needed
    # _need_any(user, ["billing.cases.view", "billing.view", "billing.case.view"])

    case = db.query(BillingCase).filter(BillingCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Billing case not found")

    patient_obj = None
    try:
        patient_obj = db.query(Patient).filter(
            Patient.id == case.patient_id).first()
    except Exception:
        patient_obj = None

    # invoices
    invs = (db.query(BillingInvoice).filter(
        BillingInvoice.billing_case_id == case_id).order_by(
            BillingInvoice.id.asc()).all())
    inv_ids = [int(i.id) for i in invs]

    # lines (all invoices)
    lines: List[BillingInvoiceLine] = []
    if inv_ids:
        lines = (db.query(BillingInvoiceLine).filter(
            BillingInvoiceLine.invoice_id.in_(inv_ids)).order_by(
                BillingInvoiceLine.invoice_id.asc(),
                BillingInvoiceLine.id.asc()).all())

    by_inv: Dict[int, List[BillingInvoiceLine]] = {}
    for ln in lines:
        by_inv.setdefault(int(ln.invoice_id), []).append(ln)

    # payments
    pays = (db.query(BillingPayment).filter(
        BillingPayment.billing_case_id == case_id).order_by(
            BillingPayment.id.asc()).all())

    # advances
    advs = (db.query(BillingAdvance).filter(
        BillingAdvance.billing_case_id == case_id).order_by(
            BillingAdvance.id.asc()).all())

    # insurance + preauth + claims (FIXED)
    ins: Optional[BillingInsuranceCase] = None
    preauths: List[BillingPreauthRequest] = []
    claims: List[BillingClaim] = []

    try:
        ins = (db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == case_id).first())
        if ins:
            preauths = (
                db.query(BillingPreauthRequest).filter(
                    BillingPreauthRequest.insurance_case_id == ins.id)  # ✅ FIX
                .order_by(BillingPreauthRequest.id.asc()).all())
            claims = (
                db.query(BillingClaim).filter(
                    BillingClaim.insurance_case_id == ins.id)  # ✅ FIX
                .order_by(BillingClaim.id.asc()).all())
    except Exception:
        ins = None

    # branding
    branding = _get_branding(db)

    # encounter meta OP/IP numbers
    encounter_meta = _load_encounter_meta(db, case)

    # payer context
    payer_ctx = _load_payer_context(db, case, ins)

    # finance summary (minimal)
    safe_invs = [
        i for i in invs if str(getattr(i, "status", "")).upper() != "VOID"
    ]
    total_billed = sum((_to_dec(getattr(i, "grand_total", 0))
                        for i in safe_invs), Decimal("0"))

    total_paid = Decimal("0")
    for p in pays:
        if str(getattr(p, "status", "")).upper() == "VOID":
            continue
        # receipts will be filtered in PDF by kind/direction; but finance wants total received
        total_paid += _to_dec(getattr(p, "amount", 0))

    # advances totals
    adv_total = Decimal("0")
    adv_balance = Decimal("0")
    for a in advs:
        t = str(getattr(a, "entry_type", "ADVANCE")).upper()
        amt = _to_dec(getattr(a, "amount", 0))
        if t == "ADVANCE":
            adv_total += amt
            adv_balance += amt
        elif t in {"REFUND", "ADJUSTMENT"}:
            adv_balance -= amt

    due = total_billed - total_paid
    if due < 0:
        due = Decimal("0")

    payload: Dict[str, Any] = {
        "branding": branding,

        # used by pdf builder for header fallback
        "printed_dt": datetime.now(),

        # ✅ these are used in footer/signatures
        "printed_by": getattr(user, "name", None),
        "billed_by": getattr(user, "name", None),
        "checked_by":
        None,  # we will fill below based on invoice approver if available
        "case": {
            "case_number": getattr(case, "case_number", None),
            "status": getattr(case, "status", None),
            "payer_mode": getattr(case, "payer_mode", None),
            "encounter_type": getattr(case, "encounter_type", None),
            "encounter_id": getattr(case, "encounter_id", None),
        },
        "patient": {
            "name":
            getattr(patient_obj, "name", None) if patient_obj else None,
            "uhid":
            getattr(patient_obj, "uhid", None) if patient_obj else None,
            "phone":
            getattr(patient_obj, "phone", None) if patient_obj else None,
            "gender":
            getattr(patient_obj, "gender", None) if patient_obj else None,
            "age":
            getattr(patient_obj, "age", None) if patient_obj else None,
            "address":
            getattr(patient_obj, "address", None) if patient_obj else None,
        },

        # ✅ critical for OP/IP number printing
        "encounter_meta": encounter_meta,

        # ✅ payer/insurer names for PDF
        "payer": payer_ctx.get("payer"),
        "insurer": payer_ctx.get("insurer"),
        "insurance_company": payer_ctx.get("insurance_company"),
        "invoices": [],
        "payments": [],
        "advances": [],
        "insurance": None,
        "preauths": [],
        "claims": [],
        "finance": {
            "total_billed": str(total_billed),
            "total_paid": str(total_paid),
            "due": str(due),
            "advance_total": str(adv_total),
            "advance_balance": str(adv_balance),
            # optional: if you compute later
            "advance_consumed": "0",
            "advance_refund": "0",
        },
    }

    # map invoice_id -> invoice_number for payment/claim prints
    inv_no_by_id: Dict[int, str] = {}
    for inv in invs:
        inv_no_by_id[int(inv.id)] = getattr(inv, "invoice_number",
                                            None) or f"INV-{int(inv.id):06d}"

    # find main bill invoice to set checked_by (approved/poster)
    main_bill = None
    for inv in invs:
        if str(getattr(inv, "status", "")).upper() in {"APPROVED", "POSTED"}:
            it = str(getattr(inv, "invoice_type", "")).upper()
            if it == "PATIENT":
                main_bill = inv
    if not main_bill and invs:
        main_bill = invs[-1]

    if main_bill:
        # checked_by = approved_by user name if available
        try:
            ab = getattr(main_bill, "approved_by_user", None)
            pb = getattr(main_bill, "posted_by_user", None)
            payload["checked_by"] = getattr(ab, "name", None) or getattr(
                pb, "name", None)
        except Exception:
            payload["checked_by"] = None

        # billed_by prefer invoice created_by_user
        try:
            cb = getattr(main_bill, "created_by_user", None)
            payload["billed_by"] = getattr(cb, "name",
                                           None) or payload["billed_by"]
        except Exception:
            pass

    # invoices + lines (dict structure is fine for pdf builder)
    for inv in invs:
        inv_no = getattr(inv, "invoice_number",
                         None) or f"INV-{int(inv.id):06d}"

        inv_dict = {
            "invoice_number": inv_no,
            "module": getattr(inv, "module", None),
            "invoice_type": getattr(inv, "invoice_type", None),
            "status": getattr(inv, "status", None),
            "created_at": getattr(inv, "created_at", None),
            "service_date": getattr(inv, "service_date", None),
            "sub_total": str(getattr(inv, "sub_total", 0) or 0),
            "discount_total": str(getattr(inv, "discount_total", 0) or 0),
            "tax_total": str(getattr(inv, "tax_total", 0) or 0),
            "grand_total": str(getattr(inv, "grand_total", 0) or 0),
            "lines": [],
        }

        for ln in by_inv.get(int(inv.id), []):
            # skip deleted style flags if present in your DB (optional)
            if getattr(ln, "is_deleted", False) or getattr(
                    ln, "deleted_at", None):
                continue

            meta_json = getattr(ln, "meta_json", None) or {}
            if not isinstance(meta_json, dict):
                meta_json = {}

            # normalize pharmacy keys for pdf split-up (batch/expiry/hsn)
            batch = meta_json.get("batch_id") or meta_json.get(
                "batch_no") or meta_json.get("batch")
            expiry = meta_json.get("expiry") or meta_json.get(
                "expiry_date") or meta_json.get("exp_date")
            hsn = meta_json.get("hsn") or meta_json.get(
                "hsn_code") or meta_json.get("hsn_sac")

            # keep normalized keys also inside meta_json (so pdf builder reads consistently)
            if batch and "batch_id" not in meta_json:
                meta_json["batch_id"] = batch
            if expiry and "expiry" not in meta_json:
                meta_json["expiry"] = expiry
            if hsn and "hsn" not in meta_json:
                meta_json["hsn"] = hsn

            inv_dict["lines"].append({
                "service_group":
                getattr(ln, "service_group",
                        None),  # ✅ needed for section grouping
                "description":
                getattr(ln, "description", None),
                "item_code":
                getattr(ln, "item_code", None),
                "service_date":
                getattr(ln, "service_date", None)
                or getattr(inv, "service_date", None)
                or getattr(inv, "created_at", None),
                "qty":
                getattr(ln, "qty", None),
                "unit_price":
                str(getattr(ln, "unit_price", 0) or 0),
                "discount_amount":
                str(getattr(ln, "discount_amount", 0) or 0),
                "gst_rate":
                getattr(ln, "gst_rate", None),
                "tax_amount":
                str(getattr(ln, "tax_amount", 0) or 0),
                "net_amount":
                str(getattr(ln, "net_amount", 0) or 0),

                # doc_status fallback used by pdf builder filters
                "doc_status":
                getattr(inv, "status", None),

                # ✅ pharmacy/hsn extras in meta_json
                "meta_json":
                meta_json,
            })

        payload["invoices"].append(inv_dict)

    # payments (IMPORTANT: include kind + direction)
    for p in pays:
        if str(getattr(p, "status", "")).upper() == "VOID":
            continue

        invoice_id = getattr(p, "invoice_id", None)
        payload["payments"].append({
            "receipt_number":
            getattr(p, "receipt_number", None)
            or f"RCPT-{int(getattr(p, 'id', 0) or 0):06d}",
            "received_at":
            getattr(p, "received_at", None) or getattr(p, "created_at", None),
            "mode":
            getattr(p, "mode", None),
            "txn_ref":
            getattr(p, "txn_ref", None),
            "amount":
            str(getattr(p, "amount", 0) or 0),
            "invoice_number":
            inv_no_by_id.get(int(invoice_id), None) if invoice_id else None,
            "status":
            getattr(p, "status", None),

            # ✅ required for PDF filters
            "kind":
            getattr(p, "kind", None),
            "direction":
            getattr(p, "direction", None),
        })

    # advances
    for a in advs:
        # advances table doesn't have status in your model; safe anyway
        payload["advances"].append({
            "receipt_number":
            getattr(a, "receipt_number", None)
            or f"ADV-{int(getattr(a, 'id', 0) or 0):06d}",
            "entry_at":
            getattr(a, "entry_at", None) or getattr(a, "created_at", None),
            "entry_type":
            getattr(a, "entry_type", None),
            "mode":
            getattr(a, "mode", None),
            "txn_ref":
            getattr(a, "txn_ref", None),
            "remarks":
            getattr(a, "remarks", None),
            "amount":
            str(getattr(a, "amount", 0) or 0),
        })

    # insurance block
    if ins:
        payload["insurance"] = {
            "status": getattr(ins, "status", None),
            "payer_kind": getattr(ins, "payer_kind", None),
            "policy_no": getattr(ins, "policy_no", None),
            "member_id": getattr(ins, "member_id", None),
            "plan_name": getattr(ins, "plan_name", None),
            "approved_limit": str(getattr(ins, "approved_limit", 0) or 0),
        }

    for pa in preauths or []:
        payload["preauths"].append({
            "status":
            getattr(pa, "status", None),
            "requested_amount":
            str(getattr(pa, "requested_amount", 0) or 0),
            "approved_amount":
            str(getattr(pa, "approved_amount", 0) or 0),
            "remarks":
            getattr(pa, "remarks", None),
            "submitted_at":
            getattr(pa, "submitted_at", None),
            "approved_at":
            getattr(pa, "approved_at", None),
            "created_at":
            getattr(pa, "created_at", None),
        })

    for cl in claims or []:
        payload["claims"].append({
            "status":
            getattr(cl, "status", None),
            "claim_amount":
            str(getattr(cl, "claim_amount", 0) or 0),
            "approved_amount":
            str(getattr(cl, "approved_amount", 0) or 0),
            "settled_amount":
            str(getattr(cl, "settled_amount", 0) or 0),
            "remarks":
            getattr(cl, "remarks", None),
            "submitted_at":
            getattr(cl, "submitted_at", None),
            "settled_at":
            getattr(cl, "settled_at", None),
            "created_at":
            getattr(cl, "created_at", None),
        })

    return payload


@router.get("/cases/{case_id}/exports/pdf")
def billing_case_export_pdf(
        case_id: int,
        kind: BillingExportKind = BillingExportKind.FULL_CASE,
        download: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    payload = _load_case_export_payload(db, user, case_id)

    if kind == BillingExportKind.FULL_CASE:
        pdf_bytes = build_full_case_pdf(payload)
        case_no = (payload.get("case")
                   or {}).get("case_number") or f"CASE-{case_id:06d}"
        filename = f"BillingCase_{case_no}_FullCase.pdf"
    else:
        raise HTTPException(status_code=400, detail="Unsupported export kind")

    disp = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disp}; filename="{filename}"'}
    return StreamingResponse(BytesIO(pdf_bytes),
                             media_type="application/pdf",
                             headers=headers)


@router.post("/ot/cases/{case_id}/sync")
def billing_sync_ot_case(
        case_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    if create_ot_invoice_items_for_case is None:
        raise HTTPException(status_code=500,
                            detail="billing_ot service not available")

    try:
        inv = create_ot_invoice_items_for_case(db, case_id=case_id, user=user)
        db.commit()
        return {
            "ok":
            True,
            "invoice_id":
            int(getattr(inv, "id", 0) or 0),
            "billing_case_id":
            int(getattr(inv, "billing_case_id", 0) or 0),
            "status":
            getattr(getattr(inv, "status", None), "value",
                    str(getattr(inv, "status", ""))),
        }
    except BillingError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
