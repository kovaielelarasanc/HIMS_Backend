from __future__ import annotations

from datetime import date
from typing import Optional, List, Dict, Any
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.patient import Patient
from app.models.opd import Visit, Vitals
from app.services.pdf_opd_summary import build_visit_summary_pdf
from app.services.pdf_patient_opd_history import build_patient_opd_history_pdf
from app.services.emr_opd_summary import build_emr_opd_visit_summary

from app.models.lis import LisOrder, LisOrderItem, LisResultLine
from app.services.emr_lab_report import build_emr_lab_report
from app.services.pdf_patient_lab_history import build_patient_lab_history_pdf
from app.services.ui_branding import get_ui_branding
from app.services.pdf_lab_report_weasy import build_lab_report_pdf_bytes
from app.services.emr_lab_report import build_emr_lab_report_object_for_pdf

router = APIRouter()


# ---------- permissions ----------
def _has_any_perm(user: User, codes: set[str]) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
                return True
    return False


def _require_emr_view(user: User) -> None:
    # for now: reuse existing perms
    if not _has_any_perm(
            user, {"patients.view", "visits.view", "appointments.view"}):
        raise HTTPException(status_code=403, detail="Not permitted")


# ---------- helpers ----------
def _patient_name(p: Patient) -> str:
    name = f"{getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}".strip(
    )
    return name or (getattr(p, "full_name", "") or "—")


def _patient_phone(p: Patient) -> str:
    return (getattr(p, "phone", "") or getattr(p, "mobile", "") or "—")


# ---------- Patient Search (EMR Entry) ----------
@router.get("/patients", response_model=List[Dict[str, Any]])
def emr_search_patients(
        q: str = Query("", description="Search by UHID / name / phone"),
        limit: int = Query(20, ge=1, le=50),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    term = (q or "").strip()
    if not term:
        return []

    like = f"%{term}%"

    query = db.query(Patient)

    # if patient has is_active column, prefer active only
    if hasattr(Patient, "is_active"):
        query = query.filter(Patient.is_active.is_(True))

    # safe filters (use whichever columns exist)
    filters = []
    if hasattr(Patient, "uhid"):
        filters.append(Patient.uhid.ilike(like))
    if hasattr(Patient, "first_name"):
        filters.append(Patient.first_name.ilike(like))
    if hasattr(Patient, "last_name"):
        filters.append(Patient.last_name.ilike(like))
    if hasattr(Patient, "phone"):
        filters.append(Patient.phone.ilike(like))
    if hasattr(Patient, "mobile"):
        filters.append(Patient.mobile.ilike(like))

    if not filters:
        return []

    rows = (query.filter(or_(*filters)).order_by(
        Patient.id.desc()).limit(limit).all())

    out = []
    for p in rows:
        out.append({
            "id":
            p.id,
            "uhid":
            getattr(p, "uhid", "") or "—",
            "name":
            _patient_name(p),
            "phone":
            _patient_phone(p),
            "dob":
            getattr(p, "dob", None) or getattr(p, "date_of_birth", None),
            "gender":
            getattr(p, "gender", "") or getattr(p, "sex", ""),
        })
    return out


# ---------- EMR: OPD Visits List ----------
@router.get("/patients/{patient_id}/opd/visits",
            response_model=List[Dict[str, Any]])
def emr_patient_opd_visits(
        patient_id: int,
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    rows: List[Visit] = (db.query(Visit).options(
        joinedload(Visit.doctor),
        joinedload(Visit.department),
        joinedload(Visit.appointment),
    ).filter(Visit.patient_id == patient_id).order_by(
        Visit.visit_at.desc(), Visit.id.desc()).limit(limit).all())

    out: List[Dict[str, Any]] = []
    for v in rows:
        dtv = getattr(v, "visit_at", None)
        out.append({
            "visit_id":
            v.id,
            "episode_id":
            getattr(v, "episode_id", "") or "",
            "visit_at":
            dtv.isoformat() if dtv else "",
            "doctor_name":
            getattr(getattr(v, "doctor", None), "name", "") or "—",
            "department_name":
            getattr(getattr(v, "department", None), "name", "") or "—",
            "appointment_id":
            getattr(v, "appointment_id", None),
            "appointment_status":
            getattr(getattr(v, "appointment", None), "status", None),
        })
    return out


# ---------- EMR: Single Visit Summary PDF ----------
@router.get("/opd/visits/{visit_id}/summary/pdf")
def emr_visit_summary_pdf(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    buff = build_visit_summary_pdf(db, visit_id)
    filename = f"OPD_Visit_{visit_id}.pdf"

    return StreamingResponse(
        buff,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------- EMR: Patient Full OPD History (Merged PDF) ----------
@router.get("/patients/{patient_id}/opd/history/pdf")
def emr_patient_opd_history_pdf(
        patient_id: int,
        date_from: Optional[date] = Query(None),
        date_to: Optional[date] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    buff = build_patient_opd_history_pdf(
        db,
        patient_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )

    uhid = getattr(p, "uhid", "") or f"PAT-{patient_id}"
    filename = f"EMR_OPD_HISTORY_{uhid}.pdf"

    return StreamingResponse(
        buff,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/opd/visits/{visit_id}/summary")
def emr_visit_summary_json(
        visit_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)
    return build_emr_opd_visit_summary(db, visit_id)


# ----------------------------------------------------------------------------------------------
# ---------------------LAB EMR ------------------------------------------------------------------


@router.get("/patients/{patient_id}/lab/orders",
            response_model=List[Dict[str, Any]])
def emr_patient_lab_orders(
        patient_id: int,
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    rows = (db.query(LisOrder).filter(
        LisOrder.patient_id == patient_id).order_by(
            LisOrder.id.desc()).limit(limit).all())

    out: List[Dict[str, Any]] = []
    for o in rows:
        items = (db.query(LisOrderItem).filter(
            LisOrderItem.order_id == o.id).order_by(
                LisOrderItem.id.asc()).all())
        tests = [it.test_name for it in items if it.test_name]
        critical_count = (db.query(LisOrderItem).filter(
            LisOrderItem.order_id == o.id).filter(
                LisOrderItem.is_critical.is_(True)).count())

        out.append({
            "order_id":
            o.id,
            "lab_no":
            f"LAB-{o.id:06d}",
            "status":
            o.status,
            "priority":
            o.priority,
            "context_type":
            o.context_type,
            "context_id":
            o.context_id,
            "created_at":
            o.created_at.isoformat() if o.created_at else "",
            "collected_at":
            o.collected_at.isoformat() if o.collected_at else "",
            "reported_at":
            o.reported_at.isoformat() if o.reported_at else "",
            "tests_count":
            len(tests),
            "critical_count":
            critical_count,
            "tests":
            tests[:6],  # small preview
        })
    return out


@router.get("/lab/orders/{order_id}/report")
def emr_lab_report_json(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)
    return build_emr_lab_report(db, order_id)


@router.get("/lab/orders/{order_id}/report/pdf")
def emr_lab_report_pdf(
        order_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    branding = get_ui_branding(db)
    report_obj, patient_obj, lab_no, order_date, collected_by_name = (
        build_emr_lab_report_object_for_pdf(db, order_id))

    pdf_bytes = build_lab_report_pdf_bytes(
        branding=branding,
        report=report_obj,
        patient=patient_obj,
        lab_no=lab_no,
        order_date=order_date,
        collected_by_name=collected_by_name,
    )

    buf = BytesIO(pdf_bytes)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
            f'inline; filename="lab-report-{order_id}.pdf"'
        },
    )


@router.get("/patients/{patient_id}/lab/history/pdf")
def emr_patient_lab_history_pdf(
        patient_id: int,
        date_from: Optional[date] = Query(None),
        date_to: Optional[date] = Query(None),
        limit: int = Query(200, ge=1, le=400),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _require_emr_view(user)

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found")

    buff = build_patient_lab_history_pdf(
        db,
        patient_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )

    uhid = getattr(p, "uhid", "") or f"PAT-{patient_id}"
    filename = f"EMR_LAB_HISTORY_{uhid}.pdf"

    return StreamingResponse(
        buff,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
