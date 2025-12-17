# NEW FILE: app/api/routes_billing.py
from __future__ import annotations

from datetime import datetime, date
from typing import List, Optional, Dict, Any
from decimal import Decimal
from math import ceil
import logging
import io
import uuid
from datetime import datetime, date, timedelta
from pydantic import ValidationError
from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.services.room_type import normalize_room_type
from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.patient import Patient
from app.services.ipd_billing import compute_ipd_bed_charges_daily, ensure_invoice_for_context  # if in same file, remove this line
from app.models.billing import (
    Invoice,
    InvoiceItem,
    Payment,
    Advance,
    AdvanceAdjustment,
    BillingProvider,
)

# ✅ OT imports (this was missing in your file)
from app.models.ot import OtCase, OtScheduleProcedure, OtProcedure, OtSchedule  # IMPORTANT

from app.schemas.billing import (
    InvoiceCreate,
    ManualItemIn,
    AddServiceIn,
    UpdateItemIn,
    VoidItemIn,
    PaymentIn,
    InvoiceOut,
    InvoiceItemOut,
    PaymentOut,
    AutoBedChargesIn,
    AutoOtChargesIn,
    AutoOtInvoiceIn,
    AdvanceCreate,
    ApplyAdvanceIn,
)
from app.models.ipd import IpdAdmission
import zlib
from app.models.ipd import IpdPackage, IpdBed, IpdBedRate, IpdBedAssignment, IpdRoom
from app.models.payer import Payer, Tpa, CreditPlan
from app.core.config import settings
from app.services.ui_branding import get_or_create_default_ui_branding
from app.services.pdf_branding import brand_header_css, render_brand_header_html
import base64
import mimetypes
from pathlib import Path

from fastapi import Query

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IPD Billing lock helpers
# ---------------------------------------------------------------------------


def _get_ipd_admission_for_invoice(db: Session,
                                   inv: Invoice) -> IpdAdmission | None:
    """
    If invoice context is IPD admission, return the admission row, else None.
    """
    if (inv.context_type or "").lower() != "ipd":
        return None
    if not inv.context_id:
        return None
    return db.query(IpdAdmission).get(int(inv.context_id))


def _ensure_ipd_not_locked_for_item_mutation(db: Session,
                                             inv: Invoice) -> None:
    """
    Block item edits if admission is discharged/locked.
    Payments are handled separately (allowed).
    """
    adm = _get_ipd_admission_for_invoice(db, inv)
    if adm and getattr(adm, "billing_locked", False):
        raise HTTPException(
            400,
            "Billing is locked for this IPD admission (discharged). Items cannot be edited."
        )


def _ensure_ipd_not_locked_for_invoice_update(db: Session,
                                              inv: Invoice) -> None:
    """
    Block invoice header edits/cancel when locked (optional but recommended).
    """
    adm = _get_ipd_admission_for_invoice(db, inv)
    if adm and getattr(adm, "billing_locked", False):
        raise HTTPException(
            400,
            "Billing is locked for this IPD admission (discharged). Invoice cannot be edited."
        )


def _service_ref_id(key: str) -> int:
    """
    Global-safe service_ref_id for your UNIQUE(service_type, service_ref_id, is_voided).
    Uses crc32 -> int (stable and safe).
    """
    return int(zlib.crc32(key.encode("utf-8")))


def ensure_invoice_for_context(
    db: Session,
    *,
    patient_id: int,
    billing_type: str,
    context_type: str,
    context_id: int,
    created_by: int | None,
) -> Invoice:
    inv = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id,
        Invoice.billing_type == billing_type,
        Invoice.context_type == context_type,
        Invoice.context_id == context_id,
        Invoice.status != "cancelled",
    ).order_by(Invoice.id.desc()).first())
    if inv:
        # backfill ids
        if not inv.invoice_uid:
            inv.invoice_uid = _new_invoice_uid()
        if not inv.invoice_number:
            inv.invoice_number = _new_invoice_number()
        db.flush()
        return inv

    inv = Invoice(
        invoice_uid=_new_invoice_uid(),
        invoice_number=_new_invoice_number(),
        patient_id=patient_id,
        context_type=context_type,
        context_id=context_id,
        billing_type=billing_type,
        status="draft",
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


# ---------------------------------------------------------------------------
# Permissions helper
# ---------------------------------------------------------------------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []):
        for p in getattr(r, "permissions", []):
            if p.code == code:
                return True
    return False


# ---------------------------------------------------------------------------
# Invoice identity helpers
# ---------------------------------------------------------------------------
def _new_invoice_uid() -> str:
    return str(uuid.uuid4())


def _new_invoice_number() -> str:
    # UUID based, short, realtime, no raw id exposure
    return f"INV-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------
def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x or "0"))
    except Exception:
        return Decimal("0")


def _money(x: Any) -> str:
    return f"{float(x or 0):.2f}"


def _next_seq_for_invoice(db: Session, invoice_id: int) -> int:
    max_seq = db.query(func.max(InvoiceItem.seq)).filter(
        InvoiceItem.invoice_id == invoice_id).scalar()
    return (max_seq or 0) + 1


def _manual_ref_for_invoice(invoice_id: int, seq: int) -> int:
    return invoice_id * 1_000_000 + seq


# ---------------------------------------------------------------------------
# Totals (FIXED)
# ---------------------------------------------------------------------------
def recalc_totals(inv: Invoice, db: Session) -> None:
    """
    ✅ Correct totals:
      - gross_total = sum(qty*price) for active items
      - discount_total = sum(line_discount) + header_discount_amount
      - tax_total = sum(line_tax)
      - net_total = (gross - line_discounts - header_discount) + tax
      - amount_paid = sum(payments)
      - advance_adjusted = sum(advance_adjustments)
      - balance_due = net_total - amount_paid - advance_adjusted
    """
    gross_subtotal = Decimal("0")
    line_discount = Decimal("0")
    tax_total = Decimal("0")

    for it in inv.items:
        if it.is_voided:
            continue
        qty = _d(it.quantity)
        price = _d(it.unit_price)
        base = qty * price

        disc_amt = _d(it.discount_amount)
        tax_amt = _d(it.tax_amount)

        gross_subtotal += base
        line_discount += disc_amt
        tax_total += tax_amt

    # header discount
    header_disc_amt = _d(inv.header_discount_amount)
    header_disc_pct = _d(inv.header_discount_percent)

    # if percent provided but amount empty -> derive
    if header_disc_pct and (not header_disc_amt or header_disc_amt == 0):
        header_disc_amt = (gross_subtotal -
                           line_discount) * header_disc_pct / Decimal("100")
        header_disc_amt = header_disc_amt.quantize(Decimal("0.01"))
        inv.header_discount_amount = header_disc_amt

    total_discount = (line_discount + header_disc_amt).quantize(
        Decimal("0.01"))
    net = (gross_subtotal - line_discount - header_disc_amt +
           tax_total).quantize(Decimal("0.01"))

    inv.gross_total = gross_subtotal.quantize(Decimal("0.01"))
    inv.tax_total = tax_total.quantize(Decimal("0.01"))
    inv.discount_total = total_discount
    inv.net_total = net

    paid = Decimal("0")
    for pay in inv.payments:
        paid += _d(pay.amount)
    inv.amount_paid = paid.quantize(Decimal("0.01"))

    adv_used = Decimal("0")
    for adj in inv.advance_adjustments:
        adv_used += _d(adj.amount_applied)
    inv.advance_adjusted = adv_used.quantize(Decimal("0.01"))

    inv.balance_due = (net - paid - adv_used).quantize(Decimal("0.01"))


from app.schemas.billing import PatientMiniOut  # ✅ import


def serialize_invoice(inv: Invoice) -> InvoiceOut:
    if not getattr(inv, "invoice_uid", None):
        inv.invoice_uid = _new_invoice_uid()
    if not getattr(inv, "invoice_number", None):
        inv.invoice_number = _new_invoice_number()

    items_out = [
        InvoiceItemOut.model_validate(it, from_attributes=True)
        for it in inv.items
    ]
    pays_out = [
        PaymentOut.model_validate(p, from_attributes=True)
        for p in inv.payments
    ]

    data = InvoiceOut.model_validate(inv, from_attributes=True)
    data.items = items_out
    data.payments = pays_out

    # ✅ ADD patient object
    if getattr(inv, "patient", None):
        p = inv.patient
        full = (f"{p.first_name or ''} {p.last_name or ''}").strip() or None
        data.patient = PatientMiniOut.model_validate(p, from_attributes=True)
        data.patient.full_name = full

    return data


@router.post("/ipd/admissions/{admission_id}/ensure-invoices",
             response_model=dict)
def ensure_ipd_invoices(
        admission_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    ip_inv = ensure_invoice_for_context(
        db,
        patient_id=adm.patient_id,
        billing_type="ip_billing",
        context_type="ipd",
        context_id=adm.id,
        created_by=user.id,
    )

    pharm_inv = ensure_invoice_for_context(
        db,
        patient_id=adm.patient_id,
        billing_type="pharmacy",
        context_type="ipd",
        context_id=adm.id,
        created_by=user.id,
    )

    ot_inv = ensure_invoice_for_context(
        db,
        patient_id=adm.patient_id,
        billing_type="ot",
        context_type="ipd",
        context_id=adm.id,
        created_by=user.id,
    )
    ris_inv = ensure_invoice_for_context(
        db,
        patient_id=adm.patient_id,
        billing_type="radiology",  # ✅ ADD THIS
        context_type="ipd",
        context_id=adm.id,
        created_by=user.id,
    )

    db.commit()
    return {
        "ip_invoice_id": ip_inv.id,
        "pharmacy_invoice_id": pharm_inv.id,
        "ot_invoice_id": ot_inv.id,
        "radiology_invoice_id": ris_inv.id,  # ✅ ADD
    }


def _resolve_bed_daily_rate(db: Session, room_type: str,
                            on_date: date) -> Decimal:
    rt = normalize_room_type(room_type)
    r = (
        db.query(IpdBedRate).filter(IpdBedRate.is_active.is_(True)).filter(
            IpdBedRate.room_type == rt).filter(
                IpdBedRate.effective_from <= on_date).filter(
                    (IpdBedRate.effective_to == None) |
                    (IpdBedRate.effective_to >= on_date))  # noqa
        .order_by(IpdBedRate.effective_from.desc()).first())
    return _d(getattr(r, "daily_rate", 0))


@router.post("/ipd/admissions/{admission_id}/auto-bed-charges",
             response_model=InvoiceOut)
def auto_ipd_bed_charges(
        admission_id: int,
        payload: AutoBedChargesIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(403, "Not permitted")

    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")

    inv = ensure_invoice_for_context(
        db,
        patient_id=adm.patient_id,
        billing_type="ip_billing",
        context_type="ipd",
        context_id=adm.id,
        created_by=user.id,
    )

    # decide date range
    start_date = payload.from_date or (adm.admitted_at.date()
                                       if adm.admitted_at else
                                       datetime.utcnow().date())
    end_date = payload.to_date or datetime.utcnow().date()

    assigns = (db.query(IpdBedAssignment).filter(
        IpdBedAssignment.admission_id == adm.id).order_by(
            IpdBedAssignment.from_ts.asc()).all())

    d = start_date
    while d <= end_date:
        # pick active assignment for day (simple daily)
        active = None
        day_start = datetime.combine(d, datetime.min.time())
        day_end = datetime.combine(d, datetime.max.time())
        for a in assigns:
            if a.from_ts <= day_end and ((a.to_ts is None) or
                                         (a.to_ts >= day_start)):
                active = a

        if active:
            bed = db.get(IpdBed, active.bed_id)
            room = db.get(IpdRoom, bed.room_id) if bed and getattr(
                bed, "room_id", None) else None

            room_type = normalize_room_type(getattr(room, "type", None))
            rate = _resolve_bed_daily_rate(db, room_type, d)

            # ✅ global-safe item
            ref_key = f"ipd:{adm.id}:bed:{active.id}:{d.isoformat()}"
            safe_ref = _service_ref_id(ref_key)

            exists = db.query(InvoiceItem).filter(
                InvoiceItem.service_type == "ipd_bed",
                InvoiceItem.service_ref_id == safe_ref,
                InvoiceItem.is_voided.is_(False),
            ).first()

            if not exists and rate > 0:
                seq = _next_seq_for_invoice(db, inv.id)
                qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
                    Decimal("1"), rate, Decimal("0"), Decimal("0"),
                    payload.tax_rate or Decimal("0"))
                db.add(
                    InvoiceItem(
                        invoice_id=inv.id,
                        seq=seq,
                        service_type="ipd_bed",
                        service_ref_id=safe_ref,
                        description=
                        f"Bed charge — {d.strftime('%d-%m-%Y')} — {room_type}",
                        quantity=qty,
                        unit_price=price,
                        tax_rate=tax_rate,
                        discount_percent=disc_pct,
                        discount_amount=disc_amt,
                        tax_amount=tax_amt,
                        line_total=line_total,
                        created_by=user.id,
                    ))

        d = (d + timedelta(days=1))

    db.flush()
    inv = db.query(Invoice).options(
        joinedload(Invoice.items),
        joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments),
    ).get(inv.id)

    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.get("/masters", response_model=dict)
def billing_masters(
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    from app.models.user import User as UserModel

    doctors_q = db.query(UserModel).filter(UserModel.is_active.is_(True))
    if hasattr(UserModel, "is_doctor"):
        doctors_q = doctors_q.filter(UserModel.is_doctor.is_(True))
    doctors = [{
        "id": d.id,
        "name": d.name,
        "email": d.email
    } for d in doctors_q.order_by(UserModel.name.asc()).all()]

    providers = [{
        "id": p.id,
        "name": p.name,
        "code": p.code,
        "provider_type": p.provider_type
    } for p in db.query(BillingProvider).filter(
        BillingProvider.is_active.is_(True)).order_by(
            BillingProvider.name.asc()).all()]

    payers = [{
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "payer_type": p.payer_type
    } for p in db.query(Payer).order_by(Payer.name.asc()).all()]

    tpas = [{
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "payer_id": t.payer_id
    } for t in db.query(Tpa).order_by(Tpa.name.asc()).all()]

    credit_plans = [{
        "id": c.id,
        "code": c.code,
        "name": c.name,
        "payer_id": c.payer_id,
        "tpa_id": c.tpa_id
    } for c in db.query(CreditPlan).order_by(CreditPlan.name.asc()).all()]

    packages = [{
        "id": pkg.id,
        "name": pkg.name,
        "charges": float(pkg.charges or 0)
    } for pkg in db.query(IpdPackage).order_by(IpdPackage.name.asc()).all()]

    return {
        "doctors": doctors,
        "credit_providers": providers,
        "payers": payers,
        "tpas": tpas,
        "credit_plans": credit_plans,
        "packages": packages,
    }


def apply_ipd_bed_charges_to_invoice(
    db: Session,
    admission_id: int,
    upto_date: date,
    user_id: int | None = None,
    tax_rate: float = 0.0,
) -> Invoice:
    adm = db.query(IpdAdmission).get(admission_id)
    if not adm:
        raise ValueError("Admission not found")

    from_date = (adm.admitted_at.date() if adm.admitted_at else upto_date)

    inv = ensure_invoice_for_context(
        db=db,
        patient_id=adm.patient_id,
        billing_type="ip_billing",
        context_type="ipd",
        context_id=adm.id,
    )

    # Daily breakdown from your existing calculator
    preview = compute_ipd_bed_charges_daily(db, admission_id, from_date,
                                            upto_date)

    for d in preview.days:
        # unique per day + assignment
        safe_ref = f"{d.assignment_id}:{d.date.isoformat()}"

        exists = (
            db.query(InvoiceItem).filter(
                InvoiceItem.invoice_id == inv.id,  # ✅ IMPORTANT
                InvoiceItem.service_type == "ipd_bed",
                InvoiceItem.service_ref_id == safe_ref,
                InvoiceItem.is_voided.is_(False),
            ).first())

        qty = 1
        unit_price = float(d.rate or 0.0)
        amount = round(qty * unit_price, 2)

        if exists:
            # update if rate changed
            exists.qty = qty
            exists.unit_price = unit_price
            exists.amount = amount
            exists.tax_rate = tax_rate
            exists.tax_amount = round(amount * (tax_rate / 100.0), 2)
            exists.total_amount = round(amount + exists.tax_amount, 2)
        else:
            item = InvoiceItem(
                invoice_id=inv.id,
                name=
                f"Bed Charges ({d.room_type}) - {d.date.strftime('%d-%m-%Y')}",
                qty=qty,
                unit_price=unit_price,
                amount=amount,
                tax_rate=tax_rate,
                tax_amount=round(amount * (tax_rate / 100.0), 2),
                total_amount=round(
                    amount + round(amount * (tax_rate / 100.0), 2), 2),
                service_type="ipd_bed",
                service_ref_id=safe_ref,
                created_by=user_id,
            )
            db.add(item)

    # make totals correct
    inv.recalc()
    db.add(inv)
    return inv


# ---------------------------------------------------------------------------
# Core Invoices
# ---------------------------------------------------------------------------


@router.get("/patients/{patient_id}/summary", response_model=dict)
def patient_billing_summary(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    invs = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id).order_by(
            Invoice.created_at.asc()).all())

    invoices_out = []
    total_net = total_paid = total_balance = 0.0

    for inv in invs:
        inv = db.query(Invoice).options(
            joinedload(Invoice.items),
            joinedload(Invoice.payments),
            joinedload(Invoice.advance_adjustments),
        ).get(inv.id)

        recalc_totals(inv, db)

        net = float(inv.net_total or 0)
        paid = float(inv.amount_paid or 0)
        bal = float(inv.balance_due or 0)

        total_net += net
        total_paid += paid
        total_balance += bal

        invoices_out.append({
            "id":
            inv.id,
            "invoice_number":
            getattr(inv, "invoice_number", None) or str(inv.id),
            "billing_type":
            getattr(inv, "billing_type", None) or "general",
            "context_type":
            inv.context_type,
            "context_id":
            inv.context_id,
            "status":
            inv.status,
            "net_total":
            net,
            "amount_paid":
            paid,
            "balance_due":
            bal,
            "created_at":
            inv.created_at.isoformat() if inv.created_at else None,
            "finalized_at":
            inv.finalized_at.isoformat() if inv.finalized_at else None,
        })

    db.commit()

    return {
        "patient": {
            "id": patient.id,
            "uhid": getattr(patient, "uhid", None),
            "name":
            f"{patient.first_name or ''} {patient.last_name or ''}".strip(),
            "phone": patient.phone,
        },
        "invoices": invoices_out,
        "totals": {
            "net_total": total_net,
            "amount_paid": total_paid,
            "balance_due": total_balance,
        },
    }


@router.get("/patient/{patient_id}/summary", response_model=dict)
def patient_billing_summary_alias(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return patient_billing_summary(patient_id=patient_id, db=db, user=user)


@router.get("/invoices/{invoice_id}/unbilled", response_model=List[dict])
def fetch_unbilled_services(
        invoice_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    # TODO: later fetch OPD/LIS/RIS/Pharmacy unbilled items for this invoice context
    return []


@router.post("/invoices", response_model=InvoiceOut)
def create_invoice(
        payload: InvoiceCreate,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = Invoice(
        invoice_uid=_new_invoice_uid(),
        invoice_number=_new_invoice_number(),
        patient_id=payload.patient_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        billing_type=payload.billing_type,
        provider_id=payload.provider_id,
        consultant_id=payload.consultant_id,
        visit_no=payload.visit_no,
        remarks=payload.remarks,
        status="draft",
        created_by=user.id,
    )

    db.add(inv)
    db.commit()
    db.refresh(inv)
    recalc_totals(inv, db)
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")
    inv = db.query(Invoice).options(
        joinedload(Invoice.items), joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")
    recalc_totals(inv, db)
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.get("/invoices", response_model=List[InvoiceOut])
def list_invoices(
        patient_id: Optional[int] = None,
        patient_uhid: Optional[str] = None,
        billing_type: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(Invoice)

    if patient_uhid:
        patient = db.query(Patient).filter(
            Patient.uhid == patient_uhid).first()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")
        q = q.filter(Invoice.patient_id == patient.id)
    elif patient_id:
        q = q.filter(Invoice.patient_id == patient_id)

    if billing_type:
        q = q.filter(Invoice.billing_type == billing_type)
    if status:
        q = q.filter(Invoice.status == status)
    if from_date:
        q = q.filter(Invoice.created_at >= datetime.combine(
            from_date, datetime.min.time()))
    if to_date:
        q = q.filter(Invoice.created_at <= datetime.combine(
            to_date, datetime.max.time()))

    invs = q.order_by(Invoice.id.desc()).limit(500).all()

    # lightweight serialize
    out = []
    for inv in invs:
        inv = db.query(Invoice).options(
            joinedload(Invoice.patient),
            joinedload(Invoice.items),
            joinedload(Invoice.payments),
            joinedload(Invoice.advance_adjustments),
        ).get(inv.id)
        recalc_totals(inv, db)
        out.append(serialize_invoice(inv))
    db.commit()
    return out


@router.put("/invoices/{invoice_id}", response_model=InvoiceOut)
def update_invoice(
        invoice_id: int,
        payload: dict = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")

    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    for k, v in payload.items():
        if hasattr(inv, k):
            setattr(inv, k, v)

    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)

    recalc_totals(inv, db)
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/finalize", response_model=InvoiceOut)
def finalize_invoice(invoice_id: int,
                     db: Session = Depends(get_db),
                     user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.finalize"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(
        joinedload(Invoice.items), joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if inv.status == "cancelled":
        raise HTTPException(status_code=400,
                            detail="Cancelled invoice cannot be finalized")

    recalc_totals(inv, db)
    inv.status = "finalized"
    inv.finalized_at = datetime.utcnow()
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/cancel")
def cancel_invoice(invoice_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.finalize"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Not found")

    inv.status = "cancelled"
    inv.cancelled_at = datetime.utcnow()
    inv.updated_by = user.id
    db.commit()
    return {"message": "Cancelled"}


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
def _compute_line(payload_qty, payload_price, payload_disc_pct,
                  payload_disc_amt, payload_tax_rate):
    qty = _d(payload_qty)
    price = _d(payload_price)
    base = (qty * price)

    disc_pct = _d(payload_disc_pct)
    disc_amt = _d(payload_disc_amt)

    if disc_pct and (not disc_amt or disc_amt == 0):
        disc_amt = (base * disc_pct / Decimal("100")).quantize(Decimal("0.01"))
    elif disc_amt and (not disc_pct or disc_pct == 0) and base:
        disc_pct = (disc_amt * Decimal("100") / base).quantize(Decimal("0.01"))

    taxable = base - disc_amt
    tax_rate = _d(payload_tax_rate)
    tax_amt = (taxable * tax_rate / Decimal("100")).quantize(Decimal("0.01"))

    line_total = (taxable + tax_amt).quantize(Decimal("0.01"))
    return qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total


@router.post("/invoices/{invoice_id}/items/manual", response_model=InvoiceOut)
def add_manual_item(invoice_id: int,
                    payload: ManualItemIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    seq = _next_seq_for_invoice(db, invoice_id)
    service_type = (payload.service_type or "manual").strip() or "manual"
    service_ref_id = payload.service_ref_id or _manual_ref_for_invoice(
        invoice_id, seq)

    qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
        payload.quantity, payload.unit_price, payload.discount_percent,
        payload.discount_amount, payload.tax_rate)

    it = InvoiceItem(
        invoice_id=invoice_id,
        seq=seq,
        service_type=service_type,
        service_ref_id=int(service_ref_id),
        description=(payload.description or "").strip(),
        quantity=qty,
        unit_price=price,
        tax_rate=tax_rate,
        discount_percent=disc_pct,
        discount_amount=disc_amt,
        tax_amount=tax_amt,
        line_total=line_total,
        created_by=user.id,
    )

    db.add(it)
    db.flush()
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/items/service", response_model=InvoiceOut)
def add_service_item(
        invoice_id: int,
        payload: AddServiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    # ✅ GLOBAL SAFE service_ref_id (critical)
    # Create a full context key so OP/IP never collide
    ref_key = f"{inv.context_type}:{inv.context_id}:{payload.service_type}:{payload.service_ref_id}"
    safe_ref_id = _service_ref_id(ref_key)

    # ✅ Idempotent (avoid duplicate insert + unique constraint crash)
    existing = db.query(InvoiceItem).filter(
        InvoiceItem.service_type == payload.service_type,
        InvoiceItem.service_ref_id == safe_ref_id,
        InvoiceItem.is_voided.is_(False),
    ).first()
    if existing:
        recalc_totals(inv, db)
        db.commit()
        db.refresh(inv)
        return serialize_invoice(inv)

    seq = _next_seq_for_invoice(db, invoice_id)
    desc = (payload.description
            or f"{payload.service_type.upper()} #{payload.service_ref_id}")

    qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
        payload.quantity,
        (payload.unit_price or 0),
        payload.discount_percent,
        payload.discount_amount,
        payload.tax_rate,
    )

    it = InvoiceItem(
        invoice_id=invoice_id,
        seq=seq,
        service_type=payload.service_type,
        service_ref_id=safe_ref_id,  # ✅ safe
        description=desc,
        quantity=qty,
        unit_price=price,
        tax_rate=tax_rate,
        discount_percent=disc_pct,
        discount_amount=disc_amt,
        tax_amount=tax_amt,
        line_total=line_total,
        is_voided=False,
        created_by=user.id,
    )

    db.add(it)
    db.flush()
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.put("/invoices/{invoice_id}/items/{item_id}",
            response_model=InvoiceOut)
def update_item(invoice_id: int,
                item_id: int,
                payload: UpdateItemIn,
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Item not found")

    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(it, k, v)

    qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
        it.quantity, it.unit_price, it.discount_percent, it.discount_amount,
        it.tax_rate)
    it.quantity = qty
    it.unit_price = price
    it.discount_percent = disc_pct
    it.discount_amount = disc_amt
    it.tax_rate = tax_rate
    it.tax_amount = tax_amt
    it.line_total = line_total
    it.updated_by = user.id

    db.commit()
    db.refresh(inv)
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/items/{item_id}/void",
             response_model=InvoiceOut)
def void_item(invoice_id: int,
              item_id: int,
              payload: VoidItemIn,
              db: Session = Depends(get_db),
              user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status in ("finalized", ):
        raise HTTPException(status_code=400,
                            detail="Finalized invoice cannot be edited")

    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Item not found")

    it.is_voided = True
    it.void_reason = payload.reason
    it.voided_by = user.id
    it.voided_at = datetime.utcnow()
    db.commit()

    db.refresh(inv)
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


# ---------------------------------------------------------------------------
# OT Billing: FIX + Auto Create Invoice
# ---------------------------------------------------------------------------
def _case_completed(case: OtCase) -> bool:
    s = (getattr(case, "status", "") or "").lower()
    o = (getattr(case, "outcome", "") or "").lower()
    return (s in ("completed", "done",
                  "closed")) or (o in ("completed", "converted", "success",
                                       "successful"))


from datetime import timedelta  # make sure this exists at top


@router.post("/invoices/{invoice_id}/items/ot-auto", response_model=InvoiceOut)
def auto_add_ot_charges(
        invoice_id: int,
        payload: AutoOtChargesIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.items.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure)).get(payload.case_id))
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    if not _case_completed(case):
        raise HTTPException(
            status_code=400,
            detail="OT case is not completed; cannot auto-bill.",
        )

    start = getattr(case, "actual_start_time", None)
    end = getattr(case, "actual_end_time", None)
    if not start or not end or end <= start:
        raise HTTPException(status_code=400,
                            detail="Invalid OT timings; cannot auto-bill.")

    hours = Decimal(str(
        (end - start).total_seconds() / 3600.0)).quantize(Decimal("0.01"))
    if hours <= 0:
        raise HTTPException(status_code=400,
                            detail="OT duration is zero; cannot auto-bill.")

    schedule = case.schedule
    if not schedule:
        raise HTTPException(status_code=400,
                            detail="OT case not linked to schedule.")

    links: list[OtScheduleProcedure] = list(
        getattr(schedule, "procedures", []) or [])
    if not links:
        raise HTTPException(status_code=400,
                            detail="No OT procedures linked to this schedule.")

    # Existing OT items (service_ref_id values already stored)
    existing_ids = set(
        int(x[0]) for x in db.query(InvoiceItem.service_ref_id).filter(
            InvoiceItem.invoice_id == invoice_id,
            InvoiceItem.service_type == "ot_procedure",
            InvoiceItem.is_voided.is_(False),
            InvoiceItem.service_ref_id.isnot(None),
        ).all() if x[0] is not None)

    created_any = False

    for link in links:
        proc: OtProcedure | None = getattr(link, "procedure", None)
        if not proc:
            continue

        rate = _d(getattr(proc, "rate_per_hour", 0))
        if rate <= 0:
            continue

        # ✅ GLOBAL SAFE ref id for UNIQUE(service_type, service_ref_id, is_voided)
        ref_key = f"otcase:{payload.case_id}:proc_link:{link.id}"
        safe_ref_id = _service_ref_id(ref_key)

        # ✅ Idempotent
        if safe_ref_id in existing_ids:
            continue

        seq = _next_seq_for_invoice(db, invoice_id)
        desc = f"OT charges - {proc.name} ({float(hours):.2f} hr, Case #{case.id})"

        qty, price, disc_pct, disc_amt, tax_rate, tax_amt, line_total = _compute_line(
            hours, rate, Decimal("0"), Decimal("0"), Decimal("0"))

        item = InvoiceItem(
            invoice_id=invoice_id,
            seq=seq,
            service_type="ot_procedure",
            service_ref_id=safe_ref_id,  # ✅ correct
            description=desc,
            quantity=qty,
            unit_price=price,
            tax_rate=tax_rate,
            discount_percent=disc_pct,
            discount_amount=disc_amt,
            tax_amount=tax_amt,
            line_total=line_total,
            is_voided=False,
            created_by=user.id,
        )
        db.add(item)
        created_any = True
        existing_ids.add(safe_ref_id)

    db.flush()
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)

    if not created_any:
        logger.info(
            "No new OT charges created (already billed) invoice=%s case=%s",
            invoice_id, payload.case_id)

    return serialize_invoice(inv)


@router.post("/ot/auto-invoice", response_model=InvoiceOut)
def auto_create_invoice_for_ot_case(
        payload: AutoOtInvoiceIn,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    case_id = payload.case_id

    case = (db.query(OtCase).options(
        joinedload(OtCase.schedule).joinedload(
            OtSchedule.procedures).joinedload(
                OtScheduleProcedure.procedure)).get(case_id))
    if not case:
        raise HTTPException(status_code=404, detail="OT case not found")

    if not case.schedule or not case.schedule.patient_id:
        raise HTTPException(status_code=400,
                            detail="OT case not linked to schedule/patient")

    patient_id = case.schedule.patient_id

    # ✅ keep context_type consistent everywhere
    ctx_type = "ot_case"

    inv = (db.query(Invoice).filter(
        Invoice.context_type == ctx_type,
        Invoice.context_id == case_id,
        Invoice.billing_type == "ot",
        Invoice.status != "cancelled",
    ).order_by(Invoice.id.desc()).first())

    if not inv:
        inv = Invoice(
            invoice_uid=_new_invoice_uid(),
            invoice_number=_new_invoice_number(),
            patient_id=patient_id,
            context_type=ctx_type,
            context_id=case_id,
            billing_type="ot",
            status="draft",
            created_by=user.id,
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)

    # ✅ idempotent OT item add
    auto_add_ot_charges(inv.id, AutoOtChargesIn(case_id=case_id), db, user)

    inv = (db.query(Invoice).options(
        joinedload(Invoice.items),
        joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments),
    ).get(inv.id))
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)

    if payload.finalize:
        inv.status = "finalized"
        inv.finalized_at = datetime.utcnow()
        inv.updated_by = user.id
        db.commit()
        db.refresh(inv)

    return serialize_invoice(inv)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
@router.post("/invoices/{invoice_id}/payments/bulk", response_model=InvoiceOut)
def add_payments_bulk(invoice_id: int,
                      payload: dict,
                      db: Session = Depends(get_db),
                      user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(
        joinedload(Invoice.payments), joinedload(Invoice.items),
        joinedload(Invoice.advance_adjustments)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    raw = payload
    raw_payments = raw if isinstance(raw, list) else (raw.get("payments")
                                                      or [])
    if not raw_payments:
        raise HTTPException(status_code=400, detail="No payments provided")

    parsed: list[PaymentIn] = []
    for p in raw_payments:
        try:
            parsed.append(PaymentIn(**p))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors())

    for p in parsed:
        db.add(
            Payment(
                invoice_id=invoice_id,
                amount=p.amount,
                mode=p.mode,
                reference_no=p.reference_no,
                notes=p.notes,
                created_by=user.id,
            ))

    db.flush()
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


@router.post("/invoices/{invoice_id}/payments", response_model=InvoiceOut)
def add_payment(invoice_id: int,
                payload: dict = Body(...),
                db: Session = Depends(get_db),
                user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")
    if isinstance(payload, dict) and "payments" in payload:
        return add_payments_bulk(invoice_id, payload, db, user)
    try:
        p = PaymentIn(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    return add_payments_bulk(invoice_id, {"payments": [p.model_dump()]}, db,
                             user)


@router.delete("/invoices/{invoice_id}/payments/{payment_id}",
               response_model=InvoiceOut)
def delete_payment(invoice_id: int,
                   payment_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(
        joinedload(Invoice.payments), joinedload(Invoice.items),
        joinedload(Invoice.advance_adjustments)).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    pay = db.query(Payment).get(payment_id)
    if not pay or pay.invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Payment not found")

    db.delete(pay)
    db.commit()

    inv = db.query(Invoice).options(
        joinedload(Invoice.payments), joinedload(Invoice.items),
        joinedload(Invoice.advance_adjustments)).get(invoice_id)
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


# ---------------------------------------------------------------------------
# Advances (FIXED using AdvanceAdjustment table)
# ---------------------------------------------------------------------------
@router.post("/advances", response_model=dict)
def create_advance(payload: AdvanceCreate,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth_current_user)):
    if not has_perm(user, "billing.create"):
        raise HTTPException(status_code=403, detail="Not permitted")

    adv = Advance(
        patient_id=payload.patient_id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        amount=payload.amount,
        balance_remaining=payload.amount,
        mode=payload.mode,
        reference_no=payload.reference_no,
        remarks=payload.remarks,
        created_by=user.id,
    )
    db.add(adv)
    db.commit()
    db.refresh(adv)
    return {
        "id": adv.id,
        "patient_id": adv.patient_id,
        "amount": float(adv.amount or 0),
        "balance_remaining": float(adv.balance_remaining or 0),
        "mode": adv.mode,
        "reference_no": adv.reference_no,
        "remarks": adv.remarks,
    }


@router.get("/advances", response_model=List[dict])
def list_advances(
        patient_id: Optional[int] = None,
        patient_uhid: Optional[str] = None,
        only_with_balance: bool = False,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    q = db.query(Advance)
    if patient_uhid:
        patient = db.query(Patient).filter(
            Patient.uhid == patient_uhid).first()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")
        q = q.filter(Advance.patient_id == patient.id)
    elif patient_id:
        q = q.filter(Advance.patient_id == patient_id)

    if only_with_balance:
        q = q.filter(Advance.balance_remaining > 0)

    res = []
    for adv in q.order_by(Advance.id.desc()).all():
        res.append({
            "id":
            adv.id,
            "patient_id":
            adv.patient_id,
            "amount":
            float(adv.amount or 0),
            "balance_remaining":
            float(adv.balance_remaining or 0),
            "mode":
            adv.mode,
            "reference_no":
            adv.reference_no,
            "remarks":
            adv.remarks,
            "created_at":
            adv.created_at.isoformat()
            if getattr(adv, "created_at", None) else None,
        })
    return res


@router.post("/invoices/{invoice_id}/apply-advances",
             response_model=InvoiceOut)
def apply_advances_to_invoice(
        invoice_id: int,
        payload: ApplyAdvanceIn = Body(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.payments.add"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv = db.query(Invoice).options(
        joinedload(Invoice.items),
        joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments),
    ).get(invoice_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if inv.status in ("finalized", "cancelled"):
        raise HTTPException(status_code=400, detail="Invoice locked")

    recalc_totals(inv, db)

    remaining = _d(inv.balance_due)
    if remaining <= 0:
        db.commit()
        return serialize_invoice(inv)

    q = db.query(Advance).filter(
        Advance.patient_id == inv.patient_id,
        Advance.balance_remaining > 0,
    )

    advance_ids = (payload.advance_ids if payload else None)
    if advance_ids:
        q = q.filter(Advance.id.in_(advance_ids))

    advances = q.order_by(Advance.created_at.asc()).all()
    if not advances:
        return serialize_invoice(inv)

    max_to_use = _d(payload.max_to_use
                    ) if payload and payload.max_to_use is not None else None
    if max_to_use is not None:
        remaining = min(remaining, max_to_use)

    for adv in advances:
        if remaining <= 0:
            break

        avail = _d(adv.balance_remaining)
        if avail <= 0:
            continue

        use = min(avail, remaining).quantize(Decimal("0.01"))

        # ✅ AdvanceAdjustment row (real accounting)
        # One row per (advance, invoice); if already exists, increase it
        adj = db.query(AdvanceAdjustment).filter(
            AdvanceAdjustment.advance_id == adv.id,
            AdvanceAdjustment.invoice_id == inv.id).first()

        if adj:
            adj.amount_applied = (_d(adj.amount_applied) + use).quantize(
                Decimal("0.01"))
            adj.applied_at = datetime.utcnow()
        else:
            db.add(
                AdvanceAdjustment(
                    advance_id=adv.id,
                    invoice_id=inv.id,
                    amount_applied=use,
                ))

        adv.balance_remaining = (avail - use).quantize(Decimal("0.01"))
        remaining -= use

    db.flush()
    recalc_totals(inv, db)
    inv.updated_by = user.id
    db.commit()
    db.refresh(inv)
    return serialize_invoice(inv)


# ---------------------------------------------------------------------------
# Single-page Billing Workbench (for your NEW UI)
# ---------------------------------------------------------------------------
@router.get("/workbench", response_model=dict)
def billing_workbench(
        patient_uhid: Optional[str] = None,
        patient_id: Optional[int] = None,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    ✅ Single API for single-page UI:
      - patient basic
      - latest invoices
      - advances
      - billing masters
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = None
    if patient_uhid:
        patient = db.query(Patient).filter(
            Patient.uhid == patient_uhid).first()
    elif patient_id:
        patient = db.query(Patient).get(patient_id)

    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    invs = db.query(Invoice).filter(Invoice.patient_id == patient.id).order_by(
        Invoice.id.desc()).limit(50).all()
    invoices = []
    for inv in invs:
        inv = db.query(Invoice).options(
            joinedload(Invoice.items),
            joinedload(Invoice.payments),
            joinedload(Invoice.advance_adjustments),
        ).get(inv.id)
        recalc_totals(inv, db)
        invoices.append(serialize_invoice(inv).model_dump())

    advances = list_advances(patient_id=patient.id, db=db, user=user)

    # Masters (same logic you already have)
    from app.models.user import User as UserModel
    doctors_q = db.query(UserModel).filter(UserModel.is_active.is_(True))
    if hasattr(UserModel, "is_doctor"):
        doctors_q = doctors_q.filter(UserModel.is_doctor.is_(True))
    doctors = [{
        "id": d.id,
        "name": d.name,
        "email": d.email
    } for d in doctors_q.order_by(UserModel.name.asc()).all()]

    providers = [{
        "id": p.id,
        "name": p.name,
        "code": p.code,
        "provider_type": p.provider_type
    } for p in db.query(BillingProvider).filter(
        BillingProvider.is_active.is_(True)).order_by(
            BillingProvider.name.asc()).all()]

    payers = [{
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "payer_type": p.payer_type
    } for p in db.query(Payer).order_by(Payer.name.asc()).all()]
    tpas = [{
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "payer_id": t.payer_id
    } for t in db.query(Tpa).order_by(Tpa.name.asc()).all()]
    credit_plans = [{
        "id": c.id,
        "code": c.code,
        "name": c.name,
        "payer_id": c.payer_id,
        "tpa_id": c.tpa_id
    } for c in db.query(CreditPlan).order_by(CreditPlan.name.asc()).all()]
    packages = [{
        "id": pkg.id,
        "name": pkg.name,
        "charges": float(pkg.charges or 0)
    } for pkg in db.query(IpdPackage).order_by(IpdPackage.name.asc()).all()]

    db.commit()

    return {
        "patient": {
            "id": patient.id,
            "uhid": getattr(patient, "uhid", None),
            "name":
            f"{patient.first_name or ''} {patient.last_name or ''}".strip(),
            "phone": patient.phone,
        },
        "invoices": invoices,
        "advances": advances,
        "masters": {
            "doctors": doctors,
            "credit_providers": providers,
            "payers": payers,
            "tpas": tpas,
            "credit_plans": credit_plans,
            "packages": packages,
        }
    }


from datetime import datetime
from uuid import uuid4


# ---------------------------------------------------------------------------
# Patient Billing Summary (JSON API for FE)
# ---------------------------------------------------------------------------
@router.get("/patients/{patient_id}/summary", response_model=dict)
def patient_billing_summary(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    JSON summary of patient's complete billing history, including:
    - All invoices
    - Totals
    - AR ageing buckets (0–30 / 31–60 / 61–90 / >90)
    - Revenue by billing_type (OP/IP/Lab/Pharmacy/etc.)
    - Payment mode breakup (cash/card/upi/etc.)
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    invs: List[Invoice] = (db.query(Invoice).filter(
        Invoice.patient_id == patient_id).order_by(
            Invoice.created_at.asc()).all())

    invoices_out: List[Dict[str, Any]] = []
    total_net = total_paid = total_balance = 0.0
    today = datetime.utcnow().date()

    aging = {
        "bucket_0_30": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_31_60": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_61_90": {
            "count": 0,
            "amount": 0.0
        },
        "bucket_90_plus": {
            "count": 0,
            "amount": 0.0
        },
    }

    by_type: Dict[str, Dict[str, float]] = {}

    for inv in invs:
        created = inv.created_at.date() if inv.created_at else today
        net = float(inv.net_total or 0)
        paid = float(inv.amount_paid or 0)
        bal = float(inv.balance_due or 0)

        total_net += net
        total_paid += paid
        total_balance += bal

        billing_type = getattr(inv, "billing_type", None) or "general"
        if billing_type not in by_type:
            by_type[billing_type] = {
                "net_total": 0.0,
                "amount_paid": 0.0,
                "balance_due": 0.0,
            }
        by_type[billing_type]["net_total"] += net
        by_type[billing_type]["amount_paid"] += paid
        by_type[billing_type]["balance_due"] += bal

        if bal > 0:
            days = (today - created).days
            if days <= 30:
                bucket_key = "bucket_0_30"
            elif days <= 60:
                bucket_key = "bucket_31_60"
            elif days <= 90:
                bucket_key = "bucket_61_90"
            else:
                bucket_key = "bucket_90_plus"

            aging[bucket_key]["count"] += 1
            aging[bucket_key]["amount"] += bal

        invoices_out.append({
            "id":
            inv.id,
            "invoice_number":
            getattr(inv, "invoice_number", inv.id),
            "billing_type":
            billing_type,
            "context_type":
            inv.context_type,
            "context_id":
            inv.context_id,
            "status":
            inv.status,
            "net_total":
            net,
            "amount_paid":
            paid,
            "balance_due":
            bal,
            "created_at":
            inv.created_at.isoformat() if inv.created_at else None,
            "finalized_at":
            inv.finalized_at.isoformat() if inv.finalized_at else None,
        })

    pay_rows = (db.query(Payment.mode, func.sum(Payment.amount)).join(
        Invoice, Invoice.id == Payment.invoice_id).filter(
            Invoice.patient_id == patient_id).group_by(Payment.mode).all())
    payment_modes = {mode: float(amount or 0) for mode, amount in pay_rows}

    return {
        "patient": {
            "id": patient.id,
            "uhid": getattr(patient, "uhid", None),
            "name":
            f"{patient.first_name or ''} {patient.last_name or ''}".strip(),
            "phone": patient.phone,
        },
        "invoices": invoices_out,
        "totals": {
            "net_total": total_net,
            "amount_paid": total_paid,
            "balance_due": total_balance,
        },
        "by_billing_type": by_type,
        "ar_aging": aging,
        "payment_modes": payment_modes,
    }


# Alias for FE path: /billing/patient/{id}/summary
@router.get("/patient/{patient_id}/summary", response_model=dict)
def patient_billing_summary_alias(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    return patient_billing_summary(patient_id=patient_id, db=db, user=user)


# UHID-based summary (no raw numeric ID needed in FE)
@router.get("/patients/by-uhid/{uhid}/summary", response_model=dict)
def patient_billing_summary_by_uhid(
        uhid: str,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Same as /billing/patients/{id}/summary but resolved via UHID.
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).filter(Patient.uhid == uhid).first()
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient_billing_summary(patient_id=patient.id, db=db, user=user)


# ---------------------------------------------------------------------------
# Printing: Single Invoice & Patient Billing Summary (PDF/HTML)
# ---------------------------------------------------------------------------
def _h(x: Any) -> str:
    s = "" if x is None else str(x)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _logo_data_uri(branding) -> Optional[str]:
    rel = (getattr(branding, "logo_path", None) or "").strip()
    if not rel:
        return None
    abs_path = Path(settings.STORAGE_DIR).joinpath(rel)
    if not abs_path.exists() or not abs_path.is_file():
        return None

    mime, _ = mimetypes.guess_type(str(abs_path))
    mime = mime or "image/png"
    b64 = base64.b64encode(abs_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _billing_title(bt: str | None) -> str:
    b = (bt or "").strip().lower()
    return {
        "op": "OP Bill Receipt",
        "opd": "OP Bill Receipt",
        "lab": "Lab Bill Receipt",
        "lis": "Lab Bill Receipt",
        "radiology": "Radiology Bill Receipt",
        "ris": "Radiology Bill Receipt",
        "pharmacy": "Pharmacy Bill Receipt",
        "ip_billing": "IP Bill Receipt",
        "ip": "IP Bill Receipt",
        "ot": "OT Bill Receipt",
        "general": "Bill Receipt",
        "": "Bill Receipt",
    }.get(b, "Bill Receipt")


def _qty_str(x: Any) -> str:
    try:
        d = _d(x)
        if d == d.to_integral():
            return str(int(d))
        s = format(d.normalize(), "f")
        return s.rstrip("0").rstrip(".")
    except Exception:
        return str(x or "")


def _sex_short(v: Any) -> str:
    s = ("" if v is None else str(v)).strip().lower()
    return {
        "male": "M",
        "m": "M",
        "female": "F",
        "f": "F",
        "other": "O",
        "others": "O",
        "transgender": "TG",
        "tg": "TG",
    }.get(s, (str(v).strip().title() if v else "—"))


def _calc_age_years(dob: date | None, asof: date | None = None) -> int | None:
    if not dob:
        return None
    asof = asof or date.today()
    try:
        years = asof.year - dob.year - (
            (asof.month, asof.day) < (dob.month, dob.day))
        return max(0, int(years))
    except Exception:
        return None


# -------------------------------
# Helpers (ADD THESE)
# -------------------------------
def _gender_short(g: str | None) -> str:
    s = (g or "").strip().lower()
    if not s:
        return "—"
    if s.startswith("m"):
        return "M"
    if s.startswith("f"):
        return "F"
    if s.startswith("o"):
        return "O"
    return (g or "").strip()[:1].upper() or "—"


def _age_years_from_dob(dob: date | None, *, today: date | None = None) -> str:
    if not dob:
        return "—"
    t = today or datetime.utcnow().date()
    try:
        years = t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))
        if years < 0:
            return "—"
        return f"{years} Y"
    except Exception:
        return "—"


@router.get("/invoices/{invoice_id}/print")
def print_invoice(
        invoice_id: int,
        paper: str
    | None = Query(
        default=None,
        description=
        "Optional: 'half' to force receipt half-page, 'full' to force A4. Default: auto.",
    ),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    inv: Invoice | None = (db.query(Invoice).options(
        joinedload(Invoice.items),
        joinedload(Invoice.payments),
        joinedload(Invoice.advance_adjustments),
        joinedload(Invoice.patient),
    ).get(invoice_id))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    patient: Patient | None = getattr(
        inv, "patient", None) or db.query(Patient).get(inv.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    # active items only
    items = [
        it for it in (inv.items or []) if not getattr(it, "is_voided", False)
    ]
    payments = list(inv.payments or [])

    created_at = inv.created_at or datetime.utcnow()
    bill_date = created_at.strftime("%d %b %Y")  # ✅ date only
    printed_at = datetime.utcnow().strftime("%d/%m/%Y %I:%M %p")

    branding = get_or_create_default_ui_branding(db, updated_by_id=user.id)
    receipt_title = _billing_title(getattr(inv, "billing_type", None))

    # decide layout (<=10 = half receipt)
    force = (paper or "").strip().lower()
    if force == "half":
        use_half = True
    elif force == "full":
        use_half = False
    else:
        use_half = (len(items) <= 12)  # ✅ exactly what you asked

    # common patient fields
    patient_name = " ".join([
        patient.prefix or "", patient.first_name or "", patient.last_name or ""
    ]).strip() or "—"

    age_text = _age_years_from_dob(getattr(patient, "dob", None))
    gender_text = _gender_short(getattr(patient, "gender", None))
    marital = (getattr(patient, "marital_status", None) or "").strip() or "—"
    phone = (getattr(patient, "phone", None) or "").strip() or "—"
    uhid = (getattr(patient, "uhid", None) or "").strip() or "—"
    inv_no = _h(getattr(inv, "invoice_number", None) or str(inv.id))

    # payment text
    modes = []
    for p in payments:
        m = (getattr(p, "mode", None) or "").strip()
        if m and m not in modes:
            modes.append(m)
    payment_text = " / ".join(modes) if modes else "—"

    # ============================================================
    # HALF RECEIPT (A5 landscape)  ✅ stable single page <= 10 items
    # ============================================================
    if use_half:
        n = len(items)

        # ✅ dynamic density so it never collapses / splits
        if n <= 6:
            base_font = 10.4
            row_pad = 3
        elif n <= 8:
            base_font = 9.7
            row_pad = 2
        else:  # 9–10
            base_font = 9.1
            row_pad = 2

        brand_header = render_brand_header_html(branding)

        # item rows (NO S.No – matches your sample)
        rows = ""
        for it in items:
            desc = _h(getattr(it, "description", "") or "")
            qty = _qty_str(getattr(it, "quantity", 0))
            amt = _money(getattr(it, "unit_price", 0))
            tot = _money(getattr(it, "line_total", 0))
            rows += f"""
              <tr>
                <td class="svc">{desc}</td>
                <td class="num col-qty">{qty}</td>
                <td class="num col-amt">{amt}</td>
                <td class="num col-tot">{tot}</td>
              </tr>
            """

        if not rows:
            rows = "<tr><td colspan='4' class='empty'>No items</td></tr>"

        total_service = _money(getattr(inv, "gross_total", 0))
        net_amount = _money(getattr(inv, "net_total", 0))
        paid_amount = _money(getattr(inv, "amount_paid", 0))
        balance_amount = _money(getattr(inv, "balance_due", 0))

        html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Receipt {inv_no}</title>
  <style>
    @page {{
      size: A5 landscape;
      margin: 6mm 8mm 6mm 8mm;
    }}

    body {{
      font-family: "SF Pro Text","SF Pro Display",-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;
      font-size: {base_font}px;
      color: #0f172a;
      margin: 0;
      background: #ffffff;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}

    /* top line */
    .topline {{
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      font-size: {base_font - 1.0}px;
      margin-bottom: 4px;
    }}
    .topline .center {{
      justify-self: center;
      font-weight: 900;
      letter-spacing: .6px;
      text-transform: uppercase;
    }}
    .topline .right {{
      justify-self: end;
      font-weight: 900;
      letter-spacing: .8px;
    }}

    /* ✅ use your branding header but "receipt-tuned" */
    {brand_header_css()}

    .brand-header {{
      --logo-col: 42mm;
      --logo-w: 38mm;
      --logo-h: 18mm;
      padding-bottom: 6px;
      margin-bottom: 6px;
      border-bottom: 1px solid #e5e7eb;
      break-inside: avoid;
    }}
    .brand-right {{
      text-align: right;     /* ✅ center like your screenshot */
      padding-left: 0;
    }}
    .brand-box {{
      text-align: right;
      max-width: 128mm;
      margin: 0 auto;
    }}
    .brand-name {{
      font-size: {base_font + 3.4}px;
      font-weight: 900;
      letter-spacing: .3px;
      text-transform: uppercase;
      color: #027F8B;
    }}
    .brand-tagline {{
      font-size: {base_font - 1.2}px;
      color: #000;
      font-weight: 600;
      
    }}
    .brand-meta {{
      font-size: {base_font - 1.3}px;
    }}

    /* patient meta card */
    .meta-card {{
      border: 1px solid #e5e7eb;
      padding: 6px 8px;
      margin-top: 4px;
      break-inside: avoid;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10mm;
    }}
    .kv .row {{
      display: grid;
      grid-template-columns: 98px 10px 1fr;
      align-items: baseline;
      margin: 2px 0;
    }}
    .k {{
      color: #334155;
      font-weight: 700;
      white-space: nowrap;
    }}
    .v {{
      font-weight: 900;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    /* items table (✅ no border radius) */
    .items {{
      margin-top: 6px;
      break-inside: avoid;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      border-top: 1px solid #cbd5e1;
      border-bottom: 1px solid #cbd5e1;
    }}
    thead th {{
      text-align: left;
      font-weight: 900;
      font-size: {base_font - 1.2}px;
      color: #334155;
      padding: {row_pad}px 4px;
      border-bottom: 1px solid #cbd5e1;
      text-transform: uppercase;
      letter-spacing: .6px;
    }}
    tbody td {{
      padding: {row_pad}px 4px;
      border-bottom: 1px solid #eef2f7;
      vertical-align: top;
    }}
    tbody tr:last-child td {{
      border-bottom: 0;
    }}

    .svc {{
      width: 100%;
      line-height: 1.15;
    }}
    .num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .col-qty {{ width: 12mm; }}
    .col-amt {{ width: 20mm; }}
    .col-tot {{ width: 22mm; }}

    .empty {{
      text-align: center;
      padding: 8px 0;
      color: #475569;
    }}

    /* footer + totals (✅ never split across pages) */
    .footer {{
      display: grid;
      grid-template-columns: 1fr 70mm;
      gap: 10mm;
      margin-top: 8px;
      break-inside: avoid;
      page-break-inside: avoid;
    }}
    .code {{
      font-weight: 900;
      letter-spacing: .8px;
    }}

    .totals {{
      width: 70mm;
      margin-left: auto;
      border-top: 1px solid #cbd5e1;
      border-bottom: 1px solid #cbd5e1;
      break-inside: avoid;
      page-break-inside: avoid;
    }}
    .trow {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 4px 0;
      border-bottom: 1px solid #eef2f7;
      font-variant-numeric: tabular-nums;
    }}
    .trow:last-child {{
      border-bottom: 0;
    }}
    .trow strong {{
      font-weight: 950;
    }}
  </style>
</head>

<body>
  <div class="topline">
    <div class="center">*{inv_no}*</div>
    <div class="right">{_h(receipt_title)}</div>
    
  </div>

  {brand_header}

  <div class="meta-card">
    <div class="meta-grid">
      <div class="kv">
        <div class="row"><div class="k">Patient Name</div><div>:</div><div class="v">{_h(patient_name)}</div></div>
        <div class="row"><div class="k">Age / Gender</div><div>:</div><div class="v">{_h(age_text)} / {_h(gender_text)}</div></div>
        <div class="row"><div class="k">Marital Status</div><div>:</div><div class="v">{_h(marital)}</div></div>
        <div class="row"><div class="k">Mobile No</div><div>:</div><div class="v">{_h(phone)}</div></div>
      </div>

      <div class="kv">
        <div class="row"><div class="k">Reg No (UHID)</div><div>:</div><div class="v">{_h(uhid)}</div></div>
        <div class="row"><div class="k">Bill No</div><div>:</div><div class="v">{inv_no}</div></div>
        <div class="row"><div class="k">Bill Date</div><div>:</div><div class="v">{_h(bill_date)}</div></div>
        <div class="row"><div class="k">Payment</div><div>:</div><div class="v">{_h(payment_text)}</div></div>
      </div>
    </div>
  </div>

  <div class="items">
    <table>
      <thead>
        <tr>
          <th>Service Name</th>
          <th class="num col-qty">Qty</th>
          <th class="num col-amt">Amt</th>
          <th class="num col-tot">Total</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    <div>
      <div class="code">*{_h(uhid)}*</div>
    </div>

    <div class="totals">
      <div class="trow"><span><strong>Total Service Amount</strong></span><span><strong>{_h(total_service)}</strong></span></div>
      <div class="trow"><span>Net Amount</span><span>{_h(net_amount)}</span></div>
      <div class="trow"><span>Paid Amount</span><span>{_h(paid_amount)}</span></div>
      <div class="trow"><span>Balance Amount</span><span>{_h(balance_amount)}</span></div>
    </div>
  </div>

</body>
</html>
        """.strip()

        try:
            from weasyprint import HTML as _HTML  # type: ignore
            pdf_bytes = _HTML(string=html,
                              base_url=str(settings.STORAGE_DIR)).write_pdf()
            filename = f"invoice-{invoice_id}-half.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"'
                },
            )
        except Exception as e:
            logger.exception("WeasyPrint failed (half receipt) invoice %s: %s",
                             invoice_id, e)
            return StreamingResponse(io.BytesIO(html.encode("utf-8")),
                                     media_type="text/html; charset=utf-8")

    # ============================================================
    # FULL A4 (items > 10)  ✅ no table radius + better patient block
    # ============================================================
    brand_header = render_brand_header_html(branding)

    html_items = ""
    for idx, it in enumerate(items, start=1):
        html_items += ("<tr>"
                       f"<td>{idx}</td>"
                       f"<td>{_h(it.description or '')}</td>"
                       f"<td class='money'>{_qty_str(it.quantity)}</td>"
                       f"<td class='money'>{_money(it.unit_price)}</td>"
                       f"<td class='money'>{_money(it.tax_rate)}%</td>"
                       f"<td class='money'>{_money(it.tax_amount)}</td>"
                       f"<td class='money'>{_money(it.line_total)}</td>"
                       "</tr>")
    if not html_items:
        html_items = "<tr><td colspan='7' style='text-align:left;'>No items</td></tr>"

    html_pay = ""
    for idx, pay in enumerate(payments, start=1):
        dt = (pay.paid_at.strftime("%d-%m-%Y %H:%M") if getattr(
            pay, "paid_at", None) else "—")
        html_pay += ("<tr>"
                     f"<td>{idx}</td>"
                     f"<td>{_h(pay.mode)}</td>"
                     f"<td>{_h(pay.reference_no or '')}</td>"
                     f"<td>{_h(dt)}</td>"
                     f"<td class='money'>{_money(pay.amount)}</td>"
                     "</tr>")
    if not html_pay:
        html_pay = "<tr><td colspan='5' style='text-align:center;'>No payments</td></tr>"

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Invoice {inv_no}</title>
  <style>
    @page {{
      size: A4;
      margin: 2mm 2mm 2mm 2mm;
    }}

    body {{
      font-family: "SF Pro Text","SF Pro Display",-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;
      font-size: 12px;
      margin: 0;
      color: #0f172a;
      background: #ffffff;
    }}

    .page {{ padding: 16px; }}

    {brand_header_css()}
    
    /* ✅ Horizontal totals boxes */
.totals-strip{{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin: 10px 0 6px;
}}

.tbox{{
  flex: 1 1 180px;            /* auto wrap if needed */
  border: 1px solid #e5e7eb;
  border-radius: 0;            /* ✅ no radius */
  padding: 10px 12px;
  background: #ffffff;
}}

.tbox .k{{
  font-size: 10px;
  font-weight: 900;
  letter-spacing: .5px;
  color: #64748b;
  text-transform: uppercase;
}}

.tbox .v{{
  margin-top: 6px;
  font-size: 16px;
  font-weight: 950;
  color: #0f172a;
  text-align: right;
  font-variant-numeric: tabular-nums;
}}

.tbox.emph{{
  border-color: #0f172a;
}}
    
    

    .card {{
      border: 1px solid #e5e7eb;
      border-radius: 18px;
      padding: 12px;
      background: #ffffff;
    }}

    .header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .title {{
      font-size: 18px;
      font-weight: 900;
      letter-spacing: -0.2px;
      margin-bottom: 4px;
    }}

    .muted {{
      color: #64748b;
      font-size: 11px;
      line-height: 1.35;
    }}

    .section {{ margin-top: 12px; margin-bottom: 8px; }}

    .section-title {{
      font-weight: 800;
      font-size: 13px;
      margin-bottom: 6px;
    }}

    /* ✅ NO table border radius */
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 6px;
      border: 1px solid #e5e7eb;
      border-radius: 0;
    }}

    th, td {{
      padding: 7px 8px;
      text-align: left;
      border-bottom: 1px solid #eef2f7;
      vertical-align: top;
    }}

    th {{
      background: #f8fafc;
      font-weight: 800;
      font-size: 11px;
      color: #334155;
      text-transform: uppercase;
      letter-spacing: .5px;
    }}
    
    tr:last-child td {{ border-bottom: 0; }}

    td.money {{
      text-align: left;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}

    .totals {{
      margin-top: 10px;
      width: 100%;
      max-width: 340px;
      margin-left: auto;
      border: 1px solid #e5e7eb;
      border-radius: 0;   /* ✅ */
    }}

    .totals-row {{
      display: flex;
      justify-content: space-between;
      padding: 8px 10px;
      font-size: 11px;
      border-bottom: 1px solid #eef2f7;
      font-variant-numeric: tabular-nums;
    }}

    .totals-row:last-child {{ border-bottom: 0; }}

    .totals-row.label {{
      
      color: #000;
      font-weight: 900;
    }}
    .container{{
         display: flex;
         justify-content: center;
         align-items: center
        }}
        
        
  </style>
</head>
<body>
  <div class="page">
    {brand_header}

    <div class="card">
      <div class="header">
        <div>
          <div class="title">Tax Invoice</div>
          <div class="muted">Invoice No: {inv_no}</div>
          <div class="muted">Date: {_h(bill_date)}</div>
        </div>

        <div style="text-align:right;">
          <div class="section-title" style="margin:0 0 6px 0;">Patient</div>
          <div style="font-weight:900;">{_h(patient_name)}</div>
          <div class="muted">Reg No (UHID): {_h(uhid)}</div>
          
          <div class="muted">Age / Gender: {_h(age_text)} / {_h(gender_text)}</div>
          <div class="muted">Marital Status: {_h(marital)}</div>
          <div class="muted">Phone: {_h(phone)}</div>
        </div>
      </div>
   <div class="totals-strip">
  <div class="tbox">
    <div class="k">Gross Amount</div>
    <div class="v">{_money(inv.gross_total)}</div>
  </div>

  <div class="tbox emph">
    <div class="k">Net Amount</div>
    <div class="v">{_money(inv.net_total)}</div>
  </div>

  <div class="tbox">
    <div class="k">Amount Received</div>
    <div class="v">{_money(inv.amount_paid)}</div>
  </div>

  <div class="tbox">
    <div class="k">Balance Amount</div>
    <div class="v">{_money(inv.balance_due)}</div>
  </div>
</div>
      <div class="section">
        <div class="section-title">Bill Details</div>
        <table>
          <thead>
            <tr>
              <th style="width:40px;">S.No</th>
              <th>SERVICE NAME</th>
              <th style="width:60px;">Qty</th>
              <th style="width:80px;">Price</th>
              <th style="width:60px;">GST%</th>
              <th style="width:80px;">GST Amt</th>
              <th style="width:95px;">Line Total</th>
            </tr>
          </thead>
          <tbody>
            {html_items}
          </tbody>
        </table>

       
      </div>

  </div>
</body>
</html>
    """.strip()

    try:
        from weasyprint import HTML as _HTML  # type: ignore
        pdf_bytes = _HTML(string=html,
                          base_url=str(settings.STORAGE_DIR)).write_pdf()
        filename = f"invoice-{invoice_id}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as e:
        logger.exception("WeasyPrint failed (A4) invoice %s: %s", invoice_id,
                         e)

    return StreamingResponse(io.BytesIO(html.encode("utf-8")),
                             media_type="text/html; charset=utf-8")


@router.get("/patients/{patient_id}/print-summary")
def print_patient_billing_summary(
        patient_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    Printable patient billing history with AR ageing + revenue breakdown.
    """
    if not has_perm(user, "billing.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    patient = db.query(Patient).get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    summary = patient_billing_summary(patient_id, db=db, user=user)

    invoices = summary["invoices"]
    totals = summary["totals"]
    by_type = summary["by_billing_type"]
    aging = summary["ar_aging"]
    payment_modes = summary["payment_modes"]

    # --- Branding header (logo + org details) ---
    branding = get_or_create_default_ui_branding(db, updated_by_id=user.id)
    brand_header = render_brand_header_html(branding)

    rows_html = ""
    for idx, inv in enumerate(invoices, start=1):
        rows_html += ("<tr>"
                      f"<td>{idx}</td>"
                      f"<td>{inv['id']}</td>"
                      f"<td>{inv['invoice_number']}</td>"
                      f"<td>{inv['billing_type']}</td>"
                      f"<td>{inv['context_type'] or ''}</td>"
                      f"<td>{(inv['created_at'] or '')[:10]}</td>"
                      f"<td class='money'>{_money(inv['net_total'])}</td>"
                      f"<td class='money'>{_money(inv['amount_paid'])}</td>"
                      f"<td class='money'>{_money(inv['balance_due'])}</td>"
                      f"<td>{inv['status']}</td>"
                      "</tr>")
    if not rows_html:
        rows_html = "<tr><td colspan='10' style='text-align:center;'>No invoices</td></tr>"

    type_rows = ""
    for btype, agg in by_type.items():
        type_rows += ("<tr>"
                      f"<td>{btype}</td>"
                      f"<td class='money'>{_money(agg['net_total'])}</td>"
                      f"<td class='money'>{_money(agg['amount_paid'])}</td>"
                      f"<td class='money'>{_money(agg['balance_due'])}</td>"
                      "</tr>")
    if not type_rows:
        type_rows = "<tr><td colspan='4' style='text-align:center;'>No data</td></tr>"

    aging_rows = ""
    labels = {
        "bucket_0_30": "0–30 days",
        "bucket_31_60": "31–60 days",
        "bucket_61_90": "61–90 days",
        "bucket_90_plus": "> 90 days",
    }
    for key, label in labels.items():
        row = aging.get(key) or {"count": 0, "amount": 0}
        aging_rows += ("<tr>"
                       f"<td>{label}</td>"
                       f"<td class='money'>{row['count']}</td>"
                       f"<td class='money'>{_money(row['amount'])}</td>"
                       "</tr>")

    pay_rows_html = ""
    for mode, amt in payment_modes.items():
        pay_rows_html += ("<tr>"
                          f"<td>{mode}</td>"
                          f"<td class='money'>{_money(amt)}</td>"
                          "</tr>")
    if not pay_rows_html:
        pay_rows_html = "<tr><td colspan='2' style='text-align:center;'>No payments</td></tr>"

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Billing Summary - {summary['patient']['uhid'] or summary['patient']['id']}</title>
  <style>
    @page {{
      size: A4;
      margin: 14mm 14mm 14mm 14mm;
    }}

    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      margin: 0;
      color: #0f172a;
      background: #ffffff;
    }}

    .page {{ padding: 16px; }}

    {brand_header_css()}

    .card {{
      border: 1px solid #e5e7eb;
      border-radius: 18px;
      padding: 12px;
      background: #ffffff;
    }}

    .title {{
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.2px;
      margin-bottom: 4px;
    }}

    .muted {{
      color: #64748b;
      font-size: 11px;
    }}

    .section {{ margin-top: 12px; margin-bottom: 8px; }}

    .section-title {{
      font-weight: 700;
      font-size: 13px;
      margin-bottom: 6px;
    }}

    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      margin-top: 6px;
      border: 1px solid #e5e7eb;
      border-radius: 14px;
      overflow: hidden;
    }}

    th, td {{
      padding: 7px 8px;
      text-align: left;
      border-bottom: 1px solid #eef2f7;
      vertical-align: top;
    }}

    th {{
      background: #f8fafc;
      font-weight: 700;
      font-size: 11px;
      color: #334155;
    }}

    tr:last-child td {{ border-bottom: 0; }}

    td.money {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}

    .pills {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 6px;
    }}

    .pill {{
      padding: 6px 10px;
      border-radius: 999px;
      background: #f1f5f9;
      border: 1px solid #e5e7eb;
      font-size: 11px;
      color: #0f172a;
    }}

    .pill strong {{
      font-weight: 800;
    }}
  </style>
</head>
<body>
  <div class="page">
    {brand_header}

    <div class="card">
      <div class="title">Patient Billing Summary</div>
      <div class="muted">
        UHID: {summary['patient']['uhid'] or '—'} &nbsp;|&nbsp;
        Name: {summary['patient']['name']} &nbsp;|&nbsp;
        Phone: {summary['patient']['phone'] or '—'}
      </div>

      <div class="section">
        <div class="section-title">Overall Totals</div>
        <div class="pills">
          <span class="pill">Net Total: <strong>{_money(totals['net_total'])}</strong></span>
          <span class="pill">Amount Received: <strong>{_money(totals['amount_paid'])}</strong></span>
          <span class="pill">Balance Due: <strong>{_money(totals['balance_due'])}</strong></span>
        </div>
      </div>

      <div class="section">
        <div class="section-title">Invoice-wise Details</div>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Inv ID</th>
              <th>Invoice No</th>
              <th>Billing Type</th>
              <th>Context</th>
              <th>Date</th>
              <th>Net</th>
              <th>Paid</th>
              <th>Balance</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </div>

      <div class="section">
        <div class="section-title">Revenue by Billing Type</div>
        <table>
          <thead>
            <tr>
              <th>Billing Type</th>
              <th>Net Total</th>
              <th>Amount Received</th>
              <th>Balance</th>
            </tr>
          </thead>
          <tbody>
            {type_rows}
          </tbody>
        </table>
      </div>

      <div class="section">
        <div class="section-title">Accounts Receivable Ageing</div>
        <table>
          <thead>
            <tr>
              <th>Bucket</th>
              <th>Invoice Count</th>
              <th>Outstanding Amount</th>
            </tr>
          </thead>
          <tbody>
            {aging_rows}
          </tbody>
        </table>
      </div>

      <div class="section">
        <div class="section-title">Payment Mode Breakup</div>
        <table>
          <thead>
            <tr>
              <th>Mode</th>
              <th>Total Amount</th>
            </tr>
          </thead>
          <tbody>
            {pay_rows_html}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>
    """.strip()

    try:
        from weasyprint import HTML as _HTML  # type: ignore
        HTML = _HTML
    except Exception:
        HTML = None

    if HTML is not None:
        try:
            pdf_bytes = HTML(string=html,
                             base_url=str(settings.STORAGE_DIR)).write_pdf()
            filename = f"billing-summary-{patient_id}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"'
                },
            )
        except Exception as e:
            logger.exception(
                "WeasyPrint PDF generation failed for billing summary %s, falling back to HTML: %s",
                patient_id, e)

    return StreamingResponse(
        io.BytesIO(html.encode("utf-8")),
        media_type="text/html; charset=utf-8",
    )
