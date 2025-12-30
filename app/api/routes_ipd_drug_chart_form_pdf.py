# FILE: app/api/routes_ipd_drug_chart_pdf.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user 
from app.models.user import User

from app.services.pdfs.ipd_drug_chart_form import build_ipd_drug_chart_pdf_bytes

router = APIRouter(prefix="/ipd", tags=["IPD PDFs"])

@router.get("/admissions/{admission_id}/drug-chart/pdf")
def ipd_drug_chart_pdf(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    # TODO: put your permission check here
    # _need_any(user, ["ipd.view", "ipd.manage", "ipd.nursing", "ipd.doctor"])

    try:
        pdf_bytes = build_ipd_drug_chart_pdf_bytes(db, admission_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Drug chart PDF failed: {e}") from e

    filename = f"IPD_DrugChart_{admission_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
