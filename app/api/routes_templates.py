# app/api/routes_templates.py
from __future__ import annotations
from datetime import datetime
from typing import Optional
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import StreamingResponse
from jinja2 import Environment, BaseLoader, select_autoescape
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user
from app.core.config import settings
from app.models.user import User
from app.models.template import DocumentTemplate, TemplateRevision, PatientConsentTemp
from app.schemas.template import (DocumentTemplateCreate,
                                  DocumentTemplateUpdate, DocumentTemplateOut,
                                  ConsentCreateFromTemplate, PatientConsentOut)
from app.services.template_context import abs_url, media_url, build_patient_context, merge_context
from app.services.pdf import render_html, generate_pdf

router = APIRouter()
MEDIA_ROOT = Path(settings.STORAGE_DIR).resolve()


def _need_any(user: User, codes: list[str]):
    # TODO: plug real RBAC
    if getattr(user, "is_admin", False):
        return


def _jinja_env():
    env = Environment(loader=BaseLoader(),
                      autoescape=select_autoescape(enabled_extensions=("html",
                                                                       "xml")))

    def datefmt(val, fmt="%d-%b-%Y"):
        if not val: return ""
        try:
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace(
                        "Z", "+00:00")).strftime(fmt)
                except Exception:
                    return val
            return val.strftime(fmt)
        except Exception:
            return str(val)

    env.filters["datefmt"] = datefmt
    env.globals["abs_url"] = abs_url
    env.globals["media"] = media_url
    return env


# ---------- Templates CRUD ----------
@router.post("/templates", response_model=DocumentTemplateOut)
def create_template(payload: DocumentTemplateCreate,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    _need_any(user, ["templates.manage"])
    exists = db.query(DocumentTemplate).filter(
        DocumentTemplate.code == payload.code).first()
    if exists:
        raise HTTPException(409, "Template code already exists")
    t = DocumentTemplate(
        name=payload.name.strip(),
        code=payload.code.strip(),
        category=(payload.category or "report"),
        subcategory=payload.subcategory,
        description=payload.description,
        html=payload.html or "",
        css=payload.css or "",
        placeholders=payload.placeholders or {},
        is_active=bool(payload.is_active),
        version=1,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(t)
    db.flush()
    db.add(
        TemplateRevision(template_id=t.id,
                         version=1,
                         html=t.html,
                         css=t.css,
                         placeholders=t.placeholders,
                         updated_by=user.id))
    db.commit()
    return DocumentTemplateOut.model_validate(t)


@router.get("/templates", response_model=list[DocumentTemplateOut])
def list_templates(category: Optional[str] = Query(None),
                   active: Optional[bool] = Query(None),
                   q: Optional[str] = Query(None),
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    _need_any(user, ["templates.view"])
    query = db.query(DocumentTemplate)
    if category: query = query.filter(DocumentTemplate.category == category)
    if active is not None:
        query = query.filter(DocumentTemplate.is_active == active)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((DocumentTemplate.name.ilike(like))
                             | (DocumentTemplate.code.ilike(like)))
    rows = query.order_by(DocumentTemplate.id.desc()).limit(200).all()
    return [DocumentTemplateOut.model_validate(r) for r in rows]


@router.get("/templates/{template_id}", response_model=DocumentTemplateOut)
def get_template(template_id: int,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    _need_any(user, ["templates.view"])
    t = db.query(DocumentTemplate).get(template_id)
    if not t:
        raise HTTPException(404, "Template not found")
    return DocumentTemplateOut.model_validate(t)


@router.patch("/templates/{template_id}", response_model=DocumentTemplateOut)
def update_template(template_id: int,
                    payload: DocumentTemplateUpdate,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    _need_any(user, ["templates.manage"])
    t = db.query(DocumentTemplate).get(template_id)
    if not t:
        raise HTTPException(404, "Template not found")
    changed = payload.model_dump(exclude_unset=True)
    if changed:
        for k, v in changed.items():
            setattr(t, k, v)
        t.updated_by = user.id
        if any(k in changed for k in ("html", "css", "placeholders")):
            t.version = int(t.version or 1) + 1
            db.add(
                TemplateRevision(template_id=t.id,
                                 version=t.version,
                                 html=t.html,
                                 css=t.css,
                                 placeholders=t.placeholders,
                                 updated_by=user.id))
        db.commit()
    return DocumentTemplateOut.model_validate(t)


@router.delete("/templates/{template_id}")
def delete_template(template_id: int,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    _need_any(user, ["templates.manage"])
    t = db.query(DocumentTemplate).get(template_id)
    if not t:
        raise HTTPException(404, "Template not found")
    db.delete(t)
    db.commit()
    return {"message": "Deleted"}


# ---------- Preview HTML ----------
class _RenderBody(BaseModel):
    data: Optional[dict] = None


from pydantic import BaseModel


@router.post("/templates/{template_id}/render-html")
def render_template_html_post(
        request: Request,
        template_id: int,
        patient_id: int = Query(...),
        payload: _RenderBody | None = Body(default=None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["templates.view", "patients.view", "emr.view"])
    t = db.query(DocumentTemplate).get(template_id)
    if not t or not t.is_active:
        raise HTTPException(404, "Template not found")

    base_ctx = build_patient_context(db, patient_id)
    ctx = merge_context(base_ctx, (payload.data if payload else None))

    env = _jinja_env()
    body = env.from_string(t.html or "").render(**ctx)

    base_url = str(request.base_url).rstrip("/")
    html = render_html(body, t.css or "", ctx, base_url=base_url)
    return {"html": html}


# ---------- Render PDF ----------
class _PdfBody(BaseModel):
    patient_id: int
    data: Optional[dict] = None
    inline: Optional[bool] = False
    engine: Optional[str] = None  # "weasyprint" | "xhtml2pdf" | None


@router.get("/templates/{template_id}/pdf")
def render_template_pdf_get(
        request: Request,
        template_id: int,
        patient_id: int = Query(...),
        inline: bool = Query(False),
        engine: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # GET variant (no extra body data)
    return _render_pdf(request, template_id, patient_id, inline, engine, None,
                       db, user)


@router.post("/templates/{template_id}/pdf")
def render_template_pdf_post(
        request: Request,
        template_id: int,
        body: _PdfBody,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    return _render_pdf(request, template_id, body.patient_id,
                       bool(body.inline), body.engine, (body.data or None), db,
                       user)


def _render_pdf(request: Request, template_id: int, patient_id: int,
                inline: bool, engine: Optional[str],
                extra_data: Optional[dict], db: Session, user: User):
    _need_any(user, ["templates.view", "patients.view", "emr.view"])
    t = db.query(DocumentTemplate).get(template_id)
    if not t or not t.is_active:
        raise HTTPException(404, "Template not found")

    base_ctx = build_patient_context(db, patient_id)
    ctx = merge_context(base_ctx, extra_data)

    env = _jinja_env()
    body = env.from_string(t.html or "").render(**ctx)
    full = render_html(body, t.css or "", ctx)

    base_url = str(request.base_url).rstrip("/")
    pdf_bytes, used_engine = generate_pdf(full,
                                          base_url=base_url,
                                          prefer=engine)
    print("PDF engine:", used_engine)

    fname = f"{ctx.get('patient',{}).get('uhid','UHID')}-{(ctx.get('patient',{}).get('name') or 'Report').replace('/', '-')}-{t.code}.pdf".replace(
        ' ', '_')
    disp = "inline" if inline else "attachment"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disp}; filename="{fname}"'})


# ---------- Consents ----------
@router.post("/patients/{patient_id}/consents",
             response_model=PatientConsentOut)
def create_consent_from_template(patient_id: int,
                                 payload: ConsentCreateFromTemplate,
                                 db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    _need_any(user, ["consents.manage", "patients.view"])
    t = db.query(DocumentTemplate).get(payload.template_id)
    if not t or t.category != "consent":
        raise HTTPException(404, "Consent template not found")

    base_ctx = build_patient_context(db, patient_id)
    ctx = merge_context(base_ctx, payload.data)

    env = _jinja_env()
    body = env.from_string(t.html or "").render(**ctx)
    full_html = render_html(body, t.css or "", ctx)
    pdf_bytes, engine = generate_pdf(full_html,
                                     base_url=settings.SITE_URL.rstrip("/"))

    subdir = datetime.utcnow().strftime("consents/%Y/%m/%d")
    dest_dir = MEDIA_ROOT / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    uhid = base_ctx.get("patient", {}).get("uhid") or str(patient_id)
    pname = (base_ctx.get("patient", {}).get("name") or "").replace("/", "-")
    filename = f"{uhid}-{pname}-{t.code}.pdf".strip("-").replace(" ", "_")
    dest_path = dest_dir / filename
    with dest_path.open("wb") as f:
        f.write(pdf_bytes)

    rel = dest_path.relative_to(MEDIA_ROOT).as_posix()
    c = PatientConsentTemp(patient_id=patient_id,
                           template_id=t.id,
                           data=payload.data or {},
                           html_rendered=full_html,
                           pdf_path=f"{settings.MEDIA_URL.rstrip('/')}/{rel}",
                           status="finalized" if payload.finalize else "draft",
                           signed_by=payload.signed_by,
                           witness_name=payload.witness_name,
                           created_by=user.id)
    db.add(c)
    db.commit()
    return PatientConsentOut.model_validate(c)


@router.get("/patients/{patient_id}/consents",
            response_model=list[PatientConsentOut])
def list_patient_consents(patient_id: int,
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    _need_any(user, ["consents.view", "patients.view"])
    rows = db.query(PatientConsentTemp).filter(PatientConsentTemp.patient_id == patient_id) \
        .order_by(PatientConsentTemp.id.desc()).limit(200).all()
    return [PatientConsentOut.model_validate(r) for r in rows]
