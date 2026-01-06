# FILE: app/utils/login_id.py
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Integer
from sqlalchemy.exc import IntegrityError

from app.models.user import User  # adjust import to your project


def next_login_id(db: Session) -> str:
    # only numeric 6-digit login_ids
    max_val = (
        db.query(func.max(cast(User.login_id, Integer)))
        .filter(User.login_id.op("REGEXP")("^[0-9]{6}$"))
        .scalar()
    )
    n = int(max_val or 0) + 1
    return f"{n:06d}"


def create_user_with_unique_login_id(db: Session, user_obj: User, max_retry: int = 30) -> User:
    """
    user_obj should NOT have login_id set. We'll set it.
    """
    last_err = None
    for _ in range(max_retry):
        user_obj.login_id = next_login_id(db)
        db.add(user_obj)
        try:
            db.commit()
            db.refresh(user_obj)
            return user_obj
        except IntegrityError as e:
            db.rollback()
            last_err = e
            # retry only if login_id unique constraint collision
            if "uq_users_login_id" in str(e).lower() or "login_id" in str(e).lower():
                continue
            raise
    raise last_err  # type: ignore
