# app/api/routes_lis_history.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from app.api.deps import get_db, current_user
from app.models.user import User
from app.models.lis import LisOrder, LisOrderItem

router = APIRouter(prefix="/lab", tags=["LIS History"])


@router.get("/history")
def lab_history(
        patient_id: int = Query(...),
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
):
    # Orders for the patient with items
    orders = (db.query(LisOrder).options(joinedload(LisOrder.items)).filter(
        LisOrder.patient_id == patient_id).order_by(LisOrder.id.desc()).all())

    out = []
    for o in orders:
        for i in o.items:
            out.append({
                "order_id":
                o.id,
                "item_id":
                i.id,
                "test_name":
                i.test_name,
                "status":
                i.status,
                "result_value":
                i.result_value,
                "result_at":
                i.result_at,
                "collected_at":
                o.collected_at,
                "reported_at":
                o.reported_at,
                "attachments": [{
                    "id": a.id,
                    "url": a.file_url,
                    "note": a.note,
                    "created_at": a.created_at
                } for a in i.attachments],
            })
    # Latest first (by meaningful timestamp)
    out.sort(key=lambda r: r.get("result_at") or r.get("collected_at") or r.
             get("reported_at") or 0,
             reverse=True)
    return out
