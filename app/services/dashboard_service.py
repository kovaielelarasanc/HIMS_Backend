# FILE: app/services/dashboard_service.py
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple, Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Appointment, Visit
from app.models.ipd import IpdAdmission, IpdBed

from app.models.pharmacy_prescription import PharmacySale, PharmacySaleItem
from app.models.lis import LisOrder, LisOrderItem
from app.models.ris import RisOrder

# ✅ USE YOUR BILLING MODELS (not old Invoice/Payment)
from app.models.billing import (
    BillingCase,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPayment,
    EncounterType,
    DocStatus,
    ServiceGroup,
)

from app.schemas.dashboard import DashboardDataResponse, DashboardWidget


# ---------- Helpers: time range ----------
def _dt_range(d_from: date, d_to: date) -> Tuple[datetime, datetime]:
    """
    Convert date range [date_from, date_to] into datetime range [start, end).
    """
    start = datetime.combine(d_from, time.min)
    end = datetime.combine(d_to + timedelta(days=1), time.min)  # exclusive
    return start, end


def _safe_scalar(val: Any) -> float:
    try:
        return float(val or 0)
    except Exception:
        return 0.0


def _invoice_posted_dt_expr():
    # POSTED date is main revenue recognition; fallback to created_at for safety
    return func.coalesce(BillingInvoice.posted_at, BillingInvoice.created_at)


def _sg(*names: str):
    """Safe ServiceGroup resolver (handles enum naming differences)."""
    for n in names:
        if hasattr(ServiceGroup, n):
            return getattr(ServiceGroup, n)
    return None


# ---------- Helpers: permissions & roles ----------
def _get_role_for_dashboard(user: User) -> str:
    if getattr(user, "is_admin", False):
        return "admin"

    perm_codes = set()
    for role in getattr(user, "roles", []) or []:
        for p in getattr(role, "permissions", []) or []:
            code = getattr(p, "code", None)
            if code:
                perm_codes.add(code)

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
    if has("billing.") or has("invoices."):
        return "billing"

    return "admin"


def _collect_perm_codes(user: User) -> set[str]:
    if getattr(user, "is_admin", False):
        return {"*"}
    codes: set[str] = set()
    for role in getattr(user, "roles", []) or []:
        for p in getattr(role, "permissions", []) or []:
            code = getattr(p, "code", None)
            if code:
                codes.add(code)
    return codes


def _build_capabilities(user: User) -> Dict[str, bool]:
    if getattr(user, "is_admin", False):
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


def _filter_widgets_by_perm(widgets: List[DashboardWidget],
                            caps: Dict[str, bool]) -> List[DashboardWidget]:
    out: List[DashboardWidget] = []

    for w in widgets:
        code = w.code

        # PATIENTS
        if code.startswith("metric_new_patients") and not caps.get(
                "can_patients", False):
            continue

        # OPD
        if code in {"metric_opd_visits", "appointment_status"
                    } and not caps.get("can_opd", False):
            continue

        if code == "patient_flow" and not (caps.get("can_opd")
                                           or caps.get("can_ipd")
                                           or caps.get("can_patients")):
            continue

        # IPD
        if code in {
                "metric_ipd_admissions", "ipd_bed_occupancy", "ipd_status",
                "recent_ipd_admissions"
        } and not caps.get("can_ipd", False):
            continue

        # PHARMACY
        if code in {"top_medicines"} and not caps.get("can_pharmacy", False):
            continue

        # LAB
        if code in {"top_lab_tests"} and not caps.get("can_lab", False):
            continue

        # RADIOLOGY
        if code in {"top_radiology_tests"
                    } and not caps.get("can_radiology", False):
            continue

        # BILLING (Revenue/Collections)
        if code.startswith("rev_") and not caps.get("can_billing", False):
            continue
        if code in {"payment_modes", "billing_summary"
                    } and not caps.get("can_billing", False):
            continue

        out.append(w)

    return out


# ---------- Core builder ----------
def build_dashboard_for_user(db: Session, user: User, date_from: date,
                             date_to: date) -> DashboardDataResponse:
    start_dt, end_dt = _dt_range(date_from, date_to)
    role = _get_role_for_dashboard(user)
    caps = _build_capabilities(user)

    widgets: List[DashboardWidget] = []

    widgets.extend(_build_patient_and_visit_metrics(db, start_dt, end_dt))

    revenue_data = _build_revenue_metrics_v2(db, start_dt, end_dt)
    widgets.extend(revenue_data["widgets"])

    # (keep your existing ops widgets)
    widgets.append(_build_ipd_bed_occupancy_widget(db))
    widgets.append(_build_top_medicines_widget(db, start_dt, end_dt))
    widgets.append(_build_patient_flow_chart(db, date_to))
    widgets.append(_build_revenue_stream_chart(revenue_data["streams"]))
    widgets.append(_build_revenue_doctor_chart(revenue_data["top_doctors"]))
    widgets.append(_build_recent_admissions_widget(db))
    widgets.append(_build_appointment_status_widget(db, date_from, date_to))
    widgets.append(_build_ipd_status_widget(db, start_dt, end_dt))
    widgets.append(_build_payment_mode_widget_v2(db, start_dt, end_dt))
    lab_widget, ris_widget = _build_top_tests_widgets(db, start_dt, end_dt)
    widgets.append(lab_widget)
    widgets.append(ris_widget)
    widgets.append(_build_billing_summary_widget_v2(db, start_dt, end_dt))

    widgets = _filter_widgets_by_perm(widgets, caps)

    return DashboardDataResponse(
        role=role,
        date_from=date_from,
        date_to=date_to,
        filters={"caps": caps},
        widgets=widgets,
    )


# ---------- Widgets ----------
def _build_patient_and_visit_metrics(
        db: Session, start_dt: datetime,
        end_dt: datetime) -> List[DashboardWidget]:
    total_patients = db.query(func.count(Patient.id)).filter(
        Patient.created_at >= start_dt, Patient.created_at
        < end_dt).scalar() or 0
    opd_visits = db.query(func.count(Visit.id)).filter(
        Visit.visit_at >= start_dt, Visit.visit_at < end_dt).scalar() or 0
    ipd_admissions = db.query(func.count(IpdAdmission.id)).filter(
        IpdAdmission.admitted_at >= start_dt, IpdAdmission.admitted_at
        < end_dt).scalar() or 0

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


def _build_revenue_metrics_v2(db: Session, start_dt: datetime,
                              end_dt: datetime) -> Dict[str, Any]:
    """
    ✅ Correct revenue engine for your schema.

    Revenue recognition:
      - Billed Revenue = POSTED invoices (posted_at in date range)
      - Collections    = payments received (received_at in date range)
      - Outstanding(A/R) = sum(invoice.grand_total - paid_to_date) as of end_dt
    Breakdown:
      - OP vs IP by BillingCase.encounter_type
      - Stream by BillingInvoiceLine.service_group
      - Doctor-wise by BillingInvoiceLine.doctor_id
    """

    posted_dt = _invoice_posted_dt_expr()

    # ----------------------------
    # Billed (POSTED) - Total
    # ----------------------------
    billed_total = db.query(
        func.coalesce(func.sum(BillingInvoice.grand_total), 0)).filter(
            BillingInvoice.status == DocStatus.POSTED,
            posted_dt >= start_dt,
            posted_dt < end_dt,
        ).scalar() or 0

    # ----------------------------
    # OP/IP billed using BillingCase.encounter_type
    # ----------------------------
    def _billed_by_encounter(enc: EncounterType) -> float:
        return _safe_scalar(
            db.query(func.coalesce(func.sum(
                BillingInvoice.grand_total), 0)).join(
                    BillingCase,
                    BillingInvoice.billing_case_id == BillingCase.id).filter(
                        BillingInvoice.status == DocStatus.POSTED,
                        posted_dt >= start_dt,
                        posted_dt < end_dt,
                        BillingCase.encounter_type == enc,
                    ).scalar())

    billed_op = _billed_by_encounter(EncounterType.OP)
    billed_ip = _billed_by_encounter(EncounterType.IP)

    # ----------------------------
    # Stream-wise billed using invoice lines (more accurate split)
    # ----------------------------
    sg_lab = _sg("LAB")
    sg_rad = _sg("RAD", "RADIOLOGY")
    sg_pharm = _sg("PHARM", "PHARMACY")
    sg_ot = _sg("OT")

    sg_rows = (db.query(
        BillingInvoiceLine.service_group.label("sg"),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0).label("amt"),
    ).join(BillingInvoice,
           BillingInvoiceLine.invoice_id == BillingInvoice.id).filter(
               BillingInvoice.status == DocStatus.POSTED,
               posted_dt >= start_dt,
               posted_dt < end_dt,
           ).group_by(BillingInvoiceLine.service_group).all())

    streams: Dict[str, float] = {
        "op": _safe_scalar(billed_op),
        "ip": _safe_scalar(billed_ip),
        "lab": 0.0,
        "radiology": 0.0,
        "pharmacy": 0.0,
        "ot": 0.0,
        "misc": 0.0,
    }

    # map enum -> labels
    for r in sg_rows:
        sg = r.sg
        amt = _safe_scalar(r.amt)
        if sg_lab is not None and sg == sg_lab:
            streams["lab"] += amt
        elif sg_rad is not None and sg == sg_rad:
            streams["radiology"] += amt
        elif sg_pharm is not None and sg == sg_pharm:
            streams["pharmacy"] += amt
        elif sg_ot is not None and sg == sg_ot:
            streams["ot"] += amt
        else:
            streams["misc"] += amt

    # ----------------------------
    # Collections (payments received) - date range
    # ----------------------------
    collected_total = db.query(
        func.coalesce(func.sum(BillingPayment.amount), 0)).filter(
            BillingPayment.received_at >= start_dt,
            BillingPayment.received_at < end_dt,
        ).scalar() or 0

    # ----------------------------
    # Outstanding (A/R) as of end_dt
    # invoices posted before end_dt minus payments received before end_dt
    # ----------------------------
    paid_sq = (db.query(
        BillingPayment.invoice_id.label("invoice_id"),
        func.coalesce(func.sum(BillingPayment.amount), 0).label("paid"),
    ).filter(
        BillingPayment.invoice_id.isnot(None),
        BillingPayment.received_at < end_dt,
    ).group_by(BillingPayment.invoice_id).subquery())

    outstanding_total = db.query(
        func.coalesce(
            func.sum(BillingInvoice.grand_total -
                     func.coalesce(paid_sq.c.paid, 0)), 0)).outerjoin(
                         paid_sq, paid_sq.c.invoice_id
                         == BillingInvoice.id).filter(
                             BillingInvoice.status == DocStatus.POSTED,
                             posted_dt < end_dt,
                         ).scalar() or 0

    # ----------------------------
    # Doctor-wise revenue (Top 10) for POSTED invoices in range
    # ----------------------------
    doc_rows = (db.query(
        BillingInvoiceLine.doctor_id.label("doctor_id"),
        func.coalesce(func.sum(BillingInvoiceLine.net_amount), 0).label("amt"),
    ).join(BillingInvoice,
           BillingInvoiceLine.invoice_id == BillingInvoice.id).filter(
               BillingInvoice.status == DocStatus.POSTED,
               posted_dt >= start_dt,
               posted_dt < end_dt,
               BillingInvoiceLine.doctor_id.isnot(None),
           ).group_by(BillingInvoiceLine.doctor_id).order_by(
               desc("amt")).limit(10).all())

    doctor_ids = [int(r.doctor_id) for r in doc_rows if r.doctor_id]
    users = db.query(User).filter(
        User.id.in_(doctor_ids)).all() if doctor_ids else []
    umap: Dict[int, str] = {}
    for u in users:
        # robust name fallback
        nm = (getattr(u, "full_name", None) or getattr(u, "name", None)
              or (" ".join([
                  str(getattr(u, "first_name", "") or "").strip(),
                  str(getattr(u, "last_name", "") or "").strip()
              ]).strip()) or getattr(u, "username", None)
              or getattr(u, "email", None)
              or f"Doctor #{getattr(u, 'id', '')}")
        umap[int(u.id)] = (nm or f"Doctor #{u.id}")

    top_doctors = [{
        "doctor_id":
        int(r.doctor_id),
        "doctor_name":
        umap.get(int(r.doctor_id), f"Doctor #{int(r.doctor_id)}"),
        "amount":
        _safe_scalar(r.amt),
    } for r in doc_rows]

    # ----------------------------
    # Widgets
    # ----------------------------
    widgets: List[DashboardWidget] = [
        DashboardWidget(
            code="rev_billed_total",
            title="Billed Revenue",
            widget_type="metric",
            description="Sum of POSTED invoices (posted_at in selected range)",
            data=_safe_scalar(billed_total),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_collected_total",
            title="Collections",
            widget_type="metric",
            description="Payments received (received_at in selected range)",
            data=_safe_scalar(collected_total),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_outstanding_total",
            title="Outstanding (A/R)",
            widget_type="metric",
            description=
            "As of end date: POSTED invoice total - payments received till end date",
            data=_safe_scalar(outstanding_total),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_op_billed",
            title="OP Revenue (Billed)",
            widget_type="metric",
            description="POSTED invoices for OP billing cases",
            data=_safe_scalar(billed_op),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_ip_billed",
            title="IP Revenue (Billed)",
            widget_type="metric",
            description="POSTED invoices for IP billing cases",
            data=_safe_scalar(billed_ip),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_stream_pharmacy",
            title="Pharmacy Revenue",
            widget_type="metric",
            description=
            "Sum of POSTED invoice lines (service_group=PHARM/PHARMACY)",
            data=_safe_scalar(streams.get("pharmacy", 0)),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_stream_lab",
            title="Lab Revenue",
            widget_type="metric",
            description="Sum of POSTED invoice lines (service_group=LAB)",
            data=_safe_scalar(streams.get("lab", 0)),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_stream_radiology",
            title="Radiology Revenue",
            widget_type="metric",
            description=
            "Sum of POSTED invoice lines (service_group=RAD/RADIOLOGY)",
            data=_safe_scalar(streams.get("radiology", 0)),
            config={"currency": "INR"},
        ),
        DashboardWidget(
            code="rev_stream_ot",
            title="OT Revenue",
            widget_type="metric",
            description="Sum of POSTED invoice lines (service_group=OT)",
            data=_safe_scalar(streams.get("ot", 0)),
            config={"currency": "INR"},
        ),
    ]

    return {
        "widgets": widgets,
        "streams": streams,
        "top_doctors": top_doctors,
    }


def _build_ipd_bed_occupancy_widget(db: Session) -> DashboardWidget:
    total_beds = db.query(func.count(IpdBed.id)).scalar() or 0
    occupied_beds = db.query(func.count(IpdBed.id)).filter(
        IpdBed.state.in_(["occupied", "preoccupied"])).scalar() or 0
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


def _build_top_medicines_widget(db: Session, start_dt: datetime,
                                end_dt: datetime) -> DashboardWidget:
    date_col = getattr(PharmacySale, "bill_datetime", None) or getattr(
        PharmacySale, "created_at", None)
    status_lc = func.lower(func.coalesce(PharmacySale.invoice_status, ""))

    q = db.query(
        PharmacySaleItem.item_name.label("medicine"),
        func.coalesce(func.sum(PharmacySaleItem.quantity), 0).label("qty"),
    ).join(PharmacySale, PharmacySaleItem.sale_id == PharmacySale.id)

    if date_col is not None:
        q = q.filter(date_col >= start_dt, date_col < end_dt)

    rows = q.filter(status_lc == "finalized").group_by(
        PharmacySaleItem.item_name).order_by(
            func.sum(PharmacySaleItem.quantity).desc()).limit(10).all()

    return DashboardWidget(
        code="top_medicines",
        title="Top 10 Medicines (Dispensed)",
        widget_type="chart",
        description="Most frequently dispensed medicines in selected period",
        data=[{
            "label": r.medicine,
            "value": int(r.qty or 0)
        } for r in rows],
        config={"chart_type": "bar"},
    )


def _build_patient_flow_chart(db: Session, end_date: date) -> DashboardWidget:
    start_date = end_date - timedelta(days=6)
    start_dt, end_dt = _dt_range(start_date, end_date)

    patient_rows = db.query(
        func.date(Patient.created_at).label("d"),
        func.count(Patient.id).label("c"),
    ).filter(Patient.created_at >= start_dt, Patient.created_at
             < end_dt).group_by(func.date(Patient.created_at)).all()
    patient_map = {r.d: r.c for r in patient_rows}

    visit_rows = db.query(
        func.date(Visit.visit_at).label("d"),
        func.count(Visit.id).label("c"),
    ).filter(Visit.visit_at >= start_dt, Visit.visit_at
             < end_dt).group_by(func.date(Visit.visit_at)).all()
    visit_map = {r.d: r.c for r in visit_rows}

    adm_rows = db.query(
        func.date(IpdAdmission.admitted_at).label("d"),
        func.count(IpdAdmission.id).label("c"),
    ).filter(IpdAdmission.admitted_at >= start_dt, IpdAdmission.admitted_at
             < end_dt).group_by(func.date(IpdAdmission.admitted_at)).all()
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
            "label": "OP",
            "value": streams.get("op", 0)
        },
        {
            "label": "IP",
            "value": streams.get("ip", 0)
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
        {
            "label": "Misc",
            "value": streams.get("misc", 0)
        },
    ]
    return DashboardWidget(
        code="rev_by_stream",
        title="Revenue by Stream",
        widget_type="chart",
        description=
        "Stream-wise billed revenue split (POSTED invoices in selected range)",
        data=data,
        config={"chart_type": "bar"},
    )


def _build_revenue_doctor_chart(
        top_doctors: List[Dict[str, Any]]) -> DashboardWidget:
    return DashboardWidget(
        code="rev_by_doctor",
        title="Top Doctors (Revenue)",
        widget_type="chart",
        description=
        "Doctor-wise billed revenue (POSTED invoice lines in selected range)",
        data=[{
            "label": d["doctor_name"],
            "value": float(d["amount"])
        } for d in (top_doctors or [])],
        config={"chart_type": "bar"},
    )


def _build_recent_admissions_widget(db: Session) -> DashboardWidget:
    from app.models.patient import Patient as PatientModel

    rows = db.query(
        IpdAdmission.id,
        IpdAdmission.admission_code,
        IpdAdmission.admitted_at,
        IpdAdmission.status,
        PatientModel.uhid,
        PatientModel.first_name,
        PatientModel.last_name,
    ).join(PatientModel, IpdAdmission.patient_id == PatientModel.id).order_by(
        IpdAdmission.admitted_at.desc()).limit(10).all()

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
        widget_type="table",
        description="Last 10 IPD admissions",
        data=data,
        config={
            "columns": [
                "admission_code", "uhid", "patient_name", "admitted_at",
                "status"
            ]
        },
    )


def _build_appointment_status_widget(db: Session, d_from: date,
                                     d_to: date) -> DashboardWidget:
    rows = db.query(
        Appointment.status,
        func.count(Appointment.id).label("count"),
    ).filter(
        Appointment.date >= d_from,
        Appointment.date <= d_to,
    ).group_by(Appointment.status).all()

    return DashboardWidget(
        code="appointment_status",
        title="OPD Appointments by Status",
        widget_type="chart",
        description=
        "Booked / checked-in / completed / cancelled / no-show distribution",
        data=[{
            "label": (r.status or "unknown").replace("_", " ").title(),
            "value": int(r.count or 0)
        } for r in rows],
        config={"chart_type": "pie"},
    )


def _build_ipd_status_widget(db: Session, start_dt: datetime,
                             end_dt: datetime) -> DashboardWidget:
    rows = db.query(
        IpdAdmission.status,
        func.count(IpdAdmission.id).label("count"),
    ).filter(
        IpdAdmission.admitted_at >= start_dt,
        IpdAdmission.admitted_at < end_dt,
    ).group_by(IpdAdmission.status).all()

    return DashboardWidget(
        code="ipd_status",
        title="IPD Cases by Status",
        widget_type="chart",
        description="Admitted / discharged / LAMA / cancelled etc.",
        data=[{
            "label": (r.status or "unknown").replace("_", " ").title(),
            "value": int(r.count or 0)
        } for r in rows],
        config={"chart_type": "pie"},
    )


# ✅ Updated payment mode widget to BillingPayment
def _build_payment_mode_widget_v2(db: Session, start_dt: datetime,
                                  end_dt: datetime) -> DashboardWidget:
    rows = db.query(
        BillingPayment.mode.label("mode"),
        func.coalesce(func.sum(BillingPayment.amount), 0).label("amount"),
    ).filter(
        BillingPayment.received_at >= start_dt,
        BillingPayment.received_at < end_dt,
    ).group_by(BillingPayment.mode).all()

    return DashboardWidget(
        code="payment_modes",
        title="Payment Modes (Collections)",
        widget_type="chart",
        description="Payments received split by mode (selected range)",
        data=[{
            "label":
            str(getattr(r.mode, "value", r.mode)).replace("_", " ").title(),
            "value":
            float(r.amount or 0),
        } for r in rows],
        config={"chart_type": "pie"},
    )


def _build_top_tests_widgets(
        db: Session, start_dt: datetime,
        end_dt: datetime) -> Tuple[DashboardWidget, DashboardWidget]:
    lab_rows = db.query(
        LisOrderItem.test_name.label("name"),
        func.count(LisOrderItem.id).label("count"),
    ).join(LisOrder, LisOrderItem.order_id == LisOrder.id).filter(
        LisOrder.created_at >= start_dt,
        LisOrder.created_at < end_dt,
        LisOrder.status != "cancelled",
    ).group_by(LisOrderItem.test_name).order_by(
        func.count(LisOrderItem.id).desc()).limit(5).all()

    lab_widget = DashboardWidget(
        code="top_lab_tests",
        title="Top 5 Lab Tests",
        widget_type="chart",
        description="Most frequently ordered lab investigations",
        data=[{
            "label": r.name,
            "value": int(r.count or 0)
        } for r in lab_rows],
        config={"chart_type": "bar"},
    )

    ris_rows = db.query(
        RisOrder.test_name.label("name"),
        func.count(RisOrder.id).label("count"),
    ).filter(
        RisOrder.created_at >= start_dt,
        RisOrder.created_at < end_dt,
        RisOrder.status != "cancelled",
    ).group_by(RisOrder.test_name).order_by(func.count(
        RisOrder.id).desc()).limit(5).all()

    ris_widget = DashboardWidget(
        code="top_radiology_tests",
        title="Top 5 Radiology Tests",
        widget_type="chart",
        description="Most frequently ordered imaging tests",
        data=[{
            "label": r.name,
            "value": int(r.count or 0)
        } for r in ris_rows],
        config={"chart_type": "bar"},
    )

    return lab_widget, ris_widget


def _build_billing_summary_widget_v2(db: Session, start_dt: datetime,
                                     end_dt: datetime) -> DashboardWidget:
    posted_dt = _invoice_posted_dt_expr()

    billed = db.query(func.coalesce(func.sum(
        BillingInvoice.grand_total), 0)).filter(
            BillingInvoice.status == DocStatus.POSTED,
            posted_dt >= start_dt,
            posted_dt < end_dt,
        ).scalar() or 0

    collected = db.query(func.coalesce(func.sum(
        BillingPayment.amount), 0)).filter(
            BillingPayment.received_at >= start_dt,
            BillingPayment.received_at < end_dt,
        ).scalar() or 0

    return DashboardWidget(
        code="billing_summary",
        title="Billing Summary (Billed vs Collected)",
        widget_type="chart",
        description=
        "Billed revenue (POSTED invoices) vs collections (payments received)",
        data=[
            {
                "label": "Billed",
                "value": _safe_scalar(billed)
            },
            {
                "label": "Collected",
                "value": _safe_scalar(collected)
            },
        ],
        config={"chart_type": "bar"},
    )
