import random
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.core.security import hash_password, verify_password
from app.core.emailer import send_email
from app.core.config import settings
from app.models.user import User
from app.models.otp import OtpToken
from app.schemas.auth import RegisterAdminIn, LoginIn, OtpVerifyIn, TokenOut
from app.utils.jwt import create_access_refresh
from app.api.deps import  get_current_user_from_token
from typing import Optional



router = APIRouter()

@router.post("/register-admin")
def register_admin(payload: RegisterAdminIn, db: Session = Depends(get_db)):
# Enforce single Admin
    existing_admin = db.query(User).filter(User.is_admin.is_(True)).first()
    if existing_admin:
     raise HTTPException(status_code=400, detail="Admin already exists. Registration closed.")


    if payload.password != payload.confirm_password:
     raise HTTPException(status_code=400, detail="Passwords do not match")


    if db.query(User).filter(User.email == payload.email).first():
      raise HTTPException(status_code=400, detail="Email already registered")


    user = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_admin=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Admin registered. Proceed to login."}

@router.post("/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")


    otp = f"{random.randint(0, 999999):06d}"
    token = OtpToken(user_id=user.id, otp_code=otp, expires_at=OtpToken.expiry(10))
    db.add(token)
    db.commit()


    send_email(
        to_email=user.email,
        subject=f"{settings.PROJECT_NAME} — Your OTP",
        body=f"Your OTP is {otp}. It will expire in 10 minutes.")


    return {"message": "OTP sent to email"}

@router.post("/verify-otp", response_model=TokenOut)
def verify_otp(payload: OtpVerifyIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    otp_row = (
        db.query(OtpToken)
        .filter(OtpToken.user_id == user.id, OtpToken.otp_code == payload.otp, OtpToken.used.is_(False))
        .order_by(OtpToken.id.desc())
        .first()
    )
    if not otp_row:
        raise HTTPException(status_code=400, detail="Invalid OTP")


    from datetime import datetime
    if otp_row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired")


    otp_row.used = True
    db.commit()


    access, refresh = create_access_refresh(user.email)
    return {"access_token": access, "refresh_token": refresh}

def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

@router.get("/me")
def me(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    raw = _extract_bearer(authorization) or token
    user = get_current_user_from_token(raw, db)
    role_names = [r.name for r in (user.roles or [])]
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "is_admin": user.is_admin,
        "department_id": user.department_id,
        "roles": role_names,
    }

@router.get("/me/permissions")
def my_permissions(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    raw = _extract_bearer(authorization) or token
    user = get_current_user_from_token(raw, db)

    from app.core.config import settings
    from app.models.permission import Permission

    # If you ever want Admin to see everything, set ADMIN_ALL_ACCESS=true in .env
    if user.is_admin and settings.ADMIN_ALL_ACCESS:
        rows = db.query(Permission).all()
        modules = {}
        for p in rows:
            modules.setdefault(p.module, []).append({"code": p.code, "label": p.label})
        return {"modules": modules}

    # Otherwise aggregate only via roles
    perms = set()
    for role in (user.roles or []):
        for p in (role.permissions or []):
            perms.add((p.code, p.label, p.module))

    modules = {}
    for code, label, module in perms:
        modules.setdefault(module, []).append({"code": code, "label": label})
    return {"modules": modules}
