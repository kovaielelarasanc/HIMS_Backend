# FILE: app/api/routes_billing_edits.py
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.billing import (
    BillingInvoice,
    BillingInvoiceLine,
    BillingInvoiceEditRequest,
    BillingAuditLog,
    DocStatus,
    InvoiceEditRequestStatus,
)

from app.services.billing_calc import recompute_invoice_totals
from app.services.billing_service import (
    update_invoice_line as svc_update_invoice_line,
    delete_invoice_line as svc_delete_invoice_line,
)

router = APIRouter(prefix="/billing", tags=["Billing"])

MONEY_Q = Decimal("0.01")
QTY_Q = Decimal("0.0001")


def _now_utc_naive() -> datetime:
    return datetime.utcnow()


def _enum_value(x):
    return x.value if hasattr(x, "value") else x


def _money(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _qty(x: Decimal) -> Decimal:
    return (x or Decimal("0")).quantize(QTY_Q, rounding=ROUND_HALF_UP)


def _audit(
    db: Session,
    *,
    user: Optional[User],
    entity_type: str,
    entity_id: int,
    action: str,
    old_json: Optional[Dict[str, Any]] = None,
    new_json: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
):
    db.add(
        BillingAuditLog(
            entity_type=entity_type,
            entity_id=int(entity_id),
            action=action,
            old_json=old_json,
            new_json=new_json,
            reason=(reason or None),
            user_id=int(user.id) if user else None,
        ))


def _get_invoice_or_404(db: Session, invoice_id: int) -> BillingInvoice:
    inv = db.query(BillingInvoice).filter(
        BillingInvoice.id == int(invoice_id)).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


def _get_line_or_404(db: Session, invoice_id: int,
                     line_id: int) -> BillingInvoiceLine:
    ln = (db.query(BillingInvoiceLine).filter(
        BillingInvoiceLine.id == int(line_id)).filter(
            BillingInvoiceLine.invoice_id == int(invoice_id)).first())
    if not ln:
        raise HTTPException(status_code=404, detail="Invoice line not found")
    return ln


def _is_edit_unlocked(db: Session, invoice_id: int) -> bool:
    now = _now_utc_naive()
    req = (db.query(BillingInvoiceEditRequest).filter(
        BillingInvoiceEditRequest.invoice_id == int(invoice_id)).filter(
            BillingInvoiceEditRequest.status ==
            InvoiceEditRequestStatus.APPROVED.value).filter(
                or_(BillingInvoiceEditRequest.expires_at.is_(None),
                    BillingInvoiceEditRequest.expires_at >= now)).order_by(
                        desc(BillingInvoiceEditRequest.reviewed_at)).first())
    return bool(req)


def _require_invoice_editable(db: Session, inv: BillingInvoice):
    st = str(_enum_value(inv.status) or "").upper()
    if st == "DRAFT":
        return
    if st == "APPROVED" and _is_edit_unlocked(db, int(inv.id)):
        return
    raise HTTPException(
        status_code=409,
        detail="Invoice locked. Request admin approval to edit.")


def _line_dict(ln: BillingInvoiceLine) -> Dict[str, Any]:
    return {
        "id":
        int(ln.id),
        "invoice_id":
        int(ln.invoice_id),
        "billing_case_id":
        int(ln.billing_case_id),
        "description":
        getattr(ln, "description", None),
        "qty":
        str(getattr(ln, "qty", 0) or 0),
        "unit_price":
        str(getattr(ln, "unit_price", 0) or 0),
        "discount_percent":
        str(getattr(ln, "discount_percent", 0) or 0),
        "discount_amount":
        str(getattr(ln, "discount_amount", 0) or 0),
        "gst_rate":
        str(getattr(ln, "gst_rate", 0) or 0),
        "tax_amount":
        str(getattr(ln, "tax_amount", 0) or 0),
        "line_total":
        str(getattr(ln, "line_total", 0) or 0),
        "net_amount":
        str(getattr(ln, "net_amount", 0) or 0),
        "is_manual":
        bool(getattr(ln, "is_manual", False)),
        "source_module":
        getattr(ln, "source_module", None),
        "source_ref_id":
        getattr(ln, "source_ref_id", None),
        "source_line_key":
        getattr(ln, "source_line_key", None),
        "meta_json":
        getattr(ln, "meta_json", None) if hasattr(ln, "meta_json") else None,
    }


# ---------------- Schemas ----------------
class EditReasonIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


class EditDecisionIn(BaseModel):
    decision_notes: str = Field(default="", max_length=255)
    unlock_hours: int = Field(default=24, ge=1, le=240)


class LinePatchIn(BaseModel):
    qty: Optional[Decimal] = Field(default=None, ge=0)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)
    discount_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    discount_amount: Optional[Decimal] = Field(default=None, ge=0)
    gst_rate: Optional[Decimal] = Field(default=None, ge=0, le=100)
    description: Optional[str] = Field(default=None, max_length=255)
    doctor_id: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=255)


# ---------------- 1) Request edit ----------------
@router.post("/invoices/{invoice_id}/edit-requests")
def create_invoice_edit_request(
        invoice_id: int,
        inp: EditReasonIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, invoice_id)
    st = str(_enum_value(inv.status) or "").upper()

    if st not in {"APPROVED"}:
        raise HTTPException(
            status_code=409,
            detail="Edit request allowed only for APPROVED invoices")

    exists = (db.query(BillingInvoiceEditRequest).filter(
        BillingInvoiceEditRequest.invoice_id == int(inv.id)).filter(
            BillingInvoiceEditRequest.status ==
            InvoiceEditRequestStatus.PENDING.value).order_by(
                desc(BillingInvoiceEditRequest.requested_at)).first())
    if exists:
        return {
            "ok": True,
            "request_id": int(exists.id),
            "status": exists.status,
            "message": "Already pending"
        }

    req = BillingInvoiceEditRequest(
        invoice_id=int(inv.id),
        billing_case_id=int(inv.billing_case_id),
        status=InvoiceEditRequestStatus.PENDING.value,
        reason=inp.reason.strip(),
        requested_by_user_id=int(user.id),
    )
    db.add(req)
    _audit(
        db,
        user=user,
        entity_type="INVOICE",
        entity_id=int(inv.id),
        action="EDIT_REQUEST_CREATE",
        old_json=None,
        new_json={"reason": inp.reason.strip()},
        reason=inp.reason.strip(),
    )
    db.commit()
    db.refresh(req)
    return {"ok": True, "request_id": int(req.id), "status": req.status}


# ---------------- 2) List edit requests ----------------
@router.get("/edit-requests")
def list_invoice_edit_requests(
        status: str = Query("PENDING"),
        invoice_id: Optional[int] = Query(None),
        case_id: Optional[int] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    from app.api.deps import require_perm
    if not getattr(user, "is_admin", False):
        require_perm(user, "billing.invoice.edit_requests.review")

    st = (status or "PENDING").strip().upper()
    if st not in {"PENDING", "APPROVED", "REJECTED", "ALL"}:
        st = "PENDING"

    q = db.query(BillingInvoiceEditRequest)
    if st != "ALL":
        q = q.filter(BillingInvoiceEditRequest.status == st)
    if invoice_id:
        q = q.filter(BillingInvoiceEditRequest.invoice_id == int(invoice_id))
    if case_id:
        q = q.filter(BillingInvoiceEditRequest.billing_case_id == int(case_id))

    rows = q.order_by(desc(
        BillingInvoiceEditRequest.requested_at)).limit(limit).all()

    return {
        "items": [{
            "id":
            int(r.id),
            "invoice_id":
            int(r.invoice_id),
            "billing_case_id":
            int(r.billing_case_id),
            "status":
            r.status,
            "reason":
            r.reason,
            "requested_by_user_id":
            r.requested_by_user_id,
            "requested_at":
            r.requested_at.isoformat() if r.requested_at else None,
            "reviewed_by_user_id":
            r.reviewed_by_user_id,
            "reviewed_at":
            r.reviewed_at.isoformat() if r.reviewed_at else None,
            "decision_notes":
            r.decision_notes,
            "unlock_hours":
            r.unlock_hours,
            "expires_at":
            r.expires_at.isoformat() if r.expires_at else None,
            "applied":
            bool(getattr(r, "applied", False)),
        } for r in rows]
    }


# ---------------- 3) Approve / Reject ----------------
@router.post("/edit-requests/{request_id}/approve")
def approve_invoice_edit_request(
        request_id: int,
        inp: EditDecisionIn = Body(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    from app.api.deps import require_perm
    if not getattr(user, "is_admin", False):
        require_perm(user, "billing.invoice.edit_requests.review")

    req = db.query(BillingInvoiceEditRequest).filter(
        BillingInvoiceEditRequest.id == int(request_id)).first()
    if not req:
        raise HTTPException(404, "Edit request not found")

    if (req.status or "").upper() != "PENDING":
        raise HTTPException(409, "Only PENDING requests can be approved")

    inv = _get_invoice_or_404(db, int(req.invoice_id))
    if str(_enum_value(inv.status) or "").upper() != "APPROVED":
        raise HTTPException(409, "Invoice must be APPROVED to unlock edit")

    now = _now_utc_naive()
    expires = now + timedelta(hours=int(inp.unlock_hours or 24))

    old_req = {"status": req.status}
    req.status = InvoiceEditRequestStatus.APPROVED.value
    req.reviewed_by_user_id = int(user.id)
    req.reviewed_at = now
    req.decision_notes = (inp.decision_notes or "").strip()
    req.unlock_hours = int(inp.unlock_hours or 24)
    req.expires_at = expires

    old_inv_status = str(_enum_value(inv.status) or "")
    inv.status = DocStatus.DRAFT
    inv.approved_at = None
    inv.approved_by = None

    _audit(
        db,
        user=user,
        entity_type="INVOICE",
        entity_id=int(inv.id),
        action="EDIT_REQUEST_APPROVE_REOPEN",
        old_json={
            "invoice_status": old_inv_status,
            "request": old_req
        },
        new_json={
            "invoice_status": "DRAFT",
            "request_status": "APPROVED",
            "expires_at": expires.isoformat()
        },
        reason=req.reason,
    )

    db.commit()
    return {
        "ok": True,
        "request_id": int(req.id),
        "invoice_id": int(inv.id),
        "invoice_status": "DRAFT",
        "expires_at": expires.isoformat()
    }


@router.post("/edit-requests/{request_id}/reject")
def reject_invoice_edit_request(
        request_id: int,
        inp: EditDecisionIn = Body(default=EditDecisionIn()),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    from app.api.deps import require_perm
    if not getattr(user, "is_admin", False):
        require_perm(user, "billing.invoice.edit_requests.review")

    req = db.query(BillingInvoiceEditRequest).filter(
        BillingInvoiceEditRequest.id == int(request_id)).first()
    if not req:
        raise HTTPException(404, "Edit request not found")

    if (req.status or "").upper() != "PENDING":
        raise HTTPException(409, "Only PENDING requests can be rejected")

    now = _now_utc_naive()
    req.status = InvoiceEditRequestStatus.REJECTED.value
    req.reviewed_by_user_id = int(user.id)
    req.reviewed_at = now
    req.decision_notes = (inp.decision_notes or "").strip()
    req.expires_at = None

    _audit(
        db,
        user=user,
        entity_type="INVOICE",
        entity_id=int(req.invoice_id),
        action="EDIT_REQUEST_REJECT",
        old_json={"status": "PENDING"},
        new_json={
            "status": "REJECTED",
            "decision_notes": req.decision_notes
        },
        reason=req.reason,
    )

    db.commit()
    return {"ok": True, "request_id": int(req.id), "status": req.status}


# ---------------- 4) Line update/delete ----------------
@router.put("/invoices/{invoice_id}/lines/{line_id}")
def update_invoice_line(
        invoice_id: int,
        line_id: int,
        inp: LinePatchIn,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, invoice_id)
    _require_invoice_editable(db, inv)

    ln = _get_line_or_404(db, invoice_id, line_id)
    old_ln = _line_dict(ln)

    updated = svc_update_invoice_line(
        db,
        line_id=int(line_id),
        user=user,
        description=inp.description,
        qty=_qty(inp.qty) if inp.qty is not None else None,
        unit_price=_money(inp.unit_price)
        if inp.unit_price is not None else None,
        discount_percent=_money(inp.discount_percent)
        if inp.discount_percent is not None else None,
        discount_amount=_money(inp.discount_amount)
        if inp.discount_amount is not None else None,
        gst_rate=_money(inp.gst_rate) if inp.gst_rate is not None else None,
        doctor_id=inp.doctor_id,
        reason=inp.reason,
    )

    # ensure totals are correct
    recompute_invoice_totals(db, int(inv.id))
    _audit(
        db,
        user=user,
        entity_type="INVOICE_LINE",
        entity_id=int(updated.id),
        action="LINE_UPDATE",
        old_json=old_ln,
        new_json=_line_dict(updated),
        reason=(inp.reason or "Line edited"),
    )

    db.commit()
    db.refresh(updated)
    db.refresh(inv)
    return {
        "ok": True,
        "line": _line_dict(updated),
        "invoice_totals": {
            "grand_total": str(inv.grand_total),
            "sub_total": str(inv.sub_total),
            "tax_total": str(inv.tax_total)
        }
    }


@router.delete("/invoices/{invoice_id}/lines/{line_id}")
def delete_invoice_line(
        invoice_id: int,
        line_id: int,
        reason: str = Query("Line removed"),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, invoice_id)
    _require_invoice_editable(db, inv)

    ln = _get_line_or_404(db, invoice_id, line_id)
    old_ln = _line_dict(ln)

    svc_delete_invoice_line(db, line_id=int(line_id), user=user, reason=reason)

    recompute_invoice_totals(db, int(inv.id))
    _audit(
        db,
        user=user,
        entity_type="INVOICE_LINE",
        entity_id=int(line_id),
        action="LINE_DELETE",
        old_json=old_ln,
        new_json=None,
        reason=(reason or "Line removed"),
    )

    db.commit()
    db.refresh(inv)
    return {
        "ok": True,
        "deleted": int(line_id),
        "invoice_totals": {
            "grand_total": str(inv.grand_total)
        }
    }


# ---------------- 5) Audit logs ----------------
@router.get("/invoices/{invoice_id}/audit-logs")
def list_invoice_audit_logs(
        invoice_id: int,
        limit: int = Query(200, ge=1, le=500),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    inv = _get_invoice_or_404(db, invoice_id)

    line_ids = [
        x[0] for x in db.query(BillingInvoiceLine.id).filter(
            BillingInvoiceLine.invoice_id == int(inv.id)).all()
    ]
    q = db.query(BillingAuditLog).filter(
        or_(
            (BillingAuditLog.entity_type == "INVOICE") &
            (BillingAuditLog.entity_id == int(inv.id)),
            (BillingAuditLog.entity_type == "INVOICE_LINE") &
            (BillingAuditLog.entity_id.in_(line_ids) if line_ids else False),
        ))
    rows = q.order_by(desc(BillingAuditLog.created_at)).limit(limit).all()

    def _pick_user_label(uid: Optional[int]) -> str:
        if not uid:
            return "—"
        u = db.query(User).filter(User.id == int(uid)).first()
        if not u:
            return f"User #{uid}"
        for k in ["full_name", "display_name", "name", "username", "email"]:
            v = getattr(u, k, None)
            if v:
                return str(v)
        return f"User #{uid}"

    return {
        "items": [{
            "id": int(r.id),
            "entity_type": r.entity_type,
            "entity_id": int(r.entity_id),
            "action": r.action,
            "reason": r.reason,
            "user_id": r.user_id,
            "user_label": _pick_user_label(r.user_id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "old_json": r.old_json,
            "new_json": r.new_json,
        } for r in rows]
    }
