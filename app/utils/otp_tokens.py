# FILE: app/utils/otp_tokens.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Set, Dict, Any
import secrets

from sqlalchemy.orm import Session
from sqlalchemy import text

_OTP_COLS_CACHE: Optional[Set[str]] = None


def _utcnow_naive() -> datetime:
    # MySQL DATETIME usually stored as naive; keep consistent
    return datetime.utcnow()


def _otp_cols(db: Session) -> Set[str]:
    global _OTP_COLS_CACHE
    if _OTP_COLS_CACHE is not None:
        return _OTP_COLS_CACHE

    rows = db.execute(
        text("""
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'otp_tokens'
        """)
    ).fetchall()

    _OTP_COLS_CACHE = {r[0] for r in rows}
    return _OTP_COLS_CACHE


def generate_otp6() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def issue_otp(
    db: Session,
    user_id: int,
    purpose: str,
    email: Optional[str] = None,
    ttl_minutes: int = 10,
) -> str:
    """
    Creates OTP row safely even if otp_tokens schema differs between envs.
    Columns supported: user_id, otp_code, purpose, email, used, expires_at, created_at, used_at
    """
    cols = _otp_cols(db)

    now = _utcnow_naive()
    expires_at = now + timedelta(minutes=ttl_minutes)
    otp_code = generate_otp6()

    # mark previous unused OTPs used (optional but recommended)
    if "used" in cols:
        if "used_at" in cols:
            db.execute(
                text("""
                    UPDATE otp_tokens
                    SET used = 1, used_at = :now
                    WHERE user_id = :uid AND purpose = :purpose AND used = 0
                """),
                {"now": now, "uid": user_id, "purpose": purpose},
            )
        else:
            db.execute(
                text("""
                    UPDATE otp_tokens
                    SET used = 1
                    WHERE user_id = :uid AND purpose = :purpose AND used = 0
                """),
                {"uid": user_id, "purpose": purpose},
            )

    # build dynamic INSERT
    insert_cols = ["user_id", "otp_code", "purpose", "expires_at"]
    params: Dict[str, Any] = {
        "user_id": user_id,
        "otp_code": otp_code,
        "purpose": purpose,
        "expires_at": expires_at,
    }

    if "email" in cols:
        insert_cols.append("email")
        params["email"] = email

    if "used" in cols:
        insert_cols.append("used")
        params["used"] = 0

    # only include created_at if column exists
    if "created_at" in cols:
        insert_cols.append("created_at")
        params["created_at"] = now

    sql = f"""
        INSERT INTO otp_tokens ({", ".join(insert_cols)})
        VALUES ({", ".join([f":{c}" for c in insert_cols])})
    """
    db.execute(text(sql), params)
    db.commit()
    return otp_code


def verify_otp(
    db: Session,
    user_id: int,
    purpose: str,
    otp_code: str,
) -> bool:
    cols = _otp_cols(db)
    now = _utcnow_naive()

    row = db.execute(
        text("""
            SELECT id, otp_code, expires_at, used
            FROM otp_tokens
            WHERE user_id = :uid AND purpose = :purpose
            ORDER BY id DESC
            LIMIT 1
        """),
        {"uid": user_id, "purpose": purpose},
    ).mappings().first()

    if not row:
        return False

    if str(row["otp_code"]) != str(otp_code):
        return False

    # expiry check
    if row["expires_at"] and row["expires_at"] < now:
        return False

    # used check (if exists)
    if "used" in cols and row.get("used") in (1, True):
        return False

    # mark used
    if "used" in cols:
        if "used_at" in cols:
            db.execute(
                text("UPDATE otp_tokens SET used=1, used_at=:now WHERE id=:id"),
                {"now": now, "id": row["id"]},
            )
        else:
            db.execute(
                text("UPDATE otp_tokens SET used=1 WHERE id=:id"),
                {"id": row["id"]},
            )

    db.commit()
    return True
