# app/services/template_context.py
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.models.patient import Patient
from app.models.opd import Appointment
from app.core.config import settings


def abs_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{settings.SITE_URL.rstrip('/')}/{path.lstrip('/')}"


def media_url(path_or_rel: str) -> str:
    if not path_or_rel:
        return ""
    # already absolute (/media/...) -> prefix with site
    if path_or_rel.startswith("/"):
        return abs_url(path_or_rel)
    # relative path under MEDIA_URL
    return abs_url(
        f"{settings.MEDIA_URL.rstrip('/')}/{path_or_rel.lstrip('/')}")


def merge_context(base: dict, extra: dict | None) -> dict:
    if not extra:
        return base
    out = {**base}
    out.update(extra)
    return out


def build_patient_context(db: Session, patient_id: int) -> dict:
    p = db.query(Patient).get(patient_id)
    if not p:
        return {}

    full_name = f"{p.first_name} {p.last_name or ''}".strip()
    patient = {
        "id": p.id,
        "uhid": p.uhid,
        "name": full_name,
        "first_name": p.first_name,
        "last_name": p.last_name or "",
        "gender": p.gender,
        "phone": p.phone or "",
        "email": p.email or "",
        "dob": p.dob.isoformat() if p.dob else None,
    }

    appt = db.query(Appointment).filter(Appointment.patient_id == p.id) \
        .order_by(desc(Appointment.date), desc(Appointment.id)).first()
    doctor_name = appt.doctor.name if appt and appt.doctor else ""
    department_name = appt.department.name if appt and appt.department else ""

    return {
        "patient": patient,
        "doctor": {
            "name": doctor_name
        },
        "department": {
            "name": department_name
        },
        "site_url": settings.SITE_URL.rstrip("/"),
        "media_url": settings.MEDIA_URL.rstrip("/"),
    }
