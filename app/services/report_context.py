# app/services/report_context.py
from sqlalchemy.orm import Session
from app.services.template_context import build_patient_context


def build_render_context(db: Session, patient_id: int) -> dict:
    return build_patient_context(db, patient_id)
