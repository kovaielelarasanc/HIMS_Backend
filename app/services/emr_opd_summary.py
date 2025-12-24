# app/services/emr_opd_summary.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, date

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import case

from app.models.opd import (
    Visit,
    Vitals,
    Prescription,
    PrescriptionItem,
    LabOrder,
    RadiologyOrder,
    LabTest,
    RadiologyTest,
    FollowUp,
)
from app.models.patient import Patient
from app.models.department import Department
from app.models.user import User


def _safe(v: Any) -> Any:
    return None if v is None else v


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return s


def _present(v: Any) -> bool:
    s = _clean_str(v)
    return bool(s) and s not in ("—", "-", "None", "null", "NULL")


def _iso(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _to_date(v: Any) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return datetime.fromisoformat(str(v).replace("Z",
                                                     "")).date().isoformat()
    except Exception:
        return str(v)[:10]


def _patient_name(p: Patient) -> str:
    name = f"{getattr(p, 'first_name', '')} {getattr(p, 'last_name', '')}".strip(
    )
    return name or (getattr(p, "full_name", "") or "—")


def _patient_phone(p: Patient) -> str:
    return (getattr(p, "phone", "") or getattr(p, "mobile", "") or "—")


def build_emr_opd_visit_summary(db: Session, visit_id: int) -> Dict[str, Any]:
    v: Visit = (db.query(Visit).options(
        joinedload(Visit.patient),
        joinedload(Visit.department),
        joinedload(Visit.doctor),
        joinedload(Visit.appointment),
    ).filter(Visit.id == visit_id).first())
    if not v:
        raise HTTPException(status_code=404, detail="Visit not found")

    patient: Patient = v.patient
    dept: Department = v.department
    doctor: User = v.doctor

    # --- vitals: appointment-linked preferred, else latest by patient
    vit = None
    if getattr(v, "appointment_id", None) and hasattr(Vitals,
                                                      "appointment_id"):
        vit = (db.query(Vitals).filter(
            Vitals.appointment_id == v.appointment_id).order_by(
                Vitals.created_at.desc()).first())
    if not vit:
        vit = (db.query(Vitals).filter(
            Vitals.patient_id == v.patient_id).order_by(
                Vitals.created_at.desc()).first())

    vitals_payload = None
    if vit:
        vitals_payload = {
            "height_cm": _safe(getattr(vit, "height_cm", None)),
            "weight_kg": _safe(getattr(vit, "weight_kg", None)),
            "bp_systolic": _safe(getattr(vit, "bp_systolic", None)),
            "bp_diastolic": _safe(getattr(vit, "bp_diastolic", None)),
            "pulse": _safe(getattr(vit, "pulse", None)),
            "rr": _safe(getattr(vit, "rr", None)),
            "temp_c": _safe(getattr(vit, "temp_c", None)),
            "spo2": _safe(getattr(vit, "spo2", None)),
            "notes": _safe(getattr(vit, "notes", None)),
            "created_at": _iso(getattr(vit, "created_at", None)),
        }

    # --- prescription + items
    rx = db.query(Prescription).filter(
        Prescription.visit_id == visit_id).first()
    rx_items: List[PrescriptionItem] = []
    if rx:
        rx_items = (db.query(PrescriptionItem).filter(
            PrescriptionItem.prescription_id == rx.id).order_by(
                PrescriptionItem.id.asc()).all())

    rx_payload = {
        "notes":
        _safe(getattr(rx, "notes", None)) if rx else None,
        "items": [{
            "drug_name":
            _clean_str(getattr(it, "drug_name", ""))
            or _clean_str(getattr(it, "medicine_name", "")),
            "strength":
            _safe(getattr(it, "strength", None)),
            "frequency":
            _safe(getattr(it, "frequency", None))
            or _safe(getattr(it, "frequency_code", None)),
            "duration_days":
            int(
                getattr(it, "duration_days", 0) or getattr(it, "days", 0)
                or 0),
            "quantity":
            int(getattr(it, "quantity", 0) or 0),
            "unit_price":
            float(getattr(it, "unit_price", 0) or 0),
            "route":
            _safe(getattr(it, "route", None)),
            "timing":
            _safe(getattr(it, "timing", None))
            or _safe(getattr(it, "instructions", None)),
        } for it in rx_items],
    }

    # --- orders
    lab_rows = (db.query(LabOrder, LabTest.name).join(
        LabTest, LabTest.id == LabOrder.test_id).filter(
            LabOrder.visit_id == visit_id).all())
    lab_names = [n for (_, n) in lab_rows if n]

    rad_rows = (db.query(RadiologyOrder, RadiologyTest.name).join(
        RadiologyTest, RadiologyTest.id == RadiologyOrder.test_id).filter(
            RadiologyOrder.visit_id == visit_id).all())
    rad_names = [n for (_, n) in rad_rows if n]

    # --- followups (optional)
    followups_q = db.query(FollowUp)
    if hasattr(FollowUp, "patient_id"):
        followups_q = followups_q.filter(FollowUp.patient_id == v.patient_id)
    else:
        followups_q = followups_q.join(
            Visit, FollowUp.source_visit_id == Visit.id).filter(
                Visit.patient_id == v.patient_id)

    followups = (followups_q.order_by(
        case((FollowUp.due_date.is_(None), 1), else_=0),
        FollowUp.due_date.desc(),
        FollowUp.id.desc(),
    ).limit(10).all())

    followup_payload = [{
        "id":
        fu.id,
        "due_date":
        _to_date(getattr(fu, "due_date", None)),
        "status":
        _safe(getattr(fu, "status", None)),
        "note":
        _safe(getattr(fu, "note", None)),
        "created_at":
        _iso(getattr(fu, "created_at", None)),
        "source_visit_id":
        _safe(getattr(fu, "source_visit_id", None)),
    } for fu in followups]

    # --- dynamic clinical sections (best for reuse)
    section_defs = [
        ("chief_complaint", "Chief Complaint"),
        ("presenting_illness", "Presenting Illness (HPI)"),
        ("symptoms", "Symptoms"),
        ("review_of_systems", "Review of Systems"),
        ("medical_history", "Past Medical History"),
        ("surgical_history", "Past Surgical History"),
        ("medication_history", "Medication History"),
        ("drug_allergy", "Drug Allergy"),
        ("family_history", "Family History"),
        ("personal_history", "Personal History"),
        ("general_examination", "General Examination"),
        ("systemic_examination", "Systemic Examination"),
        ("local_examination", "Local Examination"),
        ("provisional_diagnosis", "Provisional Diagnosis"),
        ("differential_diagnosis", "Differential Diagnosis"),
        ("final_diagnosis", "Final Diagnosis"),
        ("diagnosis_codes", "Diagnosis Codes (ICD)"),
        ("investigations", "Investigations"),
        ("treatment_plan", "Treatment Plan"),
        ("advice", "Advice / Counselling"),
        ("followup_plan", "Follow-up Plan"),
        ("referral_notes", "Referral Notes"),
        ("procedure_notes", "Procedure Notes"),
        ("counselling_notes", "Counselling Notes"),
    ]

    sections = []
    for key, title in section_defs:
        val = getattr(v, key, None)
        if _present(val):
            sections.append({"key": key, "title": title, "value": str(val)})

    payload: Dict[str, Any] = {
        "meta": {
            "visit_id":
            v.id,
            "episode_id":
            _safe(getattr(v, "episode_id", None)) or "",
            "visit_at":
            _iso(getattr(v, "visit_at", None)),
            "appointment_id":
            _safe(getattr(v, "appointment_id", None)),
            "appointment_status":
            _safe(getattr(getattr(v, "appointment", None), "status", None)),
            "doctor_name":
            _safe(getattr(doctor, "name", None))
            or _safe(getattr(doctor, "full_name", None)) or "—",
            "department_name":
            _safe(getattr(dept, "name", None)) or "—",
        },
        "patient": {
            "id":
            patient.id,
            "uhid":
            _safe(getattr(patient, "uhid", None)) or "—",
            "name":
            _patient_name(patient),
            "phone":
            _patient_phone(patient),
            "dob":
            _to_date(
                getattr(patient, "dob", None)
                or getattr(patient, "date_of_birth", None)),
            "gender":
            _safe(getattr(patient, "gender", None))
            or _safe(getattr(patient, "sex", None)) or "—",
        },
        "vitals": vitals_payload,
        "orders": {
            "lab": lab_names,
            "radiology": rad_names
        },
        "rx": rx_payload,
        "followups": followup_payload,
        "sections": sections,
    }
    return payload
