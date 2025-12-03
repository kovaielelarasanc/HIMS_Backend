from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.ot import OtOrder

router = APIRouter(prefix="/ot", tags=["OT History"])


def _need_any(user: User, codes: list[str]):
    if getattr(user, "is_admin", False):
        return
    for r in (user.roles or []):
        for p in (r.permissions or []):
            if p.code in codes:
                return
    raise HTTPException(403, "Not permitted")


@router.get("/history")
def ot_history(
        patient_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    _need_any(user, ["ot.cases.view", "ipd.view", "visits.update"])
    rows = (db.query(OtOrder).options(joinedload(OtOrder.attachments)).filter(
        OtOrder.patient_id == patient_id).order_by(
            OtOrder.scheduled_start.desc().nullslast(),
            OtOrder.id.desc(),
        ).all())
    out = []
    for c in rows:
        out.append({
            "order_id":
            c.id,
            "surgery_name":
            c.surgery_name,
            "status":
            c.status,
            "scheduled_start":
            c.scheduled_start.isoformat() if c.scheduled_start else None,
            "scheduled_end":
            c.scheduled_end.isoformat() if c.scheduled_end else None,
            "actual_start":
            c.actual_start.isoformat() if c.actual_start else None,
            "actual_end":
            c.actual_end.isoformat() if c.actual_end else None,
            "attachments": [{
                "id":
                a.id,
                "url":
                a.file_url,
                "note":
                a.note,
                "created_at":
                a.created_at.isoformat() if a.created_at else None,
            } for a in c.attachments],
        })
    return out
