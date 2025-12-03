# FILE: app/api/routes_audit_logs.py
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, current_user as auth_current_user
from app.models.user import User
from app.models.audit import AuditLog
from app.schemas.audit import AuditLogOut

router = APIRouter()


def has_perm(user: User, code: str) -> bool:
    """
    Simple permission helper (same logic as in routes_patients).
    """
    if getattr(user, "is_admin", False):
        return True
    for r in user.roles:
        for p in r.permissions:
            if p.code == code:
                return True
    return False


@router.get("", response_model=List[AuditLogOut])
@router.get("/", response_model=List[AuditLogOut])
def list_audit_logs(
        table_name: str = Query(...,
                                description="Table name, e.g. 'patients'"),
        record_id:
    Optional[int] = Query(
        None,
        description=
        "Record ID in that table (e.g. patient.id). If omitted, returns all for that table.",
    ),
        user_id: Optional[int] = Query(
            None,
            description=
            "Filter by user id who performed the action (optional).",
        ),
        action: Optional[str] = Query(
            None,
            description="Filter by action type (CREATE/UPDATE/DELETE)",
        ),
        from_date: Optional[date] = Query(
            None,
            description="Filter from this date (created_at >= from_date)"),
        to_date: Optional[date] = Query(
            None,
            description="Filter up to this date (created_at <= to_date)"),
        limit: int = Query(30, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        user: User = Depends(auth_current_user),
):
    """
    GET /api/audit-logs

    Example:
      /api/audit-logs?table_name=patients&record_id=2&limit=30
    """
    # You can change the permission code if you have a dedicated one
    if not has_perm(user, "auditlogs.view"):
        # fallback: allow admin only if you don't have permissions seeded yet
        if not getattr(user, "is_admin", False):
            raise HTTPException(status_code=403, detail="Not permitted")

    qry = db.query(AuditLog).filter(AuditLog.table_name == table_name)

    if record_id is not None:
        qry = qry.filter(AuditLog.record_id == record_id)

    if user_id is not None:
        qry = qry.filter(AuditLog.user_id == user_id)

    if action:
        qry = qry.filter(AuditLog.action == action)

    # Date filtering
    if from_date:
        start_dt = datetime.combine(from_date, datetime.min.time())
        qry = qry.filter(AuditLog.created_at >= start_dt)
    if to_date:
        end_dt = datetime.combine(to_date + timedelta(days=1),
                                  datetime.min.time())
        qry = qry.filter(AuditLog.created_at < end_dt)

    logs = (qry.order_by(AuditLog.id.desc()).offset(offset).limit(limit).all())

    return [AuditLogOut.model_validate(l, from_attributes=True) for l in logs]
