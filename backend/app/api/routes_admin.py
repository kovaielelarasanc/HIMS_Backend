from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.models.user import User


router = APIRouter()


@router.get("/status")
def admin_status(db: Session = Depends(get_db)):
    exists = db.query(User).filter(User.is_admin.is_(True)).first() is not None
    return {"admin_exists": exists}