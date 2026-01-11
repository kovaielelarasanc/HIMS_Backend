# FILE: app/api/routes_billing.py
from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Optional, Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from app.models.charge_item_master import ChargeItemMaster

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

router = APIRouter(prefix="/billing", tags=["Billing"])


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
        if inp.idempotency_key:
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
            inv = next((x for (x, _) in rows if int(x.id) == iid), None)
            if inv:
                return f"{(inv.module or 'MISC').upper()} · {inv.invoice_number or f'#{inv.id}'}"
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


def _payment_to_dict(p: BillingPayment) -> Dict[str, Any]:
    return {
        "id":
        int(p.id),
        "billing_case_id":
        int(p.billing_case_id),
        "invoice_id":
        int(p.invoice_id) if p.invoice_id else None,
        "payer_type":
        _enum_value(p.payer_type),
        "payer_id":
        p.payer_id,
        "mode":
        _enum_value(p.mode),
        "amount":
        str(getattr(p, "amount", 0) or 0),
        "txn_ref":
        getattr(p, "txn_ref", None),
        "notes":
        getattr(p, "notes", None),
        "received_at":
        p.received_at.isoformat() if getattr(p, "received_at", None) else None,
        "created_at":
        p.created_at.isoformat() if getattr(p, "created_at", None) else None,
        "received_by":
        getattr(p, "received_by", None),
    }


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


@router.get("/invoices/{invoice_id}/payments")
def list_invoice_payments(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        inv = _get_invoice_or_404(db, user, invoice_id)
        rows = (db.query(BillingPayment).filter(
            BillingPayment.invoice_id == int(inv.id)).order_by(
                desc(BillingPayment.received_at)).all())
        return {"items": [_payment_to_dict(x) for x in rows]}
    except Exception as e:
        _err(e)


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
    description: str
    qty: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    item_type: Optional[str] = None
    item_id: Optional[int] = None
    item_code: Optional[str] = None
    doctor_id: Optional[int] = None
    manual_reason: Optional[str] = "Manual entry"

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
        inv = post_invoice(db, invoice_id=invoice_id, user=user)
        db.commit()
        return {"id": int(inv.id), "status": _enum_value(inv.status)}
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


# ============================================================
# ✅ Payments / Advances
# ============================================================
@router.post("/cases/{case_id}/payments")
def pay(
        case_id: int,
        amount: Decimal,
        mode: PayMode = PayMode.CASH,
        invoice_id: Optional[int] = None,
        txn_ref: Optional[str] = None,
        notes: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)

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

        p = record_payment(
            db,
            billing_case_id=case_id,
            user=user,
            amount=amount,
            mode=mode,
            invoice_id=int(inv_id),
            txn_ref=txn_ref,
            notes=notes,
        )
        db.commit()
        db.refresh(p)
        return {
            "id": int(p.id),
            "amount": str(p.amount),
            "mode": _enum_value(p.mode),
            "invoice_id": int(p.invoice_id) if p.invoice_id else None
        }
    except Exception as e:
        db.rollback()
        _err(e)


@router.post("/cases/{case_id}/advances")
def advance(
        case_id: int,
        amount: Decimal,
        entry_type: AdvanceType = AdvanceType.ADVANCE,
        mode: PayMode = PayMode.CASH,
        txn_ref: Optional[str] = None,
        remarks: Optional[str] = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    try:
        _get_case_or_404(db, user, case_id)
        try:
            a = record_advance(
                db,
                billing_case_id=case_id,
                user=user,
                amount=amount,
                entry_type=entry_type,
                mode=mode,
                txn_ref=txn_ref,
                remarks=remarks,
            )
        except TypeError:
            a = record_advance(
                db,
                billing_case_id=case_id,
                user=user,
                amount=amount,
                advance_type=entry_type,
                mode=mode,
                txn_ref=txn_ref,
                notes=remarks,
            )

        db.commit()
        db.refresh(a)
        return {
            "id":
            int(a.id),
            "amount":
            str(getattr(a, "amount", 0) or 0),
            "type":
            _enum_value(
                getattr(a, "entry_type", None)
                or getattr(a, "advance_type", None))
        }
    except Exception as e:
        db.rollback()
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
        _get_case_or_404(db, user, case_id)
        ins = db.query(BillingInsuranceCase).filter(
            BillingInsuranceCase.billing_case_id == int(case_id)).first()
        if not ins:
            ins = BillingInsuranceCase(billing_case_id=int(case_id))

        ins.payer_kind = inp.payer_kind
        ins.insurance_company_id = inp.insurance_company_id
        ins.tpa_id = inp.tpa_id
        ins.corporate_id = inp.corporate_id
        ins.policy_no = inp.policy_no
        ins.member_id = inp.member_id
        ins.plan_name = inp.plan_name
        ins.status = inp.status
        ins.approved_limit = Decimal(str(inp.approved_limit or 0))
        ins.approved_at = inp.approved_at

        db.add(ins)
        db.commit()
        db.refresh(ins)
        return {"insurance": _insurance_to_dict(ins)}
    except Exception as e:
        db.rollback()
        _err(e)
