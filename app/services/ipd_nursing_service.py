# FILE: app/services/ipd_nursing_service.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.ipd import IpdAdmission
from app.models.ipd_nursing import (
    IpdDressingRecord, IpdBloodTransfusion,
    IpdRestraintRecord, IpdIsolationPrecaution,
    IcuFlowSheet, IpdNursingTimeline
)


def utcnow() -> datetime:
    return datetime.utcnow()


def get_admission(db: Session, admission_id: int) -> Optional[IpdAdmission]:
    return db.get(IpdAdmission, admission_id)


def add_timeline(
    db: Session,
    admission_id: int,
    event_type: str,
    event_at: datetime,
    title: str,
    summary: str,
    ref_table: Optional[str],
    ref_id: Optional[int],
    created_by_id: Optional[int],
) -> None:
    db.add(IpdNursingTimeline(
        admission_id=admission_id,
        event_type=event_type,
        event_at=event_at,
        title=title or "",
        summary=summary or "",
        ref_table=ref_table,
        ref_id=ref_id,
        created_by_id=created_by_id,
        created_at=utcnow(),
    ))


# --------------------------
# “Automations” (user-friendly workflow helpers)
# --------------------------
def compute_due_alerts(db: Session, admission_id: int) -> Dict[str, Any]:
    """
    Returns due items for UI badges:
    - dressing next due
    - isolation review due
    - active restraints needing monitoring (optional rule)
    - ICU last entry time
    - transfusion in_progress missing monitoring
    """
    now = utcnow()
    alerts: Dict[str, Any] = {}

    # Dressing next due
    last_dressing = (
        db.query(IpdDressingRecord)
        .filter(IpdDressingRecord.admission_id == admission_id)
        .order_by(IpdDressingRecord.performed_at.desc())
        .first()
    )
    if last_dressing and last_dressing.next_dressing_due:
        alerts["dressing_next_due_at"] = last_dressing.next_dressing_due
        alerts["dressing_overdue"] = last_dressing.next_dressing_due <= now

    # Isolation review due
    active_iso = (
        db.query(IpdIsolationPrecaution)
        .filter(IpdIsolationPrecaution.admission_id == admission_id,
                IpdIsolationPrecaution.status == "active")
        .order_by(IpdIsolationPrecaution.started_at.desc())
        .first()
    )
    if active_iso and active_iso.review_due_at:
        alerts["isolation_review_due_at"] = active_iso.review_due_at
        alerts["isolation_review_overdue"] = active_iso.review_due_at <= now

    # ICU last chart
    last_icu = (
        db.query(IcuFlowSheet)
        .filter(IcuFlowSheet.admission_id == admission_id)
        .order_by(IcuFlowSheet.recorded_at.desc())
        .first()
    )
    if last_icu:
        alerts["icu_last_recorded_at"] = last_icu.recorded_at
        # example rule: overdue if no entry for 6 hours (adjust per ICU policy)
        alerts["icu_chart_overdue"] = (last_icu.recorded_at + timedelta(hours=6)) <= now
    else:
        alerts["icu_last_recorded_at"] = None
        alerts["icu_chart_overdue"] = True

    # Restraint: active + monitoring reminder (example rule: last check > 2 hours)
    active_rest = (
        db.query(IpdRestraintRecord)
        .filter(IpdRestraintRecord.admission_id == admission_id,
                IpdRestraintRecord.status == "active")
        .order_by(IpdRestraintRecord.started_at.desc())
        .first()
    )
    if active_rest:
        log = active_rest.monitoring_log or []
        last_at = None
        if log:
            last_at = log[-1].get("at")
        alerts["restraint_active"] = True
        alerts["restraint_last_monitor_at"] = last_at
        if last_at:
            try:
                # last_at may be iso string in JSON
                last_dt = datetime.fromisoformat(last_at.replace("Z", ""))
                alerts["restraint_monitor_overdue"] = (last_dt + timedelta(hours=2)) <= now
            except Exception:
                alerts["restraint_monitor_overdue"] = False
        else:
            alerts["restraint_monitor_overdue"] = True
    else:
        alerts["restraint_active"] = False

    # Transfusion: in_progress with missing 15-min vital check (example rule)
    tf = (
        db.query(IpdBloodTransfusion)
        .filter(IpdBloodTransfusion.admission_id == admission_id)
        .order_by(IpdBloodTransfusion.created_at.desc())
        .first()
    )
    if tf and tf.status == "in_progress":
        alerts["transfusion_in_progress"] = True
        vitals = tf.monitoring_vitals or []
        alerts["transfusion_monitor_count"] = len(vitals)
        alerts["transfusion_needs_monitoring"] = len(vitals) == 0
    else:
        alerts["transfusion_in_progress"] = False

    return alerts
