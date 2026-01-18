# FILE: app/api/routes_billing_revenue.py
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPayment,
    EncounterType,
    DocStatus,
    ReceiptStatus,
    PaymentDirection,
)

router = APIRouter(prefix="/billing/revenue", tags=["Billing Revenue"])

D0 = Decimal("0.00")

# ✅ your module labels
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


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return D0


def _money(v: Any) -> str:
    return f"{_d(v):.2f}"


def _parse_date(s: Optional[str], default: Optional[date] = None) -> date:
    if not s:
        if default is None:
            return date.today()
        return default
    return date.fromisoformat(s)


def _date_bounds(from_date: date, to_date: date) -> Dict[str, date]:
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    return {"from": from_date, "to": to_date}


def _user_display(u: User) -> str:
    return (getattr(u, "full_name", None) or getattr(u, "name", None)
            or getattr(u, "username", None) or getattr(u, "email", None)
            or f"User #{u.id}")


@router.get("/dashboard")
def revenue_dashboard(
        db: Session = Depends(get_db),
        _user: User = Depends(current_user),
        date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
        statuses: List[str] = Query(
            ["POSTED"],
            description="POSTED, APPROVED, DRAFT (VOID always excluded)"),
        module: Optional[str] = Query(
            None,
            description=
            "Filter dashboard by invoice.module (e.g., PHM, LAB, ROOM)"),
        top_n: int = Query(10, ge=3, le=50),
) -> Dict[str, Any]:
    """
    ✅ Extreme Revenue Dashboard

    Revenue Basis:
    - Billed: BillingInvoice.grand_total (filtered by date/status)
    - Collections: BillingPayment.amount (received_at date)
    - VOID invoices & VOID receipts excluded

    Dates:
    - Invoice event date = DATE(COALESCE(posted_at, approved_at, created_at))
    - Payment date = DATE(received_at)

    Module Cards:
    - module_revenue is ALWAYS computed from the unfiltered base range (so cards remain visible even when module filter applied)
    - other metrics respect `module` filter (if provided)
    """

    # ---- date range ----
    df = _parse_date(date_from, default=date.today().replace(day=1))
    dt = _parse_date(date_to, default=date.today())
    rng = _date_bounds(df, dt)
    df, dt = rng["from"], rng["to"]

    # ---- statuses ----
    wanted_statuses: List[str] = []
    for s in (statuses or []):
        s = (s or "").strip().upper()
        if not s or s == "VOID":
            continue
        wanted_statuses.append(s)
    if not wanted_statuses:
        wanted_statuses = ["POSTED"]

    inv_event_at = func.coalesce(BillingInvoice.posted_at,
                                 BillingInvoice.approved_at,
                                 BillingInvoice.created_at)
    inv_event_day = func.date(inv_event_at)

    base_inv_filters = [
        BillingInvoice.status != DocStatus.VOID,
        BillingInvoice.status.in_(wanted_statuses),
        inv_event_day >= df,
        inv_event_day <= dt,
    ]

    # dashboard filtered view (optional module)
    inv_filters = list(base_inv_filters)
    if module:
        inv_filters.append(BillingInvoice.module == module)

    pay_day = func.date(BillingPayment.received_at)
    pay_filters = [
        BillingPayment.status != ReceiptStatus.VOID,
        pay_day >= df,
        pay_day <= dt,
    ]

    # -----------------------------
    # ✅ Module revenue cards (ALWAYS base range, not module-filtered)
    # -----------------------------
    mod_rows = (db.query(
        BillingInvoice.module.label("module"),
        func.count(BillingInvoice.id).label("invoices"),
        func.count(func.distinct(
            BillingInvoice.billing_case_id)).label("cases"),
        func.sum(BillingInvoice.grand_total).label("billed"),
    ).filter(*base_inv_filters).group_by(BillingInvoice.module).order_by(
        func.sum(BillingInvoice.grand_total).desc()).all())

    module_revenue: List[Dict[str, Any]] = []
    module_total_billed = D0
    module_total_invoices = 0
    module_total_cases = 0

    for r in mod_rows:
        m = (r.module or "UNSPEC").strip() if isinstance(
            r.module, str) else (r.module or "UNSPEC")
        billed = _d(r.billed)
        module_total_billed += billed
        module_total_invoices += int(r.invoices or 0)
        module_total_cases += int(r.cases or 0)
        module_revenue.append({
            "module": m,
            "module_name": MODULES.get(m, m),
            "invoices": int(r.invoices or 0),
            "cases": int(r.cases or 0),
            "billed": _money(billed),
        })

    # -----------------------------
    # KPI: totals (filtered by module if given)
    # -----------------------------
    kpi_row = (db.query(
        func.count(BillingInvoice.id).label("invoices"),
        func.sum(BillingInvoice.sub_total).label("sub_total"),
        func.sum(BillingInvoice.discount_total).label("discount_total"),
        func.sum(BillingInvoice.tax_total).label("tax_total"),
        func.sum(BillingInvoice.round_off).label("round_off"),
        func.sum(BillingInvoice.grand_total).label("billed"),
        func.count(func.distinct(
            BillingInvoice.billing_case_id)).label("cases"),
    ).filter(*inv_filters).first())

    billed = _d(kpi_row.billed)
    cases = int(kpi_row.cases or 0)
    invoices = int(kpi_row.invoices or 0)

    pay_row = (db.query(
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.IN, BillingPayment.amount),
                 else_=0)).label("collections"),
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.OUT, BillingPayment.amount),
                 else_=0)).label("refunds"),
        func.count(BillingPayment.id).label("receipts"),
    ).filter(*pay_filters).first())

    collections = _d(pay_row.collections)
    refunds = _d(pay_row.refunds)
    receipts = int(pay_row.receipts or 0)

    outstanding = billed - collections + refunds

    kpis = {
        "billed": _money(billed),
        "sub_total": _money(kpi_row.sub_total),
        "discount_total": _money(kpi_row.discount_total),
        "tax_total": _money(kpi_row.tax_total),
        "round_off": _money(kpi_row.round_off),
        "collections": _money(collections),
        "refunds": _money(refunds),
        "outstanding": _money(outstanding),
        "cases": cases,
        "invoices": invoices,
        "receipts": receipts,
        "avg_per_case": _money((billed / cases) if cases else D0),
    }

    # -----------------------------
    # Trend by day (filtered by module if given)
    # -----------------------------
    trend_billed = (db.query(
        inv_event_day.label("day"),
        func.sum(BillingInvoice.grand_total).label("billed"),
    ).filter(*inv_filters).group_by(inv_event_day).order_by(
        inv_event_day.asc()).all())

    trend_pay = (db.query(
        pay_day.label("day"),
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.IN, BillingPayment.amount),
                 else_=0)).label("collections"),
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.OUT, BillingPayment.amount),
                 else_=0)).label("refunds"),
    ).filter(*pay_filters).group_by(pay_day).order_by(pay_day.asc()).all())

    trend_map: Dict[str, Dict[str, Any]] = {}
    for r in trend_billed:
        day = str(r.day)
        trend_map[day] = {
            "date": day,
            "billed": _money(r.billed),
            "collections": "0.00",
            "refunds": "0.00"
        }
    for r in trend_pay:
        day = str(r.day)
        if day not in trend_map:
            trend_map[day] = {
                "date": day,
                "billed": "0.00",
                "collections": "0.00",
                "refunds": "0.00"
            }
        trend_map[day]["collections"] = _money(r.collections)
        trend_map[day]["refunds"] = _money(r.refunds)

    trend = [trend_map[k] for k in sorted(trend_map.keys())]

    # -----------------------------
    # Encounter Revenue (filtered by module if given)
    # -----------------------------
    enc_rows = (db.query(
        BillingCase.encounter_type.label("encounter_type"),
        func.count(func.distinct(BillingCase.id)).label("cases"),
        func.sum(BillingInvoice.grand_total).label("billed"),
    ).join(BillingInvoice,
           BillingInvoice.billing_case_id == BillingCase.id).filter(
               *inv_filters).group_by(BillingCase.encounter_type).all())

    encounter_revenue = [{
        "encounter_type": str(r.encounter_type),
        "cases": int(r.cases or 0),
        "billed": _money(r.billed)
    } for r in enc_rows]

    # -----------------------------
    # Service Group Revenue (from lines, filtered by module if given)
    # -----------------------------
    sg_rows = (db.query(
        BillingInvoiceLine.service_group.label("service_group"),
        func.count(BillingInvoiceLine.id).label("lines"),
        func.sum(BillingInvoiceLine.line_total).label("gross"),
        func.sum(BillingInvoiceLine.discount_amount).label("discount"),
        func.sum(BillingInvoiceLine.tax_amount).label("tax"),
        func.sum(BillingInvoiceLine.net_amount).label("net"),
    ).join(BillingInvoice,
           BillingInvoice.id == BillingInvoiceLine.invoice_id).filter(
               *inv_filters).group_by(
                   BillingInvoiceLine.service_group).order_by(
                       func.sum(BillingInvoiceLine.net_amount).desc()).all())

    service_group_revenue = [{
        "service_group": str(r.service_group),
        "lines": int(r.lines or 0),
        "gross": _money(r.gross),
        "discount": _money(r.discount),
        "tax": _money(r.tax),
        "net": _money(r.net),
    } for r in sg_rows]

    # -----------------------------
    # Referral User Revenue (full case billed, filtered by module if given)
    # -----------------------------
    ref_rows = (db.query(
        BillingCase.referral_user_id.label("user_id"),
        func.count(func.distinct(BillingCase.id)).label("cases"),
        func.sum(BillingInvoice.grand_total).label("billed"),
    ).join(BillingInvoice,
           BillingInvoice.billing_case_id == BillingCase.id).filter(
               *inv_filters).group_by(BillingCase.referral_user_id).order_by(
                   func.sum(
                       BillingInvoice.grand_total).desc()).limit(top_n).all())

    ref_user_ids = [int(r.user_id) for r in ref_rows if r.user_id]
    users_map: Dict[int, str] = {}
    if ref_user_ids:
        users = db.query(User).filter(User.id.in_(ref_user_ids)).all()
        users_map = {int(u.id): _user_display(u) for u in users}

    referral_user_revenue = []
    for r in ref_rows:
        uid = int(r.user_id) if r.user_id else None
        referral_user_revenue.append({
            "user_id":
            uid,
            "user_name":
            users_map.get(uid, "Unassigned") if uid else "Unassigned",
            "cases":
            int(r.cases or 0),
            "billed":
            _money(r.billed),
        })

    # -----------------------------
    # Cashier Collections (payments received_by)
    # -----------------------------
    cashier_rows = (db.query(
        BillingPayment.received_by.label("user_id"),
        func.count(BillingPayment.id).label("receipts"),
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.IN, BillingPayment.amount),
                 else_=0)).label("collections"),
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.OUT, BillingPayment.amount),
                 else_=0)).label("refunds"),
    ).filter(*pay_filters).group_by(BillingPayment.received_by).order_by(
        func.sum(
            case((BillingPayment.direction
                  == PaymentDirection.IN, BillingPayment.amount),
                 else_=0)).desc()).limit(top_n).all())

    cashier_user_ids = [int(r.user_id) for r in cashier_rows if r.user_id]
    cashier_users_map: Dict[int, str] = {}
    if cashier_user_ids:
        users = db.query(User).filter(User.id.in_(cashier_user_ids)).all()
        cashier_users_map = {int(u.id): _user_display(u) for u in users}

    cashier_collections = []
    for r in cashier_rows:
        uid = int(r.user_id) if r.user_id else None
        cashier_collections.append({
            "user_id":
            uid,
            "user_name":
            cashier_users_map.get(uid, "Unknown") if uid else "Unknown",
            "receipts":
            int(r.receipts or 0),
            "collections":
            _money(r.collections),
            "refunds":
            _money(r.refunds),
        })

    return {
        "range": {
            "from": str(df),
            "to": str(dt),
            "statuses": wanted_statuses,
            "module": module,
        },
        "kpis": kpis,
        "trend": trend,
        "module_revenue": module_revenue,
        "module_totals": {
            "billed": _money(module_total_billed),
            "invoices": module_total_invoices,
            "cases": module_total_cases,
        },
        "encounter_revenue": encounter_revenue,
        "service_group_revenue": service_group_revenue,
        "referral_user_revenue": referral_user_revenue,
        "cashier_collections": cashier_collections,
    }
