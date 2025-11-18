from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.api.deps import get_db, current_user
from app.models.patient import Patient

router = APIRouter()


@router.get("/patients/search")
def search_patients(q: str = Query(""),
                    db: Session = Depends(get_db),
                    user=Depends(current_user)):
    q = (q or "").strip()
    query = db.query(Patient)
    if q:
        like = f"%{q}%"
        query = query.filter((Patient.uhid.ilike(like))
                             | (Patient.first_name.ilike(like))
                             | (Patient.last_name.ilike(like))
                             | (Patient.phone.ilike(like))
                             | (Patient.email.ilike(like)))
    rows = query.order_by(Patient.id.desc()).limit(50).all()
    return [{
        "id": p.id,
        "uhid": p.uhid,
        "first_name": p.first_name,
        "last_name": p.last_name or "",
        "phone": p.phone or "",
        "email": p.email or ""
    } for p in rows]
