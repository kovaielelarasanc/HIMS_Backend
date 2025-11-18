from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.ris import RisOrder

router = APIRouter(prefix="/ris", tags=["RIS History"])


@router.get("/history")
def ris_history(
        patient_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    reps = (db.query(RisOrder).options(joinedload(
        RisOrder.attachments)).filter(
            RisOrder.patient_id == patient_id).order_by(
                RisOrder.reported_at.desc().nullslast(),
                RisOrder.id.desc()).all())
    out = []
    for r in reps:
        out.append({
            "order_id":
            r.id,
            "test_name":
            r.test_name,
            "modality":
            r.modality,
            "status":
            r.status,
            "scheduled_at":
            r.scheduled_at,
            "scanned_at":
            r.scanned_at,
            "reported_at":
            r.reported_at,
            "approved_at":
            r.approved_at,
            "report_excerpt": (r.report_text[:200] + "…")
            if r.report_text and len(r.report_text) > 200 else r.report_text,
            "attachments": [{
                "id": a.id,
                "url": a.file_url,
                "note": a.note,
                "created_at": a.created_at
            } for a in r.attachments],
        })
    return out
