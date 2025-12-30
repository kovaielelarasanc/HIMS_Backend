# FILE: app/api/routes_pdf_templates.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session


from app.models.user import User
from app.api.deps import get_db, current_user

# import your pdf template model / crud (adjust)
from app.models.pdf_template import PdfTemplate  # <-- change to your actual model

router = APIRouter(prefix="/pdf", tags=["PDF"])

def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in getattr(user, "roles", []) or []:
        for p in getattr(r, "permissions", []) or []:
            if getattr(p, "code", None) == code:
                return True
    return False

def _can_view_templates(user: User, module_code: str) -> bool:
    # ✅ Best: admin OR global pdf view OR module view/manage OR module-specific roles
    if getattr(user, "is_admin", False):
        return True

    allow = [
        "pdf.templates.view",
        f"{module_code}.view",
        f"{module_code}.manage",
        f"{module_code}.doctor",
        f"{module_code}.nursing",
        "*",
    ]
    return any(has_perm(user, c) for c in allow)


@router.get("/templates")
def list_pdf_templates(
    module: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    module_code = (module or "").strip().lower()
    if not module_code:
        raise HTTPException(400, "module is required")

    if not _can_view_templates(user, module_code):
        raise HTTPException(403, "Not permitted")

    q = (
        db.query(PdfTemplate)
        .filter(PdfTemplate.module == module_code)
        .order_by(PdfTemplate.is_active.desc(), PdfTemplate.id.desc())
    )
    items = q.all()

    return {"status": True, "data": [x.to_dict() for x in items]}  # adjust serializer


@router.get("/templates/{template_id}")
def get_pdf_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    t = db.query(PdfTemplate).filter(PdfTemplate.id == template_id).first()
    if not t:
        raise HTTPException(404, "Template not found")

    if not _can_view_templates(user, (t.module or "").lower()):
        raise HTTPException(403, "Not permitted")

    return {"status": True, "data": t.to_dict()}  # adjust serializer
