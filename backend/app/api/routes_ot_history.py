from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.ot import OtOrder

router = APIRouter(prefix="/ot", tags=["OT History"])


@router.get("/history")
def ot_history(patient_id: int = Query(...),
               db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    rows = (db.query(OtOrder).options(joinedload(OtOrder.attachments)).filter(
        OtOrder.patient_id == patient_id).order_by(
            OtOrder.scheduled_start.desc().nullslast(),
            OtOrder.id.desc()).all())
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
            c.scheduled_start,
            "scheduled_end":
            c.scheduled_end,
            "actual_start":
            c.actual_start,
            "actual_end":
            c.actual_end,
            "attachments": [{
                "id": a.id,
                "url": a.file_url,
                "note": a.note,
                "created_at": a.created_at
            } for a in c.attachments],
        })
    return out
