# FILE: app/services/mis_service.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Appointment, Visit  # noqa: F401
from app.models.ipd import IpdAdmission
# ✅ use NEW pharmacy models
from app.models.pharmacy_prescription import PharmacySale, PharmacySaleItem
from app.models.lis import LisOrder
from app.models.ris import RisOrder
from app.models.ot import OtOrder  # noqa: F401
from app.models.billing import Invoice, InvoiceItem, Payment  # noqa: F401

from app.schemas.mis import (
    MISFilter,
    MISColumn,
    MISChartConfig,
    MISRawReportResult,
    MisDefinitionOut,
)

# ---------- helpers ----------


def _user_has_perm_prefix(user: User, prefixes: List[str]) -> bool:
    """
    Returns True if user has ANY permission with one of the given prefixes.
    Admins always pass.
    """
    if user.is_admin:
        return True

    perm_codes = set()
    for role in user.roles:
        for p in getattr(role, "permissions", []):
            perm_codes.add(p.code)

    for pref in prefixes:
        for c in perm_codes:
            if c.startswith(pref):
                return True
    return False


def _date_range(filters: MISFilter) -> tuple[datetime, datetime]:
    """
    Convert MISFilter date range (inclusive) to datetime range [start, end).
    If missing, default to last 7 days.
    """
    d_from = filters.date_from
    d_to = filters.date_to

    if not d_from or not d_to:
        today = datetime.utcnow().date()
        d_to = d_to or today
        d_from = d_from or (today - timedelta(days=6))

    if d_to < d_from:
        d_from, d_to = d_to, d_from

    start = datetime.combine(d_from, time.min)
    end = datetime.combine(d_to + timedelta(days=1), time.min)
    return start, end


def _safe_float(val: Any) -> float:
    return float(val or 0)


# ---------- MIS Definition object ----------


@dataclass
class MISDefinition:
    code: str
    name: str
    group: str
    description: str
    required_perm_prefixes: List[str]
    allowed_filters: List[str]
    run_fn: Callable[[Session, User, MISFilter], MISRawReportResult]


# ---------- Report implementations ----------


def _report_patient_registration_summary(
        db: Session, user: User, filters: MISFilter) -> MISRawReportResult:
    start_dt, end_dt = _date_range(filters)

    q = db.query(
        func.count(Patient.id).label("total"),
        func.coalesce(func.min(Patient.created_at), None).label("first_at"),
        func.coalesce(func.max(Patient.created_at), None).label("last_at"),
    ).filter(
        Patient.created_at >= start_dt,
        Patient.created_at < end_dt,
    )

    # If you store department on Patient, you can filter here.
    if filters.department_id:
        # Example (adjust field name):
        # q = q.filter(Patient.department_id == filters.department_id)
        pass

    row = q.one()

    rows = (db.query(
        func.date(Patient.created_at).label("day"),
        func.count(Patient.id).label("count"),
    ).filter(
        Patient.created_at >= start_dt,
        Patient.created_at < end_dt,
    ).group_by(func.date(Patient.created_at)).order_by(
        func.date(Patient.created_at)).all())

    data_rows = [{
        "day": r.day.isoformat() if r.day else None,
        "count": int(r.count or 0),
    } for r in rows]

    columns = [
        MISColumn(key="day", label="Date", type="date", align="left"),
        MISColumn(
            key="count",
            label="New Patients",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="column",
        x_key="day",
        series=[{
            "key": "count",
            "label": "New Patients"
        }],
    )

    return MISRawReportResult(
        code="patient.registration_summary",
        name="Patient Registration Summary",
        group="Patient / Registration",
        description="New registrations per day in the selected period.",
        filters_applied=filters,
        summary={
            "total_patients":
            int(row.total or 0),
            "first_registration_at":
            row.first_at.isoformat() if row.first_at else None,
            "last_registration_at":
            row.last_at.isoformat() if row.last_at else None,
        },
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_opd_visit_summary(db: Session, user: User,
                              filters: MISFilter) -> MISRawReportResult:
    """
    OPD visit summary for the selected period.

    NOTE:
    We avoid using Visit.doctor_id because the model does not expose that
    attribute (error suggested only `Visit.doctor` exists).
    This implementation returns an overall OPD visit count (all doctors).
    Once the Visit model is shared, we can extend this to true doctor-wise summary.
    """
    start_dt, end_dt = _date_range(filters)

    # Count all visits in date range
    total_row = (db.query(func.count(Visit.id).label("visits"), ).filter(
        Visit.visit_at >= start_dt,
        Visit.visit_at < end_dt,
    ).one())

    total_visits = int(total_row.visits or 0)

    # Single aggregate row (All doctors)
    data_rows = [{
        "doctor_id": None,
        "doctor_name": "All doctors",
        "visits": total_visits,
    }]

    columns = [
        MISColumn(
            key="doctor_name",
            label="Doctor / Stream",
            type="string",
            align="left",
        ),
        MISColumn(
            key="visits",
            label="OPD Visits",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="bar",
        x_key="doctor_name",
        series=[{
            "key": "visits",
            "label": "OPD Visits",
        }],
    )

    return MISRawReportResult(
        code="opd.visit_summary",
        name="OPD Visit Summary",
        group="OPD",
        description="Total OPD visits in the selected period (all doctors).",
        filters_applied=filters,
        summary={"total_visits": total_visits},
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_ipd_admission_summary(db: Session, user: User,
                                  filters: MISFilter) -> MISRawReportResult:
    start_dt, end_dt = _date_range(filters)

    rows = (db.query(
        func.date(IpdAdmission.admitted_at).label("day"),
        func.count(IpdAdmission.id).label("admissions"),
    ).filter(
        IpdAdmission.admitted_at >= start_dt,
        IpdAdmission.admitted_at < end_dt,
    ).group_by(func.date(IpdAdmission.admitted_at)).order_by(
        func.date(IpdAdmission.admitted_at)).all())

    data_rows = [{
        "day": r.day.isoformat(),
        "admissions": int(r.admissions or 0)
    } for r in rows if r.day]

    total_admissions = sum(r["admissions"] for r in data_rows)

    columns = [
        MISColumn(key="day", label="Date", type="date", align="left"),
        MISColumn(
            key="admissions",
            label="Admissions",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="column",
        x_key="day",
        series=[{
            "key": "admissions",
            "label": "IPD Admissions"
        }],
    )

    return MISRawReportResult(
        code="ipd.admission_summary",
        name="IPD Admission Summary",
        group="IPD",
        description="Daily IPD admissions in the selected period.",
        filters_applied=filters,
        summary={"total_admissions": total_admissions},
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_billing_revenue_summary(db: Session, user: User,
                                    filters: MISFilter) -> MISRawReportResult:
    start_dt, end_dt = _date_range(filters)

    base_q = db.query(Invoice).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    )

    if filters.context_type == "opd":
        base_q = base_q.filter(Invoice.context_type == "opd")
    elif filters.context_type == "ipd":
        base_q = base_q.filter(Invoice.context_type == "ipd")

    total_row = (db.query(
        func.count(Invoice.id).label("count"),
        func.coalesce(func.sum(Invoice.net_total), 0).label("net_total"),
        func.coalesce(func.sum(Invoice.amount_paid), 0).label("amount_paid"),
        func.coalesce(func.sum(Invoice.balance_due), 0).label("balance_due"),
    ).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    ).one())

    ctx_rows = (db.query(
        func.coalesce(Invoice.context_type, "other").label("ctx"),
        func.coalesce(func.sum(Invoice.net_total), 0).label("net_total"),
    ).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    ).group_by(Invoice.context_type).all())

    data_rows = [{
        "stream": (r.ctx or "other").upper(),
        "net_total": _safe_float(r.net_total),
    } for r in ctx_rows]

    columns = [
        MISColumn(key="stream", label="Stream", type="enum", align="left"),
        MISColumn(
            key="net_total",
            label="Net Revenue (INR)",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="bar",
        x_key="stream",
        series=[{
            "key": "net_total",
            "label": "Revenue"
        }],
    )

    return MISRawReportResult(
        code="billing.revenue_summary",
        name="Revenue Summary (OPD/IPD)",
        group="Billing / Finance",
        description=(
            "Revenue by stream (OPD/IPD) and overall totals in the selected "
            "period."),
        filters_applied=filters,
        summary={
            "invoice_count": int(total_row.count or 0),
            "total_net": _safe_float(total_row.net_total),
            "total_paid": _safe_float(total_row.amount_paid),
            "total_balance_due": _safe_float(total_row.balance_due),
        },
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_billing_collection_summary(
        db: Session, user: User, filters: MISFilter) -> MISRawReportResult:
    """
    Collection summary report (by payment mode).
    """
    start_dt, end_dt = _date_range(filters)

    totals = (db.query(
        func.count(Invoice.id).label("invoice_count"),
        func.coalesce(func.sum(Invoice.net_total), 0).label("net_total"),
        func.coalesce(func.sum(Invoice.amount_paid), 0).label("amount_paid"),
        func.coalesce(func.sum(Invoice.balance_due), 0).label("balance_due"),
    ).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    ).one())

    pay_q = (db.query(
        Payment.mode.label("mode"),
        func.coalesce(func.sum(Payment.amount), 0).label("amount"),
    ).join(Invoice, Payment.invoice_id == Invoice.id).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    ).group_by(Payment.mode))

    if filters.payment_mode:
        pay_q = pay_q.filter(Payment.mode == filters.payment_mode)

    pay_rows = pay_q.all()

    data_rows = [{
        "mode": r.mode or "Unknown",
        "amount": _safe_float(r.amount)
    } for r in pay_rows]

    columns = [
        MISColumn(key="mode", label="Payment Mode", type="enum", align="left"),
        MISColumn(
            key="amount",
            label="Amount Collected (INR)",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="pie",
        x_key="mode",
        series=[{
            "key": "amount",
            "label": "Collection"
        }],
    )

    return MISRawReportResult(
        code="billing.collection_summary",
        name="Collection Summary (Payment Mode)",
        group="Billing / Finance",
        description=
        "Collection breakdown by payment mode (cash/card/UPI/credit).",
        filters_applied=filters,
        summary={
            "invoice_count": int(totals.invoice_count or 0),
            "total_net": _safe_float(totals.net_total),
            "total_paid": _safe_float(totals.amount_paid),
            "total_balance_due": _safe_float(totals.balance_due),
        },
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_pharmacy_sales_summary(db: Session, user: User,
                                   filters: MISFilter) -> MISRawReportResult:
    """
    Pharmacy Sales Summary based on NEW PharmacySale / PharmacySaleItem models.
    - Totals from PharmacySale (net_amount)
    - Top 10 items from PharmacySaleItem grouped by item_name
    """
    start_dt, end_dt = _date_range(filters)

    # ✅ Use net_amount (amount + tax) and ignore cancelled bills
    total_row = (db.query(
        func.coalesce(func.sum(PharmacySale.net_amount),
                      0).label("total_amount"),
        func.count(PharmacySale.id).label("sale_count"),
    ).filter(
        PharmacySale.created_at >= start_dt,
        PharmacySale.created_at < end_dt,
        PharmacySale.invoice_status != "CANCELLED",
    ).one())

    # ✅ Top items from NEW PharmacySaleItem
    top_rows = (db.query(
        PharmacySaleItem.item_name.label("medicine"),
        func.coalesce(func.sum(PharmacySaleItem.quantity), 0).label("qty"),
        func.coalesce(func.sum(PharmacySaleItem.total_amount),
                      0).label("amount"),
    ).join(PharmacySale, PharmacySaleItem.sale_id == PharmacySale.id).filter(
        PharmacySale.created_at >= start_dt,
        PharmacySale.created_at < end_dt,
        PharmacySale.invoice_status != "CANCELLED",
    ).group_by(PharmacySaleItem.item_name).order_by(
        func.sum(PharmacySaleItem.quantity).desc()).limit(10).all())

    data_rows = [{
        "medicine": r.medicine,
        "qty": int(r.qty or 0),
        "amount": _safe_float(r.amount),
    } for r in top_rows]

    columns = [
        MISColumn(
            key="medicine",
            label="Medicine",
            type="string",
            align="left",
        ),
        MISColumn(key="qty", label="Quantity", type="number", align="right"),
        MISColumn(
            key="amount",
            label="Amount (INR)",
            type="number",
            align="right",
        ),
    ]

    chart = MISChartConfig(
        chart_type="bar",
        x_key="medicine",
        series=[{
            "key": "qty",
            "label": "Quantity Sold"
        }],
    )

    return MISRawReportResult(
        code="pharmacy.sales_summary",
        name="Pharmacy Sales Summary",
        group="Pharmacy",
        description="Top 10 medicines by quantity sold in the selected period.",
        filters_applied=filters,
        summary={
            "total_sales_amount": _safe_float(total_row.total_amount),
            "sale_count": int(total_row.sale_count or 0),
        },
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_lab_orders_summary(db: Session, user: User,
                               filters: MISFilter) -> MISRawReportResult:
    start_dt, end_dt = _date_range(filters)

    rows = (db.query(
        LisOrder.status.label("status"),
        func.count(LisOrder.id).label("count"),
    ).filter(
        LisOrder.created_at >= start_dt,
        LisOrder.created_at < end_dt,
    ).group_by(LisOrder.status).all())

    data_rows = [{
        "status": r.status or "Unknown",
        "count": int(r.count or 0)
    } for r in rows]

    columns = [
        MISColumn(key="status", label="Status", type="enum", align="left"),
        MISColumn(key="count", label="Orders", type="number", align="right"),
    ]

    chart = MISChartConfig(
        chart_type="bar",
        x_key="status",
        series=[{
            "key": "count",
            "label": "Orders"
        }],
    )

    total_orders = sum(r["count"] for r in data_rows)

    return MISRawReportResult(
        code="lab.orders_summary",
        name="Lab Orders Summary",
        group="Lab",
        description="Lab orders by status in the selected period.",
        filters_applied=filters,
        summary={"total_orders": total_orders},
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


def _report_ris_orders_summary(db: Session, user: User,
                               filters: MISFilter) -> MISRawReportResult:
    start_dt, end_dt = _date_range(filters)

    # Use the actual datetime field available on RisOrder.
    # If your model uses a different name (e.g. `requested_at`),
    # change BOTH lines below accordingly.
    rows = (
        db.query(
            RisOrder.status.label("status"),
            func.count(RisOrder.id).label("count"),
        ).filter(
            RisOrder.created_at >= start_dt,  # ✅ likely correct
            RisOrder.created_at < end_dt,
        ).group_by(RisOrder.status).all())

    data_rows = [{
        "status": r.status or "Unknown",
        "count": int(r.count or 0),
    } for r in rows]

    columns = [
        MISColumn(key="status", label="Status", type="enum", align="left"),
        MISColumn(key="count", label="Orders", type="number", align="right"),
    ]

    chart = MISChartConfig(
        chart_type="bar",
        x_key="status",
        series=[{
            "key": "count",
            "label": "Orders"
        }],
    )

    total_orders = sum(r["count"] for r in data_rows)

    return MISRawReportResult(
        code="radiology.orders_summary",
        name="Radiology Orders Summary",
        group="Radiology",
        description="Radiology orders by status in the selected period.",
        filters_applied=filters,
        summary={"total_orders": total_orders},
        columns=columns,
        rows=data_rows,
        chart=chart,
        meta={},
    )


# ---------- Definition registry ----------

MIS_DEFINITIONS: Dict[str, MISDefinition] = {
    "patient.registration_summary":
    MISDefinition(
        code="patient.registration_summary",
        name="Patient Registration Summary",
        group="Patient / Registration",
        description="New patient registrations per day.",
        required_perm_prefixes=["patients."],
        allowed_filters=["date_from", "date_to", "department_id"],
        run_fn=_report_patient_registration_summary,
    ),
    "opd.visit_summary":
    MISDefinition(
        code="opd.visit_summary",
        name="OPD Visit Summary (Doctor-wise)",
        group="OPD",
        description="Doctor-wise OPD visit counts.",
        required_perm_prefixes=["appointments.", "visits."],
        allowed_filters=["date_from", "date_to", "doctor_id"],
        run_fn=_report_opd_visit_summary,
    ),
    "ipd.admission_summary":
    MISDefinition(
        code="ipd.admission_summary",
        name="IPD Admission Summary",
        group="IPD",
        description="Daily IPD admissions in the selected period.",
        required_perm_prefixes=["ipd."],
        allowed_filters=["date_from", "date_to", "unit_id"],
        run_fn=_report_ipd_admission_summary,
    ),
    "billing.revenue_summary":
    MISDefinition(
        code="billing.revenue_summary",
        name="Revenue Summary (OPD/IPD)",
        group="Billing / Finance",
        description="Revenue by stream (OPD/IPD) with totals.",
        required_perm_prefixes=["billing."],
        allowed_filters=["date_from", "date_to", "context_type"],
        run_fn=_report_billing_revenue_summary,
    ),
    "billing.collection_summary":
    MISDefinition(
        code="billing.collection_summary",
        name="Collection Summary (Payment Mode)",
        group="Billing / Finance",
        description="Payment collections by mode (cash/card/UPI/credit).",
        required_perm_prefixes=["billing."],
        allowed_filters=["date_from", "date_to", "payment_mode"],
        run_fn=_report_billing_collection_summary,
    ),
    "pharmacy.sales_summary":
    MISDefinition(
        code="pharmacy.sales_summary",
        name="Pharmacy Sales Summary",
        group="Pharmacy",
        description="Top medicines and pharmacy sales totals.",
        required_perm_prefixes=["pharmacy."],
        allowed_filters=["date_from", "date_to"],
        run_fn=_report_pharmacy_sales_summary,
    ),
    "lab.orders_summary":
    MISDefinition(
        code="lab.orders_summary",
        name="Lab Orders Summary",
        group="Lab",
        description="Lab orders by status.",
        required_perm_prefixes=["lab.orders", "lab.results"],
        allowed_filters=["date_from", "date_to"],
        run_fn=_report_lab_orders_summary,
    ),
    "radiology.orders_summary":
    MISDefinition(
        code="radiology.orders_summary",
        name="Radiology Orders Summary",
        group="Radiology",
        description="Radiology orders by status.",
        required_perm_prefixes=["radiology.orders", "radiology.report"],
        allowed_filters=["date_from", "date_to"],
        run_fn=_report_ris_orders_summary,
    ),
}

# ---------- Public service API ----------


def list_definitions_for_user(user: User) -> List[MisDefinitionOut]:
    """
    Returns MIS report definitions the user is allowed to see.
    These are transformed into MisDefinitionOut (category, tags, filters metadata).
    """
    allowed: List[MisDefinitionOut] = []

    for defn in MIS_DEFINITIONS.values():
        if not _user_has_perm_prefix(user, defn.required_perm_prefixes):
            continue

        filter_defs = []
        for f in defn.allowed_filters:
            if f in {"date_from", "date_to"}:
                continue

            if f == "context_type":
                filter_defs.append({
                    "key":
                    "context_type",
                    "label":
                    "Stream (OPD / IPD)",
                    "type":
                    "select",
                    "required":
                    False,
                    "options": [
                        {
                            "value": "all",
                            "label": "All"
                        },
                        {
                            "value": "opd",
                            "label": "OPD"
                        },
                        {
                            "value": "ipd",
                            "label": "IPD"
                        },
                    ],
                })
            elif f == "payment_mode":
                filter_defs.append({
                    "key":
                    "payment_mode",
                    "label":
                    "Payment mode",
                    "type":
                    "select",
                    "required":
                    False,
                    "options": [
                        {
                            "value": "cash",
                            "label": "Cash"
                        },
                        {
                            "value": "card",
                            "label": "Card"
                        },
                        {
                            "value": "upi",
                            "label": "UPI"
                        },
                        {
                            "value": "credit",
                            "label": "Credit"
                        },
                    ],
                })
            elif f == "doctor_id":
                filter_defs.append({
                    "key": "doctor_id",
                    "label": "Doctor ID",
                    "type": "text",
                    "required": False,
                })
            elif f == "department_id":
                filter_defs.append({
                    "key": "department_id",
                    "label": "Department ID",
                    "type": "text",
                    "required": False,
                })
            elif f == "unit_id":
                filter_defs.append({
                    "key": "unit_id",
                    "label": "Unit / Ward ID",
                    "type": "text",
                    "required": False,
                })
            elif f == "patient_id":
                filter_defs.append({
                    "key": "patient_id",
                    "label": "Patient ID",
                    "type": "text",
                    "required": False,
                })

        allowed.append(
            MisDefinitionOut(
                code=defn.code,
                name=defn.name,
                category=defn.group,
                description=defn.description,
                tags=[],
                filters=filter_defs,
            ))

    allowed.sort(key=lambda d: ((d.category or ""), d.name))
    return allowed


def run_report(
    db: Session,
    user: User,
    code: str,
    filters_in: MISFilter,
) -> MISRawReportResult:
    """
    Executes a MIS report for given code + filters.
    """
    from fastapi import HTTPException

    if code not in MIS_DEFINITIONS:
        raise HTTPException(status_code=404,
                            detail=f"MIS report '{code}' not found.")

    defn = MIS_DEFINITIONS[code]

    if not _user_has_perm_prefix(user, defn.required_perm_prefixes):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view this report.",
        )

    sanitized_data: Dict[str, Any] = {}
    for field_name, value in filters_in.model_dump().items():
        if field_name in defn.allowed_filters:
            sanitized_data[field_name] = value

    filters = MISFilter(**sanitized_data)

    return defn.run_fn(db, user, filters)
