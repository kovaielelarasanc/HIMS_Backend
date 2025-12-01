from sqlalchemy.orm import Session
from typing import Optional

from app.models.settings import UiBranding


def get_ui_branding(db: Session) -> Optional[UiBranding]:
    return db.query(UiBranding).first()
