from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Appointment, Visit
from app.models.ipd import IpdAdmission, IpdBed
from app.models.pharmacy import (
    PharmacySale,
    PharmacySaleItem,
    PharmacyMedicine,
)
from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder
from app.models.ot import OtOrder
from app.models.billing import Invoice, InvoiceItem
from app.schemas.dashboard import DashboardDataResponse, DashboardWidget

# ---------- Helpers: time range ----------


def _dt_range(d_from: date, d_to: date) -> tuple[datetime, datetime]:
    """
    Convert date range [date_from, date_to] into datetime range [start, end).
    """
    start = datetime.combine(d_from, time.min)
    end = datetime.combine(d_to + timedelta(days=1), time.min)  # exclusive
    return start, end


def _safe_scalar(val):
    return float(val or 0)


# ---------- Helpers: permissions & roles ----------


def _get_role_for_dashboard(user: User, db: Session) -> str:
    """
    High-level dashboard role (admin/doctor/nurse/reception/lab/radiology/pharmacy/billing).
    """
    if user.is_admin:
        return "admin"

    perm_codes = set()
    for role in user.roles:
        for p in getattr(role, "permissions", []):
            perm_codes.add(p.code)

    def has(prefix: str) -> bool:
        return any(c.startswith(prefix) for c in perm_codes)

    if has("pharmacy."):
        return "pharmacy"
    if has("lab.") or has("orders.lab."):
        return "lab"
    if has("radiology.") or has("orders.ris."):
        return "radiology"
    if has("ipd.") or has("ipd.beds.") or has("ipd.packages."):
        return "nurse"
    if has("appointments.") or has("visits.") or has("opd."):
        return "doctor"

    return "admin"


def _collect_perm_codes(user: User) -> set[str]:
    """
    Flatten all permission codes for this user.
    """
    if user.is_admin:
        # Just a marker – we will treat admin as all capabilities = True
        return {"*"}
    codes: set[str] = set()
    for role in getattr(user, "roles", []):
        for p in getattr(role, "permissions", []):
            if getattr(p, "code", None):
                codes.add(p.code)
    return codes


def _build_capabilities(user: User) -> Dict[str, bool]:
    """
    Build a high-level capability map from permission codes.
    This is what we use to decide which widgets to show.

    Returned keys:
      - can_patients
      - can_opd
      - can_ipd
      - can_pharmacy
      - can_lab
      - can_radiology
      - can_ot
      - can_billing
    """
    if user.is_admin:
        return {
            "can_patients": True,
            "can_opd": True,
            "can_ipd": True,
            "can_pharmacy": True,
            "can_lab": True,
            "can_radiology": True,
            "can_ot": True,
            "can_billing": True,
        }

    codes = _collect_perm_codes(user)

    def has(prefix: str) -> bool:
        if "*" in codes:
            return True
        return any(c.startswith(prefix) for c in codes)

    can_opd = has("opd.") or has("appointments.") or has("visits.")
    can_ipd = has("ipd.") or has("ipd.beds.") or has("ipd.packages.")
    can_pharmacy = has("pharmacy.")
    can_lab = has("lab.") or has("orders.lab.")
    can_radiology = has("radiology.") or has("orders.ris.")
    can_ot = has("ot.")
    can_billing = has("billing.") or has("invoices.")

    # Patients: if you can see OPD, IPD, or have anything clinical, you likely can see patient counts.
    can_patients = can_opd or can_ipd or can_lab or can_radiology or can_pharmacy or can_billing

    return {
        "can_patients": can_patients,
        "can_opd": can_opd,
        "can_ipd": can_ipd,
        "can_pharmacy": can_pharmacy,
        "can_lab": can_lab,
        "can_radiology": can_radiology,
        "can_ot": can_ot,
        "can_billing": can_billing,
    }


def _filter_widgets_by_perm(
    widgets: List[DashboardWidget],
    caps: Dict[str, bool],
) -> List[DashboardWidget]:
    """
    Filter widgets list based on capabilities.
    Mapping is based on widget.code patterns.
    """
    out: List[DashboardWidget] = []

    for w in widgets:
        code = w.code

        # PATIENTS / GENERIC metrics:
        if code.startswith("metric_new_patients"):
            if not caps.get("can_patients", False):
                continue

        # OPD / clinical flow
        if code in {"metric_opd_visits", "appointment_status"}:
            if not caps.get("can_opd", False):
                continue

        if code == "patient_flow":
            # Need at least some clinical access (OPD or IPD or patients)
            if not (caps.get("can_opd", False) or caps.get("can_ipd", False)
                    or caps.get("can_patients", False)):
                continue

        # IPD
        if code in {
                "metric_ipd_admissions",
                "ipd_bed_occupancy",
                "ipd_status",
                "recent_ipd_admissions",
        }:
            if not caps.get("can_ipd", False):
                continue

        # PHARMACY
        if code in {
                "revenue_pharmacy",
                "top_medicines",
                "payment_modes",
        }:
            if not caps.get("can_pharmacy", False):
                continue

        # LAB
        if code in {"revenue_lab", "top_lab_tests"}:
            if not caps.get("can_lab", False):
                continue

        # RADIOLOGY
        if code in {"revenue_radiology", "top_radiology_tests"}:
            if not caps.get("can_radiology", False):
                continue

        # OT
        if code == "revenue_ot":
            if not (caps.get("can_ot", False) or caps.get("can_ipd", False)):
                continue

        # BILLING & GLOBAL REVENUE
        if code in {
                "revenue_total",
                "revenue_pending",
                "billing_summary",
                "revenue_opd",
                "revenue_ipd",
                "revenue_by_stream",
        }:
            if not caps.get("can_billing", False):
                continue

        # If nothing blocked, keep widget
        out.append(w)

    return out


# ---------- Core builder ----------


def build_dashboard_for_user(
    db: Session,
    user: User,
    date_from: date,
    date_to: date,
) -> DashboardDataResponse:
    start_dt, end_dt = _dt_range(date_from, date_to)
    role = _get_role_for_dashboard(user, db)
    caps = _build_capabilities(user)

    widgets: List[DashboardWidget] = []

    # 1. High-level patient & visit metrics
    widgets.extend(_build_patient_and_visit_metrics(db, start_dt, end_dt))

    # 2. Revenues (OPD/IPD/Pharmacy/Lab/Radiology/OT/Total + pending)
    revenue_data = _build_revenue_metrics(db, start_dt, end_dt)
    widgets.extend(revenue_data["widgets"])

    # 3. Bed occupancy snapshot (IPD)
    widgets.append(_build_ipd_bed_occupancy_widget(db))

    # 4. Top 10 ordered / sold medicines
    widgets.append(_build_top_medicines_widget(db, start_dt, end_dt))

    # 5. Patient flow chart (last 7 days)
    widgets.append(_build_patient_flow_chart(db, date_to))

    # 6. Revenue by stream chart
    widgets.append(_build_revenue_stream_chart(revenue_data["streams"]))

    # 7. Recent admissions
    widgets.append(_build_recent_admissions_widget(db))

    # 8. Appointment status split
    widgets.append(_build_appointment_status_widget(db, date_from, date_to))

    # 9. IPD status split
    widgets.append(_build_ipd_status_widget(db, start_dt, end_dt))

    # 10. Payment mode usage (cash / UPI / card / on-account)
    widgets.append(_build_payment_mode_widget(db, start_dt, end_dt))

    # 11. Top 5 lab & radiology tests
    lab_widget, ris_widget = _build_top_tests_widgets(db, start_dt, end_dt)
    widgets.append(lab_widget)
    widgets.append(ris_widget)

    # 12. Billed vs pending amount
    widgets.append(_build_billing_summary_widget(db, start_dt, end_dt))

    # --- Permission-based filtering ---
    widgets = _filter_widgets_by_perm(widgets, caps)

    return DashboardDataResponse(
        role=role,
        date_from=date_from,
        date_to=date_to,
        # Put caps inside filters so frontend can also know what modules user has
        filters={"caps": caps},
        widgets=widgets,
    )


# ---------- Metrics widgets ----------


def _build_patient_and_visit_metrics(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> List[DashboardWidget]:
    total_patients = (db.query(func.count(Patient.id)).filter(
        Patient.created_at >= start_dt, Patient.created_at < end_dt).scalar()
                      or 0)

    opd_visits = (db.query(func.count(Visit.id)).filter(
        Visit.visit_at >= start_dt, Visit.visit_at < end_dt).scalar() or 0)

    ipd_admissions = (db.query(func.count(IpdAdmission.id)).filter(
        IpdAdmission.admitted_at >= start_dt, IpdAdmission.admitted_at
        < end_dt).scalar() or 0)

    return [
        DashboardWidget(
            code="metric_new_patients",
            title="New Patients",
            widget_type="metric",
            description="Patients registered in selected period",
            data=int(total_patients),
        ),
        DashboardWidget(
            code="metric_opd_visits",
            title="OPD Visits",
            widget_type="metric",
            description="OPD visits completed in selected period",
            data=int(opd_visits),
        ),
        DashboardWidget(
            code="metric_ipd_admissions",
            title="IPD Admissions",
            widget_type="metric",
            description="IPD admissions in selected period",
            data=int(ipd_admissions),
        ),
    ]


def _build_revenue_metrics(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> Dict[str, any]:
    # Invoice-based revenue (OPD/IPD + lab/radiology/ot inside items)
    base_invoices = (db.query(Invoice).filter(
        Invoice.status == "finalized",
        Invoice.finalized_at >= start_dt,
        Invoice.finalized_at < end_dt,
    ).subquery())

    # Total invoice revenue (OPD+IPD+services)
    total_invoice_rev = (db.query(
        func.coalesce(func.sum(base_invoices.c.net_total), 0)).scalar() or 0)

    # OPD / IPD revenue via context_type
    opd_rev = (db.query(func.coalesce(
        func.sum(base_invoices.c.net_total),
        0)).filter(base_invoices.c.context_type == "opd").scalar() or 0)
    ipd_rev = (db.query(func.coalesce(
        func.sum(base_invoices.c.net_total),
        0)).filter(base_invoices.c.context_type == "ipd").scalar() or 0)

    # Pharmacy revenue via PharmacySale
    pharmacy_rev = (db.query(
        func.coalesce(func.sum(PharmacySale.total_amount), 0)).filter(
            PharmacySale.created_at >= start_dt,
            PharmacySale.created_at < end_dt,
        ).scalar() or 0)

    # Lab / Radiology / OT revenue via InvoiceItem.service_type
    base_items = (
        db.query(
            InvoiceItem.service_type,
            func.coalesce(func.sum(InvoiceItem.line_total), 0).label("amount"),
        ).join(Invoice, InvoiceItem.invoice_id == Invoice.id).filter(
            Invoice.status == "finalized",
            Invoice.finalized_at >= start_dt,
            Invoice.finalized_at < end_dt,
            InvoiceItem.is_voided == False,  # noqa: E712
        ).group_by(InvoiceItem.service_type).all())

    lab_rev = 0
    radiology_rev = 0
    ot_rev = 0

    for row in base_items:
        if row.service_type == "lab":
            lab_rev = row.amount
        elif row.service_type == "radiology":
            radiology_rev = row.amount
        elif row.service_type == "ot":
            ot_rev = row.amount

    # Pending amount (rough view = all non-finalized invoices)
    pending_invoice_total = (db.query(
        func.coalesce(func.sum(Invoice.net_total),
                      0)).filter(Invoice.status != "finalized").scalar() or 0)

    total_revenue = _safe_scalar(total_invoice_rev) + _safe_scalar(
        pharmacy_rev)

    widgets: List[DashboardWidget] = [
        DashboardWidget(
            code="revenue_total",
            title="Total Revenue",
            widget_type="metric",
            description=
            "Total revenue (all finalized invoices + pharmacy) in selected period",
            data=_safe_scalar(total_revenue),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_opd",
            title="OPD Revenue",
            widget_type="metric",
            description="Invoice revenue attributed to OPD",
            data=_safe_scalar(opd_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_ipd",
            title="IPD Revenue",
            widget_type="metric",
            description="Invoice revenue attributed to IPD",
            data=_safe_scalar(ipd_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_pharmacy",
            title="Pharmacy Revenue",
            widget_type="metric",
            description="Pharmacy sales amount",
            data=_safe_scalar(pharmacy_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_lab",
            title="Lab Revenue",
            widget_type="metric",
            description="Revenue from laboratory services",
            data=_safe_scalar(lab_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_radiology",
            title="Radiology Revenue",
            widget_type="metric",
            description="Revenue from radiology services",
            data=_safe_scalar(radiology_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_ot",
            title="OT Revenue",
            widget_type="metric",
            description="Revenue from OT services",
            data=_safe_scalar(ot_rev),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="revenue_pending",
            title="Pending Bill Amount",
            widget_type="metric",
            description="Total value of open (non-finalized) invoices",
            data=_safe_scalar(pending_invoice_total),
            config={"currency": "INR"},
        ),
    ]

    streams = {
        "opd": _safe_scalar(opd_rev),
        "ipd": _safe_scalar(ipd_rev),
        "pharmacy": _safe_scalar(pharmacy_rev),
        "lab": _safe_scalar(lab_rev),
        "radiology": _safe_scalar(radiology_rev),
        "ot": _safe_scalar(ot_rev),
    }

    return {"widgets": widgets, "streams": streams}


def _build_ipd_bed_occupancy_widget(db: Session) -> DashboardWidget:
    total_beds = db.query(func.count(IpdBed.id)).scalar() or 0
    occupied_beds = (db.query(func.count(IpdBed.id)).filter(
        IpdBed.state.in_(["occupied", "preoccupied"])).scalar() or 0)
    available = total_beds - occupied_beds if total_beds else 0
    occupancy_pct = (occupied_beds / total_beds * 100.0) if total_beds else 0.0

    return DashboardWidget(
        code="ipd_bed_occupancy",
        title="IPD Bed Occupancy",
        widget_type="chart",
        description="Current bed occupancy snapshot (all wards)",
        data={
            "total": int(total_beds),
            "occupied": int(occupied_beds),
            "available": int(available),
            "occupancy_pct": round(occupancy_pct, 1),
        },
        config={"chart_type": "donut"},
    )


def _build_top_medicines_widget(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> DashboardWidget:
    rows = (db.query(
        PharmacyMedicine.name.label("medicine"),
        func.coalesce(func.sum(PharmacySaleItem.qty), 0).label("qty"),
        func.coalesce(func.sum(PharmacySaleItem.amount), 0).label("amount"),
    ).join(PharmacySale, PharmacySaleItem.sale_id == PharmacySale.id).join(
        PharmacyMedicine,
        PharmacySaleItem.medicine_id == PharmacyMedicine.id).filter(
            PharmacySale.created_at >= start_dt,
            PharmacySale.created_at < end_dt,
        ).group_by(PharmacyMedicine.id, PharmacyMedicine.name).order_by(
            func.sum(PharmacySaleItem.qty).desc()).limit(10).all())

    data = [{
        "medicine": r.medicine,
        "quantity": int(r.qty or 0),
        "amount": float(r.amount or 0),
    } for r in rows]

    return DashboardWidget(
        code="top_medicines",
        title="Top 10 Medicines (Dispensed)",
        widget_type="chart",
        description="Most frequently dispensed medicines in selected period",
        data=[{
            "label": r["medicine"],
            "value": r["quantity"]
        } for r in data],
        config={"chart_type": "bar"},
    )


def _build_patient_flow_chart(db: Session, end_date: date) -> DashboardWidget:
    """
    Patient flow chart for last 7 days (including end_date).
    Data points per day:
      - new_patients
      - opd_visits
      - ipd_admissions
    """
    start_date = end_date - timedelta(days=6)
    start_dt, end_dt = _dt_range(start_date, end_date)

    patient_rows = (db.query(
        func.date(Patient.created_at).label("d"),
        func.count(Patient.id).label("c"),
    ).filter(
        Patient.created_at >= start_dt,
        Patient.created_at < end_dt,
    ).group_by(func.date(Patient.created_at)).all())
    patient_map = {r.d: r.c for r in patient_rows}

    visit_rows = (db.query(
        func.date(Visit.visit_at).label("d"),
        func.count(Visit.id).label("c"),
    ).filter(
        Visit.visit_at >= start_dt,
        Visit.visit_at < end_dt,
    ).group_by(func.date(Visit.visit_at)).all())
    visit_map = {r.d: r.c for r in visit_rows}

    adm_rows = (db.query(
        func.date(IpdAdmission.admitted_at).label("d"),
        func.count(IpdAdmission.id).label("c"),
    ).filter(
        IpdAdmission.admitted_at >= start_dt,
        IpdAdmission.admitted_at < end_dt,
    ).group_by(func.date(IpdAdmission.admitted_at)).all())
    adm_map = {r.d: r.c for r in adm_rows}

    data = []
    for i in range(7):
        d = start_date + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        data.append({
            "date": ds,
            "new_patients": int(patient_map.get(d, 0)),
            "opd_visits": int(visit_map.get(d, 0)),
            "ipd_admissions": int(adm_map.get(d, 0)),
        })

    return DashboardWidget(
        code="patient_flow",
        title="Patient Flow (Last 7 Days)",
        widget_type="chart",
        description=
        "Daily trend of new patients, OPD visits and IPD admissions",
        data=data,
        config={
            "chart_type":
            "multi_bar",
            "x_key":
            "date",
            "series": [
                {
                    "key": "new_patients",
                    "label": "New Patients"
                },
                {
                    "key": "opd_visits",
                    "label": "OPD Visits"
                },
                {
                    "key": "ipd_admissions",
                    "label": "IPD Admissions"
                },
            ],
        },
    )


def _build_revenue_stream_chart(streams: Dict[str, float]) -> DashboardWidget:
    data = [
        {
            "label": "OPD",
            "value": streams.get("opd", 0)
        },
        {
            "label": "IPD",
            "value": streams.get("ipd", 0)
        },
        {
            "label": "Pharmacy",
            "value": streams.get("pharmacy", 0)
        },
        {
            "label": "Lab",
            "value": streams.get("lab", 0)
        },
        {
            "label": "Radiology",
            "value": streams.get("radiology", 0)
        },
        {
            "label": "OT",
            "value": streams.get("ot", 0)
        },
    ]
    return DashboardWidget(
        code="revenue_by_stream",
        title="Revenue by Stream",
        widget_type="chart",
        description="Breakdown of revenue by service stream",
        data=data,
        config={"chart_type": "bar"},
    )


def _build_recent_admissions_widget(db: Session) -> DashboardWidget:
    from app.models.patient import Patient as PatientModel  # avoid circular

    rows = (db.query(
        IpdAdmission.id,
        IpdAdmission.admission_code,
        IpdAdmission.admitted_at,
        IpdAdmission.status,
        PatientModel.uhid,
        PatientModel.first_name,
        PatientModel.last_name,
    ).join(PatientModel, IpdAdmission.patient_id == PatientModel.id).order_by(
        IpdAdmission.admitted_at.desc()).limit(10).all())

    data = [{
        "ipd_id": r.id,
        "admission_code": r.admission_code or f"IP-{r.id:06d}",
        "admitted_at": r.admitted_at.isoformat() if r.admitted_at else None,
        "status": r.status,
        "uhid": r.uhid,
        "patient_name": f"{r.first_name} {r.last_name or ''}".strip(),
    } for r in rows]

    return DashboardWidget(
        code="recent_ipd_admissions",
        title="Recent IPD Admissions",
        widget_type="table",  # frontend renders as cards
        description="Last 10 IPD admissions",
        data=data,
        config={
            "columns": [
                "admission_code",
                "uhid",
                "patient_name",
                "admitted_at",
                "status",
            ]
        },
    )


# ---------- NEW WIDGETS ----------


def _build_appointment_status_widget(
    db: Session,
    d_from: date,
    d_to: date,
) -> DashboardWidget:
    rows = (db.query(
        Appointment.status,
        func.count(Appointment.id).label("count"),
    ).filter(
        Appointment.date >= d_from,
        Appointment.date <= d_to,
    ).group_by(Appointment.status).all())

    data = [{
        "label": (r.status or "unknown").title(),
        "value": int(r.count or 0),
    } for r in rows]

    return DashboardWidget(
        code="appointment_status",
        title="OPD Appointments by Status",
        widget_type="chart",
        description=
        "Booked / checked-in / completed / cancelled / no-show distribution",
        data=data,
        config={"chart_type": "pie"},
    )


def _build_ipd_status_widget(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> DashboardWidget:
    rows = (db.query(
        IpdAdmission.status,
        func.count(IpdAdmission.id).label("count"),
    ).filter(
        IpdAdmission.admitted_at >= start_dt,
        IpdAdmission.admitted_at < end_dt,
    ).group_by(IpdAdmission.status).all())

    data = [{
        "label": (r.status or "unknown").title(),
        "value": int(r.count or 0),
    } for r in rows]

    return DashboardWidget(
        code="ipd_status",
        title="IPD Cases by Status",
        widget_type="chart",
        description="Admitted / discharged / LAMA / cancelled etc.",
        data=data,
        config={"chart_type": "pie"},
    )


def _build_payment_mode_widget(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> DashboardWidget:
    """
    Income type chart: which payment mode is used more (cash / UPI / card / on-account).
    Uses PharmacySale.payment_mode for now.
    """
    rows = (db.query(
        PharmacySale.payment_mode,
        func.coalesce(func.sum(PharmacySale.total_amount), 0).label("amount"),
    ).filter(
        PharmacySale.created_at >= start_dt,
        PharmacySale.created_at < end_dt,
    ).group_by(PharmacySale.payment_mode).all())

    data = [{
        "label": (r.payment_mode or "unknown").replace("_", " ").title(),
        "value": float(r.amount or 0),
    } for r in rows]

    return DashboardWidget(
        code="payment_modes",
        title="Payment Modes (Pharmacy)",
        widget_type="chart",
        description="Cash vs UPI vs card vs on-account usage",
        data=data,
        config={"chart_type": "pie"},
    )


def _build_top_tests_widgets(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[DashboardWidget, DashboardWidget]:
    # Lab tests
    lab_rows = (db.query(
        LisOrderItem.test_name.label("name"),
        func.count(LisOrderItem.id).label("count"),
    ).join(LisOrder, LisOrderItem.order_id == LisOrder.id).filter(
        LisOrder.created_at >= start_dt,
        LisOrder.created_at < end_dt,
        LisOrder.status != "cancelled",
    ).group_by(LisOrderItem.test_name).order_by(
        func.count(LisOrderItem.id).desc()).limit(5).all())

    lab_data = [{
        "label": r.name,
        "value": int(r.count or 0)
    } for r in lab_rows]

    lab_widget = DashboardWidget(
        code="top_lab_tests",
        title="Top 5 Lab Tests",
        widget_type="chart",
        description="Most frequently ordered lab investigations",
        data=lab_data,
        config={"chart_type": "bar"},
    )

    # Radiology tests
    ris_rows = (db.query(
        RisOrder.test_name.label("name"),
        func.count(RisOrder.id).label("count"),
    ).filter(
        RisOrder.created_at >= start_dt,
        RisOrder.created_at < end_dt,
        RisOrder.status != "cancelled",
    ).group_by(RisOrder.test_name).order_by(func.count(
        RisOrder.id).desc()).limit(5).all())

    ris_data = [{
        "label": r.name,
        "value": int(r.count or 0)
    } for r in ris_rows]

    ris_widget = DashboardWidget(
        code="top_radiology_tests",
        title="Top 5 Radiology Tests",
        widget_type="chart",
        description="Most frequently ordered imaging tests",
        data=ris_data,
        config={"chart_type": "bar"},
    )

    return lab_widget, ris_widget


def _build_billing_summary_widget(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> DashboardWidget:
    """
    Billed vs pending.
    Billed = finalized invoices in range.
    Pending = all non-finalized invoices (overall snapshot).
    """
    billed_amount = (db.query(func.coalesce(func.sum(
        Invoice.net_total), 0)).filter(
            Invoice.status == "finalized",
            Invoice.finalized_at >= start_dt,
            Invoice.finalized_at < end_dt,
        ).scalar() or 0)

    pending_amount = (db.query(func.coalesce(func.sum(
        Invoice.net_total), 0)).filter(Invoice.status != "finalized").scalar()
                      or 0)

    data = [
        {
            "label": "Billed (finalized)",
            "value": _safe_scalar(billed_amount)
        },
        {
            "label": "Pending (open)",
            "value": _safe_scalar(pending_amount)
        },
    ]

    return DashboardWidget(
        code="billing_summary",
        title="Billing Summary (Billed vs Pending)",
        widget_type="chart",
        description="How much is already billed vs still pending",
        data=data,
        config={"chart_type": "bar"},
    )
