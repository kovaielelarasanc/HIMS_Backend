from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.billing import Invoice, InvoiceItem, Payment
from app.models.opd import LabTest, RadiologyTest  # price fallbacks
from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder
from app.models.ot import OtOrder
from app.models.ot_master import OtSurgeryMaster
from app.schemas.billing import (InvoiceCreate, AddServiceIn, ManualItemIn,
                                 UpdateItemIn, VoidItemIn, PaymentIn,
                                 BulkAddFromUnbilledIn, InvoiceOut,
                                 InvoiceItemOut, PaymentOut)
from app.services.billing_auto import auto_add_item_for_event
from decimal import Decimal
# Optional pharmacy
try:
    from app.models.pharmacy import PharmacySale
    HAS_PHARMACY = True
except Exception:
    HAS_PHARMACY = False

router = APIRouter(prefix="/billing", tags=["Billing"])


def _recompute(inv: Invoice):
    gross = tax = 0.0
    for it in inv.items:
        if it.is_voided: continue
        gross += float(it.unit_price) * int(it.quantity or 1)
        tax += float(it.tax_amount or 0)
    inv.gross_total = gross
    inv.tax_total = tax
    inv.net_total = gross + tax
    inv.balance_due = float(inv.net_total) - float(inv.amount_paid or 0)


def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False): return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes: return
    raise HTTPException(403, "Not permitted")


# ---------------- Invoices ----------------
@router.post("/invoices")
def create_invoice(payload: InvoiceCreate,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    _need_any(user, ["billing.create"])
    inv = Invoice(patient_id=payload.patient_id,
                  context_type=payload.context_type,
                  context_id=payload.context_id,
                  status="draft",
                  created_by=user.id)
    db.add(inv)
    db.commit()
    return {"id": inv.id, "message": "Invoice created"}


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    _need_any(user, ["billing.view"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    _recompute(inv)
    db.commit()
    return InvoiceOut(
        id=inv.id,
        patient_id=inv.patient_id,
        context_type=inv.context_type,
        context_id=inv.context_id,
        status=inv.status,
        gross_total=float(inv.gross_total or 0),
        tax_total=float(inv.tax_total or 0),
        net_total=float(inv.net_total or 0),
        amount_paid=float(inv.amount_paid or 0),
        balance_due=float(inv.balance_due or 0),
        items=[InvoiceItemOut.model_validate(it) for it in inv.items],
        payments=[
            PaymentOut(id=p.id,
                       amount=float(p.amount or 0),
                       mode=p.mode,
                       reference_no=p.reference_no,
                       paid_at=p.paid_at.isoformat() if p.paid_at else None)
            for p in inv.payments
        ])


@router.get("/invoices")
def list_invoices(patient_id: Optional[int] = Query(None),
                  status: Optional[str] = Query(None),
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    _need_any(user, ["billing.view"])
    q = db.query(Invoice)
    if patient_id: q = q.filter(Invoice.patient_id == patient_id)
    if status: q = q.filter(Invoice.status == status)
    rows = q.order_by(Invoice.id.desc()).limit(200).all()
    out = []
    for inv in rows:
        _recompute(inv)
        out.append({
            "id": inv.id,
            "patient_id": inv.patient_id,
            "status": inv.status,
            "gross_total": float(inv.gross_total or 0),
            "tax_total": float(inv.tax_total or 0),
            "net_total": float(inv.net_total or 0),
            "amount_paid": float(inv.amount_paid or 0),
            "balance_due": float(inv.balance_due or 0),
            "context_type": inv.context_type,
            "context_id": inv.context_id
        })
    db.commit()
    return out


# ------------- Items -------------
@router.post("/invoices/{invoice_id}/items/add-service")
def add_service_item(invoice_id: int,
                     payload: AddServiceIn,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    _need_any(user, ["billing.items.add"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status != "draft": raise HTTPException(400, "Invoice not editable")

    # prevent duplicate
    dup = db.query(InvoiceItem).filter(
        InvoiceItem.service_type == payload.service_type,
        InvoiceItem.service_ref_id == payload.service_ref_id,
        InvoiceItem.is_voided.is_(False)).first()
    if dup: raise HTTPException(409, "Service already billed")

    desc = ""
    unit_price = 0.0
    if payload.service_type == "lab":
        it = db.query(LisOrderItem).get(payload.service_ref_id)
        if not it: raise HTTPException(404, "LIS item not found")
        if it.status not in {"validated", "reported"}:
            raise HTTPException(400, "LIS item not ready for billing")
        mt = db.query(LabTest).get(it.test_id)
        unit_price = float(getattr(mt, "price", 0) or 0)
        desc = f"Lab: {it.test_name} ({it.test_code})"

    elif payload.service_type == "radiology":
        ro = db.query(RisOrder).get(payload.service_ref_id)
        if not ro: raise HTTPException(404, "RIS order not found")
        if ro.status not in {"reported", "approved"}:
            raise HTTPException(400, "RIS order not ready for billing")
        mt = db.query(RadiologyTest).get(ro.test_id)
        unit_price = float(getattr(mt, "price", 0) or 0)
        desc = f"Radiology: {ro.test_name} ({ro.test_code})"

    elif payload.service_type == "ot":
        oc = db.query(OtOrder).get(payload.service_ref_id)
        if not oc: raise HTTPException(404, "OT order not found")
        if oc.status != "completed":
            raise HTTPException(400, "OT order not completed")
        unit_price = float(oc.estimated_cost or 0)
        if unit_price == 0 and oc.surgery_master_id:
            m = db.query(OtSurgeryMaster).get(oc.surgery_master_id)
            if m: unit_price = float(m.default_cost or 0)
        desc = f"OT: {oc.surgery_name}"

    elif payload.service_type == "pharmacy" and HAS_PHARMACY:
        sale = db.query(PharmacySale).get(payload.service_ref_id)
        if not sale: raise HTTPException(404, "Pharmacy sale not found")
        unit_price = float(sale.total_amount or 0)
        desc = f"Pharmacy sale #{sale.id}"

    else:
        raise HTTPException(400, "Unsupported service_type for auto add")

    qty = int(payload.quantity or 1)
    tax_rate = float(payload.tax_rate or 0)
    tax_amount = round(unit_price * qty * (tax_rate / 100.0), 2)
    line_total = round(unit_price * qty + tax_amount, 2)

    line = InvoiceItem(invoice_id=inv.id,
                       service_type=payload.service_type,
                       service_ref_id=payload.service_ref_id,
                       description=desc,
                       quantity=qty,
                       unit_price=unit_price,
                       tax_rate=tax_rate,
                       tax_amount=tax_amount,
                       line_total=line_total,
                       created_by=user.id)
    db.add(line)
    _recompute(inv)
    db.commit()
    return {"message": "Item added", "invoice_id": inv.id, "item_id": line.id}


@router.post("/invoices/{invoice_id}/items/manual")
def add_manual_item(invoice_id: int,
                    payload: ManualItemIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    _need_any(user, ["billing.items.add"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status != "draft": raise HTTPException(400, "Invoice not editable")
    qty = int(payload.quantity or 1)
    unit = float(payload.unit_price or 0)
    tax_rate = float(payload.tax_rate or 0)
    tax_amount = round(unit * qty * (tax_rate / 100.0), 2)
    line_total = round(unit * qty + tax_amount, 2)
    line = InvoiceItem(invoice_id=inv.id,
                       service_type=(payload.service_type or "manual"),
                       service_ref_id=int(payload.service_ref_id or 0),
                       description=payload.description.strip(),
                       quantity=qty,
                       unit_price=unit,
                       tax_rate=tax_rate,
                       tax_amount=tax_amount,
                       line_total=line_total,
                       created_by=user.id)
    db.add(line)
    _recompute(inv)
    db.commit()
    return {"message": "Manual item added", "item_id": line.id}


@router.patch("/invoices/{invoice_id}/items/{item_id}")
def update_item(invoice_id: int,
                item_id: int,
                payload: UpdateItemIn,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    _need_any(user, ["billing.items.update"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status != "draft": raise HTTPException(400, "Invoice not editable")
    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != inv.id:
        raise HTTPException(404, "Item not found")
    if payload.quantity is not None: it.quantity = int(payload.quantity)
    if payload.unit_price is not None:
        it.unit_price = float(payload.unit_price)
    if payload.tax_rate is not None: it.tax_rate = float(payload.tax_rate)
    it.tax_amount = round(
        float(it.unit_price) * int(it.quantity or 1) *
        (float(it.tax_rate) / 100.0), 2)
    it.line_total = round(
        float(it.unit_price) * int(it.quantity or 1) + float(it.tax_amount), 2)
    it.updated_by = user.id
    it.updated_at = datetime.utcnow()
    _recompute(inv)
    db.commit()
    return {"message": "Item updated"}


@router.post("/invoices/{invoice_id}/items/{item_id}/void")
def void_item(invoice_id: int,
              item_id: int,
              payload: VoidItemIn,
              db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    _need_any(user, ["billing.items.void"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status not in {"draft", "finalized"}:
        raise HTTPException(400, "Cannot void in current status")
    it = db.query(InvoiceItem).get(item_id)
    if not it or it.invoice_id != inv.id:
        raise HTTPException(404, "Item not found")
    it.is_voided = True
    it.updated_by = user.id
    it.updated_at = datetime.utcnow()
    _recompute(inv)
    db.commit()
    return {"message": "Item voided"}


# ------------- Bulk add from unbilled -------------
@router.get("/unbilled-services")
def unbilled_services(patient_id: int = Query(...),
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    _need_any(user, ["billing.view"])
    out = []

    # LAB
    lis_items = db.query(LisOrderItem).join(LisOrder, LisOrderItem.order_id==LisOrder.id)\
        .filter(LisOrder.patient_id==patient_id, LisOrderItem.status.in_(["validated","reported"])).all()
    for it in lis_items:
        exists = db.query(InvoiceItem).filter(
            InvoiceItem.service_type == "lab",
            InvoiceItem.service_ref_id == it.id,
            InvoiceItem.is_voided.is_(False)).first()
        if exists: continue
        mt = db.query(LabTest).get(it.test_id)
        price = float(getattr(mt, "price", 0) or 0)
        out.append({
            "uid": f"lab:{it.id}",
            "source": "lab",
            "source_id": it.id,
            "category": "Lab",
            "description": f"Lab: {it.test_name} ({it.test_code})",
            "qty": 1,
            "unit_price": price,
            "amount": price,
            "href": f"/lab/orders/{it.order_id}"
        })

    # RIS
    ris_orders = db.query(RisOrder).filter(
        RisOrder.patient_id == patient_id,
        RisOrder.status.in_(["reported", "approved"])).all()
    for ro in ris_orders:
        exists = db.query(InvoiceItem).filter(
            InvoiceItem.service_type == "radiology",
            InvoiceItem.service_ref_id == ro.id,
            InvoiceItem.is_voided.is_(False)).first()
        if exists: continue
        mt = db.query(RadiologyTest).get(ro.test_id)
        price = float(getattr(mt, "price", 0) or 0)
        out.append({
            "uid": f"radiology:{ro.id}",
            "source": "radiology",
            "source_id": ro.id,
            "category": "RIS",
            "description": f"Radiology: {ro.test_name} ({ro.test_code})",
            "qty": 1,
            "unit_price": price,
            "amount": price,
            "href": f"/ris/orders/{ro.id}"
        })

    # OT
    ot_orders = db.query(OtOrder).filter(OtOrder.patient_id == patient_id,
                                         OtOrder.status == "completed").all()
    for oc in ot_orders:
        exists = db.query(InvoiceItem).filter(
            InvoiceItem.service_type == "ot",
            InvoiceItem.service_ref_id == oc.id,
            InvoiceItem.is_voided.is_(False)).first()
        if exists: continue
        price = float(oc.estimated_cost or 0)
        if price == 0 and oc.surgery_master_id:
            m = db.query(OtSurgeryMaster).get(oc.surgery_master_id)
            if m: price = float(m.default_cost or 0)
        out.append({
            "uid": f"ot:{oc.id}",
            "source": "ot",
            "source_id": oc.id,
            "category": "OT",
            "description": f"OT: {oc.surgery_name}",
            "qty": 1,
            "unit_price": price,
            "amount": price,
            "href": f"/ot/orders/{oc.id}"
        })

    # Pharmacy (optional)
    if HAS_PHARMACY:
        sales = db.query(PharmacySale).filter(
            PharmacySale.patient_id == patient_id).order_by(
                PharmacySale.id.desc()).limit(250).all()
        for s in sales:
            exists = db.query(InvoiceItem).filter(
                InvoiceItem.service_type == "pharmacy",
                InvoiceItem.service_ref_id == s.id,
                InvoiceItem.is_voided.is_(False)).first()
            if exists: continue
            price = float(s.total_amount or 0)
            if price <= 0: continue
            out.append({
                "uid": f"pharmacy:{s.id}",
                "source": "pharmacy",
                "source_id": s.id,
                "category": "Pharmacy",
                "description": f"Pharmacy sale #{s.id}",
                "qty": 1,
                "unit_price": price,
                "amount": price,
                "href": f"/pharmacy/sales/{s.id}"
            })
    return out


@router.post("/invoices/{invoice_id}/items/bulk-from-unbilled")
def bulk_add_from_unbilled(invoice_id: int,
                           payload: BulkAddFromUnbilledIn,
                           patient_id: int = Query(...),
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    _need_any(user, ["billing.items.add"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status != "draft": raise HTTPException(400, "Invoice not editable")

    all_unbilled = unbilled_services(patient_id=patient_id, db=db,
                                     user=user)  # reuse logic
    pick = all_unbilled if not payload.uids else [
        x for x in all_unbilled if x["uid"] in set(payload.uids)
    ]
    added = 0
    for x in pick:
        stype, sid = x["uid"].split(":")[0], int(x["source_id"])
        dup = db.query(InvoiceItem).filter(
            InvoiceItem.service_type == stype,
            InvoiceItem.service_ref_id == sid,
            InvoiceItem.is_voided.is_(False)).first()
        if dup: continue
        qty = int(x.get("qty", 1))
        unit = float(x.get("unit_price", 0))
        tax_rate = 0.0
        tax_amount = round(unit * qty * (tax_rate / 100.0), 2)
        line_total = round(unit * qty + tax_amount, 2)
        db.add(
            InvoiceItem(invoice_id=inv.id,
                        service_type=stype,
                        service_ref_id=sid,
                        description=x["description"],
                        quantity=qty,
                        unit_price=unit,
                        tax_rate=tax_rate,
                        tax_amount=tax_amount,
                        line_total=line_total,
                        created_by=user.id))
        added += 1
    _recompute(inv)
    db.commit()
    return {"message": f"Added {added} items", "invoice_id": inv.id}


# ------------- Finalize & Payments -------------
@router.post("/invoices/{invoice_id}/finalize")
def finalize_invoice(invoice_id: int,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    _need_any(user, ["billing.finalize"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status != "draft":
        raise HTTPException(400, "Already finalized/cancelled")
    if not any(not i.is_voided for i in inv.items):
        raise HTTPException(400, "Cannot finalize empty invoice")
    inv.status = "finalized"
    inv.finalized_at = datetime.utcnow()
    _recompute(inv)
    db.commit()
    return {"message": "Finalized", "invoice_id": inv.id}


@router.post("/invoices/{invoice_id}/payments")
def add_payment(invoice_id: int,
                payload: PaymentIn,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    _need_any(user, ["billing.payments.add"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status != "finalized":
        raise HTTPException(400, "Invoice must be finalized")

    # Coerce to Decimal (avoid float math)
    amount = Decimal(str(payload.amount))
    if amount <= 0:
        raise HTTPException(400, "Amount must be > 0")

    p = Payment(
        invoice_id=inv.id,
        amount=amount,  # <-- Decimal
        mode=payload.mode,
        reference_no=payload.reference_no or None,
        created_by=user.id,
    )
    db.add(p)

    inv.amount_paid = (inv.amount_paid
                       or Decimal("0")) + amount  # <-- Decimal math
    _recompute(inv)  # ok if _recompute casts to float for response fields
    db.commit()

    return {
        "message": "Payment recorded",
        "invoice_id": inv.id,
        "balance_due": float(inv.balance_due or 0)  # keep responses as float
    }


# ------------- Admin actions -------------
@router.post("/invoices/{invoice_id}/cancel")
def cancel_invoice(invoice_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    _need_any(user, ["billing.cancel"])
    inv = db.query(Invoice).get(invoice_id)
    if not inv: raise HTTPException(404, "Invoice not found")
    if inv.status not in {"draft", "finalized"}:
        raise HTTPException(400, "Already cancelled/reversed")
    inv.status = "cancelled"
    _recompute(inv)
    db.commit()
    return {"message": "Invoice cancelled"}
