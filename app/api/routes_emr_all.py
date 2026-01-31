# FILE: app/api/routes_emr_all.py
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Request, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from fastapi import HTTPException
import hashlib
import json

from app.api.deps import get_db, current_user
from app.models.user import User

from app.schemas.emr_template_library import (
    BlockCreateIn,
    BlockUpdateIn,
    BlockOut,
    TemplateSchemaValidateIn,
)

from app.services.emr_template_builder import (
    block_list,
    block_create,
    block_update,
    block_deactivate,
    section_library_list,
    section_library_create,
    section_library_update,
    normalize_template_schema,
    suggest_template_schema,
)

from app.utils.respo import err, ok

from app.schemas.emr_all import (
    TemplateCreateIn,
    TemplateUpdateIn,
    TemplateVersionCreateIn,
    TemplatePublishIn,
    RecordCreateDraftIn,
    RecordUpdateDraftIn,
    RecordSignIn,
    RecordVoidIn,
    PinToggleIn,
    InboxPushIn,
    ExportCreateBundleIn,
    ExportUpdateBundleIn,
    ExportShareCreateIn,
    DeptCreateIn,
    DeptUpdateIn,
    DeptOut,
    TypeCreateIn,
    TypeUpdateIn,
    TypeOut,

)

from app.services.emr_all_service import (
    meta,
    template_list,
    template_get,
    template_create,
    template_update,
    template_new_version,
    template_publish_toggle,
    record_create_draft,
    record_update_draft,
    record_sign,
    record_void,
    record_get,
    record_list,
    upsert_recent,
    quick_get,
    pin_patient,
    pin_record,
    inbox_list,
    inbox_ack,
    inbox_push,
    export_create_bundle,
    export_update_bundle,
    export_generate_pdf,
    export_create_share,
    export_revoke_share,
    export_download_by_token,
    list_departments,
    create_department,
    update_department,
    delete_department,
    list_record_types,
    create_record_type,
    update_record_type,
    delete_record_type,
    template_presets,
    template_preview
)

router = APIRouter(prefix="/emr", tags=["EMR All"])


def _need_any(user: User, codes: List[str]) -> None:
    if bool(getattr(user, "is_admin", False)):
        return
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) in codes:
                return
    raise HTTPException(status_code=403, detail="Not permitted")


def _client_meta(request: Request):
    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    return ip, ua


# ---------------- Departments ----------------
@router.get("/departments")
def api_dept_list(
    active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.view", "emr.templates.manage", "emr.manage", "emr.view"])
        rows = list_departments(db, active=active)
        data = [DeptOut.model_validate(r).model_dump() for r in rows]
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Department list failed: {ex}", 500)


@router.post("/departments")
def api_dept_create(
    payload: DeptCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        row = create_department(db, **payload.model_dump())
        return ok(DeptOut.model_validate(row).model_dump(), 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Department create failed: {ex}", 500)


@router.put("/departments/{dept_id}")
def api_dept_update(
    dept_id: int,
    payload: DeptUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        row = update_department(db, dept_id=dept_id, **payload.model_dump(exclude_unset=True))
        return ok(DeptOut.model_validate(row).model_dump(), 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Department update failed: {ex}", 500)


@router.delete("/departments/{dept_id}")
def api_dept_delete(
    dept_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        delete_department(db, dept_id=dept_id)
        return ok({"deleted": True}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Department delete failed: {ex}", 500)


# ---------------- Record Types ----------------
@router.get("/record-types")
def api_type_list(
    active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.view", "emr.templates.manage", "emr.manage", "emr.view"])
        rows = list_record_types(db, active=active)
        data = [TypeOut.model_validate(r).model_dump() for r in rows]
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record type list failed: {ex}", 500)


@router.post("/record-types")
def api_type_create(
    payload: TypeCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        row = create_record_type(db, **payload.model_dump())
        return ok(TypeOut.model_validate(row).model_dump(), 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record type create failed: {ex}", 500)


@router.put("/record-types/{type_id}")
def api_type_update(
    type_id: int,
    payload: TypeUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        row = update_record_type(db, type_id=type_id, **payload.model_dump(exclude_unset=True))
        return ok(TypeOut.model_validate(row).model_dump(), 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record type update failed: {ex}", 500)


@router.delete("/record-types/{type_id}")
def api_type_delete(
    type_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        delete_record_type(db, type_id=type_id)
        return ok({"deleted": True}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record type delete failed: {ex}", 500)


# -----------------------
# META
# -----------------------
@router.get("/meta")
def emr_meta(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.view"])
    return ok(meta(db), 200)


# -----------------------
# 1) TEMPLATE LIBRARY
# -----------------------
@router.get("/templates")
def api_template_list(
    page: int = Query(default=1, ge=1, le=9999),
    limit: int = Query(default=20, ge=1, le=100),
    page_size: Optional[int] = Query(default=None, ge=1, le=100),
    q: str = Query(default="", max_length=80),
    status: str = Query(default="ALL", max_length=32),
    premium: Optional[bool] = Query(default=None),
    dept: Optional[str] = Query(default=None, max_length=64),
    record_type: Optional[str] = Query(default=None, alias="type", max_length=64),
    dept_code: Optional[str] = Query(default=None, max_length=64),
    record_type_code: Optional[str] = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.view", "emr.templates.view", "emr.manage"])
        ps = int(page_size or limit or 20)
        d = (dept_code or dept or "ALL")
        tcode = (record_type_code or record_type or "ALL")
        d = d.strip().upper() if isinstance(d, str) else "ALL"
        tcode = tcode.strip().upper() if isinstance(tcode, str) else "ALL"

        data = template_list(
            db,
            q=q,
            dept_code=(d or "ALL"),
            record_type_code=(tcode or "ALL"),
            status=(status or "ALL"),
            premium=premium,
            page=page,
            page_size=ps,
        )
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template list failed: {ex}", 500)


@router.get("/templates/{template_id:int}")
def api_template_get(template_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.view", "emr.templates.view", "emr.manage"])
        # ✅ FIX: template_get is keyword-only
        return ok(template_get(db, template_id=template_id), 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template get failed: {ex}", 500)


@router.post("/templates")
def api_template_create(
    payload: TemplateCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        data = template_create(db, payload=payload.to_service_dict(), user_id=uid)
        return ok(data, 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template create failed: {ex}", 500)


@router.put("/templates/{template_id:int}")
def api_template_update(template_id: int, payload: TemplateUpdateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        data = template_update(db, template_id=template_id, payload=payload.model_dump(exclude_unset=True), user_id=uid)
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template update failed: {ex}", 500)


@router.post("/templates/{template_id:int}/versions")
def api_template_new_version(template_id: int, payload: TemplateVersionCreateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        data = template_new_version(db, template_id=template_id, payload=payload.to_service_dict(), user_id=uid)
        return ok(data, 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template new version failed: {ex}", 500)


@router.post("/templates/{template_id:int}/publish")
def api_template_publish(template_id: int, payload: TemplatePublishIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        data = template_publish_toggle(db, template_id=template_id, publish=bool(payload.publish), user_id=uid)
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Template publish toggle failed: {ex}", 500)


# -----------------------
# Patient Chart (Main Hub) aggregator
# -----------------------
@router.get("/patients/{patient_id}/chart")
def api_patient_chart(
    patient_id: int,
    q: str = Query("", max_length=80),
    status: str = Query("ALL", max_length=16),
    stage: str = Query("ALL", max_length=16),
    dept_code: str = Query("ALL", max_length=64),
    record_type_code: str = Query("ALL", max_length=64),
    page: int = Query(1, ge=1, le=9999),
    page_size: int = Query(20, ge=1, le=100),
    request: Request = None,  # FastAPI injects; keeping for compatibility with your code style
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.view", "emr.records.view", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)

        timeline = record_list(
            db,
            patient_id=patient_id,
            q=q,
            status=status,
            stage=stage,
            dept_code=dept_code,
            record_type_code=record_type_code,
            page=page,
            page_size=page_size,
        )

        # recent tracking (patient-only => record_id 0)
        upsert_recent(db, user_id=uid, patient_id=patient_id, record_id=None)

        return ok(
            {
                "patient_id": patient_id,
                "timeline": timeline,
                "actions": {"can_create": True, "can_export": True},
            },
            200,
        )
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Patient chart failed: {ex}", 500)


# -----------------------
# 2) RECORDS
# -----------------------
@router.get("/records")
def api_record_list(
    patient_id: Optional[int] = Query(None),
    q: str = Query("", max_length=80),
    status: str = Query("ALL", max_length=16),
    stage: str = Query("ALL", max_length=16),
    dept_code: str = Query("ALL", max_length=64),
    record_type_code: str = Query("ALL", max_length=64),
    page: int = Query(1, ge=1, le=9999),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "emr.records.view", "emr.manage"])
    return ok(
        record_list(
            db,
            patient_id=patient_id,
            q=q,
            status=status,
            stage=stage,
            dept_code=dept_code,
            record_type_code=record_type_code,
            page=page,
            page_size=page_size,
        ),
        200,
    )


@router.post("/records/draft")
def api_record_create_draft(payload: RecordCreateDraftIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.records.create", "emr.manage", "emr.view"])
        uid = int(getattr(user, "id", 0) or 0)
        ip, ua = _client_meta(request)
        allow_unpublished = bool(getattr(user, "is_admin", False))

        data = record_create_draft(
            db,
            payload=payload.model_dump(),
            user_id=uid,
            ip=ip,
            ua=ua,
            allow_unpublished_template=allow_unpublished,
        )
        return ok(data, 201)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record create draft failed: {ex}", 500)


@router.put("/records/{record_id}")
def api_record_update_draft(record_id: int, payload: RecordUpdateDraftIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.records.update", "emr.manage", "emr.view"])
        uid = int(getattr(user, "id", 0) or 0)
        ip, ua = _client_meta(request)

        data = record_update_draft(
            db,
            record_id=record_id,
            payload=payload.model_dump(exclude_unset=True),
            user_id=uid,
            ip=ip,
            ua=ua,
        )
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record update failed: {ex}", 500)


@router.get("/records/{record_id}")
def api_record_get(record_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.view", "emr.records.view", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        ip, ua = _client_meta(request)

        data = record_get(db, record_id=record_id, user_id=uid, ip=ip, ua=ua)

        # recent tracking (record-level)
        try:
            upsert_recent(db, user_id=uid, patient_id=data["record"]["patient_id"], record_id=record_id)
        except Exception:
            pass

        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record get failed: {ex}", 500)


@router.post("/records/{record_id}/sign")
def api_record_sign(record_id: int, payload: RecordSignIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.records.sign", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        ip, ua = _client_meta(request)

        data = record_sign(db, record_id=record_id, user_id=uid, ip=ip, ua=ua, sign_note=payload.sign_note)
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record sign failed: {ex}", 500)


@router.post("/records/{record_id}/void")
def api_record_void(record_id: int, payload: RecordVoidIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        _need_any(user, ["emr.records.void", "emr.manage"])
        uid = int(getattr(user, "id", 0) or 0)
        ip, ua = _client_meta(request)

        data = record_void(db, record_id=record_id, user_id=uid, ip=ip, ua=ua, reason=payload.reason)
        return ok(data, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Record void failed: {ex}", 500)


# -----------------------
# RECENT & PINNED
# -----------------------
@router.get("/quick")
def api_quick(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.view"])
    uid = int(getattr(user, "id", 0) or 0)
    return ok(quick_get(db, user_id=uid), 200)


@router.post("/quick/pin/patient/{patient_id}")
def api_pin_patient(patient_id: int, payload: PinToggleIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.view"])
    uid = int(getattr(user, "id", 0) or 0)
    return ok(pin_patient(db, user_id=uid, patient_id=patient_id, pinned=bool(payload.pinned)), 200)


@router.post("/quick/pin/record/{record_id}")
def api_pin_record(record_id: int, payload: PinToggleIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.view"])
    uid = int(getattr(user, "id", 0) or 0)
    return ok(pin_record(db, user_id=uid, record_id=record_id, pinned=bool(payload.pinned)), 200)


# -----------------------
# INBOX
# -----------------------
@router.get("/inbox")
def api_inbox(
    bucket: str = Query("pending_signature"),
    q: str = Query("", max_length=80),
    page: int = Query(1, ge=1, le=9999),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.view", "emr.inbox.view", "emr.manage"])
    return ok(inbox_list(db, bucket=bucket, q=q, page=page, page_size=page_size), 200)


@router.post("/inbox/{inbox_id}/ack")
def api_inbox_ack(inbox_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.view", "emr.inbox.ack", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    return ok(inbox_ack(db, inbox_id=inbox_id, user_id=uid), 200)


@router.post("/inbox/push")
def api_inbox_push(payload: InboxPushIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.inbox.push", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    return ok(inbox_push(db, payload=payload.model_dump(), user_id=uid), 201)


# -----------------------
# EXPORTS
# -----------------------
@router.post("/exports/bundles")
def api_export_create_bundle(payload: ExportCreateBundleIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.export.create", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    ip, ua = _client_meta(request)
    data = export_create_bundle(db, payload=payload.model_dump(), user_id=uid, ip=ip, ua=ua)
    return ok(data, 201)


@router.put("/exports/bundles/{bundle_id}")
def api_export_update_bundle(bundle_id: int, payload: ExportUpdateBundleIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.export.update", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    ip, ua = _client_meta(request)
    data = export_update_bundle(db, bundle_id=bundle_id, payload=payload.model_dump(exclude_unset=True), user_id=uid, ip=ip, ua=ua)
    return ok(data, 200)


@router.post("/exports/bundles/{bundle_id}/generate")
def api_export_generate(
    bundle_id: int,
    request: Request,
    paper: str = Query("A4", pattern="^(A3|A4|A5)$"),
    orientation: str = Query("portrait", pattern="^(portrait|landscape)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.export.generate", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    ip, ua = _client_meta(request)
    data = export_generate_pdf(
        db,
        bundle_id=bundle_id,
        user_id=uid,
        ip=ip,
        ua=ua,
        paper=paper,
        orientation=orientation,
    )
    return ok(data, 200)


@router.post("/exports/bundles/{bundle_id}/share")
def api_export_share(bundle_id: int, payload: ExportShareCreateIn, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.export.share", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    ip, ua = _client_meta(request)

    data = export_create_share(
        db,
        bundle_id=bundle_id,
        user_id=uid,
        ip=ip,
        ua=ua,
        expires_in_days=int(payload.expires_in_days or 7),
        max_downloads=int(payload.max_downloads or 5),
    )
    return ok(data, 201)


@router.post("/exports/shares/{share_id}/revoke")
def api_export_revoke(share_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _need_any(user, ["emr.export.revoke", "emr.manage"])
    uid = int(getattr(user, "id", 0) or 0)
    ip, ua = _client_meta(request)
    data = export_revoke_share(db, share_id=share_id, user_id=uid, ip=ip, ua=ua)
    return ok(data, 200)


@router.get("/exports/share/{token}")
def api_export_download_share(token: str, db: Session = Depends(get_db)):
    """
    Public download endpoint (no auth). Token is hashed in DB.
    """
    filename, file_bytes, _ = export_download_by_token(db, token_plain=token)
    return StreamingResponse(
        iter([file_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------
# SECTION LIBRARY
# -----------------------
@router.get("/sections/library")
def api_section_library_list(
    q: str = Query("", max_length=80),
    dept_code: str = Query("ALL", max_length=64),
    record_type_code: str = Query("ALL", max_length=64),
    active: Optional[bool] = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.view", "emr.templates.manage", "emr.manage", "emr.view"])
    rows = section_library_list(db, q=q, dept_code=dept_code, record_type_code=record_type_code, active=active)
    data = [
        {
            "id": int(r.id),
            "code": r.code,
            "label": r.label,
            "dept_code": r.dept_code,
            "record_type_code": r.record_type_code,
            "group": r.group,
            "is_active": bool(r.is_active),
            "display_order": int(r.display_order),
        }
        for r in rows
    ]
    return ok(data, 200)


@router.post("/sections/library")
def api_section_library_create(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    row = section_library_create(db, payload=payload)
    return ok(
        {
            "id": int(row.id),
            "code": row.code,
            "label": row.label,
            "dept_code": row.dept_code,
            "record_type_code": row.record_type_code,
            "group": row.group,
            "is_active": bool(row.is_active),
            "display_order": int(row.display_order),
        },
        201,
    )


@router.put("/sections/library/{section_id}")
def api_section_library_update(
    section_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    row = section_library_update(db, section_id=section_id, payload=payload)
    return ok(
        {
            "id": int(row.id),
            "code": row.code,
            "label": row.label,
            "dept_code": row.dept_code,
            "record_type_code": row.record_type_code,
            "group": row.group,
            "is_active": bool(row.is_active),
            "display_order": int(row.display_order),
        },
        200,
    )


# -----------------------
# BLOCK LIBRARY
# -----------------------
@router.get("/blocks/library")
def api_block_list(
    q: str = Query("", max_length=80),
    dept_code: str = Query("ALL", max_length=64),
    record_type_code: str = Query("ALL", max_length=64),
    active: Optional[bool] = Query(True),
    page: int = Query(1, ge=1, le=9999),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.view", "emr.templates.manage", "emr.manage", "emr.view"])
    return ok(block_list(db, q=q, dept_code=dept_code, record_type_code=record_type_code, active=active, page=page, page_size=page_size), 200)


@router.post("/blocks/library")
def api_block_create(
    payload: BlockCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    row = block_create(db, payload=payload.to_db_dict())
    return ok(BlockOut.model_validate(row).model_dump(), 201)


@router.put("/blocks/library/{block_id}")
def api_block_update(
    block_id: int,
    payload: BlockUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    row = block_update(db, block_id=block_id, payload=payload.to_db_dict())
    return ok(BlockOut.model_validate(row).model_dump(), 200)


@router.delete("/blocks/library/{block_id}")
def api_block_deactivate(
    block_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    block_deactivate(db, block_id=block_id)
    return ok({"deactivated": True}, 200)


# -----------------------
# TEMPLATE VALIDATE
# -----------------------
@router.post("/templates/validate")
def api_template_validate(
    payload: TemplateSchemaValidateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    _need_any(user, ["emr.templates.manage", "emr.manage"])
    norm = normalize_template_schema(
        db,
        dept_code=payload.dept_code,
        record_type_code=payload.record_type_code,
        schema_input=payload.schema_json,
        sections_input=payload.sections,
    )
    return ok(norm, 200)

@router.get("/patients/{patient_id}/encounters")
def api_patient_encounters(
    patient_id: int,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        _need_any(user, ["emr.view", "emr.records.view", "emr.manage"])

        from app.services.emr_all_service import patient_encounters  # local import safe

        items = patient_encounters(db, patient_id=int(patient_id), limit=int(limit))
        return ok({"patient_id": int(patient_id), "items": items}, 200)
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Encounters fetch failed: {ex}", 500)


# -----------------------
# TEMPLATE NORMALIZE + HASH (for Visual Builder)
# -----------------------

def _stable_json_for_hash(obj) -> str:
    """
    Stable canonical JSON for hashing:
    - sort keys
    - no whitespace
    - UTF-8 safe
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@router.post("/templates/normalize")
def api_template_normalize(
    payload: TemplateSchemaValidateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """
    Server-side normalization. Same input as /templates/validate.
    Frontend uses this for consistent schema canonicalization.
    """
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])
        norm = normalize_template_schema(
            db,
            dept_code=payload.dept_code,
            record_type_code=payload.record_type_code,
            schema_input=payload.schema_json,
            sections_input=payload.sections,
            strict=bool(getattr(payload, 'strict', False)),
        )
        return ok(norm, 200)
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as ex:
        return err(f"Template normalize failed: {ex}", 500)


@router.post("/templates/hash")
def api_template_hash(
    payload: TemplateSchemaValidateIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """
    Server-side stable hash of the normalized schema.
    Always normalizes first to avoid hash drift across clients.
    """
    try:
        _need_any(user, ["emr.templates.manage", "emr.manage"])

        norm = normalize_template_schema(
            db,
            dept_code=payload.dept_code,
            record_type_code=payload.record_type_code,
            schema_input=payload.schema_json,
            sections_input=payload.sections,
            strict=bool(getattr(payload, 'strict', False)),
        )

        raw = _stable_json_for_hash(norm).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()

        return ok({"hash": sha, "algo": "sha256", "normalized": norm}, 200)
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as ex:
        return err(f"Template hash failed: {ex}", 500)


@router.get("/templates/builder/meta")
def api_template_builder_meta(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """
    Builder metadata for frontend (field types, presets, etc.).
    Keep response backward-compatible and safe to extend.
    """
    try:
        _need_any(user, ["emr.templates.view", "emr.templates.manage", "emr.manage", "emr.view"])

        # Minimal, stable contract (extend anytime without breaking UI)
        field_types = [
            {"type": "text", "label": "Text"},
            {"type": "textarea", "label": "Textarea"},
            {"type": "number", "label": "Number"},
            {"type": "date", "label": "Date"},
            {"type": "time", "label": "Time"},
            {"type": "datetime", "label": "Date & Time"},
            {"type": "boolean", "label": "Yes/No"},
            {"type": "select", "label": "Select"},
            {"type": "multiselect", "label": "Multi Select"},
            {"type": "radio", "label": "Radio"},
            {"type": "chips", "label": "Chips/Tags"},
            {"type": "table", "label": "Table"},
            {"type": "group", "label": "Group"},
            {"type": "signature", "label": "Signature"},
            {"type": "file", "label": "File"},
            {"type": "image", "label": "Image"},
            {"type": "calculation", "label": "Calculation"},
        ]

        return ok(
            {
                "field_types": field_types,
                "clinical_concepts": [],     # safe default; can be backend-driven later
                "validation_presets": [],    # safe default; can be backend-driven later
                "ui_presets": [],            # safe default
                "version": 1,
            },
            200,
        )
    except HTTPException:
        raise
    except Exception as ex:
        return err(f"Builder meta failed: {ex}", 500)
