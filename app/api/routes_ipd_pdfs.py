# FILE: app/api/routes_ipd_pdfs.py
from __future__ import annotations

from typing import Optional, Any
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.models.user import User
from app.api.deps import get_db, current_user

# ✅ keep your import (adjust if your folder is app/services/pdf not pdfs)
from app.services.pdfs.ipd_case_sheet import build_ipd_case_sheet_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf/ipd", tags=["IPD PDFs"])


# -------------------------
# Permissions helper
# -------------------------
def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False


def _can_ipd_pdf(user: User) -> bool:
    return (
        getattr(user, "is_admin", False)
        or has_perm(user, "ipd.view")
        or has_perm(user, "ipd.manage")
        or has_perm(user, "ipd.nursing")
        or has_perm(user, "ipd.doctor")
        or has_perm(user, "pdf.ipd.case_sheet")
        or has_perm(user, "*")
    )


def _extract_pdf_bytes(obj: Any) -> Optional[bytes]:
    """Accept bytes OR (bytes, meta) OR (meta, bytes) etc."""
    if obj is None:
        return None
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj)
    if isinstance(obj, (tuple, list)):
        for x in obj:
            b = _extract_pdf_bytes(x)
            if b:
                return b
        return None
    if isinstance(obj, dict):
        for x in obj.values():
            b = _extract_pdf_bytes(x)
            if b:
                return b
        return None
    return None


@router.get("/admissions/{admission_id}/case-sheet")
def get_ipd_case_sheet_pdf(
    admission_id: int,
    template_id: Optional[int] = None,
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not _can_ipd_pdf(user):
        raise HTTPException(403, "Not permitted")

    try:
        # ✅ call builder (works whether or not it supports user=)
        try:
            result = build_ipd_case_sheet_pdf(
                db=db,
                admission_id=admission_id,
                template_id=template_id,
                period_from=period_from,
                period_to=period_to,
                user=user,
            )
        except TypeError:
            result = build_ipd_case_sheet_pdf(
                db=db,
                admission_id=admission_id,
                template_id=template_id,
                period_from=period_from,
                period_to=period_to,
            )

        pdf_bytes = _extract_pdf_bytes(result)

        if not pdf_bytes:
            logger.error("Case-sheet builder returned non-bytes type=%s value=%r", type(result), result)
            raise HTTPException(500, "PDF builder did not return PDF bytes")

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="IPD_CaseSheet_{admission_id}.pdf"'
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("IPD case-sheet PDF generation failed admission_id=%s", admission_id)
        raise HTTPException(500, f"PDF generation failed: {e}")
