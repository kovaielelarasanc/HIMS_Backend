# FILE: app/services/pdf_patient_lab_history.py
from __future__ import annotations

from io import BytesIO
from typing import Optional
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lis import LisOrder
from app.services.emr_lab_report import build_emr_lab_report_object_for_pdf
from app.services.ui_branding import get_ui_branding
from app.services.pdf_lab_report_weasy import build_lab_report_pdf_bytes


def _dt_range(d_from: date, d_to: date):
    start = datetime.combine(d_from, time.min)
    end = datetime.combine(d_to + timedelta(days=1), time.min)
    return start, end


def build_patient_lab_history_pdf(
    db: Session,
    patient_id: int,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = 200,
) -> BytesIO:
    q = db.query(LisOrder).filter(LisOrder.patient_id == patient_id)

    if date_from and date_to:
        start, end = _dt_range(date_from, date_to)
        q = q.filter(LisOrder.created_at >= start).filter(
            LisOrder.created_at < end)

    orders = q.order_by(LisOrder.id.desc()).limit(limit).all()
    if not orders:
        raise HTTPException(status_code=404, detail="No lab reports found")

    branding = get_ui_branding(db)

    pdf_list: list[bytes] = []
    for o in orders:
        report_obj, patient_obj, lab_no, order_date, collected_by_name = (
            build_emr_lab_report_object_for_pdf(db, o.id))
        pdf_bytes = build_lab_report_pdf_bytes(
            branding=branding,
            report=report_obj,
            patient=patient_obj,
            lab_no=lab_no,
            order_date=order_date,
            collected_by_name=collected_by_name,
        )
        pdf_list.append(pdf_bytes)

    # Merge PDFs
    try:
        from pypdf import PdfMerger  # pip install pypdf
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="pypdf not installed. Install: pip install pypdf",
        )

    merger = PdfMerger()
    for b in pdf_list:
        merger.append(BytesIO(b))

    out = BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out
