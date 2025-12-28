# FILE: app/api/routes_ipd_nursing.py
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, current_user as auth_current_user
from app.models.ipd_nursing import (
    IcuFlowSheet,
    IpdBloodTransfusion,
    IpdDressingRecord,
    IpdIsolationPrecaution,
    IpdRestraintRecord,
)
from app.models.user import User
from app.schemas.ipd_nursing import (
    DressingCreate,
    DressingOut,
    DressingUpdate,
    IcuFlowCreate,
    IcuFlowOut,
    IcuFlowUpdate,
    IsolationCreate,
    IsolationOut,
    IsolationStop,
    IsolationUpdate,
    RestraintAppendMonitoring,
    RestraintCreate,
    RestraintOut,
    RestraintStop,
    RestraintUpdate,
    TransfusionAppendVital,
    TransfusionCreate,
    TransfusionMarkReaction,
    TransfusionOut,
    TransfusionUpdate,
)
from app.services.ipd_nursing_service import (
    add_timeline,
    compute_due_alerts,
    get_admission,
    utcnow,
)
from app.services.perm import need_any
from app.utils.resp import err, ok

router = APIRouter(prefix="/ipd", tags=["IPD Nursing"])
log = logging.getLogger(__name__)


# =========================================================
# HELPERS
# =========================================================
def _adm_or_404(db: Session, admission_id: int):
    adm = get_admission(db, admission_id)
    if not adm:
        return None, err("Admission not found", 404)
    return adm, None


def _commit(db: Session, msg: str = "Database error"):
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception(msg)
        return err(msg, 500)
    return None


def _merge_json(old: Optional[dict], new: Optional[dict]) -> dict:
    out = dict(old or {})
    for k, v in (new or {}).items():
        out[k] = v
    return out


def _load_isolation(db: Session, iso_id: int):
    return (
        db.query(IpdIsolationPrecaution)
        .options(
            joinedload(IpdIsolationPrecaution.ordered_by),
            joinedload(IpdIsolationPrecaution.updated_by),
            joinedload(IpdIsolationPrecaution.stopped_by),
        )
        .filter(IpdIsolationPrecaution.id == iso_id)
        .first()
    )


def _load_restraint(db: Session, restraint_id: int):
    return (
        db.query(IpdRestraintRecord)
        .options(
            joinedload(IpdRestraintRecord.ordered_by),
            joinedload(IpdRestraintRecord.updated_by),
            joinedload(IpdRestraintRecord.stopped_by),
        )
        .filter(IpdRestraintRecord.id == restraint_id)
        .first()
    )


def _load_transfusion(db: Session, transfusion_id: int):
    return (
        db.query(IpdBloodTransfusion)
        .options(
            joinedload(IpdBloodTransfusion.created_by),
            joinedload(IpdBloodTransfusion.updated_by),
            joinedload(IpdBloodTransfusion.ordered_by),
        )
        .filter(IpdBloodTransfusion.id == transfusion_id)
        .first()
    )


def _load_dressing(db: Session, record_id: int):
    return (
        db.query(IpdDressingRecord)
        .options(
            joinedload(IpdDressingRecord.performed_by),
            joinedload(IpdDressingRecord.verified_by),
            joinedload(IpdDressingRecord.updated_by),
        )
        .filter(IpdDressingRecord.id == record_id)
        .first()
    )


def _load_icu(db: Session, flow_id: int):
    return (
        db.query(IcuFlowSheet)
        .options(
            joinedload(IcuFlowSheet.recorded_by),
            joinedload(IcuFlowSheet.verified_by),
            joinedload(IcuFlowSheet.updated_by),
        )
        .filter(IcuFlowSheet.id == flow_id)
        .first()
    )


# =========================================================
# DUE ALERTS
# =========================================================
@router.get("/admissions/{admission_id}/nursing/alerts")
def nursing_alerts(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.view", "ipd.manage"])
    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    data = compute_due_alerts(db, admission_id)
    return ok(data)


# =========================================================
# DRESSING
# =========================================================
@router.post("/admissions/{admission_id}/dressing-records")
def create_dressing(
    admission_id: int,
    payload: DressingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.dressing.create", "ipd.nursing.create", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rec = IpdDressingRecord(
        admission_id=admission_id,
        performed_at=payload.performed_at or utcnow(),
        wound_site=payload.wound_site or "",
        dressing_type=payload.dressing_type or "",
        indication=payload.indication or "",
        assessment=payload.assessment.model_dump(),
        procedure_json=payload.procedure.model_dump(),
        asepsis=payload.asepsis.model_dump(),
        pain_score=payload.pain_score,
        patient_response=payload.patient_response or "",
        findings=payload.findings or "",
        next_dressing_due=payload.next_dressing_due,
        performed_by_id=user.id,
        verified_by_id=payload.verified_by_id,
        created_at=utcnow(),
    )
    db.add(rec)
    db.flush()  # rec.id

    add_timeline(
        db,
        admission_id,
        "dressing",
        rec.performed_at,
        title="Dressing done",
        summary=f"{rec.wound_site} • {rec.dressing_type}".strip(" •"),
        ref_table="ipd_dressing_records",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to save dressing")
    if resp:
        return resp

    rec = _load_dressing(db, rec.id) or rec
    return ok(DressingOut.model_validate(rec), 201)


@router.get("/admissions/{admission_id}/dressing-records")
def list_dressing(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.dressing.view", "ipd.nursing.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rows = (
        db.query(IpdDressingRecord)
        .options(
            joinedload(IpdDressingRecord.performed_by),
            joinedload(IpdDressingRecord.verified_by),
            joinedload(IpdDressingRecord.updated_by),
        )
        .filter(IpdDressingRecord.admission_id == admission_id)
        .order_by(IpdDressingRecord.performed_at.desc())
        .all()
    )
    return ok([DressingOut.model_validate(r) for r in rows])


@router.patch("/dressing-records/{record_id}")
def update_dressing(
    record_id: int,
    payload: DressingUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.dressing.update", "ipd.manage"])

    rec = db.get(IpdDressingRecord, record_id)
    if not rec:
        return err("Dressing record not found", 404)

    if payload.wound_site is not None:
        rec.wound_site = payload.wound_site
    if payload.dressing_type is not None:
        rec.dressing_type = payload.dressing_type
    if payload.indication is not None:
        rec.indication = payload.indication
    if payload.assessment is not None:
        rec.assessment = payload.assessment.model_dump()
    if payload.procedure is not None:
        rec.procedure_json = payload.procedure.model_dump()
    if payload.asepsis is not None:
        rec.asepsis = payload.asepsis.model_dump()
    if payload.pain_score is not None:
        rec.pain_score = payload.pain_score
    if payload.patient_response is not None:
        rec.patient_response = payload.patient_response
    if payload.findings is not None:
        rec.findings = payload.findings
    if payload.next_dressing_due is not None:
        rec.next_dressing_due = payload.next_dressing_due
    if payload.verified_by_id is not None:
        rec.verified_by_id = payload.verified_by_id

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = payload.edit_reason

    resp = _commit(db, "Failed to update dressing")
    if resp:
        return resp

    rec = _load_dressing(db, rec.id) or rec
    return ok(DressingOut.model_validate(rec))


# =========================================================
# ICU FLOW
# =========================================================
@router.post("/admissions/{admission_id}/icu-flow")
def create_icu_flow(
    admission_id: int,
    payload: IcuFlowCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.icu.create", "ipd.nursing.create", "ipd.doctor", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rec = IcuFlowSheet(
        admission_id=admission_id,
        recorded_at=payload.recorded_at or utcnow(),
        shift=payload.shift,
        vitals=payload.vitals or {},
        ventilator=payload.ventilator or {},
        infusions=payload.infusions or [],
        gcs_score=payload.gcs_score,
        urine_output_ml=payload.urine_output_ml,
        notes=payload.notes or "",
        recorded_by_id=user.id,
        verified_by_id=payload.verified_by_id,
        created_at=utcnow(),
    )
    db.add(rec)
    db.flush()

    add_timeline(
        db,
        admission_id,
        "icu",
        rec.recorded_at,
        title="ICU flow recorded",
        summary=f"Shift: {rec.shift or '-'} • GCS: {rec.gcs_score if rec.gcs_score is not None else '-'}",
        ref_table="icu_flow_sheets",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to save ICU flow")
    if resp:
        return resp

    rec = _load_icu(db, rec.id) or rec
    return ok(IcuFlowOut.model_validate(rec), 201)


@router.get("/admissions/{admission_id}/icu-flow")
def list_icu_flow(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.icu.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rows = (
        db.query(IcuFlowSheet)
        .options(
            joinedload(IcuFlowSheet.recorded_by),
            joinedload(IcuFlowSheet.verified_by),
            joinedload(IcuFlowSheet.updated_by),
        )
        .filter(IcuFlowSheet.admission_id == admission_id)
        .order_by(IcuFlowSheet.recorded_at.desc())
        .all()
    )
    return ok([IcuFlowOut.model_validate(r) for r in rows])


@router.get("/admissions/{admission_id}/icu-flow/latest")
def latest_icu_flow(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.icu.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rec = (
        db.query(IcuFlowSheet)
        .options(
            joinedload(IcuFlowSheet.recorded_by),
            joinedload(IcuFlowSheet.verified_by),
            joinedload(IcuFlowSheet.updated_by),
        )
        .filter(IcuFlowSheet.admission_id == admission_id)
        .order_by(IcuFlowSheet.recorded_at.desc())
        .first()
    )
    return ok(IcuFlowOut.model_validate(rec) if rec else None)


@router.patch("/icu-flow/{flow_id}")
def update_icu_flow(
    flow_id: int,
    payload: IcuFlowUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.icu.update", "ipd.manage"])

    rec = db.get(IcuFlowSheet, flow_id)
    if not rec:
        return err("ICU flow entry not found", 404)

    if payload.shift is not None:
        rec.shift = payload.shift
    if payload.vitals is not None:
        rec.vitals = payload.vitals
    if payload.ventilator is not None:
        rec.ventilator = payload.ventilator
    if payload.infusions is not None:
        rec.infusions = payload.infusions
    if payload.gcs_score is not None:
        rec.gcs_score = payload.gcs_score
    if payload.urine_output_ml is not None:
        rec.urine_output_ml = payload.urine_output_ml
    if payload.notes is not None:
        rec.notes = payload.notes
    if payload.verified_by_id is not None:
        rec.verified_by_id = payload.verified_by_id

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = payload.edit_reason

    resp = _commit(db, "Failed to update ICU flow")
    if resp:
        return resp

    rec = _load_icu(db, rec.id) or rec
    return ok(IcuFlowOut.model_validate(rec))


# =========================================================
# ISOLATION
# =========================================================
@router.post("/admissions/{admission_id}/isolation")
def create_isolation(
    admission_id: int,
    payload: IsolationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.isolation.create", "ipd.doctor", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rec = IpdIsolationPrecaution(
        admission_id=admission_id,
        status="active",
        precaution_type=payload.precaution_type,
        indication=payload.indication or "",
        ordered_at=utcnow(),
        ordered_by_id=user.id,
        measures=payload.measures or {},
        review_due_at=payload.review_due_at,
        started_at=payload.started_at or utcnow(),
        ended_at=payload.ended_at,
        created_at=utcnow(),
    )
    db.add(rec)
    db.flush()

    add_timeline(
        db,
        admission_id,
        "isolation",
        rec.started_at,
        title=f"Isolation started ({rec.precaution_type})",
        summary=rec.indication or "",
        ref_table="ipd_isolation_precautions",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to save isolation")
    if resp:
        return resp

    rec = _load_isolation(db, rec.id) or rec
    return ok(IsolationOut.model_validate(rec), 201)


@router.get("/admissions/{admission_id}/isolation")
def list_isolation(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.isolation.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rows = (
        db.query(IpdIsolationPrecaution)
        .options(
            joinedload(IpdIsolationPrecaution.ordered_by),
            joinedload(IpdIsolationPrecaution.updated_by),
            joinedload(IpdIsolationPrecaution.stopped_by),
        )
        .filter(IpdIsolationPrecaution.admission_id == admission_id)
        .order_by(IpdIsolationPrecaution.started_at.desc())
        .all()
    )
    return ok([IsolationOut.model_validate(r) for r in rows])


@router.patch("/isolation/{iso_id}")
def update_isolation(
    iso_id: int,
    payload: IsolationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.isolation.update", "ipd.manage"])

    rec = db.get(IpdIsolationPrecaution, iso_id)
    if not rec:
        return err("Isolation record not found", 404)

    if payload.precaution_type is not None:
        rec.precaution_type = payload.precaution_type
    if payload.indication is not None:
        rec.indication = payload.indication
    if payload.measures is not None:
        rec.measures = payload.measures
    if payload.review_due_at is not None:
        rec.review_due_at = payload.review_due_at
    if payload.ended_at is not None:
        rec.ended_at = payload.ended_at

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = payload.edit_reason

    resp = _commit(db, "Failed to update isolation")
    if resp:
        return resp

    rec = _load_isolation(db, rec.id) or rec
    return ok(IsolationOut.model_validate(rec))


@router.post("/isolation/{iso_id}/stop")
def stop_isolation(
    iso_id: int,
    payload: IsolationStop,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.isolation.stop", "ipd.doctor", "ipd.manage"])

    rec = db.get(IpdIsolationPrecaution, iso_id)
    if not rec:
        return err("Isolation record not found", 404)

    rec.status = "stopped"
    rec.stopped_at = payload.stopped_at or utcnow()
    rec.stopped_by_id = user.id
    rec.stop_reason = payload.stop_reason
    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = f"Stopped: {payload.stop_reason}"

    add_timeline(
        db,
        rec.admission_id,
        "isolation",
        rec.stopped_at,
        title="Isolation stopped",
        summary=payload.stop_reason,
        ref_table="ipd_isolation_precautions",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to stop isolation")
    if resp:
        return resp

    rec = _load_isolation(db, rec.id) or rec
    return ok(IsolationOut.model_validate(rec))


# =========================================================
# RESTRAINTS
# =========================================================
@router.post("/admissions/{admission_id}/restraints")
def create_restraint(
    admission_id: int,
    payload: RestraintCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.restraints.create", "ipd.doctor", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rec = IpdRestraintRecord(
        admission_id=admission_id,
        status="active",
        restraint_type=payload.restraint_type,
        device=payload.device or "",
        site=payload.site or "",
        reason=payload.reason or "",
        alternatives_tried=payload.alternatives_tried or "",
        ordered_at=utcnow(),
        ordered_by_id=user.id,
        valid_till=payload.valid_till,
        consent_taken=payload.consent_taken,
        consent_doc_ref=payload.consent_doc_ref,
        started_at=payload.started_at or utcnow(),
        monitoring_log=[],
        created_at=utcnow(),
    )
    db.add(rec)
    db.flush()

    add_timeline(
        db,
        admission_id,
        "restraint",
        rec.started_at,
        title="Restraint started",
        summary=f"{rec.restraint_type} • {rec.device} • {rec.site}".strip(" •"),
        ref_table="ipd_restraint_records",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to save restraint")
    if resp:
        return resp

    rec = _load_restraint(db, rec.id) or rec
    return ok(RestraintOut.model_validate(rec), 201)


@router.get("/admissions/{admission_id}/restraints")
def list_restraints(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.restraints.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rows = (
        db.query(IpdRestraintRecord)
        .options(
            joinedload(IpdRestraintRecord.ordered_by),
            joinedload(IpdRestraintRecord.updated_by),
            joinedload(IpdRestraintRecord.stopped_by),
        )
        .filter(IpdRestraintRecord.admission_id == admission_id)
        .order_by(IpdRestraintRecord.started_at.desc())
        .all()
    )
    return ok([RestraintOut.model_validate(r) for r in rows])


@router.patch("/restraints/{restraint_id}")
def update_restraint(
    restraint_id: int,
    payload: RestraintUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.restraints.update", "ipd.manage"])

    rec = db.get(IpdRestraintRecord, restraint_id)
    if not rec:
        return err("Restraint record not found", 404)

    if payload.device is not None:
        rec.device = payload.device
    if payload.site is not None:
        rec.site = payload.site
    if payload.reason is not None:
        rec.reason = payload.reason
    if payload.alternatives_tried is not None:
        rec.alternatives_tried = payload.alternatives_tried
    if payload.valid_till is not None:
        rec.valid_till = payload.valid_till
    if payload.consent_taken is not None:
        rec.consent_taken = payload.consent_taken
    if payload.consent_doc_ref is not None:
        rec.consent_doc_ref = payload.consent_doc_ref
    if payload.ended_at is not None:
        rec.ended_at = payload.ended_at

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = payload.edit_reason

    resp = _commit(db, "Failed to update restraint")
    if resp:
        return resp

    rec = _load_restraint(db, rec.id) or rec
    return ok(RestraintOut.model_validate(rec))


@router.post("/restraints/{restraint_id}/monitor")
def append_restraint_monitoring(
    restraint_id: int,
    payload: RestraintAppendMonitoring,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.restraints.monitor", "ipd.nursing.create", "ipd.manage"])

    rec = db.get(IpdRestraintRecord, restraint_id)
    if not rec:
        return err("Restraint record not found", 404)
    if rec.status != "active":
        return err("Restraint is not active", 400)

    log_points = list(rec.monitoring_log or [])
    log_points.append(payload.point.model_dump())
    rec.monitoring_log = log_points

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = "Monitoring added"

    resp = _commit(db, "Failed to append restraint monitoring")
    if resp:
        return resp

    rec = _load_restraint(db, rec.id) or rec
    return ok(RestraintOut.model_validate(rec))


@router.post("/restraints/{restraint_id}/stop")
def stop_restraint(
    restraint_id: int,
    payload: RestraintStop,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.restraints.stop", "ipd.doctor", "ipd.manage"])

    rec = db.get(IpdRestraintRecord, restraint_id)
    if not rec:
        return err("Restraint record not found", 404)

    rec.status = "stopped"
    rec.stopped_at = payload.stopped_at or utcnow()
    rec.stopped_by_id = user.id
    rec.stop_reason = payload.stop_reason
    rec.ended_at = payload.ended_at if payload.ended_at is not None else rec.stopped_at

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = f"Stopped: {payload.stop_reason}"

    add_timeline(
        db,
        rec.admission_id,
        "restraint",
        rec.stopped_at,
        title="Restraint stopped",
        summary=payload.stop_reason,
        ref_table="ipd_restraint_records",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to stop restraint")
    if resp:
        return resp

    rec = _load_restraint(db, rec.id) or rec
    return ok(RestraintOut.model_validate(rec))


# =========================================================
# TRANSFUSION
# =========================================================
def _auto_transfusion_status(payload: TransfusionCreate) -> str:
    admin = payload.administration or {}
    reaction = payload.reaction or {}
    if reaction.get("occurred"):
        return "reaction"

    st = admin.get("start_time")
    en = admin.get("end_time")
    if st and not en:
        return "in_progress"
    if st and en:
        return "completed"
    return "ordered"


@router.post("/admissions/{admission_id}/transfusions")
def create_transfusion(
    admission_id: int,
    payload: TransfusionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.transfusion.create", "ipd.nursing.create", "ipd.doctor", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    status = _auto_transfusion_status(payload)

    rec = IpdBloodTransfusion(
        admission_id=admission_id,
        status=status,
        indication=payload.indication or "",
        ordered_at=payload.ordered_at,
        ordered_by_id=(user.id if payload.ordered_at else None),
        consent_taken=payload.consent_taken,
        consent_doc_ref=payload.consent_doc_ref,
        unit=payload.unit or {},
        compatibility=payload.compatibility or {},
        issue=payload.issue or {},
        bedside_verification=payload.bedside_verification or {},
        administration=payload.administration or {},
        baseline_vitals=payload.baseline_vitals or {},
        monitoring_vitals=[v.to_json() for v in (payload.monitoring_vitals or [])],
        reaction=payload.reaction or {},
        created_by_id=user.id,
        created_at=utcnow(),
    )
    db.add(rec)
    db.flush()

    add_timeline(
        db,
        admission_id,
        "transfusion",
        utcnow(),
        title="Transfusion created",
        summary=(rec.unit or {}).get("component_type") or "",
        ref_table="ipd_blood_transfusions",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to create transfusion")
    if resp:
        return resp

    rec = _load_transfusion(db, rec.id) or rec
    return ok(TransfusionOut.model_validate(rec), 201)


@router.get("/admissions/{admission_id}/transfusions")
def list_transfusions(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.transfusion.view", "ipd.view", "ipd.manage"])

    adm, resp = _adm_or_404(db, admission_id)
    if resp:
        return resp

    rows = (
        db.query(IpdBloodTransfusion)
        .options(
            joinedload(IpdBloodTransfusion.created_by),
            joinedload(IpdBloodTransfusion.updated_by),
            joinedload(IpdBloodTransfusion.ordered_by),
        )
        .filter(IpdBloodTransfusion.admission_id == admission_id)
        .order_by(IpdBloodTransfusion.created_at.desc())
        .all()
    )
    return ok([TransfusionOut.model_validate(r) for r in rows])


@router.patch("/transfusions/{transfusion_id}")
def update_transfusion(
    transfusion_id: int,
    payload: TransfusionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.transfusion.update", "ipd.manage"])

    rec = db.get(IpdBloodTransfusion, transfusion_id)
    if not rec:
        return err("Transfusion record not found", 404)

    if payload.status is not None:
        rec.status = payload.status
    if payload.indication is not None:
        rec.indication = payload.indication
    if payload.consent_taken is not None:
        rec.consent_taken = payload.consent_taken
    if payload.consent_doc_ref is not None:
        rec.consent_doc_ref = payload.consent_doc_ref

    # merge JSON blobs (prevents data loss)
    if payload.unit is not None:
        rec.unit = _merge_json(rec.unit, payload.unit)
    if payload.compatibility is not None:
        rec.compatibility = _merge_json(rec.compatibility, payload.compatibility)
    if payload.issue is not None:
        rec.issue = _merge_json(rec.issue, payload.issue)
    if payload.bedside_verification is not None:
        rec.bedside_verification = _merge_json(rec.bedside_verification, payload.bedside_verification)
    if payload.administration is not None:
        rec.administration = _merge_json(rec.administration, payload.administration)
    if payload.baseline_vitals is not None:
        rec.baseline_vitals = _merge_json(rec.baseline_vitals, payload.baseline_vitals)

    # automation status
    if (rec.reaction or {}).get("occurred"):
        rec.status = "reaction"
    else:
        st = (rec.administration or {}).get("start_time")
        en = (rec.administration or {}).get("end_time")
        if st and not en and rec.status in {"ordered", "issued"}:
            rec.status = "in_progress"
        if st and en:
            rec.status = "completed"

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = payload.edit_reason

    resp = _commit(db, "Failed to update transfusion")
    if resp:
        return resp

    rec = _load_transfusion(db, rec.id) or rec
    return ok(TransfusionOut.model_validate(rec))


@router.post("/transfusions/{transfusion_id}/vitals")
def append_transfusion_vital(
    transfusion_id: int,
    payload: TransfusionAppendVital,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.transfusion.update", "ipd.nursing.create", "ipd.manage"])

    rec = db.get(IpdBloodTransfusion, transfusion_id)
    if not rec:
        return err("Transfusion record not found", 404)

    vitals = list(rec.monitoring_vitals or [])
    vitals.append(payload.point.to_json())
    rec.monitoring_vitals = vitals

    # automation
    st = (rec.administration or {}).get("start_time")
    en = (rec.administration or {}).get("end_time")
    if st and not en and rec.status in {"ordered", "issued"}:
        rec.status = "in_progress"

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = "Vitals appended"

    resp = _commit(db, "Failed to append transfusion vitals")
    if resp:
        return resp

    rec = _load_transfusion(db, rec.id) or rec
    return ok(TransfusionOut.model_validate(rec))


@router.post("/transfusions/{transfusion_id}/reaction")
def mark_transfusion_reaction(
    transfusion_id: int,
    payload: TransfusionMarkReaction,
    db: Session = Depends(get_db),
    user: User = Depends(auth_current_user),
):
    need_any(user, ["ipd.transfusion.update", "ipd.nursing.create", "ipd.manage"])

    rec = db.get(IpdBloodTransfusion, transfusion_id)
    if not rec:
        return err("Transfusion record not found", 404)

    rec.reaction = {
        "occurred": payload.occurred,
        "started_at": (payload.started_at or utcnow()).isoformat(),
        "symptoms": payload.symptoms,
        "actions_taken": payload.actions_taken,
        "doctor_notified_at": payload.doctor_notified_at.isoformat() if payload.doctor_notified_at else None,
        "bloodbank_notified_at": payload.bloodbank_notified_at.isoformat() if payload.bloodbank_notified_at else None,
        "outcome": payload.outcome,
        "notes": payload.notes,
    }
    rec.status = "reaction"

    rec.updated_at = utcnow()
    rec.updated_by_id = user.id
    rec.edit_reason = "Reaction marked"

    add_timeline(
        db,
        rec.admission_id,
        "transfusion",
        utcnow(),
        title="Transfusion reaction flagged",
        summary="; ".join(payload.symptoms)[:200] if payload.symptoms else "",
        ref_table="ipd_blood_transfusions",
        ref_id=rec.id,
        created_by_id=user.id,
    )

    resp = _commit(db, "Failed to mark transfusion reaction")
    if resp:
        return resp

    rec = _load_transfusion(db, rec.id) or rec
    return ok(TransfusionOut.model_validate(rec))
