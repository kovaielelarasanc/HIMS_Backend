from __future__ import annotations

from datetime import datetime
import random
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from sqlalchemy import or_
from app.api.deps import get_db, current_user, require_perm
from app.core.security import hash_password
from app.models.user import User, UserLoginSeq, UserSession
from app.models.role import Role
from app.schemas.user import UserCreate, UserOut, UserUpdate, UserSaveResponse, DoctorListResponse
from app.services.otp_service import send_email_verify_otp
from app.utils.otp_tokens import verify_otp

router = APIRouter()


# -------------------------
# helpers
# -------------------------
def _utcnow() -> datetime:
    return datetime.utcnow()


def _norm_email(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    return s if s else None


def _enforce_doctor_department(is_doctor: bool, department_id: Optional[int]) -> None:
    if bool(is_doctor) and not department_id:
        raise HTTPException(
            status_code=400,
            detail="Department is mandatory when Mark as Doctor is enabled",
        )


def _enforce_2fa_email(two_fa_enabled: bool, email: Optional[str]) -> None:
    if bool(two_fa_enabled) and not email:
        raise HTTPException(status_code=400, detail="Email is mandatory when 2FA is enabled")


def _revoke_all_sessions(db: Session, user_id: int, reason: str) -> None:
    now = _utcnow().isoformat()
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == int(user_id), UserSession.revoked_at.is_(None))
        .all()
    )
    for s in rows:
        s.revoked_at = now
        s.revoke_reason = reason


def _build_user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        login_id=u.login_id,
        name=u.name,
        email=u.email,
        email_verified=bool(getattr(u, "email_verified", False)),
        two_fa_enabled=bool(getattr(u, "two_fa_enabled", False)),
        multi_login_enabled=bool(getattr(u, "multi_login_enabled", True)),
        is_active=bool(getattr(u, "is_active", True)),
        is_admin=bool(getattr(u, "is_admin", False)),
        is_doctor=bool(getattr(u, "is_doctor", False)),
        department_id=getattr(u, "department_id", None),
        role_ids=[r.id for r in (u.roles or [])],
    )


def _email_exists(db: Session, email: str, *, exclude_user_id: Optional[int] = None) -> bool:
    q = db.query(User.id).filter(func.lower(User.email) == email.lower())
    if exclude_user_id:
        q = q.filter(User.id != int(exclude_user_id))
    return db.query(q.exists()).scalar() is True


def _load_roles(db: Session, role_ids: List[int]) -> List[Role]:
    uniq = sorted({int(x) for x in (role_ids or [])})
    if not uniq:
        return []
    roles = db.query(Role).filter(Role.id.in_(uniq)).all()
    if len(roles) != len(uniq):
        raise HTTPException(status_code=400, detail="Invalid role_ids")
    return roles


def _gen_random_login_id() -> str:
    # 6 digits, allows leading zeros
    return f"{random.randint(0, 999999):06d}"


def _generate_login_id(db: Session) -> str:
    """
    Prefer UserLoginSeq if table exists; fallback to random+unique check.
    Robust even if UserLoginSeq table is missing in some tenants.
    """
    # 1) try sequence table
    try:
        with db.begin_nested():
            seq = UserLoginSeq()
            db.add(seq)
            db.flush()
            lid = f"{int(seq.id):06d}"
        # ensure not used (very rare, but safe)
        exists = db.query(User.id).filter(User.login_id == lid).first()
        if not exists:
            return lid
    except (OperationalError, Exception):
        # table missing or any issue -> fallback
        db.rollback()

    # 2) fallback random with retry
    for _ in range(30):
        lid = _gen_random_login_id()
        if not db.query(User.id).filter(User.login_id == lid).first():
            return lid

    raise HTTPException(status_code=500, detail="Unable to generate unique Login ID. Try again.")


def _integrity_to_http(e: IntegrityError) -> HTTPException:
    msg = str(getattr(e, "orig", e)).lower()

    # Try to classify common unique violations
    if "duplicate" in msg or "unique" in msg:
        if "email" in msg:
            return HTTPException(status_code=409, detail="Email already exists")
        if "login_id" in msg or "login id" in msg:
            return HTTPException(status_code=409, detail="Login ID conflict. Please retry create.")
        return HTTPException(status_code=409, detail="Duplicate value conflict")

    return HTTPException(status_code=400, detail="Request rejected by database constraints")


def _safe_send_verify_otp(db: Session, user: User) -> None:
    """
    Never fail the whole API response if SMTP fails.
    User is already created/updated; OTP can be resent.
    """
    try:
        send_email_verify_otp(db, user, ttl_minutes=10)
    except Exception:
        # Do not raise: UI can use resend OTP
        pass


# -------------------------
# routes
# -------------------------
@router.get("/", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "users.view")

    users = db.query(User).options(joinedload(User.roles)).order_by(User.id.desc()).all()
    return [_build_user_out(u) for u in users]


@router.get("/doctors", response_model=List[UserOut])
def list_doctors(db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "users.view")

    users = (
        db.query(User)
        .options(joinedload(User.roles))
        .filter(User.is_doctor.is_(True))
        .order_by(User.id.desc())
        .all()
    )
    return [_build_user_out(u) for u in users]


@router.post("/", response_model=UserSaveResponse)
def create_user(payload: UserCreate, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "users.create")

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    email = _norm_email(payload.email)
    _enforce_doctor_department(bool(payload.is_doctor), payload.department_id)
    _enforce_2fa_email(bool(payload.two_fa_enabled), email)

    if email and _email_exists(db, email):
        raise HTTPException(status_code=409, detail="Email already exists")

    if not (payload.password or "").strip():
        raise HTTPException(status_code=400, detail="Password is required")

    roles = _load_roles(db, payload.role_ids or [])

    # retry create if login_id conflict happens
    last_err: Optional[Exception] = None
    for _ in range(5):
        try:
            login_id = _generate_login_id(db)

            u = User(
                login_id=login_id,
                name=name,
                email=email,
                email_verified=False,
                password_hash=hash_password(payload.password),
                two_fa_enabled=bool(payload.two_fa_enabled),
                multi_login_enabled=bool(payload.multi_login_enabled),
                token_version=int(getattr(payload, "token_version", 1) or 1),
                is_active=bool(payload.is_active),
                is_admin=False,
                is_doctor=bool(payload.is_doctor),
                department_id=(payload.department_id if payload.is_doctor else None),
            )
            u.roles = roles

            db.add(u)
            db.commit()
            db.refresh(u)

            needs_email_verify = False
            if u.two_fa_enabled:
                needs_email_verify = True
                u.email_verified = False
                db.commit()
                _safe_send_verify_otp(db, u)

            return UserSaveResponse(
                user=_build_user_out(u),
                needs_email_verify=needs_email_verify,
                otp_sent_to=u.email if needs_email_verify else None,
                otp_purpose="email_verify" if needs_email_verify else None,
            )

        except IntegrityError as e:
            db.rollback()
            last_err = e
            # if email conflict or other -> raise immediately
            raise _integrity_to_http(e) from e

        except Exception as e:
            db.rollback()
            last_err = e

    raise HTTPException(status_code=400, detail=f"Create failed: {str(last_err)}")


@router.put("/{user_id}", response_model=UserSaveResponse)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    require_perm(me, "users.update")

    u = db.query(User).options(joinedload(User.roles)).filter(User.id == int(user_id)).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    new_email = _norm_email(payload.email)
    _enforce_doctor_department(bool(payload.is_doctor), payload.department_id)
    _enforce_2fa_email(bool(payload.two_fa_enabled), new_email)

    email_changed = (new_email or None) != (u.email or None)

    if new_email and email_changed and _email_exists(db, new_email, exclude_user_id=u.id):
        raise HTTPException(status_code=409, detail="Email already exists")

    prev_two_fa = bool(getattr(u, "two_fa_enabled", False))
    prev_multi = bool(getattr(u, "multi_login_enabled", True))

    # apply updates
    u.name = name
    u.email = new_email
    u.is_active = bool(payload.is_active)
    u.is_doctor = bool(payload.is_doctor)
    u.department_id = (payload.department_id if payload.is_doctor else None)
    u.two_fa_enabled = bool(payload.two_fa_enabled)
    u.multi_login_enabled = bool(payload.multi_login_enabled)

    # password update (optional)
    if payload.password and payload.password.strip():
        u.password_hash = hash_password(payload.password.strip())
        u.token_version = int(getattr(u, "token_version", 1) or 1) + 1
        _revoke_all_sessions(db, u.id, reason="password_changed_by_admin")

    # roles update (optional)
    if payload.role_ids is not None:
        roles = _load_roles(db, payload.role_ids or [])
        u.roles = roles

    needs_email_verify = False

    # 2FA rules
    if u.two_fa_enabled:
        # If turning ON or email changed -> verify again
        if (not prev_two_fa) or email_changed:
            u.email_verified = False
            needs_email_verify = True

    # multi-login turned OFF -> revoke all sessions (so it won't block wrongly)
    if prev_multi and (u.multi_login_enabled is False):
        _revoke_all_sessions(db, u.id, reason="multi_login_disabled")

    try:
        db.commit()
        db.refresh(u)
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_http(e) from e

    if needs_email_verify and u.email:
        _safe_send_verify_otp(db, u)

    return UserSaveResponse(
        user=_build_user_out(u),
        needs_email_verify=needs_email_verify,
        otp_sent_to=u.email if needs_email_verify else None,
        otp_purpose="email_verify" if needs_email_verify else None,
    )


@router.delete("/{user_id}")
def deactivate_user(user_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "users.delete")

    u = db.query(User).filter(User.id == int(user_id)).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    # soft deactivate
    u.is_active = False
    _revoke_all_sessions(db, u.id, reason="deactivated_by_admin")

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Failed to deactivate user") from e

    return {"ok": True}


# -------------------------
# OTP endpoints for Admin UI
# -------------------------
class OtpBody(BaseModel):
    otp_code: Optional[str] = None
    otp: Optional[str] = None


@router.post("/{user_id}/email/verify-otp", response_model=UserOut)
def admin_verify_email_otp(
    user_id: int,
    payload: OtpBody,
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    require_perm(me, "users.update")

    u = db.query(User).filter(User.id == int(user_id)).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if not u.email:
        raise HTTPException(status_code=400, detail="Email not set")

    code = (payload.otp_code or payload.otp or "").strip()
    code = "".join([c for c in code if c.isdigit()])[:6]
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Enter 6-digit OTP")

    ok = verify_otp(db, user_id=u.id, purpose="email_verify", otp_code=code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    u.email_verified = True
    db.commit()
    db.refresh(u)
    return _build_user_out(u)


@router.post("/{user_id}/email/resend-otp")
def admin_resend_email_otp(user_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    require_perm(me, "users.update")

    u = db.query(User).filter(User.id == int(user_id)).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if not u.email:
        raise HTTPException(status_code=400, detail="Email not set")
    if not bool(getattr(u, "two_fa_enabled", False)):
        raise HTTPException(status_code=400, detail="2FA is disabled for this user")

    _safe_send_verify_otp(db, u)
    return {"ok": True, "message": "OTP sent"}


# Backward compatible alias (if any old UI calls /email/verify)
@router.post("/{user_id}/email/verify", response_model=UserOut)
def admin_verify_email_alias(
    user_id: int,
    payload: OtpBody,
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    return admin_verify_email_otp(user_id, payload, db, me)


def has_perm(user: User, code: str) -> bool:
    if getattr(user, "is_admin", False):
        return True
    for r in (getattr(user, "roles", None) or []):
        for p in (getattr(r, "permissions", None) or []):
            if getattr(p, "code", None) == code:
                return True
    return False

@router.get("/doctor")
def list_doctors(
    q: Optional[str] = Query(default=None, description="Search by name/login_id/email"),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    # ✅ permission (change code if your system uses different one)
    if not has_perm(user, "doctors.view"):
        raise HTTPException(status_code=403, detail="Not permitted")

    qry = db.query(User).filter(User.is_doctor.is_(True))

    if not include_inactive:
        qry = qry.filter(User.is_active.is_(True))

    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                User.name.ilike(like),
                User.login_id.ilike(like),
                User.email.ilike(like),
            )
        )

    qry = qry.order_by(User.name.asc()).limit(limit)

    doctors = []
    for d in qry.all():
        doctors.append(
            {
                "id": d.id,
                "login_id": d.login_id,
                "name": d.name,
                "email": d.email,
                "is_active": bool(d.is_active),
                "department_id": d.department_id,  # keep if you want, but no join
            }
        )

    # ✅ IMPORTANT: frontend expects res.data.doctors
    return {"status": True, "doctors": doctors}