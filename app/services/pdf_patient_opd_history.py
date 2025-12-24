from __future__ import annotations

from io import BytesIO
from datetime import date, datetime, time, timedelta
from typing import Optional, List

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.opd import Visit
from app.services.pdf_opd_summary import build_visit_summary_pdf


def _merge_pdfs(buffers: List[BytesIO]) -> BytesIO:
    """
    Merge multiple PDFs into one.
    Tries pypdf first, then PyPDF2.
    """
    # pypdf (recommended)
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore

        writer = PdfWriter()
        for b in buffers:
            b.seek(0)
            reader = PdfReader(b)
            for page in reader.pages:
                writer.add_page(page)

        out = BytesIO()
        writer.write(out)
        out.seek(0)
        return out
    except Exception:
        pass

    # PyPDF2 fallback
    try:
        from PyPDF2 import PdfMerger  # type: ignore

        merger = PdfMerger()
        for b in buffers:
            b.seek(0)
            merger.append(b)

        out = BytesIO()
        merger.write(out)
        merger.close()
        out.seek(0)
        return out
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="PDF merge library missing. Install: pip install pypdf",
        )


def build_patient_opd_history_pdf(
    db: Session,
    patient_id: int,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = 50,
) -> BytesIO:
    """
    Builds ONE PDF that contains all OPD Visit Summary PDFs for a patient.
    Uses your existing build_visit_summary_pdf() per visit (WeasyPrint + ReportLab fallback),
    then merges into one file.
    """
    q = db.query(Visit).filter(Visit.patient_id == patient_id)

    if date_from:
        start_dt = datetime.combine(date_from, time.min)
        q = q.filter(Visit.visit_at >= start_dt)

    if date_to:
        end_dt = datetime.combine(date_to + timedelta(days=1), time.min)
        q = q.filter(Visit.visit_at < end_dt)

    visits = (q.order_by(Visit.visit_at.asc(),
                         Visit.id.asc()).limit(int(limit or 50)).all())

    if not visits:
        raise HTTPException(status_code=404,
                            detail="No OPD visits found for this patient")

    parts: List[BytesIO] = []
    for v in visits:
        parts.append(build_visit_summary_pdf(db, v.id))

    return _merge_pdfs(parts)
